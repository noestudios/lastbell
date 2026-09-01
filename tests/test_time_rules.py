"""Phase 2: time-based rules as status derivations + their transition events."""
from __future__ import annotations

import datetime

from lastbell.differ import apply_time_rules, diff
from lastbell.models import AlertType, Assignment, AssignmentStatus, Course, Snapshot

TODAY = datetime.date(2026, 9, 15)


def _snap(assignments) -> Snapshot:
    return Snapshot(
        student_agu="1",
        courses=[Course(edupoint_gu="C1", title="Algebra 2", term="MP1")],
        assignments=assignments,
    )


def _assignment(due=None, **kw) -> Assignment:
    base = dict(edupoint_gu="A1", course_gu="C1", name="Essay",
                score=None, points=10.0, status=AssignmentStatus.DUE,
                due_date=due)
    base.update(kw)
    return Assignment(**base)


def _derive(a, **kw):
    apply_time_rules(_snap([a]), today=TODAY, **kw)
    return a.status


# ── apply_time_rules ──────────────────────────────────────────────────


def test_derivation_windows():
    day = datetime.timedelta(days=1)
    # past due beyond grace -> UNGRADED_PAST_DUE; within grace -> still DUE
    assert _derive(_assignment(due=TODAY - 4 * day)) is AssignmentStatus.UNGRADED_PAST_DUE
    assert _derive(_assignment(due=TODAY - 3 * day)) is AssignmentStatus.DUE
    # inside the look-ahead window -> DUE; beyond it -> NOT_DUE
    assert _derive(_assignment(due=TODAY + 7 * day)) is AssignmentStatus.DUE
    assert _derive(_assignment(due=TODAY + 8 * day)) is AssignmentStatus.NOT_DUE
    # knobs are honored
    assert _derive(_assignment(due=TODAY - 4 * day), grace_days=10) is AssignmentStatus.DUE
    assert _derive(_assignment(due=TODAY + 8 * day), lookahead_days=14) is AssignmentStatus.DUE


def test_derivation_never_second_guesses_the_portal():
    graded = _assignment(due=TODAY - datetime.timedelta(days=30),
                         score=8.0, status=AssignmentStatus.GRADED)
    missing = _assignment(due=TODAY - datetime.timedelta(days=30),
                          status=AssignmentStatus.MISSING)
    dateless = _assignment(due=None)
    assert _derive(graded) is AssignmentStatus.GRADED
    assert _derive(missing) is AssignmentStatus.MISSING
    assert _derive(dateless) is AssignmentStatus.DUE


# ── the transitions the derivations produce ───────────────────────────


def test_crossing_the_grace_line_alerts_once():
    due = TODAY - datetime.timedelta(days=4)
    prev = _snap([_assignment(due=due, status=AssignmentStatus.DUE)])
    curr = apply_time_rules(_snap([_assignment(due=due)]), today=TODAY)

    events = diff(prev, curr, today=TODAY)
    assert [e.type for e in events] == [AlertType.UNGRADED_PAST_DUE]
    assert "still ungraded" in events[0].detail
    assert "was due" in events[0].detail

    # next poll: previous already says UNGRADED_PAST_DUE -> quiet
    again = apply_time_rules(_snap([_assignment(due=due)]), today=TODAY)
    assert diff(curr, again, today=TODAY) == []


def test_entering_the_lookahead_window_alerts_once():
    due = TODAY + datetime.timedelta(days=6)
    prev = _snap([_assignment(due=due, status=AssignmentStatus.NOT_DUE)])
    curr = apply_time_rules(_snap([_assignment(due=due)]), today=TODAY)

    events = diff(prev, curr, today=TODAY)
    assert [e.type for e in events] == [AlertType.UPCOMING_DEADLINE]
    assert "due Mon Sep 21" in events[0].detail

    again = apply_time_rules(_snap([_assignment(due=due)]), today=TODAY)
    assert diff(curr, again, today=TODAY) == []


def test_new_assignment_already_inside_window_alerts():
    prev = _snap([])
    curr = apply_time_rules(
        _snap([_assignment(due=TODAY + datetime.timedelta(days=2))]), today=TODAY
    )
    events = diff(prev, curr, today=TODAY)
    assert [e.type for e in events] == [AlertType.UPCOMING_DEADLINE]


def test_new_assignment_outside_window_or_past_is_silent():
    prev = _snap([])
    far = apply_time_rules(
        _snap([_assignment(due=TODAY + datetime.timedelta(days=30))]), today=TODAY
    )
    assert diff(prev, far, today=TODAY) == []
    past = apply_time_rules(
        _snap([_assignment(edupoint_gu="A2", due=TODAY - datetime.timedelta(days=1))]),
        today=TODAY,
    )
    assert diff(prev, past, today=TODAY) == []


def test_baseline_still_quiet_with_time_rules():
    curr = apply_time_rules(
        _snap([_assignment(due=TODAY + datetime.timedelta(days=2))]), today=TODAY
    )
    assert diff(None, curr, today=TODAY) == []


def test_past_due_then_graded_speaks_as_grade_not_status():
    due = TODAY - datetime.timedelta(days=10)
    prev = _snap([_assignment(due=due, status=AssignmentStatus.UNGRADED_PAST_DUE)])
    curr = apply_time_rules(
        _snap([_assignment(due=due, score=9.0, status=AssignmentStatus.GRADED)]),
        today=TODAY,
    )
    events = diff(prev, curr, today=TODAY)
    assert [e.type for e in events] == [AlertType.GRADE_CHANGED]
    assert "graded: 9/10" in events[0].detail
