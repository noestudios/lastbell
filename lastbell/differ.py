"""Snapshot diffing -> alert events.

Compares the latest collection against the previous one and emits typed events.
The differ is only as good as its key: it matches assignments on the stable
Edupoint GUID, never the display name (names change; corrections happen).

Time-based rules (Phase 2) are *status derivations*, not a separate engine:
``apply_time_rules`` reclassifies each unscored assignment from its due date
before the diff runs, and the persisted status remembers the result. So
"crossed the past-due grace line" or "entered the look-ahead window" is just
another status transition — alerted exactly once, deduped by the same
persisted state that Phase 1's score diffing already relies on.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from .models import (
    SOURCE_CANVAS,
    AlertType,
    Assignment,
    AssignmentStatus,
    Snapshot,
    format_percent,
    parse_percent,
)


@dataclass
class Event:
    type: AlertType
    student_agu: str
    course_title: str
    detail: str            # human-readable, low-PII (no full names)


def apply_time_rules(
    snapshot: Snapshot,
    *,
    today: Optional[date] = None,
    grace_days: int = 3,
    lookahead_days: int = 7,
) -> Snapshot:
    """Reclassify unscored assignments from their due dates (in place).

    Only DUE/NOT_DUE/UNGRADED_PAST_DUE shuffle among themselves — GRADED and
    MISSING are the portal's own words and are never second-guessed. An
    assignment without a due date can't be time-judged and stays DUE.
    """
    today = today or date.today()
    for a in snapshot.assignments:
        if a.status not in (
            AssignmentStatus.DUE,
            AssignmentStatus.NOT_DUE,
            AssignmentStatus.UNGRADED_PAST_DUE,
        ) or a.due_date is None:
            continue
        if a.due_date + timedelta(days=grace_days) < today:
            a.status = AssignmentStatus.UNGRADED_PAST_DUE
        elif a.due_date > today + timedelta(days=lookahead_days):
            a.status = AssignmentStatus.NOT_DUE
        else:
            a.status = AssignmentStatus.DUE
    return snapshot


def diff(
    previous: Optional[Snapshot], current: Snapshot, *,
    today: Optional[date] = None, grade_drop_points: float = 5.0,
) -> list[Event]:
    """Return the events implied by moving from ``previous`` to ``current``.

    ``previous is None`` means this student has never been persisted: the run
    establishes a baseline and stays quiet. A *new* assignment on a known
    student speaks only through the deadline look-ahead: it alerts if it's
    already inside the window (status DUE with a future due date), otherwise
    it waits silently to enter it — so year-start backfill stays quiet.
    """
    if previous is None:
        return []
    today = today or date.today()
    events: list[Event] = []
    agu = current.student_agu

    # Term rollover first: the old quarter's last-seen marks ARE its finals.
    roll = term_rollover(previous, current)
    if roll:
        events.append(roll)

    # Course-level: overall mark/percent moved. Keyed on (GUID, term) so a new
    # grading period starts a fresh baseline instead of a spurious "change".
    prev_courses = {(c.edupoint_gu, c.term): c for c in previous.courses}
    for c in current.courses:
        p = prev_courses.get((c.edupoint_gu, c.term))
        if p is None:
            continue
        if (c.mark, c.percent) != (p.mark, p.percent):
            # A drop past the threshold is the same change wearing a louder
            # type — one event either way, so a '*' subscriber isn't told twice.
            drop = _percent_drop(p, c)
            if drop is not None and drop >= grade_drop_points:
                events.append(Event(
                    type=AlertType.GRADE_DROP, student_agu=agu, course_title=c.title,
                    detail=(f"{c.title}: overall DROPPED {drop:g} points: "
                            f"{_overall(p)} → {_overall(c)}"),
                ))
            else:
                events.append(Event(
                    type=AlertType.GRADE_CHANGED, student_agu=agu, course_title=c.title,
                    detail=f"{c.title}: overall {_overall(p)} → {_overall(c)}",
                ))

    titles = {c.edupoint_gu: c.title for c in current.courses}
    prev_assignments = _by_gu(previous.assignments)
    current_by_gu = _by_gu(current.assignments)
    for a in current_by_gu.values():
        p = prev_assignments.get(a.edupoint_gu)
        course = titles.get(a.course_gu) or a.course_gu
        if a.superseded_by:
            # A hidden Canvas twin speaks only through disagreement with its
            # gradebook row — once, when the disagreement first appears.
            twin = current_by_gu.get(a.superseded_by)
            if twin is not None and conflicts(twin, a) and not (
                    p is not None and conflicts(
                        prev_assignments.get(a.superseded_by) or twin, p)):
                events.append(Event(
                    type=AlertType.SOURCE_CONFLICT, student_agu=agu, course_title=course,
                    detail=(f"{course}: “{twin.name}” gradebook shows {_score(twin)} "
                            f"but Canvas shows {_score(a)} — likely not synced yet; "
                            f"worth checking with the teacher"),
                ))
            continue
        # Which app to open: a Canvas-sourced line says so, since the same
        # work won't be in the gradebook yet.
        via = " [Canvas]" if a.source == SOURCE_CANVAS else ""
        if p is None:
            if (a.status is AssignmentStatus.DUE and a.due_date
                    and a.due_date >= today):
                events.append(Event(
                    type=AlertType.UPCOMING_DEADLINE, student_agu=agu,
                    course_title=course,
                    detail=f"{course}: “{a.name}” due {_day(a.due_date)}{via}",
                ))
            continue
        if (a.score, a.points) != (p.score, p.points):
            if p.score is None and a.score is not None:
                detail = f"{course}: “{a.name}” graded: {_score(a)}{via}"
            else:
                detail = (f"{course}: “{a.name}” score changed "
                          f"{_score(p)} → {_score(a)}{via}")
            events.append(Event(
                type=AlertType.GRADE_CHANGED, student_agu=agu,
                course_title=course, detail=detail,
            ))
        if a.status is AssignmentStatus.MISSING and p.status is not AssignmentStatus.MISSING:
            events.append(Event(
                type=AlertType.ASSIGNMENT_MISSING, student_agu=agu, course_title=course,
                detail=f"{course}: “{a.name}” is marked missing{via}",
            ))
        if (a.status is AssignmentStatus.UNGRADED_PAST_DUE
                and p.status is not AssignmentStatus.UNGRADED_PAST_DUE):
            due = f" (was due {_day(a.due_date)})" if a.due_date else ""
            what = "still ungraded"
            if a.source == SOURCE_CANVAS:
                from .canvas import submits_online  # lazy: canvas imports models only
                if submits_online(a):
                    what = "still not turned in"
            events.append(Event(
                type=AlertType.UNGRADED_PAST_DUE, student_agu=agu, course_title=course,
                detail=f"{course}: “{a.name}”{due} is {what}{via}",
            ))
        if (a.status is AssignmentStatus.DUE
                and p.status is AssignmentStatus.NOT_DUE and a.due_date):
            events.append(Event(
                type=AlertType.UPCOMING_DEADLINE, student_agu=agu, course_title=course,
                detail=f"{course}: “{a.name}” due {_day(a.due_date)}{via}",
            ))
    return events


def conflicts(record: Assignment, twin: Assignment) -> bool:
    """Does Canvas (``twin``) disagree with the gradebook (``record``) in a
    way worth a word? Canvas has a score, and the gradebook has a different
    one, a zero, or a missing flag. A gradebook row that is merely ungraded
    while Canvas has a score is ordinary sync lag — the dashboard hints at
    it, nobody is alerted."""
    if twin.score is None:
        return False
    if record.status is AssignmentStatus.MISSING:
        return True
    if record.score is None:
        return False
    if record.score == 0 and twin.score > 0:
        return True
    return abs(_pct(record) - _pct(twin)) > 0.5


def _pct(a: Assignment) -> float:
    if a.points:
        return (a.score or 0.0) / a.points * 100
    return float(a.score or 0.0)


def term_rollover(previous: Optional[Snapshot], current: Snapshot) -> Optional[Event]:
    """A one-shot final-grades summary when the marking period changes.

    The persisted term is the dedup: it only differs from the collected one on
    the first pass after the portal flips (persisting the new snapshot updates
    it), so the event fires exactly once — the same status-transition pattern
    as every other rule. The previous snapshot's courses for the closing term
    are, by definition, that term's final grades.
    """
    if previous is None or not previous.term or not current.term:
        return None
    if previous.term == current.term:
        return None
    # Canvas-only courses carry no course grade (the gradebook is the record).
    finals = sorted((c for c in previous.courses
                     if c.term == previous.term and c.source != SOURCE_CANVAS),
                    key=lambda c: c.title)
    listing = "; ".join(f"{c.title} {_overall(c)}" for c in finals) \
        or "no courses recorded"
    return Event(
        type=AlertType.TERM_FINAL, student_agu=current.student_agu,
        course_title="",
        detail=f"{previous.term} closed — final grades: {listing} "
               f"(now in {current.term})",
    )


def _day(d: date) -> str:
    return f"{d:%a %b} {d.day}"


def _by_gu(assignments: list[Assignment]) -> dict[str, Assignment]:
    # Keyless rows can't be matched across runs — leave them to the store's
    # name-keyed persistence and out of the diff.
    return {a.edupoint_gu: a for a in assignments if a.edupoint_gu}


def _score(a: Assignment) -> str:
    if a.score is None:
        return "ungraded"
    return f"{a.score:g}/{a.points:g}" if a.points is not None else f"{a.score:g}"


def _overall(c) -> str:
    pct = format_percent(c.percent)
    shown = f"{pct}%" if pct is not None else c.percent
    if shown and c.mark:
        return f"{shown} ({c.mark})"
    return shown or c.mark or "n/a"


def _percent_drop(prev, cur) -> Optional[float]:
    p, c = parse_percent(prev.percent), parse_percent(cur.percent)
    if p is None or c is None or c >= p:
        return None
    return p - c
