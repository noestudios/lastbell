"""The `lastbell setup` wizard (install Phase 2) — scripted end-to-end runs
with the network, keyring, and terminal stubbed out."""
from __future__ import annotations

import os

import pytest

from lastbell import preflight
from lastbell import secrets as secretstore
from lastbell import setup_wizard as wiz

_real_get_password = secretstore.get_password
_real_offer_service = wiz._offer_service


# ── env-file bookkeeping ──────────────────────────────────────────────


def test_read_env_parses_and_strips_quotes(tmp_path):
    f = tmp_path / "env"
    f.write_text("# comment\n\nLASTBELL_DISTRICT=host.example\n"
                 'LASTBELL_USERNAME="quoted"\nnot a pair\n')
    assert wiz.read_env(f) == {"LASTBELL_DISTRICT": "host.example",
                               "LASTBELL_USERNAME": "quoted"}
    assert wiz.read_env(tmp_path / "missing") == {}


def test_write_env_updates_in_place_preserving_comments(tmp_path, monkeypatch):
    monkeypatch.delenv("LASTBELL_DISTRICT", raising=False)
    f = tmp_path / "env"
    f.write_text("# keep me\nLASTBELL_DISTRICT=old.example\n"
                 "# LASTBELL_PASSWORD=   <- commented template line, untouched\n")
    wiz.write_env(f, {"LASTBELL_DISTRICT": "new.example",
                      "LASTBELL_USERNAME": "parent1"})
    text = f.read_text()
    assert "# keep me" in text
    assert "LASTBELL_DISTRICT=new.example" in text
    assert "old.example" not in text
    assert "# LASTBELL_PASSWORD=" in text            # commented lines survive
    assert text.rstrip().endswith("LASTBELL_USERNAME=parent1")  # appended
    # …and the process env sees the values immediately (later steps rely on it)
    import os
    assert os.environ["LASTBELL_DISTRICT"] == "new.example"


def test_write_env_creates_fresh_file_with_header(tmp_path):
    f = tmp_path / "cfg" / "env"
    wiz.write_env(f, {"LASTBELL_DISTRICT": "host.example"})
    text = f.read_text()
    assert text.startswith("# Last Bell settings")
    assert "LASTBELL_DISTRICT=host.example" in text


# ── scripted wizard runs ──────────────────────────────────────────────


class Script:
    """Canned terminal: pops answers in order, records prompts and output."""

    def __init__(self, asks, yns, passwords):
        self.asks, self.yns, self.passwords = list(asks), list(yns), list(passwords)
        self.ask_log: list = []   # (prompt, default) pairs
        self.said: list = []

    def install(self, monkeypatch):
        def _ask(prompt, default=""):
            self.ask_log.append((prompt, default))
            answer = self.asks.pop(0)
            return answer if answer is not None else default
        monkeypatch.setattr(wiz, "_ask", _ask)
        monkeypatch.setattr(wiz, "_ask_yn", lambda p, default=True: self.yns.pop(0))
        monkeypatch.setattr(wiz, "_getpass", lambda p: self.passwords.pop(0))
        monkeypatch.setattr(wiz, "_say", lambda t="": self.said.append(t))
        monkeypatch.setattr(wiz, "_interactive", lambda: True)

    @property
    def output(self):
        return "\n".join(self.said)


def go_report(district="host.example"):
    r = preflight.Report(district=district, mode="full")
    for cid in ("web_login", "gate_fetch", "parse_classes", "parse_assignments"):
        r.add(cid, cid, preflight.PASS, "ok")
    assert r.verdict == "go"
    return r


