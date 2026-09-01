"""Daily summaries: content and the once-per-day send gate."""
from __future__ import annotations

import datetime

import pytest

from lastbell import store, summary, watchers
from lastbell.differ import Event
from lastbell.models import (
    AlertType,
    Assignment,
    AssignmentStatus,
    Course,
    Snapshot,
    Student,
    WatcherKind,
)

TODAY = datetime.date(2026, 9, 1)
MORNING = datetime.datetime(2026, 9, 1, 7, 30)


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "test.db")
    store.ensure_schema(c)
    snap = Snapshot(
        student_agu="1",
        courses=[Course(edupoint_gu="c1", title="Math", term="MP1",
                        mark="B+", percent="87.20%")],
        assignments=[
            Assignment(edupoint_gu="a1", course_gu="c1", name="Fractions Quiz",
                       due_date=TODAY + datetime.timedelta(days=2),
                       status=AssignmentStatus.DUE),
            Assignment(edupoint_gu="a2", course_gu="c1", name="Collage",
                       status=AssignmentStatus.MISSING),
        ],
    )
    store.persist_snapshot(
        c, Student(agu="1", name="Jasper P. Hays", school="Example ES",
                   initials="J.P.H."), snap)
    yield c
    c.close()


class FakeChannel:
    def __init__(self):
        self.calls = []

    def send(self, to, subject, body):
        self.calls.append((to, subject, body))


def test_build_lists_standing_state(conn):
    body = summary.build(conn, "1", "J.P.H.", today=TODAY)
    assert "Math 87.2% (B+)" in body
    assert "Missing (1):" in body and "Collage" in body
    assert "Due in the next 7 days (1):" in body and "Fractions Quiz" in body
    assert "Jasper" not in body   # low-PII: initials only, never the name


def test_build_lists_recent_alerts(conn):
    store.record_alert(conn, "1", Event(
        type=AlertType.ASSIGNMENT_MISSING, student_agu="1", course_title="Math",
        detail="Math: “Collage” is marked missing"))
    body = summary.build(conn, "1", "J.P.H.", today=TODAY)
    assert "Recent alerts this week (1):" in body
    assert "Collage" in body


def test_send_due_fires_once_per_day(conn):
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                             {"email": {"to": "mom@example.com"}})
    watchers.subscribe(conn, w, "1", ["daily_summary"], send_at="07:00")
    ch = FakeChannel()

    # before the scheduled time: nothing
    early = MORNING.replace(hour=6)
    assert summary.send_due(conn, now=early, channel_factory=lambda n: ch)[0] == 0

    sent, warnings = summary.send_due(conn, now=MORNING, channel_factory=lambda n: ch)
    assert sent == 1 and warnings == []
    ((to, subject, body),) = ch.calls
    assert to == {"to": "mom@example.com"}
    assert subject == "[Last Bell] Daily summary for J.P.H."
    assert "Missing (1):" in body

    # same day again: gated by last_sent_on
    assert summary.send_due(conn, now=MORNING.replace(hour=9),
                            channel_factory=lambda n: ch)[0] == 0
    # next morning: fires again
    tomorrow = MORNING + datetime.timedelta(days=1)
    assert summary.send_due(conn, now=tomorrow, channel_factory=lambda n: ch)[0] == 1


def test_send_due_wildcard_channel_uses_all_addresses(conn):
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                             {"email": {"to": "m@x.com"}, "ntfy": {"topic": "t"}})
    watchers.subscribe(conn, w, "1", ["daily_summary"])   # channel '*', 07:00 default
    ch = FakeChannel()
    sent, _ = summary.send_due(conn, now=MORNING, channel_factory=lambda n: ch)
    assert sent == 2
    assert {tuple(c[0].items()) for c in ch.calls} == \
        {(("to", "m@x.com"),), (("topic", "t"),)}
