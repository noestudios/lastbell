"""Outbox: schedule math (digests + quiet hours) and queue/flush behavior."""
from __future__ import annotations

import datetime

import pytest

from lastbell import outbox, store, watchers
from lastbell.differ import Event
from lastbell.models import AlertType, Snapshot, Student, WatcherKind
from lastbell.router import Delivery

NOON = datetime.datetime(2026, 9, 1, 12, 0)
QUIET = {"start": "21:00", "end": "07:00"}

GRADE = Event(type=AlertType.GRADE_CHANGED, student_agu="1",
              course_title="Math", detail="Math: quiz graded: 8/10")
MISSING = Event(type=AlertType.ASSIGNMENT_MISSING, student_agu="1",
                course_title="Art", detail="Art: “Collage” is marked missing")


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "test.db")
    store.ensure_schema(c)
    store.persist_snapshot(
        c, Student(agu="1", name="Jasper P. Hays", initials="J.P.H."),
        Snapshot(student_agu="1"))
    yield c
    c.close()


class FakeChannel:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def send(self, to, subject, body):
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append((to, subject, body))


# ── schedule math ─────────────────────────────────────────────────────


def test_quiet_hours_crossing_midnight():
    assert outbox.in_quiet_hours(NOON.replace(hour=22), QUIET)
    assert outbox.in_quiet_hours(NOON.replace(hour=6), QUIET)
    assert not outbox.in_quiet_hours(NOON, QUIET)


def test_quiet_hours_same_day_window():
    quiet = {"start": "13:00", "end": "15:00"}
    assert outbox.in_quiet_hours(NOON.replace(hour=14), quiet)
    assert not outbox.in_quiet_hours(NOON.replace(hour=16), quiet)


def test_immediate_outside_quiet_sends_now():
    assert outbox.compute_send_after(NOON, None, {}) is None
    assert outbox.compute_send_after(NOON, None, QUIET) is None


def test_immediate_inside_quiet_defers_to_window_end():
    late = NOON.replace(hour=22, minute=30)
    assert outbox.compute_send_after(late, None, QUIET) == \
        datetime.datetime(2026, 9, 2, 7, 0)
    early = NOON.replace(hour=6, minute=30)
    assert outbox.compute_send_after(early, None, QUIET) == \
        datetime.datetime(2026, 9, 1, 7, 0)


def test_digest_time_later_today():
    assert outbox.compute_send_after(NOON, "17:00", {}) == \
        datetime.datetime(2026, 9, 1, 17, 0)


def test_digest_time_already_passed_rolls_to_tomorrow():
    assert outbox.compute_send_after(NOON.replace(hour=18), "17:00", {}) == \
        datetime.datetime(2026, 9, 2, 17, 0)


def test_digest_inside_quiet_pushed_to_window_end():
    assert outbox.compute_send_after(NOON, "22:00", QUIET) == \
        datetime.datetime(2026, 9, 2, 7, 0)


# ── queue + flush ─────────────────────────────────────────────────────


def _delivery(w, events, channel="ntfy"):
    return Delivery(watcher_name=w.name, channel=channel,
                    to=w.channels.get(channel) or {}, events=list(events),
                    watcher_id=w.id, quiet_hours=w.quiet_hours)


def test_enqueue_dedupes_unsent_rows(conn):
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                             {"ntfy": {"topic": "t"}})
    d = _delivery(w, [GRADE])
    later = NOON.replace(hour=17)
    assert outbox.enqueue(conn, d, later) == 1
    assert outbox.enqueue(conn, d, later) == 0   # same event re-planned next poll


def test_flush_before_due_sends_nothing(conn):
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                             {"ntfy": {"topic": "t"}})
    outbox.enqueue(conn, _delivery(w, [GRADE]), NOON.replace(hour=17))
    ch = FakeChannel()
    sent, warnings = outbox.flush_due(conn, now=NOON, channel_factory=lambda n: ch)
    assert sent == 0 and ch.calls == []
    assert len(outbox.pending(conn)) == 1


def test_flush_groups_into_one_message_and_marks_sent(conn):
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                             {"ntfy": {"topic": "t"}})
    outbox.enqueue(conn, _delivery(w, [GRADE, MISSING]), NOON.replace(hour=17))
    ch = FakeChannel()
    sent, warnings = outbox.flush_due(conn, now=NOON.replace(hour=17, minute=1),
                                      channel_factory=lambda n: ch)
    assert sent == 1 and warnings == []
    ((to, subject, body),) = ch.calls
    assert to == {"topic": "t"}
    assert subject.startswith("[Last Bell] J.P.H.: ") and subject.count("1 ") == 2
    assert "quiz graded" in body and "Collage" in body
    assert outbox.pending(conn) == []
    # a second flush is a no-op
    assert outbox.flush_due(conn, now=NOON.replace(hour=18),
                            channel_factory=lambda n: ch)[0] == 0


def test_flush_uses_current_address_not_queued_one(conn):
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                             {"ntfy": {"topic": "old"}})
    outbox.enqueue(conn, _delivery(w, [GRADE]), NOON.replace(hour=17))
    watchers.set_channels(conn, "Mom", {"ntfy": {"topic": "new"}})
    ch = FakeChannel()
    outbox.flush_due(conn, now=NOON.replace(hour=18), channel_factory=lambda n: ch)
    assert ch.calls[0][0] == {"topic": "new"}


def test_failed_flush_retries_next_time(conn):
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                             {"ntfy": {"topic": "t"}})
    outbox.enqueue(conn, _delivery(w, [GRADE]), NOON.replace(hour=17))
    bad = FakeChannel(fail=True)
    sent, warnings = outbox.flush_due(conn, now=NOON.replace(hour=18),
                                      channel_factory=lambda n: bad)
    assert sent == 0 and "will retry" in warnings[0]
    good = FakeChannel()
    sent, _ = outbox.flush_due(conn, now=NOON.replace(hour=18),
                               channel_factory=lambda n: good)
    assert sent == 1