@pytest.fixture
def wizard_world(monkeypatch, tmp_path):
    """Isolated home, no cwd .env, stubbed network/keyring/run/attach."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("LASTBELL_HOME", str(tmp_path / "home"))
    for var in ("LASTBELL_DISTRICT", "LASTBELL_USERNAME", "LASTBELL_DB_PATH",
                "LASTBELL_SNAPSHOT_DIR", "LASTBELL_NOTIFY_CHANNEL"):
        monkeypatch.delenv(var, raising=False)

    world = {"keyring": {}, "sends": [], "baseline_runs": 0, "attached": [],
             "service_offers": []}
    # A desktop with a working keyring, not Linux: the keyring path, no
    # unattended question. Tests for the fallback flip these.
    monkeypatch.setattr(wiz, "_keyring_available", lambda: True)
    monkeypatch.setattr(wiz, "_is_linux", lambda: False)
    monkeypatch.setattr(wiz, "_offer_service",
                        lambda unattended: world["service_offers"].append(unattended) or False)
    monkeypatch.setattr(wiz, "_anonymous_check",
                        lambda d: (True, "login form found"))
    monkeypatch.setattr(wiz, "_full_check",
                        lambda d, u, p: go_report(d))
    monkeypatch.setattr(wiz, "_test_send",
                        lambda name, addr: world["sends"].append((name, addr)))
    monkeypatch.setattr(wiz, "_baseline_run",
                        lambda: world.__setitem__("baseline_runs",
                                                  world["baseline_runs"] + 1))
    monkeypatch.setattr(wiz, "_attach_channel",
                        lambda user, chosen: world["attached"].append((user, chosen)) or True)

    def fake_get(username, backend="keyring"):
        if backend == "env":                    # the real thing: reads the process env
            return _real_get_password(username, backend)
        if username not in world["keyring"]:
            raise secretstore.SecretError("none stored")
        return world["keyring"][username]
    monkeypatch.setattr(wiz.secretstore, "get_password", fake_get)
    monkeypatch.setattr(wiz.secretstore, "set_password",
                        lambda u, p: world["keyring"].__setitem__(u, p))
    return world


def test_happy_path_ntfy(wizard_world, monkeypatch):
    script = Script(
        asks=[None,          # district — accept the MCPS default
              "parent1",     # username
              "2"],          # channel menu: ntfy
        yns=[True,           # ready for test push
             True,           # push arrived
             True],          # run the baseline now
        passwords=["hunter2"])
    script.install(monkeypatch)

    assert wiz.main() == 0

    # district default was offered, answers hit the keyring and the env file
    assert script.ask_log[0][1] == wiz.MCPS_HOST
    assert script.ask_log[2][1] == "1"                    # email is the default
    assert wizard_world["keyring"]["parent1"] == "hunter2"
    saved = wiz.read_env(wiz.paths.default_env_file())
    assert saved["LASTBELL_DISTRICT"] == wiz.MCPS_HOST
    assert saved["LASTBELL_USERNAME"] == "parent1"

    # one test push to a generated unguessable topic, baseline ran, channel attached
    (name, addr), = wizard_world["sends"]
    assert name == "ntfy" and addr["topic"].startswith("lastbell-")
    assert len(addr["topic"]) > len("lastbell-") + 10
    assert wizard_world["baseline_runs"] == 1
    assert wizard_world["attached"] == [("parent1", ("ntfy", addr))]
    assert "lastbell run --loop" in script.output

    # keyring path: the password never lands in the settings file
    assert saved["LASTBELL_SECRET_BACKEND"] == "keyring"
    assert "LASTBELL_PASSWORD" not in saved
    assert "hunter2" not in wiz.paths.default_env_file().read_text()
    assert wizard_world["service_offers"] == [False]   # offered, not pre-checked


def test_rerun_offers_saved_values_as_defaults(wizard_world, monkeypatch):
    wiz.write_env(wiz.paths.default_env_file(),
                  {"LASTBELL_DISTRICT": "other-host.example",
                   "LASTBELL_USERNAME": "parent1"})
    wizard_world["keyring"]["parent1"] = "stored-pw"
    script = Script(
        asks=[None, None, "3"],          # accept both saved defaults; console
        yns=[True],                      # run baseline
        passwords=[""])                  # keep the stored password
    script.install(monkeypatch)

    assert wiz.main() == 0
    assert script.ask_log[0][1] == "other-host.example"   # saved district offered
    assert script.ask_log[1][1] == "parent1"              # saved username offered
    assert wizard_world["keyring"]["parent1"] == "stored-pw"   # untouched
    assert wizard_world["sends"] == []                    # console: nothing to test
    assert wiz.read_env(wiz.paths.default_env_file())["LASTBELL_NOTIFY_CHANNEL"] == "console"


def test_failed_verify_saves_progress_and_stops(wizard_world, monkeypatch):
    bad = preflight.Report(district="host.example", mode="full")
    bad.add("web_login", "Web login", preflight.FAIL, "rejected")
    monkeypatch.setattr(wiz, "_full_check", lambda d, u, p: bad)
    script = Script(asks=[None, "parent1"], yns=[], passwords=["wrong-pw"])
    script.install(monkeypatch)

    assert wiz.main() == 1
    # the resume state is on disk even though the wizard bailed
    saved = wiz.read_env(wiz.paths.default_env_file())
    assert saved["LASTBELL_USERNAME"] == "parent1"
    assert wizard_world["baseline_runs"] == 0
    assert "saved" in script.output


def test_email_path_stores_smtp_password_in_keyring(wizard_world, monkeypatch):
    smtp_slot = {}
    monkeypatch.setattr(wiz.secretstore, "set_smtp_password",
                        lambda p: smtp_slot.__setitem__("pw", p))
    script = Script(
        asks=[None, "parent1", "1",              # email channel
              "smtp.example", "587", "me@example.com", None,   # host/port/user/from
              "mom@example.com"],           # recipient
        yns=[True,   # send test email
             True,   # it arrived
             True],  # run baseline
        passwords=["hunter2", "smtp-secret"])
    script.install(monkeypatch)

    assert wiz.main() == 0
    assert smtp_slot["pw"] == "smtp-secret"
    saved = wiz.read_env(wiz.paths.default_env_file())
    assert saved["LASTBELL_SMTP_HOST"] == "smtp.example"
    assert saved["LASTBELL_SMTP_TO"] == "mom@example.com"
    assert saved["LASTBELL_SMTP_FROM"] == "me@example.com"   # defaulted from user
    # neither password ever lands in the settings file
    assert "LASTBELL_PASSWORD" not in saved
    assert "LASTBELL_PASSWORD_SMTP" not in saved
    assert "smtp-secret" not in wiz.paths.default_env_file().read_text()
    assert wizard_world["sends"] == [("email", {"to": "mom@example.com"})]


# ── Phase 3a: no usable keyring / always-on box → env-file store ──────


def test_write_env_none_removes_a_key(tmp_path, monkeypatch):
    f = tmp_path / "env"
    wiz.write_env(f, {"LASTBELL_PASSWORD": "pw", "LASTBELL_USERNAME": "p"})
    assert os.environ["LASTBELL_PASSWORD"] == "pw"
    wiz.write_env(f, {"LASTBELL_PASSWORD": None})
    assert "LASTBELL_PASSWORD" not in f.read_text()
    assert "LASTBELL_USERNAME=p" in f.read_text()
    assert "LASTBELL_PASSWORD" not in os.environ


def test_no_keyring_falls_back_to_env_file(wizard_world, monkeypatch):
    monkeypatch.setattr(wiz, "_keyring_available", lambda: False)
    script = Script(
        asks=[None, "parent1", "2"],
        yns=[True,        # keep the password in the settings file
             True, True,  # ntfy test push, arrived
             True],       # baseline
        passwords=["hunter2"])
    script.install(monkeypatch)

    assert wiz.main() == 0
    env_file = wiz.paths.default_env_file()
    saved = wiz.read_env(env_file)
    assert saved["LASTBELL_SECRET_BACKEND"] == "env"
    assert saved["LASTBELL_PASSWORD"] == "hunter2"
    assert wizard_world["keyring"] == {}                      # keyring untouched
    assert (env_file.stat().st_mode & 0o777) == 0o600
    assert "no usable OS keyring" in script.output
    assert "plain text" in script.output                     # the trade-off, said
    assert wizard_world["service_offers"] == [True]          # service pre-checked


def test_no_keyring_declined_stops_cleanly(wizard_world, monkeypatch):
    monkeypatch.setattr(wiz, "_keyring_available", lambda: False)
    script = Script(asks=[None, "parent1"], yns=[False], passwords=[])
    script.install(monkeypatch)
    assert wiz.main() == 1
    saved = wiz.read_env(wiz.paths.default_env_file())
    assert saved["LASTBELL_USERNAME"] == "parent1"            # progress kept
    assert "LASTBELL_PASSWORD" not in saved


def test_linux_unattended_uses_env_file_even_with_keyring(wizard_world, monkeypatch):
    monkeypatch.setattr(wiz, "_is_linux", lambda: True)
    script = Script(
        asks=[None, "parent1", "3"],
        yns=[True,   # will run as a background service
             True,   # use the settings file
             True],  # baseline
        passwords=["hunter2"])
    script.install(monkeypatch)

    assert wiz.main() == 0
    saved = wiz.read_env(wiz.paths.default_env_file())
    assert saved["LASTBELL_SECRET_BACKEND"] == "env"
    assert saved["LASTBELL_PASSWORD"] == "hunter2"
    assert wizard_world["keyring"] == {}
    assert "can't unlock the desktop keyring" in script.output
    assert wizard_world["service_offers"] == [True]


def test_linux_attended_keeps_the_keyring(wizard_world, monkeypatch):
    monkeypatch.setattr(wiz, "_is_linux", lambda: True)
    script = Script(asks=[None, "parent1", "3"],
                    yns=[False,  # not a background service
                         True],  # baseline
                    passwords=["hunter2"])
    script.install(monkeypatch)
    assert wiz.main() == 0
    saved = wiz.read_env(wiz.paths.default_env_file())
    assert saved["LASTBELL_SECRET_BACKEND"] == "keyring"
    assert "LASTBELL_PASSWORD" not in saved
    assert wizard_world["keyring"]["parent1"] == "hunter2"


def test_env_backend_rerun_keeps_stored_password(wizard_world, monkeypatch):
    monkeypatch.setattr(wiz, "_keyring_available", lambda: False)
    wiz.write_env(wiz.paths.default_env_file(),
                  {"LASTBELL_USERNAME": "parent1",
                   "LASTBELL_SECRET_BACKEND": "env",
                   "LASTBELL_PASSWORD": "stored-pw"})
    script = Script(asks=[None, None, "3"], yns=[True, True], passwords=[""])
    script.install(monkeypatch)
    prompts = []
    monkeypatch.setattr(wiz, "_getpass", lambda p: prompts.append(p) or "")
    assert wiz.main() == 0
    assert "press Enter to keep the stored one" in prompts[0]
    assert wiz.read_env(wiz.paths.default_env_file())["LASTBELL_PASSWORD"] == "stored-pw"


def test_switching_back_to_keyring_scrubs_the_file(wizard_world, monkeypatch):
    """A box that used the env store, later given a keyring: the old password
    must not linger in the settings file."""
    monkeypatch.setattr(wiz, "_is_linux", lambda: True)
    wiz.write_env(wiz.paths.default_env_file(),
                  {"LASTBELL_USERNAME": "parent1",
                   "LASTBELL_SECRET_BACKEND": "env",
                   "LASTBELL_PASSWORD": "old-pw"})
    script = Script(asks=[None, None, "3"],
                    yns=[False,  # no longer unattended (default was True: env before)
                         True],
                    passwords=["new-pw"])
    script.install(monkeypatch)
    assert wiz.main() == 0
    text = wiz.paths.default_env_file().read_text()
    assert "old-pw" not in text and "new-pw" not in text
    saved = wiz.read_env(wiz.paths.default_env_file())
    assert saved["LASTBELL_SECRET_BACKEND"] == "keyring"
    assert wizard_world["keyring"]["parent1"] == "new-pw"
    assert "LASTBELL_PASSWORD" not in os.environ


def test_email_with_env_backend_writes_smtp_password_to_file(wizard_world, monkeypatch):
    monkeypatch.setattr(wiz, "_keyring_available", lambda: False)
    smtp_slot = {}
    monkeypatch.setattr(wiz.secretstore, "set_smtp_password",
                        lambda p: smtp_slot.__setitem__("pw", p))
    script = Script(
        asks=[None, "parent1", "1",
              "smtp.example", "587", "me@example.com", None, "you@example.com"],
        yns=[True,          # settings-file store
             True, True,    # test email, arrived
             True],         # baseline
        passwords=["hunter2", "smtp-secret"])
    script.install(monkeypatch)
    assert wiz.main() == 0
    saved = wiz.read_env(wiz.paths.default_env_file())
    assert saved["LASTBELL_PASSWORD_SMTP"] == "smtp-secret"
    assert smtp_slot == {}                                   # keyring never asked
    assert os.environ["LASTBELL_PASSWORD_SMTP"] == "smtp-secret"  # test send sees it


# ── Phase 3b: the wizard offers install-service ───────────────────────


def test_offer_service_calls_installer(monkeypatch):
    from lastbell import service
    said, calls = [], []
    monkeypatch.setattr(wiz, "_say", said.append)
    monkeypatch.setattr(wiz, "_ask_yn", lambda p, default=True: calls.append(default) or True)
    monkeypatch.setattr(service, "platform_name", lambda: "linux")
    monkeypatch.setattr(service, "install", lambda say: calls.append("install") or 0)
    assert wiz._offer_service(unattended=True) is True
    assert calls == [True, "install"]                        # default followed unattended

    calls.clear()
    monkeypatch.setattr(wiz, "_ask_yn", lambda p, default=True: False)
    assert wiz._offer_service(unattended=False) is False
    assert calls == [] and "install-service" in "\n".join(said)


def test_offer_service_windows_prints_hint_only(monkeypatch):
    from lastbell import service
    said = []
    monkeypatch.setattr(wiz, "_say", said.append)
    monkeypatch.setattr(wiz, "_ask_yn", lambda p, default=True: pytest.fail("no prompt"))
    monkeypatch.setattr(service, "platform_name", lambda: "windows")
    assert wiz._offer_service(unattended=False) is False
    assert "Task Scheduler" in "\n".join(said)


def test_offer_service_survives_installer_error(monkeypatch):
    from lastbell import service
    said = []
    monkeypatch.setattr(wiz, "_say", said.append)
    monkeypatch.setattr(wiz, "_ask_yn", lambda p, default=True: True)
    monkeypatch.setattr(service, "platform_name", lambda: "darwin")

    def boom(say):
        raise service.ServiceError("no launcher")
    monkeypatch.setattr(service, "install", boom)
    assert wiz._offer_service(unattended=False) is False
    assert "no launcher" in "\n".join(said)


def test_offer_service_in_a_container_skips_with_one_line(monkeypatch):
    from lastbell import service
    said = []
    monkeypatch.setenv("LASTBELL_CONTAINER", "1")
    monkeypatch.setattr(wiz, "_say", said.append)
    monkeypatch.setattr(wiz, "_ask_yn", lambda p, default=True: pytest.fail("no prompt"))
    monkeypatch.setattr(service, "install", lambda say: pytest.fail("no install"))
    assert wiz._offer_service(unattended=True) is False
    assert "Docker keeps the container" in "\n".join(said)


def test_container_setup_writes_everything_to_the_volume(wizard_world, monkeypatch):
    """0.3.0 first run: `docker compose run --rm lastbell setup`. The image
    sets LASTBELL_HOME=/data and LASTBELL_CONTAINER=1; the wizard must take
    the settings-file path without asking about keyrings, land the file on
    the volume, skip the service step, and end with the compose commands."""
    monkeypatch.setenv("LASTBELL_CONTAINER", "1")
    monkeypatch.setattr(wiz, "_offer_service", _real_offer_service)   # the real one
    monkeypatch.setattr(wiz, "_keyring_available", lambda: pytest.fail("keyring probed"))
    script = Script(
        asks=[None, "parent1", "2"],
        yns=[True, True,  # ntfy test push, arrived
             True],       # baseline
        passwords=["hunter2"])
    script.install(monkeypatch)

    assert wiz.main() == 0
    volume = wiz.paths.data_dir()
    env_file = wiz.paths.default_env_file()
    assert env_file == volume / "env"                      # settings beside the data
    saved = wiz.read_env(env_file)
    assert saved["LASTBELL_SECRET_BACKEND"] == "env"
    assert saved["LASTBELL_PASSWORD"] == "hunter2"
    assert (env_file.stat().st_mode & 0o777) == 0o600
    assert wizard_world["keyring"] == {}
    assert wizard_world["baseline_runs"] == 1
    out = script.output
    assert "mounted volume" in out and "no OS keyring" in out
    assert "Docker keeps the container" in out            # the service step, explained
    assert "docker compose up -d" in out
    assert "docker compose exec dashboard lastbell dashboard --show-key" in out
    assert "lastbell run --loop" not in out               # not a command anyone types here


def test_container_setup_names_the_fix_for_an_unwritable_volume(wizard_world, monkeypatch, capsys):
    """Docker made ./data as root: say the chown line, don't traceback."""
    monkeypatch.setenv("LASTBELL_CONTAINER", "1")
    monkeypatch.setattr(wiz, "_interactive", lambda: True)
    monkeypatch.setattr(wiz, "_say", lambda t="": pytest.fail("the wizard must stop first"))
    wiz.paths.data_dir().mkdir(parents=True)
    monkeypatch.setattr(wiz.os, "access", lambda path, mode: False)
    assert wiz.main() == 2
    assert "sudo chown -R 1000:1000 data" in capsys.readouterr().err


