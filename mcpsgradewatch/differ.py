"""Snapshot diffing -> alert events.

Compares the latest collection against the previous one and emits typed events.
The differ is only as good as its key: it matches assignments on the stable
Edupoint GUID, never the display name (names change; corrections happen).

Phase 1 scope: score changes (assignment and course-level) and the flip to
MISSING. The time-based rules — ungraded-past-due, deadline look-ahead — land
in Phase 2 on top of the same Event shape.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .models import AlertType, Assignment, AssignmentStatus, Snapshot


@dataclass
class Event:
    type: AlertType
    student_agu: str
    course_title: str
    detail: str            # human-readable, low-PII (no full names)


def diff(previous: Optional[Snapshot], current: Snapshot) -> list[Event]:
    """Return the events implied by moving from ``previous`` to ``current``.

    ``previous is None`` means this student has never been persisted: the run
    establishes a baseline and stays quiet. New assignments *appearing* are
    also silent in Phase 1 — at year start that's a firehose of backfilled
    rows, and the deadline look-ahead (Phase 2) is the right voice for them.
    """
    if previous is None:
        return []
    events: list[Event] = []
    agu = current.student_agu

    # Course-level: overall mark/percent moved. Keyed on (GUID, term) so a new
    # grading period starts a fresh baseline instead of a spurious "change".
    prev_courses = {(c.edupoint_gu, c.term): c for c in previous.courses}
    for c in current.courses:
        p = prev_courses.get((c.edupoint_gu, c.term))
        if p is None:
            continue
        if (c.mark, c.percent) != (p.mark, p.percent):
            events.append(Event(
                type=AlertType.GRADE_CHANGED, student_agu=agu, course_title=c.title,
                detail=f"{c.title}: overall {_overall(p)} → {_overall(c)}",
            ))

    titles = {c.edupoint_gu: c.title for c in current.courses}
    prev_assignments = _by_gu(previous.assignments)
    for a in _by_gu(current.assignments).values():
        p = prev_assignments.get(a.edupoint_gu)
        if p is None:
            continue  # new assignment: quiet in Phase 1 (see docstring)
        course = titles.get(a.course_gu) or a.course_gu
        if (a.score, a.points) != (p.score, p.points):
            if p.score is None and a.score is not None:
                detail = f"{course}: “{a.name}” graded: {_score(a)}"
            else:
                detail = (f"{course}: “{a.name}” score changed "
                          f"{_score(p)} → {_score(a)}")
            events.append(Event(
                type=AlertType.GRADE_CHANGED, student_agu=agu,
                course_title=course, detail=detail,
            ))
        if a.status is AssignmentStatus.MISSING and p.status is not AssignmentStatus.MISSING:
            events.append(Event(
                type=AlertType.ASSIGNMENT_MISSING, student_agu=agu, course_title=course,
                detail=f"{course}: “{a.name}” is marked missing",
            ))
    return events


def _by_gu(assignments: list[Assignment]) -> dict[str, Assignment]:
    # Keyless rows can't be matched across runs — leave them to the store's
    # name-keyed persistence and out of the diff.
    return {a.edupoint_gu: a for a in assignments if a.edupoint_gu}


def _score(a: Assignment) -> str:
    if a.score is None:
        return "ungraded"
    return f"{a.score:g}/{a.points:g}" if a.points is not None else f"{a.score:g}"


def _overall(c) -> str:
    if c.mark and c.percent:
        return f"{c.percent} ({c.mark})"
    return c.percent or c.mark or "n/a"
