"""The domain's small interpretation rules — chiefly ``course_grade``, which
says what the portal's unevenly-filled mark/percent pair actually means."""
from __future__ import annotations

import pytest

from lastbell.models import course_grade


@pytest.mark.parametrize("mark,percent,expected", [
    # The ordinary shape: a percent and a letter mark, both real.
    ("B+", "87.20%", (87.2, "B+")),
    ("A", "90.00%", (90.0, "A")),
    ("A", "90", (90.0, "A")),
    # A percent with no mark beside it stands alone.
    ("", "51.15%", (51.15, "")),
    # Shape (a): the number sits in the MARK slot, percent empty. It is the
    # percent — and must not also be shown as a mark.
    ("81", "", (81.0, "")),
    ("81", "N/A", (81.0, "")),
    ("0", "", (0.0, "")),          # a course of nothing but zeros really is 0
    ("93.5", "", (93.5, "")),
    # Shape (b): "N/A" with a zero percent is the portal saying nothing has
    # been graded yet — never a zero that reads as failing.
    ("N/A", "0.0", (None, "")),
    ("N/A", "0.00%", (None, "")),
    ("n/a", "0", (None, "")),
    ("na", "0.0", (None, "")),
    ("—", "0.0", (None, "")),
    ("-", "0.0", (None, "")),
    ("", "0.0", (None, "")),
    # …but a real zero WITH a real mark is a real zero.
    ("E", "0.00%", (0.0, "E")),
    # A no-mark placeholder alongside a real percent keeps the percent and
    # drops the placeholder.
    ("N/A", "85.00%", (85.0, "")),
    # Neither slot parses: the mark (when it is one) stands alone.
    ("B", "", (None, "B")),
    ("B", "N/A", (None, "B")),
    ("Pass", "", (None, "Pass")),
    # Nothing at all.
    ("", "", (None, "")),
    ("N/A", "", (None, "")),
    ("—", "", (None, "")),
    # Whitespace is the portal's, not the reader's.
    ("  A  ", "  90%  ", (90.0, "A")),
    ("  81  ", "   ", (81.0, "")),
    (" N/A ", " 0.0 ", (None, "")),
])
def test_course_grade_rules(mark, percent, expected):
    assert course_grade(mark, percent) == expected


def test_course_grade_tolerates_none_slots():
    """Rows come from SQLite, where a nullable column can hand back None."""
    assert course_grade(None, None) == (None, "")
    assert course_grade(None, "88%") == (88.0, "")
    assert course_grade("81", None) == (81.0, "")


def test_case_folding_is_only_for_the_placeholder_set():
    """"NA" is a placeholder; "n" or "P" are marks and survive as typed."""
    assert course_grade("NA", "") == (None, "")
    assert course_grade("P", "") == (None, "P")
    assert course_grade("Inc", "") == (None, "Inc")