def test_email_is_first_and_gateway_addresses_are_refused(wizard_world, monkeypatch):
    """Menu option 1 is email (text message was withdrawn in 0.1.5); a
    carrier gateway address is refused with the reason and re-asked."""
    monkeypatch.setattr(wiz.secretstore, "set_smtp_password", lambda p: None)
    script = Script(
        asks=[None, "parent1", "1",
              "smtp.example", "587", "me@example.com", None,
              "3015551234@vtext.com",          # gateway: refused, re-asked
              "3015551234@tmomail.net",        # dead gateway: refused, re-asked
              "mom@example.com"],
        yns=[True, True, True],                # test email, arrived, baseline
        passwords=["hunter2", "smtp-secret"])
    script.install(monkeypatch)

    assert wiz.main() == 0
    assert "Verizon is retiring" in script.output
    assert "T-Mobile shut down" in script.output
    assert "text message" not in script.output.lower()   # not offered anywhere
    chosen = ("email", {"to": "mom@example.com"})
    assert wizard_world["sends"] == [chosen]
    assert wizard_world["attached"] == [("parent1", chosen)]
    assert "arrive by email" in script.output


def test_non_interactive_refuses(monkeypatch, capsys):
    monkeypatch.setattr(wiz, "_interactive", lambda: False)
    assert wiz.main() == 2
    assert "interactive" in capsys.readouterr().err


