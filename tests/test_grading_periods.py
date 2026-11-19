"""Canvas grading periods (0.3.14). A Canvas class runs all semester while
the gradebook's course rows are per marking period, so last period's
missing Canvas work landed on this period's course rows and stayed on the
student page and in its missing count. Only the current period's Canvas
work is kept now, and copies an earlier poll attached to this period's
course rows are dropped. The periods below are MCPS's for 2026–27, as
Canvas reported them in November 2026."""
from __future__ import annotations

import datetime as dt

import pytest

from lastbell import canvas, store
from lastbell.dashboard import queries
from lastbell.models import (
    SOURCE_CANVAS,
    Assignment,
    AssignmentStatus,
    Course,
    Snapshot,
    Student,
)

UTC = dt.timezone.utc
PERIODS_JSON = {"grading_periods": [
    {"title": "MP 1", "start_date": "2026-08-25T04:00:00Z", "end_date": "2026-10-31T03:59:59Z"},
    {"title": "MP 2", "start_date": "2026-11-04T05:00:00Z", "end_date": "2027-01-23T04:59:59Z"},
    {"title": "No Due Date", "start_date": "2027-01-27T05:00:00Z",
     "end_date": "2027-01-28T04:59:59Z"},
]}
NOW = dt.datetime(2026, 11, 19, 15, 0, tzinfo=UTC)
OBSERVEE = 9


def _periods():
    class Stub:
        def get(self, path, **params):
            return PERIODS_JSON
    return canvas.fetch_grading_periods(Stub(), 1)


def _at(iso):
    return canvas._parse_ts(iso)


# ── which period a date belongs to ────────────────────────────────────


def test_a_due_date_belongs_to_the_first_period_not_yet_ended():
    p = _periods()
    assert [x.title for x in p] == ["MP 1", "MP 2", "No Due Date"]
    assert canvas.period_of(p, _at("2026-09-29T03:59:59Z")).title == "MP 1"
    assert canvas.period_of(p, _at("2026-10-31T03:59:59Z")).title == "MP 1"   # last second
    assert canvas.period_of(p, _at("2026-11-05T04:59:59Z")).title == "MP 2"
    assert canvas.period_of(p, NOW).title == "MP 2"


def test_the_gap_between_periods_counts_toward_the_next_one():
    """The gradebook's MP2 starts 11/1; Canvas's MP 2 starts 11/4."""
    p = _periods()
    assert canvas.period_of(p, _at("2026-11-02T03:59:59Z")).title == "MP 2"


def test_undated_work_and_work_past_the_last_period_go_to_the_last_period():
    p = _periods()
    assert canvas.period_of(p, None).title == "No Due Date"
    assert canvas.period_of(p, _at("2027-06-01T00:00:00Z")).title == "No Due Date"


def test_no_periods_or_an_unreadable_answer_means_no_filtering():
    class Refuses:
        def get(self, path, **params):
            raise canvas.CanvasError("Canvas answered HTTP 403")

    class Odd:
        def get(self, path, **params):
            return []

    assert canvas.fetch_grading_periods(Refuses(), 1) == []
    assert canvas.fetch_grading_periods(Odd(), 1) == []
    assert canvas.period_of([], NOW) is None


# ── collect keeps the current period's work ───────────────────────────


def _row(id, name, due_at, *, missing=False, score=None, state="unsubmitted"):
    return {"id": id, "name": name, "due_at": due_at, "points_possible": 10,
            "published": True, "assignment_group_id": 1,
            "submission": [{"user_id": OBSERVEE, "missing": missing, "score": score,
                            "workflow_state": state}]}


ROWS = [
    _row(1, "Character Sketch", "2026-09-29T03:59:59Z", missing=True, score=0, state="graded"),
    _row(2, "Showcase Reflection", "2026-10-28T03:59:59Z", missing=True, score=0,
         state="graded"),
    _row(3, "Undated Analysis Chart", None, missing=True, score=0, state="graded"),
    _row(4, "Q2 Pre Assessment", "2026-11-05T04:59:59Z", missing=True, score=0,
         state="graded"),
    _row(5, "Gap Week Warm-Up", "2026-11-02T03:59:59Z"),
    _row(6, "Strike Night", "2026-11-24T04:59:59Z"),
]


