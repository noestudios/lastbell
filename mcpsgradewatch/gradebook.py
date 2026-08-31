"""Parse the HTML fragments returned by ``LoadControl`` into domain objects.

Wired against real MCPS fragments captured 2026-08-31 (elementary subject
view, term "MP1 Interim"). Two fragment kinds:

``Gradebook_SchoolClasses``
    The term selector (every mark period with its GUID — the key to sweeping
    all grading periods) plus the subject/teacher rows. In the elementary
    subject view, rows are grouped per teacher; secondary schools render class
    rows with period/score cells instead — both shapes are handled.

``Gradebook_ClassDetails``
    The current class grade (``.gb-current-grade`` → ``.mark`` / ``.score``),
    a missing-assignments indicator, and the assignments themselves as a
    DevExpress grid config whose ``"dataSource": [...]`` array is plain JSON
    with fields: Date, GBAssignment, GBAssignmentType, GBSubject, GBResources,
    GBScore, GBScoreType, GBPoints, GBNotes (per the grid's own column spec).

The gate fragments were captured with an empty gradebook (year had just
started), so populated-row parsing is schema-driven and defensive: every raw
row is preserved on the Assignment so nothing is lost if MCPS adds fields.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from .models import Assignment, AssignmentStatus


@dataclass
class MarkPeriod:
    """One entry from the term selector (e.g. 'MP2 Interim')."""

    name: str
    gu: str
    group: str = ""
    current: bool = False


@dataclass
class SubjectRow:
    """A subject/class row from SchoolClasses (elementary: one per teacher)."""

    teacher: str
    teacher_id: str = ""
    title: str = ""       # class/subject title when the view provides one
    period: str = ""
    room: str = ""
    score: str = ""       # raw score/mark text when present


@dataclass
class SchoolClasses:
    school_name: str
    school_id: str
    org_year_gu: str
    current_term: str
    mark_periods: list[MarkPeriod] = field(default_factory=list)
    rows: list[SubjectRow] = field(default_factory=list)


@dataclass
class ClassDetails:
    mark: str                 # e.g. "B+" or "N/A"
    percent: str              # e.g. "87.20%" or "0.00%"
    missing_text: str         # raw missing-assignments indicator text ("" if none)
    assignments: list[Assignment] = field(default_factory=list)


class ParseError(RuntimeError):
    """Fragment didn't match the expected structure — portal layout may have
    changed. Callers should save the raw fragment for inspection."""


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


# ── Gradebook_SchoolClasses ───────────────────────────────────────────


def parse_school_classes(html: str) -> SchoolClasses:
    panel = re.search(
        r'data-school-id="([^"]*)"[^>]*data-orgyear-id="([^"]*)"', html
    )
    school_id, org_year_gu = (panel.group(1), panel.group(2)) if panel else ("", "")

    title = re.search(r'<span class="title">([^<]*)</span>', html)
    school_name = title.group(1).strip() if title else ""
    school_name = re.sub(r"^(Subjects|Classes)\s+for\s+", "", school_name)

    current = re.search(
        r'class="current breadcrumb-term">\s*([^<]*?)\s*</div>', html, re.DOTALL
    )
    current_term = current.group(1).strip() if current else ""

    periods = [
        MarkPeriod(name=_text(m.group(3)), gu=m.group(2), group=m.group(1),
                   current=_text(m.group(3)) == current_term)
        for m in re.finditer(
            r'data-period-group="([^"]*)"\s+data-period-id="([^"]*)"\s+'
            r'data-action="GB\.SetTerm"\s*>\s*(.*?)\s*</a>',
            html,
            re.DOTALL,
        )
    ]

    rows: list[SubjectRow] = []
    for m in re.finditer(r'<div class="[^"]*gb-class-row[^"]*"[^>]*>', html):
        # A row segment runs until the next row (or end of fragment).
        nxt = re.search(r'<div class="[^"]*gb-class-row', html[m.end():])
        seg = html[m.start(): m.end() + (nxt.start() if nxt else len(html))]

        teacher = ""
        t = re.search(r'<div class="teacher hide-for-screen">([^<]*)</div>', seg)
        if t:
            teacher = t.group(1).strip()
        tid = re.search(r"data-teacher-id='(\d+)'", seg)

        # Secondary-school class rows carry period/title/room/score cells.
        def cell(cls: str) -> str:
            c = re.search(rf'class="[^"]*\b{cls}\b[^"]*"[^>]*>(.*?)</', seg, re.DOTALL)
            return _text(c.group(1)) if c else ""

        rows.append(
            SubjectRow(
                teacher=teacher,
                teacher_id=tid.group(1) if tid else "",
                title=cell("class-title") or cell("course-title"),
                period=cell("period"),
                room=cell("room"),
                score=cell("score") or cell("mark"),
            )
        )

    if not periods and not rows:
        raise ParseError("SchoolClasses fragment had neither terms nor rows.")

    return SchoolClasses(
        school_name=school_name,
        school_id=school_id,
        org_year_gu=org_year_gu,
        current_term=current_term,
        mark_periods=periods,
        rows=rows,
    )


# ── Gradebook_ClassDetails ────────────────────────────────────────────


def _extract_datasource(html: str) -> list[dict]:
    """Pull the AssignmentsGrid's ``"dataSource": [...]`` JSON array.

    The surrounding grid config contains JS function references, so only the
    array itself is parsed — found by bracket matching from the marker.
    """
    i = html.find('"dataSource":')
    if i == -1:
        return []
    j = html.find("[", i)
    if j == -1:
        return []
    depth = 0
    in_str = False
    esc = False
    for k in range(j, len(html)):
        ch = html[k]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    rows = json.loads(html[j: k + 1])
                except ValueError:
                    return []
                return rows if isinstance(rows, list) else []
    return []


_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _first_number(raw) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    m = _NUM.search(str(raw))
    return float(m.group(0)) if m else None


def _parse_date(raw) -> Optional[date]:
    if not raw:
        return None
    s = _text(str(raw))
    # Choose 2- vs 4-digit year format by inspection: "8/28/26" parsed with
    # %Y would silently become year 26.
    parts = s.split("/")
    if len(parts) == 3:
        fmts = ("%m/%d/%y",) if len(parts[2]) <= 2 else ("%m/%d/%Y",)
    else:
        fmts = ("%Y-%m-%d",)
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _cell_value(raw) -> str:
    """Unwrap a grid cell to its display text.

    LinkColumn cells (GBAssignment, GBScore) arrive as JSON strings like
    ``{"href": ..., "hrefAttributes": <data-focus for AssignmentDetails>,
    "value": "Not Graded", "dataType": "LinkColumn"}`` — the display text is
    ``value``. Plain cells are already text.
    """
    if raw is None:
        return ""
    if isinstance(raw, dict):
        return str(raw.get("value", ""))
    s = str(raw)
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                return str(obj.get("value", ""))
        except ValueError:
            pass
    return _text(s)


def _row_to_assignment(row: dict, course_gu: str) -> Assignment:
    score_text = _cell_value(row.get("GBScore"))
    points_text = _text(str(row.get("GBPoints", "")))

    # Score text: "8 out of 10", "Missing", "Not Graded", ...
    score_nums = _NUM.findall(score_text)
    score = float(score_nums[0]) if score_nums else None

    # Points: ungraded renders "10.0000 Points Possible"; graded "8.00 / 10.0000".
    pts_nums = _NUM.findall(points_text)
    points = float(pts_nums[-1]) if pts_nums else None
    if score is None and len(pts_nums) >= 2:
        score = float(pts_nums[0])

    lowered = f"{score_text} {_cell_value(row.get('GBNotes'))}".lower()
    if "missing" in lowered:
        status = AssignmentStatus.MISSING
    elif score is not None:
        status = AssignmentStatus.GRADED
    else:  # "Not Graded" and anything else unscored
        status = AssignmentStatus.DUE

    return Assignment(
        edupoint_gu=str(
            row.get("gradeBookId") or row.get("GradebookID") or row.get("boid") or ""
        ),
        course_gu=course_gu,
        name=_cell_value(row.get("GBAssignment")),
        kind=_cell_value(row.get("GBAssignmentType")),
        due_date=_parse_date(row.get("Date")),
        score=score,
        points=points,
        status=status,
        # Verbatim row — includes hrefAttributes carrying the ready-made
        # Gradebook_AssignmentDetails focus for the per-assignment drill-down.
        raw=dict(row),
    )


def parse_class_details(html: str, course_gu: str = "") -> ClassDetails:
    grade = re.search(
        r'gb-current-grade.*?<div class="mark">\s*([^<]*?)\s*</div>\s*'
        r'<div class="score">\s*([^<]*?)\s*</div>',
        html,
        re.DOTALL,
    )
    if grade is None:
        raise ParseError("ClassDetails fragment had no current-grade block.")

    missing = re.search(
        r'gb-classdetail-missingassignments[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL
    )

    return ClassDetails(
        mark=grade.group(1).strip(),
        percent=grade.group(2).strip(),
        missing_text=_text(missing.group(1)) if missing else "",
        assignments=[_row_to_assignment(r, course_gu) for r in _extract_datasource(html)],
    )
