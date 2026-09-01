"""Watcher/subscription CRUD and student resolution."""
from __future__ import annotations

import pytest

from mcpsgradewatch import store, watchers
from mcpsgradewatch.models import Snapshot, Student, WatcherKind


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
    assert watchers.subscribe(conn, w, "1") == 1                   # '*' / '*'
    assert watchers.subscribe(conn, w, "1") == 0                   # same row again
    assert watchers.subscribe(conn, w, "1", ["grade_changed"], ["email"]) == 1


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
