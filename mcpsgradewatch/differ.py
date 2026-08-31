"""Snapshot diffing -> alert events.

Compares the latest collection against the previous one and emits typed events.
The differ is only as good as its key: it matches assignments on the stable
Edupoint GUID, never the display name (names change; corrections happen).

Skeleton for Phase 1 — the event shapes are defined so the notifier and
subscription matching can be built against them.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import AlertType, Assignment, Snapshot


@dataclass
class Event:
    type: AlertType
    student_agu: str
    course_title: str
    detail: str            # human-readable, low-PII (no full names)


def diff(previous: Snapshot | None, current: Snapshot) -> list[Event]:
    """Return the events implied by moving from ``previous`` to ``current``.

    TODO(Phase 1/2): implement
      - GRADE_CHANGED:      assignment score or overall mark changed
      - ASSIGNMENT_MISSING: status became MISSING
      - UNGRADED_PAST_DUE:  due_date passed and still ungraded after N days
      - UPCOMING_DEADLINE:  future due_date within the look-ahead window
    """
    if previous is None:
        return []  # first run establishes a baseline, no alerts
    return []


def _by_gu(assignments: list[Assignment]) -> dict[str, Assignment]:
    return {a.edupoint_gu: a for a in assignments}
