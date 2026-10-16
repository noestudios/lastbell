"""Standards-scored (elementary) work: the assignment list says "See
Standards" whether or not the teacher has marked it, so the marks are read
from each assignment's detail page. Fixtures are synthetic."""
from __future__ import annotations

import json
import pathlib
import sqlite3
from datetime import date
from types import SimpleNamespace

import pytest

from lastbell import differ, store
from lastbell.collector import CollectedClass, StudentCollection, _read_marks
from lastbell.gradebook import (
    ClassDetails,
    ParseError,
    SchoolClasses,
    assignment_focus,
    parse_assignment_marks,
    standards_scored,
)
from lastbell.models import (
    AlertType,
    Assignment,
    AssignmentStatus,
    Course,
    Snapshot,
    Student,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
TODAY = date(2026, 10, 16)


def _detail_page() -> str:
    return (FIXTURES / "assignmentdetails_standards.html").read_text(encoding="utf-8")


def _standards_row(gu: str, due: date, *, mark: str = "",
                   status=AssignmentStatus.DUE) -> Assignment:
    focus = {"LoadParams": {"ControlName": "Gradebook_AssignmentDetails"},
             "FocusArgs": {"assignmentID": int(gu), "classID": 500102}}
    name_cell = json.dumps({"value": f"Work {gu}",
                            "hrefAttributes": "data-focus=" + json.dumps(focus)})
    return Assignment(
        edupoint_gu=gu, course_gu="500102", name=f"Work {gu}", due_date=due,
        mark=mark, status=status,
        raw={"GBAssignment": name_cell,
             "GBScore": json.dumps({"value": "See Standards"})})


# ── parsing ───────────────────────────────────────────────────────────


def test_marks_are_read_once_each_in_order_and_blank_standards_skipped():
    assert parse_assignment_marks(_detail_page()) == "B, A"


def test_an_unmarked_page_has_no_mark_and_a_strange_page_is_an_error():
    blank = _detail_page().replace("<b>B</b>", "<b></b>").replace("<b>A</b>", "<b></b>")
    assert parse_assignment_marks(blank) == ""
    with pytest.raises(ParseError):
        parse_assignment_marks("<div>Assignment Details</div>")


def test_the_detail_link_comes_from_the_name_cell():
    a = _standards_row("22220001", TODAY)
    assert standards_scored(a)
    assert assignment_focus(a) == {"assignmentID": 22220001, "classID": 500102}
    assert assignment_focus(Assignment(edupoint_gu="1", course_gu="c", name="x")) == {}


# ── which marks are fetched ───────────────────────────────────────────


class _Client:
    def __init__(self, page: str | Exception):
        self.page, self.asked = page, []

    def assignment_details(self, args, agu_header="0"):
        self.asked.append(args["assignmentID"])
        if isinstance(self.page, Exception):
            raise self.page
        return self.page


def _collection(*assignments) -> StudentCollection:
    sc = SchoolClasses(school_name="Example ES", school_id="1", org_year_gu="o",
                       current_term="MP1")
    course = Course(edupoint_gu="500102", title="Mathematics", term="MP1")
    return StudentCollection(
        student=Student(agu="7", name="Jules P. Harlow", initials="J.P.H."),
        school_classes=sc,
        classes=[CollectedClass(course=course, details=ClassDetails(
            mark="", percent="", missing_text="", assignments=list(assignments)))])


def test_unmarked_and_recent_work_is_fetched_settled_marks_are_kept():
    recent_marked = _standards_row("1", date(2026, 10, 1))
    old_marked = _standards_row("2", date(2026, 9, 1))
    old_unmarked = _standards_row("3", date(2026, 9, 1))
    points_scored = Assignment(edupoint_gu="4", course_gu="500102", name="Quiz",
                               raw={"GBScore": json.dumps({"value": "3 out of 4"})})
    out = _collection(recent_marked, old_marked, old_unmarked, points_scored)
    client = _Client(_detail_page())

    _read_marks(client, out, {"1": "C", "2": "A"}, TODAY, delay_s=0)

    assert client.asked == [1, 3]                 # not 2 (settled), not 4 (points)
    assert recent_marked.mark == "B, A"           # re-read: the teacher revised it
    assert old_marked.mark == "A"                 # recorded mark kept, no fetch
    assert old_unmarked.mark == "B, A"
    assert all(a.status is AssignmentStatus.GRADED
               for a in (recent_marked, old_marked, old_unmarked))
    assert out.mark_errors == []


def test_a_page_that_fails_keeps_the_recorded_mark():
    from lastbell.client import ParentVueError

    a = _standards_row("1", date(2026, 10, 10))
    b = _standards_row("2", date(2026, 10, 10))
    out = _collection(a, b)
    _read_marks(_Client(ParentVueError("server error")), out, {"1": "B"}, TODAY, delay_s=0)
    assert (a.mark, a.status) == ("B", AssignmentStatus.GRADED)
    assert (b.mark, b.status) == ("", AssignmentStatus.DUE)   # still unmarked
    assert len(out.mark_errors) == 2


def test_missing_work_stays_missing_even_with_a_mark():
    a = _standards_row("1", date(2026, 10, 10), status=AssignmentStatus.MISSING)
    _read_marks(_Client(_detail_page()), _collection(a), {}, TODAY, delay_s=0)
    assert a.status is AssignmentStatus.MISSING


# ── alerts ────────────────────────────────────────────────────────────


def _snap(*assignments) -> Snapshot:
    return Snapshot(student_agu="7", term="MP1",
                    courses=[Course(edupoint_gu="500102", title="Mathematics", term="MP1")],
                    assignments=list(assignments))


def test_a_new_or_changed_mark_alerts_like_a_grade():
    due = date(2026, 10, 1)
    before = _snap(_standards_row("1", due), _standards_row("2", due, mark="B"),
                   _standards_row("3", due, mark="A"))
    after = _snap(_standards_row("1", due, mark="B"), _standards_row("2", due, mark="A"),
                  _standards_row("3", due))
    events = differ.diff(before, after, today=TODAY)
    assert [(e.type, e.what) for e in events] == [
        (AlertType.GRADE_CHANGED, "graded: B"),
        (AlertType.GRADE_CHANGED, "mark changed B → A"),
        # a mark the teacher cleared is not news
    ]
    assert differ.diff(before, after, today=TODAY, quiet_marks=True) == []


# ── storage ───────────────────────────────────────────────────────────


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.ensure_schema(c)
    yield c
    c.close()


def test_marks_are_stored_loaded_and_kept_in_history(conn):
    student = Student(agu="7", name="Jules P. Harlow", initials="J.P.H.")
    store.persist_snapshot(conn, student, _snap(_standards_row("1", TODAY)))
    store.persist_snapshot(conn, student, _snap(
        _standards_row("1", TODAY, mark="B", status=AssignmentStatus.GRADED)))
    assert store.load_snapshot(conn, "7").assignments[0].mark == "B"
    rows = conn.execute("SELECT field, old_value, new_value FROM grade_history "
                        "ORDER BY id").fetchall()
    assert ("mark", "", "B") in [tuple(r) for r in rows]


def test_upgrading_a_database_with_work_on_it_makes_the_first_read_quiet(tmp_path):
    path = tmp_path / "old.db"
    c = store.connect(path)
    store.ensure_schema(c)
    assert store.get_meta(c, store.MARKS_FIRST_READ_KEY) is None   # fresh: no flag
    store.persist_snapshot(c, Student(agu="7", name="J", initials="J."),
                           _snap(_standards_row("1", TODAY)))
    c.execute("ALTER TABLE assignments DROP COLUMN mark")          # as 0.3.5 left it
    c.commit()
    store.ensure_schema(c)
    assert store.get_meta(c, store.MARKS_FIRST_READ_KEY) == "1"
    c.close()


def test_the_first_poll_after_upgrade_records_marks_without_alerting(conn, monkeypatch):
    from lastbell import cli, collector

    store.set_meta(conn, store.MARKS_FIRST_READ_KEY, "1")
    student = Student(agu="7", name="Jules P. Harlow", initials="J.P.H.")
    due = date(2026, 10, 1)
    store.persist_snapshot(conn, student, _snap(_standards_row("1", due)))

    def collect(client, child, **kw):
        col = _collection(_standards_row("1", due, mark="B",
                                         status=AssignmentStatus.GRADED))
        col.student = student
        return col

    monkeypatch.setattr(collector, "collect_student", collect)
    sent = []
    notifier = SimpleNamespace(send=lambda subject, body: sent.append(subject))
    conf = SimpleNamespace(canvas="off", ungraded_grace_days=2, lookahead_days=3,
                           grade_drop_points=5.0, username="someparent")
    client = SimpleNamespace(get_children=lambda: [SimpleNamespace(agu="7")])

    assert cli._run_once(client, conn, notifier, conf) == 0
    assert sent == []
    assert store.get_meta(conn, store.MARKS_FIRST_READ_KEY) is None
    assert store.load_snapshot(conn, "7").assignments[0].mark == "B"


# ── dashboard ─────────────────────────────────────────────────────────


def _row(**values):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = ", ".join(f"? AS {k}" for k in values)
    return conn.execute(f"SELECT {cols}", tuple(values.values())).fetchone()


def test_a_mark_shows_where_a_score_would():
    from lastbell.dashboard.render import _score

    assert _score(_row(score=None, points=None, mark="A, B"), 0) == "A, B"
    assert _score(_row(score=None, points=None, mark=""), 0) == "—"
    assert _score(_row(score=None, points=None), 0) == "—"     # no mark column


def test_history_reads_a_first_mark_as_graded():
    from lastbell.dashboard.render import _compose_history_group

    group = [_row(id=2, field="mark", old_value="", new_value="B", seen_at="t"),
             _row(id=1, field="status", old_value="ungraded_past_due",
                  new_value="graded", seen_at="t")]
    items = _compose_history_group(group, None)
    assert [(i["label"], i["transition"]) for i in items] == [("graded", "— → B")]


def test_the_recent_card_tallies_marks_when_there_are_no_percents(conn):
    from lastbell import dashboard

    student = Student(agu="7", name="Jules P. Harlow", initials="J.P.H.")
    due = date(2026, 10, 1)
    rows = [_standards_row(str(i), due) for i in range(1, 4)]
    store.persist_snapshot(conn, student, _snap(*rows))
    for a, m in zip(rows, ("A", "B, A", "A")):
        a.mark, a.status = m, AssignmentStatus.GRADED
    store.persist_snapshot(conn, student, _snap(*rows))   # history: '' → mark

    status, html = dashboard._handle(conn, "/student/7")
    assert status == 200
    assert "last 3: 3 A, 1 B" in html
