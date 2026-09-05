"""`lastbell status` (0.2.8): one pasteable screen — initials only, no
password, no account name in paths, nothing created, nothing fetched."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lastbell import cli, health, service, status, store, updates, watchers
from lastbell.models import WatcherKind

NOW = datetime(2026, 9, 4, 16, 0, tzinfo=timezone.utc)


@pytest.fixture
def world(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("LASTBELL_HOME", str(home))
    monkeypatch.chdir(tmp_path)                          # no checkout .env
    for key in ("LASTBELL_DB_PATH", "LASTBELL_SNAPSHOT_DIR", "LASTBELL_PASSWORD",
                "LASTBELL_PASSWORD_FILE", "LASTBELL_DASHBOARD_KEY", "LASTBELL_CANVAS_HOST",
                "LASTBELL_SMTP_HOST", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LASTBELL_DISTRICT", "x.example")
    monkeypatch.setenv("LASTBELL_USERNAME", "parent.person@example.com")
    monkeypatch.setenv("LASTBELL_SECRET_BACKEND", "env")
    monkeypatch.setenv("LASTBELL_PASSWORD", "hunter2-secret")
    monkeypatch.setenv("LASTBELL_NOTIFY_CHANNEL", "console")
    monkeypatch.setenv("LASTBELL_CANVAS", "auto")
    monkeypatch.setenv("LASTBELL_POLL_MINUTES", "180")
    (home / "env").write_text("LASTBELL_USERNAME=parent.person@example.com\n"
                              "LASTBELL_PASSWORD=hunter2-secret\n")
    monkeypatch.setattr(service, "platform_name", lambda: "linux")
    monkeypatch.setattr(service, "executable", lambda: str(home / ".local/bin/lastbell"))
    monkeypatch.setattr(service, "_host_is_utc", lambda: False)
    monkeypatch.setattr(service, "_home", lambda: home)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(updates, "installed_version", lambda: updates.__version__)
    monkeypatch.setattr(status, "dashboard_listening", lambda host, port: False)

    def boom():
        raise AssertionError("the keyring must not be touched on the env backend")
    monkeypatch.setattr(status, "keyring_name", boom)
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        import subprocess
        return subprocess.CompletedProcess(cmd, 0, "active\n", "")
    monkeypatch.setattr(service, "_run", fake_run)
    return {"home": home, "calls": calls}


def _db(home: Path) -> sqlite3.Connection:
    conn = store.connect(home / "lastbell.db")
    store.ensure_schema(conn)
    return conn


def _students(conn):
    conn.execute("INSERT INTO students (id, agu, name, initials) VALUES "
                 "('s1', '1', 'Elena Rivera', 'E.R.'), ('s2', '2', 'Marcus Rivera', '')")
    conn.commit()


# ── formatters ────────────────────────────────────────────────────────


def test_tilde_hides_the_account_name(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "dcAgent"))
    assert status.tilde(tmp_path / "dcAgent" / ".config" / "lastbell" / "env") == \
        "~/.config/lastbell/env"
    assert status.tilde(tmp_path / "dcAgent") == "~"
    assert status.tilde(tmp_path / "elsewhere" / "x") == str(tmp_path / "elsewhere" / "x")


def test_ago_in_words():
    stamp = lambda **kw: (NOW - timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
    assert status.ago(stamp(seconds=30), NOW) == "just now"
    assert status.ago(stamp(minutes=12), NOW) == "12 min ago"
    assert status.ago(stamp(hours=1), NOW) == "1 hour ago"
    assert status.ago(stamp(hours=5), NOW) == "5 hours ago"
    assert status.ago(stamp(days=3), NOW) == "3 days ago"
    assert status.ago(None, NOW) == ""
    assert status.ago("garbage", NOW) == ""


# ── the report ────────────────────────────────────────────────────────


def test_report_without_settings_still_says_what_to_do(world, monkeypatch):
    monkeypatch.delenv("LASTBELL_USERNAME")
    (world["home"] / "env").unlink()
    text = "\n".join(status.report(NOW))
    assert text.startswith(f"Last Bell {updates.__version__}")
    assert "Settings file: none — `lastbell setup` writes one" in text
    assert "Settings: NOT LOADED" in text
    assert "not installed — `lastbell install-service`" in text
    assert "Paste this when reporting a problem" in text
    assert not (world["home"] / "lastbell.db").exists()      # nothing created


def test_report_is_pasteable(world):
    home = world["home"]
    conn = _db(home)
    _students(conn)
    store.record_poll(conn, (NOW - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"))
    mom = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                               {"email": {"to": "mom@example.com"}})
    kid = watchers.add_watcher(conn, "Marcus", WatcherKind.STUDENT, {"ntfy": {"topic": "t-1"}})
    watchers.subscribe(conn, mom, "s1", None, None)
    watchers.subscribe(conn, mom, "s2", None, None)
    watchers.subscribe(conn, kid, "s2", None, None)
    conn.close()
    (home / ".config/systemd/user").mkdir(parents=True)
    (home / ".config/systemd/user/lastbell.service").write_text("[Unit]")
    (home / "logs").mkdir()
    (home / "logs/lastbell.log").write_text("x" * 2048)

    lines = status.report(NOW)
    text = "\n".join(lines)
    assert "parent.person" not in text and "hunter2" not in text
    assert "Elena" not in text and "Rivera" not in text
    assert "Students: 2 (E.R., M.R.)" in text                # initials derived when blank
    assert "Mom (guardian): email=mom@example.com → E.R., M.R." in text
    assert "Marcus (student): ntfy=t-1 → M.R." in text
    assert "Password: the settings file (LASTBELL_PASSWORD, owner-only)" in text
    assert "District: x.example" in text
    assert "Last successful check:" in text and "(2 hours ago)" in text
    assert "Checking: OK" in text
    assert "Next check: about" in text
    assert "Service: poller: systemd user unit lastbell.service — active" in text
    assert "dashboard" not in text.split("Service:")[1].split("\n")[0]   # no dashboard unit
    assert "Dashboard: 127.0.0.1:8321 — not listening" in text
    assert "Log: ~/logs/lastbell.log (2.0 KB" in text
    assert "Database: ~/lastbell.db" in text
    assert str(home) not in text                               # every path is ~-relative
    assert world["calls"] == [["systemctl", "--user", "is-active", "lastbell.service"]]


def test_report_names_a_failing_watcher_and_the_pending_restart(world, monkeypatch):
    home = world["home"]
    conn = _db(home)
    store.record_poll(conn, (NOW - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"))
    for _ in range(3):
        health.record_failure(conn, "login", "Invalid user id or password")
    health.mark_notified(conn)
    conn.close()
    monkeypatch.setattr(updates, "installed_version", lambda: "99.0.0")
    text = "\n".join(status.report(NOW))
    assert "99.0.0 installed — restart to use it" in text
    assert "Checking: FAILING — 3 in a row since" in text
    assert "the portal rejected the sign-in; guardians were told just now" in text
    assert "last error: Invalid user id or password" in text
    assert "Next try: once a day while sign-in is rejected" in text


def test_report_notices_a_stopped_service(world):
    conn = _db(world["home"])
    store.record_poll(conn, (NOW - timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S"))
    conn.close()
    text = "\n".join(status.report(NOW))
    assert "Next check: was due 6 hours ago — the service doesn't look like it's running" in text
    assert "Watchers: none yet" in text
    assert "Students: none yet" in text


def test_report_dashboard_unit_and_utc_clock(world, monkeypatch):
    home = world["home"]
    (home / ".config/systemd/user").mkdir(parents=True)
    for unit in ("lastbell.service", "lastbell-dashboard.service"):
        (home / ".config/systemd/user" / unit).write_text("[Unit]")
    monkeypatch.setattr(service, "_host_is_utc", lambda: True)
    monkeypatch.setattr(status, "dashboard_listening", lambda host, port: True)
    monkeypatch.setenv("LASTBELL_DASHBOARD_KEY", "k")
    text = "\n".join(status.report(NOW))
    assert "UTC! due dates" in text
    assert "dashboard: systemd user unit lastbell-dashboard.service — active" in text
    assert "listening; network key set" in text
    assert "k" not in text.split("network key")[1].split("\n")[0][:4]   # never the key itself


def test_report_tells_a_network_dashboard_where_the_key_lives(world, monkeypatch):
    monkeypatch.setenv("LASTBELL_DASHBOARD_HOST", "0.0.0.0")
    monkeypatch.setenv("LASTBELL_DASHBOARD_KEY", "super-secret-key")
    monkeypatch.setattr(status, "dashboard_listening", lambda host, port: True)
    text = "\n".join(status.report(NOW))
    assert ("other devices need the key once: lastbell dashboard --show-key "
            "prints the link") in text
    assert "super-secret-key" not in text                 # still never the key


def test_report_leaves_a_loopback_dashboard_alone(world, monkeypatch):
    monkeypatch.setattr(status, "dashboard_listening", lambda host, port: True)
    text = "\n".join(status.report(NOW))
    assert "Dashboard: 127.0.0.1:" in text
    assert "--show-key" not in text


def test_report_keyring_backend_names_it_without_reading(world, monkeypatch):
    monkeypatch.setenv("LASTBELL_SECRET_BACKEND", "keyring")
    monkeypatch.setattr(status, "keyring_name", lambda: "macOS Keychain")
    text = "\n".join(status.report(NOW))
    assert "Password: the OS keyring (macOS Keychain)" in text


def test_report_names_the_heartbeat_host_only(world, monkeypatch):
    monkeypatch.setenv("LASTBELL_HEARTBEAT_URL", "https://hc-ping.com/secret-uuid")
    text = "\n".join(status.report(NOW))
    assert "Heartbeat: hc-ping.com (pinged after every successful check)" in text
    assert "secret-uuid" not in text


def test_report_env_backend_without_a_password_says_so(world, monkeypatch):
    monkeypatch.delenv("LASTBELL_PASSWORD")
    (world["home"] / "env").write_text("LASTBELL_USERNAME=p\n")
    text = "\n".join(status.report(NOW))
    assert "Password: NOT SET" in text


def test_darwin_service_state(world, monkeypatch):
    import subprocess
    monkeypatch.setattr(service, "platform_name", lambda: "darwin")
    monkeypatch.setattr(service.os, "getuid", lambda: 501, raising=False)
    assert status.service_state("darwin")[0].startswith("not installed")
    plist = world["home"] / "Library/LaunchAgents/com.noestudios.lastbell.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("<plist/>")
    monkeypatch.setattr(service, "_run", lambda cmd: subprocess.CompletedProcess(
        cmd, 0, "com.noestudios.lastbell = {\n\tstate = running\n}\n", ""))
    assert status.service_state("darwin") == \
        ["poller: launchd agent com.noestudios.lastbell — running"]
    monkeypatch.setattr(service, "_run", lambda cmd: subprocess.CompletedProcess(cmd, 113, "", ""))
    assert "installed but not loaded" in status.service_state("darwin")[0]


def test_keyring_name_maps_backend_modules(monkeypatch):
    import keyring

    class Backend:
        pass
    for module, expect in (("keyring.backends.macOS", "macOS Keychain"),
                           ("keyring.backends.SecretService", "Secret Service"),
                           ("keyring.backends.fail", "none usable"),
                           ("keyring.backends.chainer", "several, chained")):
        Backend.__module__ = module
        monkeypatch.setattr(keyring, "get_keyring", lambda: Backend())
        assert status.keyring_name() == expect

    def boom():
        raise RuntimeError("no dbus")
    monkeypatch.setattr(keyring, "get_keyring", boom)
    assert status.keyring_name() == "none usable (RuntimeError)"


def test_dashboard_listening_probes_loopback_only(monkeypatch):
    import socket
    seen = []

    class Sock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def connect(addr, timeout):
        seen.append(addr)
        if addr[1] == 1:
            raise OSError("refused")
        return Sock()
    monkeypatch.setattr(socket, "create_connection", connect)
    assert status.dashboard_listening("0.0.0.0", 8321) is True
    assert status.dashboard_listening("::", 8321) is True
    assert status.dashboard_listening("127.0.0.1", 1) is False
    assert seen == [("127.0.0.1", 8321), ("::1", 8321), ("127.0.0.1", 1)]


def test_cli_status_prints_the_report(world, capsys):
    import argparse
    assert cli._cmd_status(argparse.Namespace()) == 0
    assert "Paste this when reporting a problem" in capsys.readouterr().out
