"""0.2.6 hardening: the things a careful reader checks before trusting a tool
with a school-portal password — SMTP certificate verification, the
dashboard's Host-header allow-list (DNS rebinding), env-file quoting that
survives python-dotenv, owner-only file creation, Docker ``*_FILE`` secrets,
and a SOAP probe that never carries the real password."""
from __future__ import annotations

import os
import ssl

import pytest

from lastbell import secrets as secretstore
from lastbell import setup_wizard as wiz
from lastbell.dashboard.server import host_allowed


# ── 1. SMTP verifies the server certificate ───────────────────────────


class _FakeSMTP:
    instances: list = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.context = host, port, context
        self.starttls_context = None
        self.sent = None
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.starttls_context = context

    def login(self, user, password):
        self.creds = (user, password)

    def send_message(self, msg):
        self.sent = msg


class _FakeSMTPS(_FakeSMTP):
    pass


@pytest.fixture
def smtp(monkeypatch):
    import smtplib

    _FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTPS)
    return _FakeSMTP


def _transport(port):
    from lastbell.notify.email import SmtpTransport

    return SmtpTransport(host="smtp.example", port=port, user="u", password="p",
                         sender="lastbell@example.com")


def test_starttls_uses_a_verifying_context(smtp):
    _transport(587).deliver("kid@example.com", "hi", "body")
    (conn,) = smtp.instances
    assert type(conn) is _FakeSMTP
    ctx = conn.starttls_context
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert conn.sent["To"] == "kid@example.com"


def test_port_465_is_implicit_tls_with_the_same_verification(smtp):
    _transport(465).deliver("kid@example.com", "hi", "body")
    (conn,) = smtp.instances
    assert type(conn) is _FakeSMTPS
    assert conn.context.verify_mode == ssl.CERT_REQUIRED
    assert conn.starttls_context is None          # no STARTTLS on top of SMTPS
    assert conn.sent is not None


# ── 2. the dashboard answers only to names it recognises ──────────────


@pytest.mark.parametrize("host_header", [
    "127.0.0.1:8321", "localhost:8321", "LOCALHOST", "[::1]:8321", "::1",
    "192.168.1.20:8321",              # an IP literal can't be rebound
    "[fe80::1]:8321",
    "pi.local:8321", "Pi.LOCAL",      # mDNS: never served by public DNS
    None, "",                          # no Host at all: not a browser
])
def test_host_allowed_defaults(host_header):
    assert host_allowed(host_header, "127.0.0.1")


@pytest.mark.parametrize("host_header", [
    "evil.example:8321", "rebind.attacker.net", "localhost.evil.example:8321",
    "app.localhost:8321",
    "pi.local.evil.example",
])
def test_host_refused_for_any_other_name(host_header):
    assert not host_allowed(host_header, "127.0.0.1")
    assert not host_allowed(host_header, "0.0.0.0")


def test_bound_hostname_and_configured_extras_are_allowed():
    assert host_allowed("nas.home.arpa:8321", "nas.home.arpa")
    assert not host_allowed("nas.home.arpa:8321", "127.0.0.1")
    assert host_allowed("grades.tail1234.ts.net", "0.0.0.0",
                        ("Grades.tail1234.ts.net", " other.example "))
    # binding to every interface doesn't make "every name" allowed
    assert not host_allowed("evil.example", "0.0.0.0", ("nas.home.arpa",))


