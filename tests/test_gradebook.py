"""Parser tests against synthetic fixtures that mirror the real MCPS fragment
structure (captured 2026-08-31) with entirely fake values."""
from __future__ import annotations

import datetime
import pathlib

from mcpsgradewatch.gradebook import parse_class_details
from mcpsgradewatch.models import AssignmentStatus

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def test_parse_class_details_sample():
    html = (FIXTURES / "classdetails_sample.html").read_text(encoding="utf-8")
    cd = parse_class_details(html, course_gu="COURSE-1")

    assert cd.mark == "B+"
    assert cd.percent == "87.20%"
    assert cd.missing_text == "2 Missing Assignments"
    assert len(cd.assignments) == 3

    graded, missing, pending = cd.assignments
    assert graded.name == "Fractions Quiz"          # unwrapped from LinkColumn JSON
    assert graded.status is AssignmentStatus.GRADED
    assert graded.score == 8.0
    assert graded.points == 10.0
    assert graded.due_date == datetime.date(2026, 9, 12)   # 2-digit year handled
    assert graded.edupoint_gu == "11110001"
    assert graded.course_gu == "COURSE-1"

    assert missing.name == "Reading Log Week 3"
    assert missing.status is AssignmentStatus.MISSING
    assert missing.score is None
    assert missing.points == 5.0

    assert pending.name == "Science Fair Proposal"
    assert pending.status is AssignmentStatus.DUE   # "Not Graded"
    assert pending.score is None
    assert pending.points == 20.0
    # raw row preserved verbatim for future fields / drill-down focus payloads
    assert pending.raw["Teacher"] == "Pat Example"
