"""Parse the HTML fragments returned by ``LoadControl`` into domain objects.

⚠ PHASE 0 GATE lives here. We know *which* controls to call and that they
return server-rendered HTML fragments (not JSON). We do NOT yet have a verified
sample of those fragments for MCPS, because the end-to-end LoadControl fetch is
unverified. So the selectors below are intentionally stubs: fill them in from a
real captured fragment as the first implementation step, then delete the raises.

Do the capture with ``gradewatch collect --dump`` once focus args resolve, or
grab one LoadControl response from the browser Network tab.
"""
from __future__ import annotations

from .models import Assignment, Course


class NotYetImplemented(NotImplementedError):
    """Raised by the gate stubs until real fragment selectors are wired up."""


def parse_school_classes(html: str) -> list[Course]:
    """Gradebook_SchoolClasses fragment -> overall marks per course."""
    raise NotYetImplemented(
        "parse_school_classes: wire up against a real MCPS fragment (Phase 0 gate)."
    )


def parse_class_details(html: str, course_gu: str) -> list[Assignment]:
    """Gradebook_ClassDetails fragment -> the assignments in one course."""
    raise NotYetImplemented(
        "parse_class_details: wire up against a real MCPS fragment (Phase 0 gate)."
    )


def parse_assignment_details(html: str) -> Assignment:
    """Gradebook_AssignmentDetails fragment -> one assignment's full detail
    (due date, score, points, and the stable Edupoint GUID used as diff key)."""
    raise NotYetImplemented(
        "parse_assignment_details: wire up against a real MCPS fragment (Phase 0 gate)."
    )