def test_dashboard_refuses_a_rebound_host_end_to_end(tmp_path):
    """Through the real handler: a GET with a foreign Host gets 421 and no
    student data; the same request with a loopback Host is served."""
    import http.client
    import threading
    from http.server import ThreadingHTTPServer

    from lastbell import dashboard, store

    db = tmp_path / "d.db"
    conn = store.connect(db)
    store.ensure_schema(conn)
    conn.close()

    # serve() blocks; drive its Handler by building the same server here.
    captured = {}

    def fake_server(addr, handler):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        captured["srv"] = srv
        return srv

    import lastbell.dashboard.server as srvmod
    real = srvmod.ThreadingHTTPServer
    srvmod.ThreadingHTTPServer = fake_server
    try:
        t = threading.Thread(target=dashboard.serve, args=(db, "127.0.0.1", 0),
                             kwargs={"hostnames": ("pi.example.net",)}, daemon=True)
        t.start()
        for _ in range(200):
            if "srv" in captured:
                break
            threading.Event().wait(0.01)
        port = captured["srv"].server_address[1]

        def get(host_header):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.putrequest("GET", "/", skip_host=True)
            if host_header is not None:
                c.putheader("Host", host_header)
            c.endheaders()
            r = c.getresponse()
            body = r.read().decode()
            c.close()
            return r.status, body

        status, body = get(f"evil.example:{port}")
        assert status == 421
        assert "evil.example" in body and "No students yet" not in body
        assert get(f"127.0.0.1:{port}")[0] == 200
        assert get("pi.example.net")[0] == 200
        assert get("pi.local")[0] == 200
        # a POST from a rebound page is refused before the Origin check
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.putrequest("POST", "/settings/watcher-add", skip_host=True)
        c.putheader("Host", f"evil.example:{port}")
        c.putheader("Origin", f"http://evil.example:{port}")
        c.putheader("Content-Length", "0")
        c.endheaders()
        assert c.getresponse().status == 421
    finally:
        srvmod.ThreadingHTTPServer = real
        if "srv" in captured:
            captured["srv"].shutdown()


def test_config_reads_dashboard_hostnames(monkeypatch):
    from lastbell import config as cfg

    monkeypatch.setenv("LASTBELL_DISTRICT", "x.example")
    monkeypatch.setenv("LASTBELL_USERNAME", "p")
    monkeypatch.setenv("LASTBELL_DASHBOARD_HOSTNAMES", " pi.example.net, nas ")
    assert cfg.load().dashboard_hostnames == ("pi.example.net", "nas")
    monkeypatch.delenv("LASTBELL_DASHBOARD_HOSTNAMES")
    assert cfg.load().dashboard_hostnames == ()


# ── 4. env-file values survive python-dotenv ──────────────────────────


NASTY = [
    "hunter2",                 # plain stays plain
    "abc #def",                # dotenv treats " #" as a comment start
    "a${HOME}b",               # dotenv would interpolate
    '"quoted"',                # matching quotes would be stripped
    "trail ", " lead",         # whitespace would be trimmed
    "it's", 'say "hi"', "mix'\"$\\ #x",
    "back\\slash", "new\\nline-escape-text",
    "a#b", "x=y", "unicode·é", "",
]


@pytest.mark.parametrize("value", NASTY)
def test_write_env_round_trips_through_dotenv_and_read_env(tmp_path, value):
    dotenv = pytest.importorskip("dotenv")
    f = tmp_path / "env"
    wiz.write_env(f, {"LASTBELL_PASSWORD": value, "LASTBELL_USERNAME": "p"})
    assert dotenv.dotenv_values(f, interpolate=False)["LASTBELL_PASSWORD"] == value
    assert wiz.read_env(f)["LASTBELL_PASSWORD"] == value
    assert wiz.reread(f, "LASTBELL_PASSWORD") == value
    # and rewriting another key leaves it intact
    wiz.write_env(f, {"LASTBELL_USERNAME": "q"})
    assert dotenv.dotenv_values(f, interpolate=False)["LASTBELL_PASSWORD"] == value


def test_plain_values_are_written_bare(tmp_path):
    f = tmp_path / "env"
    wiz.write_env(f, {"LASTBELL_DISTRICT": "host.example", "LASTBELL_PASSWORD": "abc #def"})
    text = f.read_text()
    assert "LASTBELL_DISTRICT=host.example\n" in text          # hand-editable
    assert 'LASTBELL_PASSWORD="abc #def"\n' in text


def test_write_env_rejects_line_breaks(tmp_path):
    with pytest.raises(ValueError):
        wiz.write_env(tmp_path / "env", {"LASTBELL_PASSWORD": "a\nb"})


