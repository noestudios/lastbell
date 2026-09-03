"""Differ tests: events are keyed on the Edupoint GUID, baselines stay quiet."""
from __future__ import annotations

from lastbell.differ import diff
from lastbell.models import (
    AlertType,
    Assignment,
    AssignmentStatus,
    Course,
    Snapshot,
)


def _snap(courses=None, assignments=None) -> Snapshot:
    return Snapshot(student_agu="1", courses=courses or [], assignments=assignments or [])


def _course(**kw) -> Course:
    base = dict(edupoint_gu="C1", title="Algebra 2", term="MP1", mark="B+", percent="87.20%")
    base.update(kw)
    return Course(**base)


def _assignment(**kw) -> Assignment:
    base = dict(edupoint_gu="A1", course_gu="C1", name="Quiz 3",
                score=8.0, points=10.0, status=AssignmentStatus.GRADED)
    base.update(kw)
    return Assignment(**base)


def test_first_run_is_a_quiet_baseline():
    assert diff(None, _snap(assignments=[_assignment()])) == []


def test_no_change_no_events():
    prev = _snap(courses=[_course()], assignments=[_assignment()])
    curr = _snap(courses=[_course()], assignments=[_assignment()])
    assert diff(prev, curr) == []


def test_score_change_emits_grade_changed():
    prev = _snap(assignments=[_assignment(score=8.0)])
    curr = _snap(courses=[_course()], assignments=[_assignment(score=9.0)])
    events = diff(prev, curr)
    assert len(events) == 1
    assert events[0].type is AlertType.GRADE_CHANGED
    assert "Quiz 3" in events[0].detail
    assert "8/10 → 9/10" in events[0].detail
    assert events[0].course_title == "Algebra 2"  # resolved via course_gu


def test_newly_graded_reads_as_graded_not_changed():
    prev = _snap(assignments=[_assignment(score=None, status=AssignmentStatus.DUE)])
    curr = _snap(assignments=[_assignment(score=8.0)])
    events = diff(prev, curr)
    assert len(events) == 1
    assert "graded: 8/10" in events[0].detail


def test_flip_to_missing_emits_assignment_missing():
    prev = _snap(assignments=[_assignment(score=None, status=AssignmentStatus.DUE)])
    curr = _snap(assignments=[_assignment(score=None, status=AssignmentStatus.MISSING)])
    events = diff(prev, curr)
    assert [e.type for e in events] == [AlertType.ASSIGNMENT_MISSING]


def test_matching_is_by_guid_not_name():
    prev = _snap(assignments=[_assignment(name="Quiz 3 (old title)")])
    curr = _snap(assignments=[_assignment(name="Quiz 3 — corrected")])
    assert diff(prev, curr) == []  # rename alone is not an event


def test_new_assignment_is_silent_in_phase_1():
    prev = _snap(assignments=[_assignment()])
    curr = _snap(assignments=[_assignment(), _assignment(edupoint_gu="A2", name="HW 5")])
    assert diff(prev, curr) == []


def test_course_mark_change():
    prev = _snap(courses=[_course(mark="B+", percent="87.20%")])
    curr = _snap(courses=[_course(mark="A-", percent="90.10%")])
    events = diff(prev, curr)
    assert len(events) == 1
    assert events[0].type is AlertType.GRADE_CHANGED
    assert "87.2% (B+) → 90.1% (A-)" in events[0].detail


def test_new_term_is_a_fresh_course_baseline():
    prev = _snap(courses=[_course(term="MP1", percent="87.20%")])
    curr = _snap(courses=[_course(term="MP2", percent="0.00%")])
    assert diff(prev, curr) == []


def test_keyless_assignments_are_ignored():
    prev = _snap(assignments=[_assignment(edupoint_gu="", score=5.0)])
    curr = _snap(assignments=[_assignment(edupoint_gu="", score=9.0)])
    assert diff(prev, curr) == []


def test_canvas_lines_say_which_app_and_finals_skip_canvas_only_courses():
    from lastbell.models import SOURCE_CANVAS

    prev = _snap(courses=[_course(term="MP1"), _course(edupoint_gu="canvas:1", title="Art",
                                                        term="MP1", source=SOURCE_CANVAS)],
                 assignments=[_assignment(edupoint_gu="canvas:5", score=None,
                                          status=AssignmentStatus.DUE, source=SOURCE_CANVAS)])
    curr = _snap(courses=[_course(term="MP1")],
                 assignments=[_assignment(edupoint_gu="canvas:5", score=9.0,
                                          status=AssignmentStatus.GRADED, source=SOURCE_CANVAS)])
    (graded,) = diff(prev, curr)
    assert graded.type is AlertType.GRADE_CHANGED
    assert graded.detail.endswith("graded: 9/10 [Canvas]")

    prev.term, curr.term = "MP1", "MP2"
    roll = [e for e in diff(prev, curr) if e.type is AlertType.TERM_FINAL]
    assert len(roll) == 1
    assert "Art" not in roll[0].detail and "Algebra 2" in roll[0].detail