# ── the watcher fix-up runs against a real database ───────────────────


def test_attach_channel_replaces_console_on_default_watcher(monkeypatch, tmp_path):
    from lastbell import store, watchers
    from lastbell.models import WatcherKind

    db = tmp_path / "t.db"
    monkeypatch.setenv("LASTBELL_DISTRICT", "host.example")
    monkeypatch.setenv("LASTBELL_USERNAME", "parent1")
    monkeypatch.setenv("LASTBELL_DB_PATH", str(db))
    conn = store.connect(db)
    store.ensure_schema(conn)
    watchers.add_watcher(conn, "parent1", WatcherKind.GUARDIAN, {"console": {}})
    conn.close()

    assert wiz._attach_channel("parent1", ("ntfy", {"topic": "lastbell-abc"}))
    conn = store.connect(db)
    w = watchers.get_watcher(conn, "parent1")
    conn.close()
    assert w.channels == {"ntfy": {"topic": "lastbell-abc"}}

    # the first run seeds an *email* channel from LASTBELL_SMTP_TO; choosing
    # text message in the wizard must replace it, not deliver twice
    conn = store.connect(db)
    watchers.set_channels(conn, "parent1",
                          {"ntfy": None, "email": {"to": "3015551234@vtext.com"}})
    conn.close()
    assert wiz._attach_channel("parent1", ("sms", {"to": "3015551234@vtext.com"}))
    conn = store.connect(db)
    w = watchers.get_watcher(conn, "parent1")
    conn.close()
    assert w.channels == {"sms": {"to": "3015551234@vtext.com"}}

    assert not wiz._attach_channel("nobody", ("ntfy", {"topic": "t"}))
