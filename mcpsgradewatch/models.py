"""Normalized domain objects.

These mirror the persisted schema (see ``schema.sql``). Two join tables carry
the design: ``watcher_student`` makes it multi-watcher; ``credential_student``
makes it multi-account (each guardian brings their own ParentVUE login).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional


class AssignmentStatus(str, Enum):
    GRADED = "graded"
    NOT_DUE = "not_due"
    DUE = "due"
    MISSING = "missing"
    UNGRADED_PAST_DUE = "ungraded_past_due"


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


@dataclass
class Assignment:
    edupoint_gu: str       # stable id — the differ keys on this, not the name
    course_gu: str
    name: str
    kind: str = ""
    assigned: Optional[date] = None
    due_date: Optional[date] = None
    graded_at: Optional[date] = None
    score: Optional[float] = None
    points: Optional[float] = None
    status: AssignmentStatus = AssignmentStatus.DUE
    # The portal's row verbatim, so no field is ever lost to normalization.
    raw: dict = field(default_factory=dict)


@dataclass
class Snapshot:
    """One collection pass for one student: the courses + assignments seen."""

    student_agu: str
    courses: list[Course] = field(default_factory=list)
    assignments: list[Assignment] = field(default_factory=list)
