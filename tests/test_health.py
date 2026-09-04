"""Watcher health (0.2.7): a watcher that can't sign in or can't reach the
portal tells the guardians once, backs off while sign-in is rejected, and
sends the all-clear when checking resumes. Plus `lastbell forget`."""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone

import pytest
import requests

from lastbell import cli, health, store, watchers
from lastbell.client import LoginError
from lastbell.models import WatcherKind


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.ensure_schema(c)
    yield c
    c.close()


class Notifier:
    def __init__(self):
        self.sent = []

    def send(self, subject, body):
        self.sent.append((subject, body))


class Conf:
    poll_minutes = 180
    heartbeat_url = ""


# ── the record ────────────────────────────────────────────────────────


def test_classify():
    assert health.classify(LoginError("rejected")) == "login"
    assert health.classify(requests.ConnectionError("x")) == "portal"
    assert health.classify(requests.HTTPError("500")) == "portal"
    assert health.classify(ValueError("parse")) == "other"


def test_failures_accumulate_and_success_clears(conn):
    assert not health.current(conn).failing
    s1 = health.record_failure(conn, "portal", "ConnectionError")
    s2 = health.record_failure(conn, "login", "rejected")
    assert (s1.failures, s2.failures) == (1, 2)
    assert s2.since == s1.since                    # first failure's time is kept
    assert s2.kind == "login"                       # latest kind wins
    cleared = health.record_success(conn)
    assert cleared.failures == 2
    assert not health.current(conn).failing
    assert health.current(conn).since is None


def test_notice_thresholds_per_kind(conn):
    for _ in range(health.NOTIFY_AFTER["login"] - 1):
        assert not health.record_failure(conn, "login", "x").notice_due
    assert health.record_failure(conn, "login", "x").notice_due
    health.mark_notified(conn)
    assert not health.record_failure(conn, "login", "x").notice_due  # told once
    health.record_success(conn)
    for _ in range(health.NOTIFY_AFTER["portal"] - 1):
        assert not health.record_failure(conn, "portal", "x").notice_due
    assert health.record_failure(conn, "portal", "x").notice_due


def test_login_rejections_back_off_to_daily(conn):
    for _ in range(health.LOGIN_BACKOFF_AFTER - 1):
        s = health.record_failure(conn, "login", "x")
        assert health.next_delay_minutes(s, 180) == 180
    s = health.record_failure(conn, "login", "x")
    assert health.next_delay_minutes(s, 180) == 24 * 60
    # an outage never backs off: the portal may come back any minute
    health.record_success(conn)
    for _ in range(10):
        s = health.record_failure(conn, "portal", "x")
    assert health.next_delay_minutes(s, 180) == 180


def test_messages_are_low_pii_and_say_what_to_do(conn):
    for _ in range(2):
        s = health.record_failure(conn, "login", "the portal rejected the sign-in")
    subject, body = health.failure_message(s, host="pi")
    assert "action needed" in subject
    assert "lastbell setup" in body and "pi" in body and "2 tries" in body
    assert "once a day" in body
    for word in ("Jasper", "Algebra", "hunter2"):
        assert word not in body
    subject, body = health.recovery_message(s)
    assert "checking again" in subject and "2 tries" in body


def test_notices_carry_an_html_twin(conn):
    s = health.record_failure(conn, "portal", "x")
    subject, body = health.failure_message(s, host="pi")
    assert body.html.startswith("<!doctype html>")
    assert "check the gradebook" in body.html and "pi" in body.html
    assert "<" not in str(body)                          # the text stays plain
    _, body = health.recovery_message(s)
    assert "Checking again" in body.html and "checking the gradebook again" in str(body)


