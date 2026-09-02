"""Watcher/subscription CRUD and student resolution."""
from __future__ import annotations

import pytest

from lastbell import store, watchers
from lastbell.models import Snapshot, Student, WatcherKind


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "test.db")
    store.ensure_schema(c)
    yield c
    c.close()


@pytest.fixture
def conn_with_student(conn):
    student = Student(agu="1", name="Jasper P. Hays", school="Example ES", initials="J.P.H.")
    store.persist_snapshot(conn, student, Snapshot(student_agu="1"))
    return conn


def test_add_and_get_watcher(conn):
    watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                         {"email": {"to": "mom@example.com"}})
    w = watchers.get_watcher(conn, "mom")   # case-insensitive
    assert w is not None
    assert w.kind is WatcherKind.GUARDIAN
    assert w.channels == {"email": {"to": "mom@example.com"}}


def test_duplicate_watcher_name_rejected(conn):
    watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    with pytest.raises(watchers.WatcherError, match="already exists"):
        watchers.add_watcher(conn, "mom", WatcherKind.STUDENT)


def test_set_channels_merges_and_removes(conn):
    watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                         {"email": {"to": "mom@example.com"}})
    w = watchers.set_channels(conn, "Mom", {"ntfy": {"topic": "t"}, "console": {}})
    assert set(w.channels) == {"email", "ntfy", "console"}
    w = watchers.set_channels(conn, "Mom", {"email": None})
    assert set(w.channels) == {"ntfy", "console"}   # console's {} survives


def test_remove_watcher_cascades_subscriptions(conn_with_student):
    conn = conn_with_student
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    watchers.subscribe(conn, w, "1")
    watchers.remove_watcher(conn, "Mom")
    assert conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM watcher_student").fetchone()[0] == 0


def test_resolve_student_by_agu_and_name_prefix(conn_with_student):
    conn = conn_with_student
    assert watchers.resolve_student(conn, "1")["name"] == "Jasper P. Hays"
    assert watchers.resolve_student(conn, "jas")["agu"] == "1"
    assert watchers.resolve_student(conn, "JPH")["agu"] == "1"   # initials, dots optional
    with pytest.raises(watchers.WatcherError, match="no student matching"):
        watchers.resolve_student(conn, "zz")


def test_resolve_student_empty_db_says_run_first(conn):
    with pytest.raises(watchers.WatcherError, match="run.*once"):
        watchers.resolve_student(conn, "1")


def test_subscribe_is_idempotent(conn_with_student):
    conn = conn_with_student
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    assert len(watchers.subscribe(conn, w, "1")) == 1              # '*' / '*'
    assert watchers.subscribe(conn, w, "1") == []                  # same row again
    assert len(watchers.subscribe(conn, w, "1", ["grade_changed"], ["email"])) == 1


def test_subscribe_rejects_unknown_alert_type(conn_with_student):
    conn = conn_with_student
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    with pytest.raises(watchers.WatcherError, match="unknown alert type"):
        watchers.subscribe(conn, w, "1", ["not_a_thing"])


def test_unsubscribe_scoped_and_full(conn_with_student):
    conn = conn_with_student
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    watchers.subscribe(conn, w, "1", ["grade_changed", "assignment_missing"])
    assert watchers.unsubscribe(conn, w, "1") == 2
    watchers.subscribe(conn, w, "1")
    assert watchers.unsubscribe(conn, w) == 1
    assert watchers.list_subscriptions(conn) == []


# ── default watcher (UX decision 3) ───────────────────────────────────


def test_ensure_default_watcher_seeds_guardian_with_email(conn_with_student):
    conn = conn_with_student
    w = watchers.ensure_default_watcher(conn, "parent_login", "mom@example.com")
    assert w is not None
    assert w.kind is WatcherKind.GUARDIAN
    assert w.channels == {"email": {"to": "mom@example.com"}}
    subs = watchers.list_subscriptions(conn)
    assert [(s.watcher_name, s.student_name, s.alert_type, s.channel)
            for s in subs] == [("parent_login", "Jasper P. Hays", "*", "*")]


def test_ensure_default_watcher_falls_back_to_console(conn_with_student):
    w = watchers.ensure_default_watcher(conn_with_student, "parent_login", None)
    assert w.channels == {"console": {}}


def test_ensure_default_watcher_noop_when_any_watcher_exists(conn_with_student):
    conn = conn_with_student
    watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    assert watchers.ensure_default_watcher(conn, "parent_login") is None
    assert [w.name for w in watchers.list_watchers(conn)] == ["Mom"]


def test_ensure_default_watcher_with_no_students_still_creates(conn):
    w = watchers.ensure_default_watcher(conn, "parent_login")
    assert w is not None
    assert watchers.list_subscriptions(conn) == []


def test_default_watcher_seeds_4pm_digest_with_urgent(conn_with_student):
    conn = conn_with_student
    watchers.ensure_default_watcher(conn, "parent_login", "mom@example.com")
    (sub,) = watchers.list_subscriptions(conn)
    assert (sub.send_at, sub.urgent_now) == ("16:00", True)