def test_config_loads_the_env_file_without_interpolation(tmp_path, monkeypatch):
    pytest.importorskip("dotenv")
    import importlib

    from lastbell import config as cfg

    monkeypatch.setenv("LASTBELL_HOME", str(tmp_path))
    monkeypatch.delenv("LASTBELL_HARDENING_PROBE", raising=False)
    (tmp_path / "env").write_text('LASTBELL_HARDENING_PROBE="a${HOME}b"\n')
    monkeypatch.chdir(tmp_path)                 # no checkout .env in the way
    importlib.reload(cfg)
    try:
        assert os.environ["LASTBELL_HARDENING_PROBE"] == "a${HOME}b"
    finally:
        monkeypatch.delenv("LASTBELL_HARDENING_PROBE", raising=False)
        monkeypatch.delenv("LASTBELL_HOME")
        importlib.reload(cfg)


def test_wizard_refuses_to_keep_a_password_that_would_not_read_back(tmp_path, monkeypatch):
    """The read-back guard in step 2: if dotenv ever disagreed with the
    writer, the wizard says so and removes the value rather than saving a
    password the service would get wrong."""
    monkeypatch.setattr(wiz, "reread", lambda path, key: "something-else")
    said: list[str] = []
    monkeypatch.setattr(wiz, "_say", said.append)
    monkeypatch.setattr(wiz, "_ask", lambda prompt, default="": "parent1")
    monkeypatch.setattr(wiz, "_choose_backend", lambda env: "env")
    monkeypatch.setattr(wiz, "_getpass", lambda prompt: "abc #def")
    env_path = tmp_path / "env"
    username, password = wiz._step_credentials(env_path, {})
    assert (username, password) == ("parent1", "")
    assert "LASTBELL_PASSWORD" not in wiz.read_env(env_path)
    assert any("didn't read that password back" in line for line in said)


# ── 6. the settings file is owner-only from the first byte ────────────


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_env_file_is_created_0600_even_under_a_permissive_umask(tmp_path):
    old = os.umask(0o000)
    try:
        f = tmp_path / "sub" / "env"
        wiz.write_env(f, {"LASTBELL_PASSWORD": "pw"})
        assert (f.stat().st_mode & 0o777) == 0o600
        assert not (tmp_path / "sub" / "env.tmp").exists()
        # an existing wider file is tightened
        os.chmod(f, 0o644)
        wiz.write_env(f, {"LASTBELL_USERNAME": "p"})
        assert (f.stat().st_mode & 0o777) == 0o600
    finally:
        os.umask(old)


def test_a_failed_write_leaves_the_old_file_and_no_temp(tmp_path, monkeypatch):
    f = tmp_path / "env"
    wiz.write_env(f, {"LASTBELL_USERNAME": "p"})
    before = f.read_text()

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        wiz.write_env(f, {"LASTBELL_USERNAME": "q"})
    assert f.read_text() == before
    assert not (tmp_path / "env.tmp").exists()


# ── 5. Docker-style *_FILE secrets ────────────────────────────────────


def test_password_file_is_read_when_the_env_var_is_unset(tmp_path, monkeypatch):
    secret = tmp_path / "parentvue_password"
    secret.write_text("hunter2\n")                 # editors add the newline
    monkeypatch.delenv("LASTBELL_PASSWORD", raising=False)
    monkeypatch.setenv("LASTBELL_PASSWORD_FILE", str(secret))
    assert secretstore.get_password("p", "env") == "hunter2"
    # the environment variable still wins when both are set
    monkeypatch.setenv("LASTBELL_PASSWORD", "from-env")
    assert secretstore.get_password("p", "env") == "from-env"


def test_missing_password_file_is_a_plain_error(tmp_path, monkeypatch):
    monkeypatch.delenv("LASTBELL_PASSWORD", raising=False)
    monkeypatch.setenv("LASTBELL_PASSWORD_FILE", str(tmp_path / "nope"))
    with pytest.raises(secretstore.SecretError, match="LASTBELL_PASSWORD_FILE"):
        secretstore.get_password("p", "env")
    monkeypatch.delenv("LASTBELL_PASSWORD_FILE")
    with pytest.raises(secretstore.SecretError, match="LASTBELL_PASSWORD_FILE"):
        secretstore.get_password("p", "env")     # the hint names the option