def test_heartbeat_pings_once_and_never_raises(monkeypatch):
    seen = []

    class Resp:
        def __init__(self, code):
            self.code = code

        def raise_for_status(self):
            if self.code >= 400:
                raise requests.HTTPError(str(self.code))

    def fake_get(url, timeout, headers):
        seen.append((url, headers["User-Agent"]))
        if "down" in url:
            raise requests.ConnectionError("no route")
        return Resp(500 if "broken" in url else 200)
    monkeypatch.setattr(requests, "get", fake_get)
    assert health.heartbeat("https://hc-ping.com/abc") is None
    from lastbell import __version__
    assert seen == [("https://hc-ping.com/abc", f"lastbell/{__version__}")]
    warn = health.heartbeat("https://down.example/abc")
    assert warn.startswith("heartbeat: couldn't reach down.example (ConnectionError)")
    assert "abc" not in warn                             # the URL is a secret; host only
    assert "HTTPError" in health.heartbeat("https://broken.example/x")
    assert "isn't an http(s) URL" in health.heartbeat("ftp://x")
    assert len(seen) == 3


def test_poll_succeeded_pings_the_heartbeat(conn, monkeypatch):
    pinged = []
    monkeypatch.setattr(health, "heartbeat", lambda url: pinged.append(url) or None)

    class C(Conf):
        heartbeat_url = "https://hc-ping.com/abc"
    cli._poll_succeeded(conn, Notifier(), C())
    assert pinged == ["https://hc-ping.com/abc"]
    cli._poll_succeeded(conn, Notifier(), Conf())        # no URL: nothing pinged
    assert pinged == ["https://hc-ping.com/abc"]
    monkeypatch.setattr(health, "heartbeat", lambda url: "heartbeat: couldn't reach x (Boom)")
    warned = []
    monkeypatch.setattr(cli.log, "warning", warned.append)
    cli._poll_succeeded(conn, Notifier(), C())
    assert warned == ["heartbeat: couldn't reach x (Boom)"]


def test_deliver_reaches_every_guardian_channel_not_students(conn, monkeypatch):
    sent = []

    class Chan:
        def __init__(self, name):
            self.name = name

        def send(self, to, subject, body):
            if self.name == "ntfy":
                raise RuntimeError("ntfy down")
            sent.append((self.name, to, subject))

    from lastbell import notify
    monkeypatch.setattr(notify, "channel", lambda name: Chan(name))
    watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                         {"email": {"to": "mom@x"}, "ntfy": {"topic": "t"}})
    watchers.add_watcher(conn, "Dad", WatcherKind.GUARDIAN, {"email": {"to": "dad@x"}})
    watchers.add_watcher(conn, "Kid", WatcherKind.STUDENT, {"email": {"to": "kid@x"}})
    n, warnings = health.deliver(conn, Notifier(), "s", "b")
    assert n == 2 and sorted(t["to"] for _, t, _ in sent) == ["dad@x", "mom@x"]
    assert len(warnings) == 1 and "Mom via ntfy" in warnings[0]


def test_deliver_falls_back_to_the_global_notifier(conn):
    notifier = Notifier()
    n, warnings = health.deliver(conn, notifier, "s", "b")
    assert n == 1 and notifier.sent == [("s", "b")] and warnings == []


def test_dashboard_note(conn):
    assert health.dashboard_note(health.current(conn)) == ""
    s = health.record_failure(conn, "login", "x")
    note = health.dashboard_note(s)
    assert "1 try failed" in note and "lastbell setup" in note


def test_home_page_footer_shows_the_failure(conn, monkeypatch):
    from lastbell.dashboard.render import _freshness_html

    when = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    html = _freshness_html(when, failure_note="The last 3 tries failed: the portal couldn't be reached.")
    assert "stale" in html and "3 tries failed" in html and "role='status'" in html


# ── the loop hooks ────────────────────────────────────────────────────


def test_poll_failed_tells_guardians_once_and_backs_off(conn, monkeypatch, caplog):
    notifier = Notifier()
    delays = [cli._poll_failed(conn, notifier, Conf(), LoginError("rejected"))
              for _ in range(4)]
    assert delays == [180, 180, 1440, 1440]
    # told exactly once (at the second failure), via the fallback notifier
    assert len(notifier.sent) == 1 and "action needed" in notifier.sent[0][0]
    assert health.current(conn).notified
    # recovery: one all-clear, record cleared
    cli._poll_succeeded(conn, notifier)
    assert len(notifier.sent) == 2 and "checking again" in notifier.sent[1][0]
    assert not health.current(conn).failing
    # a lone blip never reaches anyone
    cli._poll_failed(conn, notifier, Conf(), requests.ConnectionError("x"))
    cli._poll_succeeded(conn, notifier)
    assert len(notifier.sent) == 2


