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

from .models import (
    SOURCE_CANVAS,
    AlertType,
    Assignment,
    AssignmentStatus,
    Snapshot,
    course_grade,
)


@dataclass
class Event:
    type: AlertType
    student_agu: str
    course_title: str
    detail: str            # the whole sentence — what plain-text channels carry
    # The sentence's parts, so HTML renderers lay it out without parsing it
    # back apart. Empty on an event built from the sentence alone.
    item: str = ""         # assignment name; "" for course-level and term events
    what: str = ""         # what happened: "is marked missing", "graded: 9/10", …
    via: str = ""          # "Canvas" when the row came from Canvas

    def as_dict(self) -> dict:
        """The stored/queued form: the parts and the sentence."""
        return {"course": self.course_title, "item": self.item, "what": self.what,
                "via": self.via, "detail": self.detail}


# ── the vocabulary, shared with the daily summary ─────────────────────

MISSING_PHRASE = "is marked missing"


def due_phrase(day: str) -> str:
    return f"due {day}"


def past_due_phrase(day: str | None, what: str = "still ungraded") -> str:
    return (f"(was due {day}) " if day else "") + f"is {what}"


def compose(course: str, what: str, item: str = "", via: str = "") -> str:
    """The one sentence shape every alert line has: ``Course: “Item” what
    [Canvas]``, with the course or the item absent where there is none."""
    head = f"{course}: " if course else ""
    body = f"“{item}” {what}".rstrip() if item else what
    tail = f" [{via}]" if via else ""
    return head + body + tail


def event(type_: AlertType, student_agu: str, course: str, what: str, *,
          item: str = "", via: str = "") -> Event:
    return Event(type=type_, student_agu=student_agu, course_title=course,
                 detail=compose(course, what, item, via), item=item, what=what, via=via)


def apply_time_rules(
    snapshot: Snapshot,
    *,
    today: date | None = None,
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
    previous: Snapshot | None, current: Snapshot, *,
    today: date | None = None, grade_drop_points: float = 5.0,
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
                events.append(event(
                    AlertType.GRADE_DROP, agu, c.title,
                    f"overall DROPPED {drop:g} points: {_overall(p)} → {_overall(c)}"))
            else:
                events.append(event(
                    AlertType.GRADE_CHANGED, agu, c.title,
                    f"overall {_overall(p)} → {_overall(c)}"))

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
                events.append(event(
                    AlertType.SOURCE_CONFLICT, agu, course,
                    f"gradebook shows {_score(twin)} but Canvas shows {_score(a)} "
                    f"— likely not synced yet; worth checking with the teacher",
                    item=twin.name))
            continue
        # Which app to open: a Canvas-sourced line says so, since the same
        # work won't be in the gradebook yet.
        via = "Canvas" if a.source == SOURCE_CANVAS else ""
        if p is None:
            if (a.status is AssignmentStatus.DUE and a.due_date
                    and a.due_date >= today):
                events.append(event(
                    AlertType.UPCOMING_DEADLINE, agu, course,
                    due_phrase(_day(a.due_date)), item=a.name, via=via))
            continue
        if (a.score, a.points) != (p.score, p.points):
            if p.score is None and a.score is not None:
                what = f"graded: {_score(a)}"
            else:
                what = f"score changed {_score(p)} → {_score(a)}"
            events.append(event(AlertType.GRADE_CHANGED, agu, course, what,
                                item=a.name, via=via))
        if a.status is AssignmentStatus.MISSING and p.status is not AssignmentStatus.MISSING:
            events.append(event(AlertType.ASSIGNMENT_MISSING, agu, course,
                                MISSING_PHRASE, item=a.name, via=via))
        if (a.status is AssignmentStatus.UNGRADED_PAST_DUE
                and p.status is not AssignmentStatus.UNGRADED_PAST_DUE):
            what = "still ungraded"
            if a.source == SOURCE_CANVAS:
                from .canvas import submits_online  # lazy: canvas imports models only
                if submits_online(a):
                    what = "still not turned in"
            events.append(event(
                AlertType.UNGRADED_PAST_DUE, agu, course,
                past_due_phrase(_day(a.due_date) if a.due_date else None, what),
                item=a.name, via=via))
        if (a.status is AssignmentStatus.DUE
                and p.status is AssignmentStatus.NOT_DUE and a.due_date):
            events.append(event(
                AlertType.UPCOMING_DEADLINE, agu, course,
                due_phrase(_day(a.due_date)), item=a.name, via=via))
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


def term_rollover(previous: Snapshot | None, current: Snapshot) -> Event | None:
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
    return event(AlertType.TERM_FINAL, current.student_agu, "",
                 f"{previous.term} closed — final grades: {listing} "
                 f"(now in {current.term})")


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
    """The course's overall grade as a sentence fragment. Read through
    ``course_grade`` so an "N/A"/0 placeholder says "n/a" rather than
    announcing a zero, and a number parked in the mark slot reads as the
    percent it is."""
    value, mark = course_grade(c.mark, c.percent)
    shown = f"{value:.1f}%" if value is not None else ""
    if shown and mark:
        return f"{shown} ({mark})"
    return shown or mark or "n/a"


def _percent_drop(prev, cur) -> float | None:
    """Points lost, or None when the two aren't comparable. Either side
    without a real grade — the "N/A"/0 placeholder included — is not a drop:
    a course flipping to or from "nothing graded yet" hasn't fallen."""
    p, _ = course_grade(prev.mark, prev.percent)
    c, _ = course_grade(cur.mark, cur.percent)
    if p is None or c is None or c >= p:
        return None
    return p - c
