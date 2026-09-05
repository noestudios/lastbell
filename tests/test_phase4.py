"""Phase 4 odds and ends: grade-drop threshold, schema migration,
and schedule-aware routing."""
from __future__ import annotations

import pytest

from lastbell import differ, router, store, watchers
from lastbell.differ import Event
from lastbell.models import (
    AlertType,
    Course,
    Snapshot,
    Student,
    WatcherKind,
)


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "test.db")
    store.ensure_schema(c)
    yield c
    c.close()


def _course_snap(percent: str, mark: str = "B") -> Snapshot:
    return Snapshot(student_agu="1", courses=[
        Course(edupoint_gu="c1", title="Math", term="MP1", mark=mark, percent=percent)])


# ── grade drop ────────────────────────────────────────────────────────


def test_small_change_is_grade_changed():
    events = differ.diff(_course_snap("87.2%"), _course_snap("85.0%"))
    assert [e.type for e in events] == [AlertType.GRADE_CHANGED]


def test_big_drop_upgrades_to_grade_drop_not_both():
    events = differ.diff(_course_snap("87.2%"), _course_snap("80.0%"))
    assert [e.type for e in events] == [AlertType.GRADE_DROP]
    assert "DROPPED 7.2 points" in events[0].detail


def test_drop_threshold_is_configurable():
    events = differ.diff(_course_snap("87%"), _course_snap("85%"),
                         grade_drop_points=2.0)
    assert [e.type for e in events] == [AlertType.GRADE_DROP]


def test_rise_is_never_a_drop():
    events = differ.diff(_course_snap("80%"), _course_snap("90%"))
    assert [e.type for e in events] == [AlertType.GRADE_CHANGED]


def test_unparseable_percent_falls_back_to_grade_changed():
    events = differ.diff(_course_snap("N/A", mark="B"), _course_snap("", mark="C"))
    assert [e.type for e in events] == [AlertType.GRADE_CHANGED]
    # An unparseable percent is no percent: the sentence shows the mark
    # alone rather than parroting "N/A" where a grade belongs.
    assert "overall B → C" in events[0].detail


def test_percent_display_rule():
    from lastbell.models import format_percent

    assert format_percent("87.20%") == "87.2"
    assert format_percent("0%") == "0.0"
    assert format_percent("93") == "93.0"
    assert format_percent("51.15%") == "51.1"   # single decimal, banker's-adjacent
    assert format_percent("N/A") is None
    assert format_percent("") is None


# ── migration ─────────────────────────────────────────────────────────


def test_phase3_db_gains_new_columns(tmp_path):
    """A database created before later columns shipped (no last_sent_on /
    current_term) is patched by ensure_schema instead of breaking."""
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE watchers (id TEXT PRIMARY KEY, name TEXT NOT NULL,
            kind TEXT NOT NULL, channels TEXT NOT NULL DEFAULT '{}',
            quiet_hours TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE students (id TEXT PRIMARY KEY, agu TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL, initials TEXT NOT NULL DEFAULT '',
            school TEXT NOT NULL DEFAULT '');
        CREATE TABLE subscriptions (id TEXT PRIMARY KEY, watcher_id TEXT NOT NULL,
            student_id TEXT NOT NULL, alert_type TEXT NOT NULL,
            channel TEXT NOT NULL, send_at TEXT);
        CREATE TABLE alerts (id TEXT PRIMARY KEY, student_id TEXT NOT NULL,
            type TEXT NOT NULL, body TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')), acked_by TEXT);
    """)
    old.commit()
    old.close()

    conn = store.connect(path)
    store.ensure_schema(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(subscriptions)")}
    assert "last_sent_on" in cols
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(students)")}
    assert "current_term" in cols
    conn.close()


# ── schedule-aware routing ────────────────────────────────────────────


GRADE = Event(type=AlertType.GRADE_CHANGED, student_agu="1",
              course_title="Math", detail="Math: quiz graded: 8/10")
MISSING = Event(type=AlertType.ASSIGNMENT_MISSING, student_agu="1",
                course_title="Art", detail="Art: “Collage” is marked missing")


@pytest.fixture
def routed(conn):
    store.persist_snapshot(conn, Student(agu="1", name="Jasper P. Hays",
                                         initials="J.P.H."),
                           Snapshot(student_agu="1"))
    return conn


def test_plan_splits_immediate_and_digest(routed):
    conn = routed
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                             {"email": {"to": "m@x.com"}})
    watchers.subscribe(conn, w, "1", ["assignment_missing"], ["email"])          # now
    watchers.subscribe(conn, w, "1", ["grade_changed"], ["email"], send_at="17:00")
    deliveries, _ = router.plan(conn, "1", [GRADE, MISSING])
    assert {(d.send_at, tuple(e.detail for e in d.events)) for d in deliveries} == {
        (None, (MISSING.detail,)),
        ("17:00", (GRADE.detail,)),
    }


def test_immediate_beats_digest_for_same_event(routed):
    conn = routed
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                             {"email": {"to": "m@x.com"}})
    watchers.subscribe(conn, w, "1", None, ["email"])                    # '*' immediate
    watchers.subscribe(conn, w, "1", ["grade_changed"], ["email"], send_at="17:00")
    deliveries, _ = router.plan(conn, "1", [GRADE])
    (d,) = deliveries
    assert d.send_at is None and d.events == [GRADE]


def test_daily_summary_rows_do_not_route_events(routed):
    conn = routed
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                             {"email": {"to": "m@x.com"}})
    watchers.subscribe(conn, w, "1", ["daily_summary"])
    deliveries, warnings = router.plan(conn, "1", [GRADE])
    assert deliveries == [] and warnings == []
    # ...but they do count as "has subscriptions" so the global fallback
    # doesn't double-send to the household address.
    assert router.has_subscriptions(conn, "1")