def test_poll_failed_never_raises_when_delivery_breaks(conn, monkeypatch):
    class Broken:
        def send(self, *a):
            raise RuntimeError("smtp down")
    for _ in range(3):
        delay = cli._poll_failed(conn, Broken(), Conf(), LoginError("x"))
    assert delay == 1440
    assert health.current(conn).notified is None    # will try again next poll
    assert health.record_failure(conn, "login", "x").notice_due


# ── forget ────────────────────────────────────────────────────────────


def test_forget_removes_service_files_and_keyring_entries(tmp_path, monkeypatch, capsys):
    from lastbell import paths, service
    from lastbell import secrets as secretstore

    home = tmp_path / "home"
    monkeypatch.setenv("LASTBELL_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LASTBELL_DISTRICT", "x.example")
    monkeypatch.setenv("LASTBELL_USERNAME", "parent1")
    monkeypatch.delenv("LASTBELL_DB_PATH", raising=False)
    monkeypatch.delenv("LASTBELL_SNAPSHOT_DIR", raising=False)
    monkeypatch.setenv("LASTBELL_SECRET_BACKEND", "keyring")
    (home / "snapshots").mkdir(parents=True)
    (home / "lastbell.db").write_text("db")
    (home / "lastbell.db-wal").write_text("wal")
    (home / "env").write_text("LASTBELL_USERNAME=parent1\n")
    (home / "logs").mkdir()
    (home / "logs" / "lastbell.log").write_text("log")

    uninstalled = []
    monkeypatch.setattr(service, "platform_name", lambda: "linux")
    monkeypatch.setattr(service, "uninstall", lambda say=print: uninstalled.append(1) or 0)
    deleted = []

    class FakeKeyring:
        @staticmethod
        def delete_password(svc, account):
            from keyring.errors import PasswordDeleteError
            if account == "canvas":
                raise PasswordDeleteError("none")
            deleted.append((svc, account))
    import keyring
    monkeypatch.setattr(keyring, "delete_password", FakeKeyring.delete_password)

    rc = cli._cmd_forget(argparse.Namespace(yes=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert uninstalled == [1]
    assert not home.exists()
    assert deleted == [(secretstore.SERVICE, "parent1"), (secretstore.SERVICE, "smtp")]
    assert "no keyring entry for 'canvas'" in out
    assert "pipx uninstall lastbell" in out
    assert paths.data_dir() == home                  # sanity: we removed the right tree


def test_forget_env_backend_leaves_the_keyring_alone(tmp_path, monkeypatch, capsys):
    from lastbell import service

    home = tmp_path / "home"
    monkeypatch.setenv("LASTBELL_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LASTBELL_DISTRICT", "x.example")
    monkeypatch.setenv("LASTBELL_USERNAME", "parent1")
    monkeypatch.setenv("LASTBELL_SECRET_BACKEND", "env")
    (home).mkdir()
    (home / "env").write_text("LASTBELL_PASSWORD=pw\n")
    monkeypatch.setattr(service, "platform_name", lambda: "windows")
    import keyring

    def boom(*a):
        raise AssertionError("keyring must not be touched on the env backend")
    monkeypatch.setattr(keyring, "delete_password", boom)
    assert cli._cmd_forget(argparse.Namespace(yes=True)) == 0
    assert not home.exists()
    assert "not touched" in capsys.readouterr().out


def test_forget_without_yes_needs_a_terminal_or_the_word(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LASTBELL_HOME", str(tmp_path / "h"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LASTBELL_DISTRICT", "x.example")
    monkeypatch.setenv("LASTBELL_USERNAME", "p")
    import sys
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert cli._cmd_forget(argparse.Namespace(yes=False)) == 2
    assert "--yes" in capsys.readouterr().err
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    assert cli._cmd_forget(argparse.Namespace(yes=False)) == 1
    assert "nothing removed" in capsys.readouterr().out
    assert os.path.exists(tmp_path)                  # nothing else touched
