"""Normalized domain objects.

These mirror the persisted schema (see ``schema.sql``). Two join tables carry
the design: ``watcher_student`` makes it multi-watcher; ``credential_student``
makes it multi-account (each guardian brings their own ParentVUE login).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class AssignmentStatus(str, Enum):
    GRADED = "graded"
    NOT_DUE = "not_due"
    DUE = "due"
    MISSING = "missing"
    UNGRADED_PAST_DUE = "ungraded_past_due"
    SUBMITTED = "submitted"         # turned in, no grade yet (Canvas only)


# Where a course/assignment row came from. ParentVUE (Synergy) is the record;
# Canvas is the leading indicator — assignments and missing flags appear there
# first and reach the gradebook days later, if at all.
SOURCE_PARENTVUE = "parentvue"
SOURCE_CANVAS = "canvas"


class WatcherKind(str, Enum):
    GUARDIAN = "guardian"
    STUDENT = "student"


class AlertType(str, Enum):
    GRADE_CHANGED = "grade_changed"
    ASSIGNMENT_MISSING = "assignment_missing"
    UNGRADED_PAST_DUE = "ungraded_past_due"
    UPCOMING_DEADLINE = "upcoming_deadline"
    GRADE_DROP = "grade_drop"
    DAILY_SUMMARY = "daily_summary"
    TERM_FINAL = "term_final"       # marking period closed: final grades summary
    SOURCE_CONFLICT = "source_conflict"  # gradebook and Canvas disagree on a grade


# The alert types a parent may want NOW rather than in the daily digest —
# things still actionable today. Grade changes are informational and batch.
URGENT_ALERT_TYPES = frozenset({
    AlertType.ASSIGNMENT_MISSING,
    AlertType.UPCOMING_DEADLINE,
    AlertType.GRADE_DROP,
})


def parse_percent(raw: str) -> float | None:
    """``"87.20%"`` -> ``87.2``; None when the text isn't a number."""
    try:
        return float(raw.strip().rstrip("%"))
    except (ValueError, AttributeError):
        return None


def format_percent(raw: str) -> str | None:
    """Display rule for course percents: always one decimal place ("93.0",
    "0.0", "51.1"). Applied at *display* time only — the stored value stays
    the portal's raw string, so the differ never sees a formatting change as
    a grade change. None when the raw text isn't a number (caller falls back
    to the raw text or a dash)."""
    value = parse_percent(raw)
    return None if value is None else f"{value:.1f}"


@dataclass
class Student:
    agu: str               # dedupe / natural key across credentials
    name: str
    school: str = ""
    initials: str = ""     # used in low-PII notification payloads


@dataclass
class Course:
    edupoint_gu: str       # natural key from the portal
    title: str
    teacher: str = ""
    term: str = ""
    mark: str = ""         # overall letter/mark
    percent: str = ""
    source: str = SOURCE_PARENTVUE


@dataclass
class Assignment:
    edupoint_gu: str       # stable id — the differ keys on this, not the name
    course_gu: str
    name: str
    kind: str = ""
    assigned: date | None = None
    due_date: date | None = None
    graded_at: date | None = None
    score: float | None = None
    points: float | None = None
    status: AssignmentStatus = AssignmentStatus.DUE
    source: str = SOURCE_PARENTVUE
    # A Canvas row whose gradebook twin exists names it here (the twin's
    # GUID). The twin is the record; this row is kept, updated, and hidden —
    # its only voice is a "Canvas says …" hint when the two disagree.
    superseded_by: str = ""
    # The portal's row verbatim, so no field is ever lost to normalization.
    raw: dict = field(default_factory=dict)


@dataclass
class Snapshot:
    """One collection pass for one student: the courses + assignments seen.

    ``term`` is the marking period the portal said was current during this
    pass (e.g. "MP1"). The store remembers it per student; a change between
    the remembered and the collected term is a rollover — the differ answers
    with a one-shot final-grades summary event.
    """

    student_agu: str
    courses: list[Course] = field(default_factory=list)
    assignments: list[Assignment] = field(default_factory=list)
    term: str = ""