def test_smtp_and_canvas_secrets_accept_files_too(tmp_path, monkeypatch):
    (tmp_path / "smtp").write_text("s3cret")
    (tmp_path / "canvas").write_text("tok\r\n")
    for key in ("LASTBELL_PASSWORD_SMTP", "LASTBELL_CANVAS_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LASTBELL_PASSWORD_SMTP_FILE", str(tmp_path / "smtp"))
    monkeypatch.setenv("LASTBELL_CANVAS_TOKEN_FILE", str(tmp_path / "canvas"))
    assert secretstore.get_smtp_password() == "s3cret"
    assert secretstore.get_canvas_token() == "tok"


# ── 7. the SOAP probe never carries the real password ─────────────────


def test_soap_probe_sends_a_placeholder_not_the_credential():
    from lastbell import preflight

    seen = {}

    class R:
        text = '<root ERROR_MESSAGE="The client version is not supported. UPD5304-00" RT_ERROR="TRUE"/>'

    def post(url, data=None, headers=None, timeout=None):
        seen["body"] = data.decode()
        return R()

    status, detail = preflight._soap_probe("https://x.example", post)
    assert "UPD5304" in detail
    assert "<password>lastbell-preflight</password>" in seen["body"]


def test_full_preflight_reaches_soap_without_the_password(monkeypatch):
    """End to end through run_full: the only POST carrying the real password
    is the web login form (the client), never the SOAP envelope."""
    from lastbell import preflight

    bodies = []

    class R:
        text = '<root ERROR_MESSAGE="UPD5304-00" RT_ERROR="TRUE"/>'

    def post(url, data=None, headers=None, timeout=None):
        bodies.append(data.decode())
        return R()

    class FailingClient:
        def login(self):
            raise preflight.LoginError("nope")

    report = preflight.run_full("x.example", "https://x.example", "parent1",
                                "s3cret-pw", client=FailingClient(),
                                get=lambda url, **kw: type("G", (), {"status_code": 200, "text": "MainContent$username MainContent$password"})(),
                                post=post)
    assert bodies and all("s3cret-pw" not in b and "parent1" not in b for b in bodies)
    assert report.verdict == "no-go"


def test_soap_probe_tells_a_live_api_from_a_dead_one():
    from lastbell import preflight

    def answering(url, **kw):
        return type("R", (), {"text": '<root ERROR_MESSAGE="Invalid user id or password" RT_ERROR="TRUE"/>'})()
    status, detail = preflight._soap_probe("https://x.example", answering)
    assert status == preflight.INFO and "may still be enabled" in detail
    assert "Invalid user id" not in detail


# ── 3. beyond loopback, the dashboard asks for its key ────────────────


def test_admitted_local_peers_need_nothing_others_need_the_cookie():
    from lastbell.dashboard.server import admitted

    assert admitted("127.0.0.1", None, "k")
    assert admitted("::1", None, "k")
    assert not admitted("192.168.1.9", None, "k")
    assert not admitted("192.168.1.9", "lastbell_key=wrong", "k")
    assert admitted("192.168.1.9", "other=1; lastbell_key=k", "k")
    assert not admitted("192.168.1.9", "lastbell_key=k", "")     # no key configured
    assert not admitted("garbage", "lastbell_key=k;;=bad", "k2")


def test_check_bind_refuses_public_addresses_unless_told(monkeypatch):
    from lastbell.dashboard.server import DashboardRefused, check_bind

    monkeypatch.delenv("LASTBELL_DASHBOARD_PUBLIC", raising=False)
    for ok in ("127.0.0.1", "0.0.0.0", "192.168.1.20", "10.0.0.5", "100.64.0.9",
               "fd00::1", "pi.local", ""):
        check_bind(ok)
    with pytest.raises(DashboardRefused, match="public address"):
        check_bind("8.8.8.8")
    monkeypatch.setenv("LASTBELL_DASHBOARD_PUBLIC", "1")
    check_bind("8.8.8.8")