def _client(periods=PERIODS_JSON):
    class Stub:
        def get(self, path, **params):
            if path.endswith("/assignment_groups"):
                return [{"id": 1, "name": "All Tasks"}]
            if path.endswith("/grading_periods"):
                return periods
            return [dict(r) for r in ROWS]
    return Stub()


COURSE = {"id": 77, "name": "Theatre HS 2A-Quill-S1-2027", "term": {"name": "S1"},
          "enrollments": [{"associated_user_id": OBSERVEE}]}


def test_collect_keeps_only_the_current_periods_work():
    col = canvas.collect(_client(), canvas.Observee(id=OBSERVEE, name="x"),
                         courses_cache=[COURSE], now=NOW)
    course = col.courses[0]
    assert [a.name for a in course.assignments] == [
        "Q2 Pre Assessment", "Gap Week Warm-Up", "Strike Night"]
    assert course.set_aside == ["canvas:1", "canvas:2", "canvas:3"]


def test_a_class_without_grading_periods_keeps_everything():
    col = canvas.collect(_client(periods={"grading_periods": []}),
                         canvas.Observee(id=OBSERVEE, name="x"),
                         courses_cache=[COURSE], now=NOW)
    assert len(col.courses[0].assignments) == len(ROWS)
    assert col.courses[0].set_aside == []


def test_merge_carries_the_set_aside_keys_onto_the_snapshot():
    col = canvas.collect(_client(), canvas.Observee(id=OBSERVEE, name="x"),
                         courses_cache=[COURSE], now=NOW)
    snap = Snapshot(student_agu="7", term="MP2 Interim", courses=[
        Course(edupoint_gu="T1", title="Theatre HS 1A", teacher="Quill, R",
               term="MP2 Interim")])
    canvas.merge(snap, col)
    assert snap.canvas_set_aside == {"canvas:1", "canvas:2", "canvas:3"}
    assert {a.name for a in snap.assignments} == {
        "Q2 Pre Assessment", "Gap Week Warm-Up", "Strike Night"}


# ── the store clears what earlier polls left ──────────────────────────

STUDENT = Student(agu="7", name="Jules Harlow", school="Example HS", initials="J.H.")


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.ensure_schema(c)
    yield c
    c.close()


def _work(n, name, status=AssignmentStatus.MISSING, score=0.0, due=dt.date(2026, 9, 28)):
    return Assignment(edupoint_gu=f"canvas:{n}", course_gu="T1", name=name, status=status,
                      score=score, points=10.0, due_date=due, source=SOURCE_CANVAS)


def _snap(term, *work, set_aside=()):
    return Snapshot(student_agu="7", term=term,
                    courses=[Course(edupoint_gu="T1", title="Theatre HS 1A", term=term)],
                    assignments=list(work), canvas_set_aside=set(set_aside))


def _missing(conn, term):
    return queries.fetch_open_counts(conn, "7", term)["missing"]


def test_last_periods_missing_work_leaves_this_periods_views(conn):
    old = [_work(1, "Character Sketch"), _work(2, "Showcase Reflection"),
           _work(3, "Undated Analysis Chart", due=None)]
    # During MP1 the work is stored on MP1's course row.
    store.persist_snapshot(conn, STUDENT, _snap("MP1", *old), prune_canvas=True)
    # 0.3.13 after the rollover: the same work lands on MP2's course row too.
    store.persist_snapshot(conn, STUDENT, _snap("MP2 Interim", *old), prune_canvas=True)
    assert _missing(conn, "MP2 Interim") == 3
    # 0.3.14: Canvas files it under MP 1 / No Due Date, so it is set aside.
    store.persist_snapshot(
        conn, STUDENT,
        _snap("MP2 Interim", _work(4, "Q2 Pre Assessment", due=dt.date(2026, 11, 4)),
              set_aside={"canvas:1", "canvas:2", "canvas:3"}),
        prune_canvas=True)
    assert _missing(conn, "MP2 Interim") == 1
    # MP1's own copies are history and stay.
    assert _missing(conn, "MP1") == 3


def test_nothing_is_dropped_when_canvas_was_not_read(conn):
    store.persist_snapshot(conn, STUDENT, _snap("MP2 Interim", _work(1, "Character Sketch")),
                           prune_canvas=True)
    store.persist_snapshot(conn, STUDENT, _snap("MP2 Interim", set_aside={"canvas:1"}),
                           prune_canvas=False)
    assert _missing(conn, "MP2 Interim") == 1
