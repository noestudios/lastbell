"""Term rollover: the one-shot final-grades summary, term persistence, and
term-aware dashboard/summary scoping."""
from __future__ import annotations

import datetime

import pytest

from mcpsgradewatch import dashboard, differ, store, summary
from mcpsgradewatch.models import (
    AlertType,
    Assignment,
    AssignmentStatus,
    Course,
    Snapshot,
    Student,
)

STUDENT = Student(agu="1", name="Jasper P. Hays", school="Example ES", initials="J.P.H.")


def _course(gu="c1", title="Algebra 1A", term="MP1", mark="A", percent="93.00%"):
    return Course(edupoint_gu=gu, title=title, term=term, mark=mark, percent=percent)


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "test.db")
    store.ensure_schema(c)
    yield c
    c.close()


# ── the differ event ──────────────────────────────────────────────────


def test_rollover_emits_one_final_grades_event():
    prev = Snapshot(student_agu="1", term="MP1", courses=[
        _course("c2", "Spanish 2A", mark="A-", percent="87.20%"),
        _course("c1", "Algebra 1A", mark="99", percent=""),
    ])
    cur = Snapshot(student_agu="1", term="MP2",
                   courses=[_course("c1", "Algebra 1A", term="MP2", mark="", percent="")])
    events = differ.diff(prev, cur)
    finals = [e for e in events if e.type is AlertType.TERM_FINAL]
    assert len(finals) == 1
    d = finals[0].detail
    assert d.startswith("MP1 closed — final grades: ")
    assert "Algebra 1A 99" in d
    assert "Spanish 2A 87.2% (A-)" in d      # display formatting applies
    assert d.endswith("(now in MP2)")
    # keyed on (gu, term): the new quarter is a fresh baseline, no change spam
    assert [e.type for e in events] == [AlertType.TERM_FINAL]


def test_same_term_and_baseline_stay_quiet():
    a = Snapshot(student_agu="1", term="MP1", courses=[_course()])
    b = Snapshot(student_agu="1", term="MP1", courses=[_course()])
    assert differ.diff(a, b) == []
    assert differ.diff(None, Snapshot(student_agu="1", term="MP1")) == []


def test_unknown_terms_never_roll():
    # A failed term parse (empty string) must not fabricate a rollover.
    assert differ.term_rollover(Snapshot(student_agu="1", term=""),
                                Snapshot(student_agu="1", term="MP2")) is None
    assert differ.term_rollover(Snapshot(student_agu="1", term="MP1"),
                                Snapshot(student_agu="1", term="")) is None


def test_rollover_with_no_prior_courses_says_so():
    e = differ.term_rollover(Snapshot(student_agu="1", term="MP1"),
                             Snapshot(student_agu="1", term="MP2"))
    assert "no courses recorded" in e.detail


# ── persistence: fires exactly once ───────────────────────────────────


def test_rollover_fires_exactly_once_across_polls(conn):
    mp1 = Snapshot(student_agu="1", term="MP1", courses=[_course()])
    store.persist_snapshot(conn, STUDENT, mp1)
    assert store.load_snapshot(conn, "1").term == "MP1"

    mp2 = Snapshot(student_agu="1", term="MP2",
                   courses=[_course(term="MP2", mark="", percent="")])
    # first pass after the flip: event
    events = differ.diff(store.load_snapshot(conn, "1"), mp2)
    assert [e.type for e in events] == [AlertType.TERM_FINAL]
    store.persist_snapshot(conn, STUDENT, mp2)
    # second pass: the persisted term now matches — quiet
    assert differ.diff(store.load_snapshot(conn, "1"), mp2) == []


def test_empty_term_preserves_last_known(conn):
    store.persist_snapshot(conn, STUDENT, Snapshot(student_agu="1", term="MP1"))
    store.persist_snapshot(conn, STUDENT, Snapshot(student_agu="1", term=""))
    assert store.load_snapshot(conn, "1").term == "MP1"


# ── dashboard + summary scoping ───────────────────────────────────────


@pytest.fixture
def rolled(conn):
    """MP1 with a lingering 'due' assignment, then MP2 as current."""
    mp1 = Snapshot(student_agu="1", term="MP1",
                   courses=[_course("c1", "Algebra 1A", "MP1", "99", "")],
                   assignments=[Assignment(
                       edupoint_gu="a1", course_gu="c1", name="Old Worksheet",
                       due_date=datetime.date(2026, 10, 30),
                       status=AssignmentStatus.DUE)])
    store.persist_snapshot(conn, STUDENT, mp1)
    mp2 = Snapshot(student_agu="1", term="MP2",
                   courses=[_course("c1", "Algebra 1A", "MP2", "", "")],
                   assignments=[Assignment(
                       edupoint_gu="a2", course_gu="c1", name="New Quiz",
                       due_date=datetime.date(2026, 11, 6),
                       status=AssignmentStatus.DUE)])
    store.persist_snapshot(conn, STUDENT, mp2)
    return conn


def test_overview_shows_only_current_term(rolled):
    status, html = dashboard._handle(rolled, "/")
    assert status == 200
    assert html.count("Algebra 1A") == 1           # one card row, not two terms'
    assert "1 due soon" in html                    # MP1's stale 'due' not counted


def test_student_page_groups_terms_current_first(rolled):
    status, html = dashboard._handle(rolled, "/student/1")
    assert status == 200
    assert "MP2 — current" in html and "MP1" in html
    assert html.index("MP2 — current") < html.index(">MP1<")
    assert "New Quiz" in html and "Old Worksheet" in html


def test_single_term_page_shows_no_term_headings(conn):
    store.persist_snapshot(conn, STUDENT, Snapshot(
        student_agu="1", term="MP1", courses=[_course()]))
    _, html = dashboard._handle(conn, "/student/1")
    assert "— current" not in html


def test_summary_scopes_to_current_term(rolled):
    body = summary.build(rolled, "1", "J.P.H.", today=datetime.date(2026, 11, 2))
    assert "New Quiz" in body
    assert "Old Worksheet" not in body
    assert body.count("Algebra 1A") >= 1