"""Parser tests against synthetic fixtures that mirror the real MCPS fragment
structure (captured 2026-08-31) with entirely fake values."""
from __future__ import annotations

import datetime
import pathlib

from lastbell.gradebook import parse_class_details, parse_school_classes
from lastbell.models import AssignmentStatus

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def test_parse_school_classes_secondary_focus_rows():
    html = (FIXTURES / "schoolclasses_secondary.html").read_text(encoding="utf-8")
    sc = parse_school_classes(html)

    assert sc.school_id == "176"
    assert sc.current_term == "MP1"
    assert [p.name for p in sc.mark_periods] == ["MP1", "MP2"]
    assert sc.mark_periods[0].current

    spanish, algebra, empty = sc.rows
    # single-quoted attribute, raw JSON inside
    assert spanish.title == "Spanish 2"
    assert spanish.focus["LoadParams"]["ControlName"] == "Gradebook_ClassDetails"
    assert spanish.focus["FocusArgs"]["classID"] == 736713
    # double-quoted attribute with &quot; entities
    assert algebra.focus["FocusArgs"]["classID"] == 736714
    # elementary-style empty data-focus stays an empty dict
    assert empty.focus == {}


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


def test_secondary_graded_row_recovers_points_from_score_text():
    """Live secondary rows leave GBPoints empty; "3 out of 4.0000" carries both."""
    from lastbell.gradebook import _row_to_assignment

    a = _row_to_assignment(
        {"GBScore": '{"value": "3 out of 4.0000", "dataType": "LinkColumn"}',
         "GBAssignment": '{"value": "U1L3 HW"}', "gradeBookId": "77"},
        course_gu="c1")
    assert a.score == 3.0
    assert a.points == 4.0
    assert a.status.value == "graded"