def test_set_subscription_group_reconciles_types(conn_with_student):
    conn = conn_with_student
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    watchers.subscribe(conn, w, "1", ["grade_changed", "grade_drop"], ["email"])
    ids = [s.id for s in watchers.list_subscriptions(conn)]
    # drop grade_drop, add assignment_missing, move everything to a digest
    watchers.set_subscription_group(
        conn, ids, ["grade_changed", "assignment_missing"], "email", "16:00", True)
    subs = watchers.list_subscriptions(conn)
    assert sorted(s.alert_type for s in subs) == ["assignment_missing", "grade_changed"]
    assert all((s.channel, s.send_at, s.urgent_now) == ("email", "16:00", True)
               for s in subs)
    # kept row keeps its id (history/identity), dropped row is gone
    kept = next(s for s in subs if s.alert_type == "grade_changed")
    assert kept.id in ids


def test_set_subscription_group_wildcard_collapses(conn_with_student):
    conn = conn_with_student
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    watchers.subscribe(conn, w, "1", ["grade_changed", "grade_drop"])
    ids = [s.id for s in watchers.list_subscriptions(conn)]
    watchers.set_subscription_group(conn, ids, ["*", "grade_changed"], "*", None)
    (sub,) = watchers.list_subscriptions(conn)
    assert sub.alert_type == "*"


def test_set_subscription_group_conflict_rolls_back(conn_with_student):
    conn = conn_with_student
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    watchers.subscribe(conn, w, "1", ["grade_changed"], ["email"])         # group A
    watchers.subscribe(conn, w, "1", ["grade_drop"], ["email"])            # group B
    subs = {s.alert_type: s.id for s in watchers.list_subscriptions(conn)}
    with pytest.raises(watchers.WatcherError, match="identical"):
        watchers.set_subscription_group(
            conn, [subs["grade_drop"]], ["grade_drop", "grade_changed"],
            "email", None)
    # nothing changed: both original rows intact
    assert sorted(s.alert_type for s in watchers.list_subscriptions(conn)) \
        == ["grade_changed", "grade_drop"]


# ── 0.1.3: `lastbell watcher test` ────────────────────────────────────


def _run_cli(monkeypatch, capsys, *argv):
    import sys

    from lastbell import cli
    monkeypatch.setattr(sys, "argv", ["lastbell", *argv])
    try:
        cli.main()
    except SystemExit as e:
        return e.code, capsys.readouterr()
    raise AssertionError("cli.main() didn't exit")


@pytest.fixture
def cli_db(monkeypatch, tmp_path):
    from lastbell import store
    from lastbell.models import WatcherKind

    db = tmp_path / "t.db"
    monkeypatch.setenv("LASTBELL_DISTRICT", "host.example")
    monkeypatch.setenv("LASTBELL_USERNAME", "parent1")
    monkeypatch.setenv("LASTBELL_DB_PATH", str(db))
    conn = store.connect(db)
    store.ensure_schema(conn)
    watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                {"sms": {"to": "3015551234@vtext.com"},
                 "email": {"to": "mom@example.com"}})
    watchers.add_watcher(conn, "Dad", WatcherKind.GUARDIAN, {})
    conn.close()
    return db


def test_watcher_test_sends_to_every_channel(cli_db, monkeypatch, capsys):
    from lastbell import notify
    sent = []
    monkeypatch.setattr(notify, "send_test", lambda c, a: sent.append((c, a)))
    code, out = _run_cli(monkeypatch, capsys, "watcher", "test", "Mom")
    assert code == 0
    assert sorted(sent) == [("email", {"to": "mom@example.com"}),
                            ("sms", {"to": "3015551234@vtext.com"})]
    assert "✓ text message 3015551234@vtext.com" in out.out
    assert "✓ email mom@example.com" in out.out


def test_watcher_test_one_channel_and_failures(cli_db, monkeypatch, capsys):
    from lastbell import notify

    def flaky(c, a):
        if c == "email":
            raise ValueError("LASTBELL_SMTP_HOST is not set")
    monkeypatch.setattr(notify, "send_test", flaky)
    code, out = _run_cli(monkeypatch, capsys, "watcher", "test", "Mom", "--channel", "sms")
    assert code == 0 and "email" not in out.out

    code, out = _run_cli(monkeypatch, capsys, "watcher", "test", "Mom")
    assert code == 1
    assert "✗ email mom@example.com: LASTBELL_SMTP_HOST is not set" in out.out
    assert "✓ text message" in out.out                 # the other one still went

    code, out = _run_cli(monkeypatch, capsys, "watcher", "test", "Mom", "--channel", "ntfy")
    assert code == 2 and "has no ntfy channel" in out.err
    code, out = _run_cli(monkeypatch, capsys, "watcher", "test", "Dad")
    assert code == 2 and "no channels yet" in out.err
    code, out = _run_cli(monkeypatch, capsys, "watcher", "test", "Nobody")
    assert code == 2 and "no watcher named" in out.err
