"""Stray course rows: a stripped-down or wrong-student gradebook page must
not leave a course behind, and one already left must clear itself.

Seen 2026-11: the portal answered one child's request with a sibling's
page, and a page with no class in focus; each left a "<teacher>'s class"
row with no work, one with an empty ID that stuck as the active filter.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lastbell import collector, dashboard, store
from lastbell.client import ParentVueClient, ParentVueError
from lastbell.gradebook import ClassDetails, SchoolClasses, SubjectRow
from lastbell.models import Assignment, AssignmentStatus, Course, Snapshot, Student

STUDENT = Student(agu="1", name="Jules P. Harlow", school="Example HS",
                  initials="J.P.H.")


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.ensure_schema(c)
    yield c
    c.close()


# ── the page names another student ────────────────────────────────────


def _client_serving(page: str) -> ParentVueClient:
    client = ParentVueClient("https://x.example", "u", "p")
    client.login = lambda: None
    client.session.get = lambda url, timeout=30: SimpleNamespace(
        text=page, url=url, raise_for_status=lambda: None)
    return client


_FOCUS = 'PXP.GBCurrentFocus = {"FocusArgs": {"classID": 5}};'


def test_a_page_for_another_student_is_an_error_not_data():
    client = _client_serving(f"<script>{_FOCUS} PXP.AGU = 0;</script>")
    with pytest.raises(ParentVueError, match="asked for student 1"):
        client.get_focus_args("1")


def test_the_page_for_the_asked_student_reads_as_before():
    client = _client_serving(f"<script>{_FOCUS} PXP.AGU = 1;</script>")
    focus = client.get_focus_args("1")
    assert focus.agu_header == "1" and focus.args == {"classID": 5}


def test_a_page_that_names_no_student_is_trusted_as_before():
    client = _client_serving(f"<script>{_FOCUS}</script>")
    assert client.get_focus_args("1").agu_header == "1"


# ── no class in focus ─────────────────────────────────────────────────


class _NoLinksPortal:
    """A class list with no per-class links: the default-class fallback."""

    def __init__(self, focus_args):
        self.focus_args = focus_args
        self.details_read = 0

    def get_focus_args(self, agu):
        return SimpleNamespace(args=self.focus_args, agu_header=agu,
                               as_parameters=lambda: dict(self.focus_args))

    def load_control(self, control, params, agu_header="0"):
        return "<html/>"

    def class_details(self, params, agu_header="0"):
        self.details_read += 1
        return "<html/>"


@pytest.fixture
def no_links(monkeypatch):
    sc = SchoolClasses(school_name="Example ES", school_id="1", org_year_gu="o",
                       current_term="MP2",
                       rows=[SubjectRow(teacher="Pat Example", teacher_id="9")])
    monkeypatch.setattr(collector, "parse_school_classes", lambda html: sc)
    monkeypatch.setattr(collector, "parse_class_details",
                        lambda html, course_gu: ClassDetails(mark="", percent="", missing_text=""))


def test_a_page_with_no_class_in_focus_saves_no_course(no_links):
    portal = _NoLinksPortal({"teacherID": "9"})
    col = collector.collect_student(portal, SimpleNamespace(
        agu="1", name="Jules P. Harlow", school=""), delay_s=0, read_marks=False)
    assert col.classes == []
    assert col.errors == ["the gradebook page has no class in focus"]
    assert portal.details_read == 0


def test_the_default_class_with_an_id_is_still_read(no_links):
    portal = _NoLinksPortal({"teacherID": "9", "classID": 77})
    col = collector.collect_student(portal, SimpleNamespace(
        agu="1", name="Jules P. Harlow", school=""), delay_s=0, read_marks=False)
    assert [(c.course.edupoint_gu, c.course.title) for c in col.classes] == [
        ("77", "Pat Example's class")]


# ── a stray row already saved clears itself ───────────────────────────


def _course(gu, title, term="MP2"):
    return Course(edupoint_gu=gu, title=title, term=term)


def _titles(conn):
    return sorted((r["title"], r["term"]) for r in conn.execute(
        "SELECT title, term FROM courses"))


def test_a_clean_read_drops_unseen_empty_courses_of_its_term(conn):
    store.persist_snapshot(conn, STUDENT, Snapshot(
        student_agu="1", term="MP1", courses=[_course("g9", "Old Art", "MP1")]))
    store.persist_snapshot(conn, STUDENT, Snapshot(
        student_agu="1", term="MP2",
        courses=[_course("g1", "Algebra"), _course("", "Pat Example's class"),
                 _course("h1", "Pat Example's class"), _course("g2", "Biology")],
        assignments=[Assignment(edupoint_gu="a1", course_gu="g2", name="Lab",
                                status=AssignmentStatus.MISSING)]))
    real = Snapshot(student_agu="1", term="MP2", courses=[_course("g1", "Algebra")])

    store.persist_snapshot(conn, STUDENT, real)          # not a clean read
    assert len(_titles(conn)) == 5

    store.persist_snapshot(conn, STUDENT, real, prune_gradebook=True)
    # the two strays go; a course with work and a past term's course stay
    assert _titles(conn) == [("Algebra", "MP2"), ("Biology", "MP2"),
                             ("Old Art", "MP1")]


def test_a_read_with_no_classes_prunes_nothing(conn):
    store.persist_snapshot(conn, STUDENT, Snapshot(
        student_agu="1", term="MP2", courses=[_course("g1", "Algebra")]))
    store.persist_snapshot(conn, STUDENT, Snapshot(student_agu="1", term="MP2"),
                           prune_gradebook=True)
    assert _titles(conn) == [("Algebra", "MP2")]


# ── an ID-less row never reads as the active filter ───────────────────


def test_an_id_less_row_is_not_the_active_filter(conn):
    store.persist_snapshot(conn, STUDENT, Snapshot(
        student_agu="1", term="MP2",
        courses=[_course("g1", "Algebra"), _course("", "Pat Example's class")]))
    _, html = dashboard._handle(conn, "/student/1")
    assert "class='scoped'" not in html
    assert "striptag" not in html
    assert "<details class='allcourses' id='allcourses'><summary>" in html