def test_key_link_names_a_reachable_host(monkeypatch):
    import socket

    from lastbell.dashboard.server import key_link

    monkeypatch.setattr(socket, "gethostname", lambda: "raspberrypi")
    assert key_link("0.0.0.0", 8321, "K") == "http://raspberrypi.local:8321/?key=K"
    monkeypatch.setattr(socket, "gethostname", lambda: "nas.home.arpa")
    assert key_link("", 8321, "K") == "http://nas.home.arpa:8321/?key=K"
    assert key_link("192.168.1.20", 8321, "K") == "http://192.168.1.20:8321/?key=K"
    assert key_link("fd00::7", 8321, "K") == "http://[fd00::7]:8321/?key=K"


def test_dashboard_key_is_generated_once_and_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("LASTBELL_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LASTBELL_DASHBOARD_KEY", raising=False)
    monkeypatch.delenv("LASTBELL_DASHBOARD_KEY_FILE", raising=False)
    key, where = secretstore.dashboard_key()
    assert len(key) >= 30 and "settings file" in where
    assert wiz.read_env(tmp_path / "env")["LASTBELL_DASHBOARD_KEY"] == key
    assert (tmp_path / "env").stat().st_mode & 0o777 == 0o600
    # write_env mirrored it into the environment, and a fresh call agrees
    assert secretstore.dashboard_key()[0] == key
    monkeypatch.setenv("LASTBELL_DASHBOARD_KEY", "from-env")
    assert secretstore.dashboard_key()[0] == "from-env"


def test_dashboard_key_survives_an_unwritable_settings_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LASTBELL_HOME", str(tmp_path / "ro"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LASTBELL_DASHBOARD_KEY", raising=False)

    def boom(path, updates):
        raise OSError("read-only file system")
    monkeypatch.setattr(wiz, "write_env", boom)
    key, where = secretstore.dashboard_key()
    assert key and where.startswith("nowhere") and "LASTBELL_DASHBOARD_KEY" in where


def _serve_in_thread(tmp_path, monkeypatch, **kwargs):
    """The real handler on an ephemeral loopback port; returns (port, shutdown)."""
    import threading
    from http.server import ThreadingHTTPServer

    from lastbell import dashboard, store
    import lastbell.dashboard.server as srvmod

    db = tmp_path / "d.db"
    conn = store.connect(db)
    store.ensure_schema(conn)
    conn.close()
    captured = {}

    def fake_server(addr, handler):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        captured["srv"] = srv
        return srv
    monkeypatch.setattr(srvmod, "ThreadingHTTPServer", fake_server)
    threading.Thread(target=dashboard.serve, args=(db, "0.0.0.0", 0),
                     kwargs=kwargs, daemon=True).start()
    for _ in range(300):
        if "srv" in captured:
            break
        threading.Event().wait(0.01)
    return captured["srv"].server_address[1], captured["srv"].shutdown


def _request(port, method, path, **headers):
    import http.client

    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.putrequest(method, path)
    for k, v in headers.items():
        c.putheader(k.replace("_", "-"), v)
    if method == "POST":
        c.putheader("Content-Length", "0")
    c.endheaders()
    r = c.getresponse()
    body = r.read().decode()
    out = (r.status, dict(r.getheaders()), body)
    c.close()
    return out


