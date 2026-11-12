"""One piece of work, one line (0.3.11). A lab zeroed and marked missing
produced four lines in one digest: "due Thursday" queued by an earlier
check, then "graded: 0/20", "is marked missing" and a gradebook/Canvas
"mismatch" of 0/20 against 0/20."""
from __future__ import annotations

import datetime

import pytest

from lastbell import differ, outbox, store, watchers
from lastbell.models import (
    SOURCE_CANVAS,
    AlertType,
    Assignment,
    AssignmentStatus,
    Course,
    Snapshot,
    Student,
    WatcherKind,
)
from lastbell.router import Delivery

S = AssignmentStatus
DUE = datetime.date(2026, 11, 12)
TODAY = datetime.date(2026, 11, 11)
NOON = datetime.datetime(2026, 11, 11, 12, 0)


def _pv(status=S.DUE, score=None, points=20.0):
    return Assignment(edupoint_gu="pv:1", course_gu="C", name="Water Lab", status=status,
                      score=score, points=points, due_date=DUE)


def _cv(status=S.DUE, score=None, points=20.0):
    return Assignment(edupoint_gu="canvas:1", course_gu="C", name="Water Lab", status=status,
                      score=score, points=points, due_date=DUE, source=SOURCE_CANVAS,
                      superseded_by="pv:1")


def _snap(*rows):
    return Snapshot(student_agu="1", term="MP2", assignments=list(rows),
                    courses=[Course(edupoint_gu="C", title="7: Science", term="MP2")])


# ── the mismatch rule ─────────────────────────────────────────────────


def test_a_missing_flag_beside_a_canvas_zero_is_agreement():
    assert not differ.conflicts(_pv(S.MISSING, 0.0), _cv(S.MISSING, 0.0))
    assert not differ.conflicts(_pv(S.MISSING, None), _cv(S.GRADED, 0.0))


def test_a_missing_flag_beside_real_canvas_credit_is_still_a_mismatch():
    assert differ.conflicts(_pv(S.MISSING, 0.0), _cv(S.GRADED, 17.0))
    assert differ.conflicts(_pv(S.GRADED, 0.0), _cv(S.GRADED, 17.0))
    assert differ.conflicts(_pv(S.GRADED, 12.0), _cv(S.GRADED, 17.0))
    assert not differ.conflicts(_pv(S.GRADED, 17.0), _cv(S.GRADED, 17.0))


# ── a missing zero is one line ────────────────────────────────────────


def test_a_zero_entered_with_the_missing_flag_is_said_once():
    events = differ.diff(_snap(_pv(), _cv()),
                         _snap(_pv(S.MISSING, 0.0), _cv(S.MISSING, 0.0)), today=TODAY)
    assert [(e.type, e.what) for e in events] == [
        (AlertType.ASSIGNMENT_MISSING, "is marked missing (0/20)")]


def test_missing_without_a_score_keeps_the_plain_line():
    events = differ.diff(_snap(_pv()), _snap(_pv(S.MISSING, None)), today=TODAY)
    assert [e.what for e in events] == ["is marked missing"]


def test_a_real_score_that_is_also_flagged_missing_says_both():
    events = differ.diff(_snap(_pv()), _snap(_pv(S.MISSING, 12.0)), today=TODAY)
    assert sorted(e.what for e in events) == ["graded: 12/20", "is marked missing"]


def test_a_zero_changed_on_work_already_missing_is_still_a_grade_line():
    events = differ.diff(_snap(_pv(S.MISSING, None)), _snap(_pv(S.MISSING, 0.0)), today=TODAY)
    assert [e.what for e in events] == ["graded: 0/20"]


# ── the digest drops what a later line made stale ─────────────────────


class _Channel:
    def __init__(self):
        self.bodies = []

    def send(self, to, subject, body):
        self.bodies.append((subject, body))


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.ensure_schema(c)
    store.persist_snapshot(c, Student(agu="1", name="Lane Harlow", initials="L.H."),
                           Snapshot(student_agu="1"))
    yield c
    c.close()


def _queue(conn, *events):
    wid = watchers.add_watcher(conn, "Parent", WatcherKind.GUARDIAN,
                               {"email": {"to": "parent@example.com"}})
    for e in events:
        outbox.enqueue(conn, Delivery(watcher_name="Parent", channel="email",
                                      to={"to": "parent@example.com"}, events=[e],
                                      watcher_id=getattr(wid, "id", wid)), NOON)
    return wid


def _text(body) -> str:
    return body if isinstance(body, str) else " ".join(str(v) for v in vars(body).values())


def _send(conn):
    ch = _Channel()
    outbox.flush_due(conn, NOON + datetime.timedelta(hours=8), channel_factory=lambda name: ch)
    assert len(ch.bodies) == 1
    return ch.bodies[0][0], _text(ch.bodies[0][1])


def _ev(kind, what, item="Water Lab", course="7: Science"):
    return differ.event(kind, "1", course, what, item=item)


def test_the_reported_digest_is_now_two_lines(conn):
    """The email as it was sent on 2026-11-12, rebuilt from the same two
    checks: the lab enters the look-ahead, then is zeroed and marked missing."""
    morning = differ.diff(
        _snap(Assignment(edupoint_gu="pv:1", course_gu="C", name="Water Lab",
                         status=S.NOT_DUE, points=20.0, due_date=DUE)),
        _snap(_pv(), _cv()), today=TODAY)
    evening = differ.diff(_snap(_pv(), _cv()),
                          _snap(_pv(S.MISSING, 0.0), _cv(S.MISSING, 0.0)), today=TODAY)
    drop = _ev(AlertType.GRADE_DROP, "overall DROPPED 22 points: 56.0% → 34.0%", item="")
    _queue(conn, *morning, *evening, drop)
    subject, text = _send(conn)
    assert "is marked missing (0/20)" in text and "DROPPED 22 points" in text
    for gone in ("due Thu", "graded: 0/20", "Canvas shows", "Check with the teacher"):
        assert gone not in text
    assert conn.execute("SELECT count(*) FROM outbox WHERE sent_at IS NULL").fetchone()[0] == 0


def test_a_coming_up_line_stays_when_nothing_later_settles_that_work(conn):
    _queue(conn, _ev(AlertType.UPCOMING_DEADLINE, "due Thu Nov 12"),
           _ev(AlertType.GRADE_CHANGED, "graded: 9/10", item="Other Quiz"),
           _ev(AlertType.GRADE_CHANGED, "graded: 9/10", course="3: Art"))
    _, text = _send(conn)
    assert "due Thu Nov 12" in text


def test_a_deadline_queued_after_the_grade_is_kept(conn):
    """A new due date on work already graded (a retake) is news, not stale."""
    _queue(conn, _ev(AlertType.GRADE_CHANGED, "graded: 9/10"),
           _ev(AlertType.UPCOMING_DEADLINE, "due Fri Nov 13"))
    _, text = _send(conn)
    assert "due Fri Nov 13" in text and "graded: 9/10" in text
