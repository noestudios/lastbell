"""When the gradebook and Canvas disagree: the record stays the record, the
disagreement is said once (differ) and shown (dashboard)."""
from __future__ import annotations


import pytest

from lastbell import dashboard, store
from lastbell.differ import conflicts, diff
from lastbell.models import (
    SOURCE_CANVAS,
    AlertType,
    Assignment,
    AssignmentStatus,
    Course,
    Snapshot,
    Student,
)

COURSE = Course(edupoint_gu="C1", title="Biology", term="MP1")


def _pv(score, points=10.0, status=None) -> Assignment:
    return Assignment(edupoint_gu="G1", course_gu="C1", name="Cell Lab", score=score,
                      points=points,
                      status=status or (AssignmentStatus.GRADED if score is not None
                                        else AssignmentStatus.DUE))


def _cv(score, points=10.0) -> Assignment:
    return Assignment(edupoint_gu="canvas:1", course_gu="C1", name="Cell Lab", score=score,
                      points=points, source=SOURCE_CANVAS, superseded_by="G1",
                      status=AssignmentStatus.GRADED if score is not None else AssignmentStatus.DUE)


def _snap(*assignments) -> Snapshot:
    return Snapshot(student_agu="1", term="MP1", courses=[COURSE], assignments=list(assignments))


def test_what_counts_as_a_conflict():
    assert not conflicts(_pv(None), _cv(9))            # ungraded vs graded: just lag
    assert conflicts(_pv(0), _cv(9))                   # a zero against a real score
    assert conflicts(_pv(7), _cv(9))                   # different scores
    assert not conflicts(_pv(9), _cv(9))
    assert not conflicts(_pv(90, 100), _cv(9, 10))     # same percentage, different scale
    assert conflicts(_pv(None, status=AssignmentStatus.MISSING), _cv(9))
    assert not conflicts(_pv(0), _cv(None))            # Canvas has nothing to say


def test_conflict_alert_fires_once_and_the_twin_is_otherwise_silent():
    quiet = diff(_snap(_pv(None), _cv(None)), _snap(_pv(None), _cv(9)))
    assert quiet == []                                 # Canvas graded first: lag, no alert
    events = diff(_snap(_pv(None), _cv(9)), _snap(_pv(0), _cv(9)))
    # The gradebook did post a 0 (its own line) — and it disagrees with Canvas.
    assert {e.type for e in events} == {AlertType.SOURCE_CONFLICT, AlertType.GRADE_CHANGED}
    (conflict,) = [e for e in events if e.type is AlertType.SOURCE_CONFLICT]
    assert "gradebook shows 0/10 but Canvas shows 9/10" in conflict.detail
    assert diff(_snap(_pv(0), _cv(9)), _snap(_pv(0), _cv(9))) == []   # said once
    # The gradebook row's own change still speaks through the normal rule.
    later = diff(_snap(_pv(0), _cv(9)), _snap(_pv(9), _cv(9)))
    assert [e.type for e in later] == [AlertType.GRADE_CHANGED]


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.ensure_schema(c)
    yield c
    c.close()


def test_twin_round_trips_stays_hidden_and_hints_on_the_dashboard(conn):
    student = Student(agu="1", name="Jasper P. Hays", school="Example ES", initials="J.P.H.")
    store.persist_snapshot(conn, student, _snap(_pv(0), _cv(9)))
    loaded = store.load_snapshot(conn, "1")
    assert {a.edupoint_gu: a.superseded_by for a in loaded.assignments} == {"G1": "", "canvas:1": "G1"}

    rows = dashboard.fetch_view_rows(conn, "1", "MP1")
    assert [r["edupoint_gu"] for r in rows] == ["G1"]           # the twin is hidden …
    assert rows[0]["canvas_score"] == 9.0                        # … but its grade rides along
    status, html = dashboard._handle(conn, "/student/1?view=everything")
    assert "Canvas says 9/10" in html

    # Agreement shows nothing.
    store.persist_snapshot(conn, student, _snap(_pv(9), _cv(9)))
    status, html = dashboard._handle(conn, "/student/1?view=everything")
    assert "Canvas says" not in html