def test_network_peers_are_gated_end_to_end(tmp_path, monkeypatch, capsys):
    import lastbell.dashboard.server as srvmod

    # The test client is on loopback; pretend every peer is on the LAN.
    monkeypatch.setattr(srvmod, "peer_is_local", lambda ip: False)
    port, shutdown = _serve_in_thread(tmp_path, monkeypatch, key="s3cret-key")
    try:
        status, _, body = _request(port, "GET", "/")
        assert status == 403 and "asks for its key" in body and "No students yet" not in body
        assert "didn't match" not in body
        status, _, body = _request(port, "GET", "/alerts?page=2&key=wrong")
        assert status == 403 and "didn't match" in body
        # static assets are not gated: the refusal page needs its stylesheet
        assert _request(port, "GET", "/static/style.css")[0] == 200
        # the right key: cookie set, redirected to the same URL minus the key
        status, headers, _ = _request(port, "GET", "/alerts?page=2&key=s3cret-key")
        assert status == 303 and headers["Location"] == "/alerts?page=2"
        cookie = headers["Set-Cookie"]
        assert cookie.startswith("lastbell_key=s3cret-key;") and "HttpOnly" in cookie
        assert _request(port, "GET", "/", Cookie="lastbell_key=s3cret-key")[0] == 200
        # POST: cookie required, ?key= not accepted
        assert _request(port, "POST", "/settings/watcher-add?key=s3cret-key")[0] == 403
        assert _request(port, "POST", "/settings/watcher-add",
                        Cookie="lastbell_key=s3cret-key",
                        Origin=f"http://127.0.0.1:{port}",
                        Host=f"127.0.0.1:{port}")[0] == 303
        out = capsys.readouterr().out
        assert "?key=s3cret-key" in out and "in the clear" in out
    finally:
        shutdown()


def test_loopback_bind_asks_nothing_and_prints_no_key(tmp_path, monkeypatch, capsys):
    import threading
    from http.server import ThreadingHTTPServer

    from lastbell import dashboard, store
    import lastbell.dashboard.server as srvmod

    db = tmp_path / "d.db"
    conn = store.connect(db)
    store.ensure_schema(conn)
    conn.close()
    captured = {}

    def fake_server(addr, handler):
        captured["srv"] = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        return captured["srv"]
    monkeypatch.setattr(srvmod, "ThreadingHTTPServer", fake_server)
    threading.Thread(target=dashboard.serve, args=(db, "127.0.0.1", 0), daemon=True).start()
    for _ in range(300):
        if "srv" in captured:
            break
        threading.Event().wait(0.01)
    try:
        port = captured["srv"].server_address[1]
        assert _request(port, "GET", "/")[0] == 200
        assert "key" not in capsys.readouterr().out.lower()
    finally:
        captured["srv"].shutdown()


def test_widened_bind_without_a_key_is_a_programming_error(tmp_path):
    from lastbell import dashboard
    from lastbell.dashboard.server import DashboardRefused

    with pytest.raises(DashboardRefused):
        dashboard.serve(tmp_path / "d.db", "0.0.0.0", 0)


def test_cli_show_key_prints_key_and_link(monkeypatch, capsys, tmp_path):
    import argparse

    from lastbell import cli

    monkeypatch.setenv("LASTBELL_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LASTBELL_DISTRICT", "x.example")
    monkeypatch.setenv("LASTBELL_USERNAME", "p")
    monkeypatch.setenv("LASTBELL_DASHBOARD_KEY", "K-from-env")
    rc = cli._cmd_dashboard(argparse.Namespace(db=None, host="192.168.1.20", port=None,
                                               show_key=True))
    out = capsys.readouterr().out
    assert rc == 0 and "K-from-env" in out and "http://192.168.1.20:8321/?key=K-from-env" in out


def test_cli_refuses_a_public_bind_in_one_line(monkeypatch, capsys, tmp_path):
    import argparse

    from lastbell import cli

    monkeypatch.setenv("LASTBELL_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LASTBELL_DISTRICT", "x.example")
    monkeypatch.setenv("LASTBELL_USERNAME", "p")
    monkeypatch.setenv("LASTBELL_DASHBOARD_KEY", "K")
    monkeypatch.delenv("LASTBELL_DASHBOARD_PUBLIC", raising=False)
    rc = cli._cmd_dashboard(argparse.Namespace(db=tmp_path / "d.db", host="8.8.8.8",
                                               port=None, show_key=False))
    err = capsys.readouterr().err
    assert rc == 2 and "public address" in err
