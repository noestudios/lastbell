"""One collection pass: every class, one student, one Snapshot.

Phase 0 proved the wire path against the *default* class only. This module
sweeps all of them, the same way the portal's own UI does: each class row in
``Gradebook_SchoolClasses`` carries a ``data-focus`` payload, and the page's
``GB.LoadControl`` click handler sends that payload's FocusArgs verbatim. Rows
without a focus (the elementary subject view renders ``data-focus=''``) fall
back to the bootstrap's default class — exactly what Phase 0 fetched.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

from .client import Child, ParentVueClient, ParentVueError
from .gradebook import (
    ClassDetails,
    ParseError,
    SchoolClasses,
    assignment_focus,
    parse_assignment_marks,
    parse_class_details,
    parse_school_classes,
    standards_scored,
)
from .models import AssignmentStatus, Course, Snapshot, Student

# Pause between per-class LoadControl calls; the portal serves a human clicking
# through classes, so a sweep shouldn't hit it faster than a person would.
CLASS_FETCH_DELAY_S = 0.5

# A standards mark already on record is read again while the work is this
# recent (a teacher may revise it); older marked work keeps its recorded
# mark, so the detail-page fetches stay near the number of recent
# assignments instead of growing all year.
MARK_RECHECK_DAYS = 30


def initials_of(name: str) -> str:
    """'JULES P. HARLOW' -> 'J.P.H.' — the low-PII handle used in alerts."""
    parts = [p for p in re.split(r"[\s.]+", name) if p and p[0].isalpha()]
    return "".join(f"{p[0].upper()}." for p in parts)


@dataclass
class CollectedClass:
    course: Course
    details: ClassDetails


@dataclass
class StudentCollection:
    student: Student
    school_classes: SchoolClasses
    classes: list[CollectedClass] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Standards marks that couldn't be read this pass (the recorded mark,
    # if any, was kept). Not class failures: the class itself was read.
    mark_errors: list[str] = field(default_factory=list)
    agu_header: str = "0"   # the page's PXP.AGU, for follow-up requests

    @property
    def snapshot(self) -> Snapshot:
        return Snapshot(
            student_agu=self.student.agu,
            courses=[c.course for c in self.classes],
            assignments=[a for c in self.classes for a in c.details.assignments],
            term=self.school_classes.current_term,
        )


def collect_student(
    client: ParentVueClient, child: Child, *, delay_s: float = CLASS_FETCH_DELAY_S,
    known_marks: dict[str, str] | None = None, today: date | None = None,
    read_marks: bool = True,
) -> StudentCollection:
    """Fetch and normalize the current term's gradebook for one student.

    A class whose fragment fails to fetch or parse is skipped and reported in
    ``errors`` rather than failing the whole pass — one reshaped class page
    shouldn't silence alerts for every other class.

    ``known_marks`` (assignment GUID → recorded standards mark) lets marked
    work older than MARK_RECHECK_DAYS keep its mark without a fetch.
    """
    out = _collect_classes(client, child, delay_s=delay_s)
    if read_marks:
        _read_marks(client, out, known_marks or {}, today or date.today(), delay_s)
    return out


def _read_marks(client, out: StudentCollection, known: dict[str, str],
                today: date, delay_s: float) -> None:
    """Fill in the marks of standards-scored work from each assignment's
    detail page, for work that is unmarked or recent; marked work gets
    status GRADED. A page that can't be read keeps the recorded mark."""
    recent = today - timedelta(days=MARK_RECHECK_DAYS)
    fetched = 0
    for c in out.classes:
        for a in c.details.assignments:
            if not standards_scored(a):
                continue
            a.mark = known.get(a.edupoint_gu, "")
            if a.mark and a.due_date is not None and a.due_date < recent:
                pass                      # settled: keep the recorded mark
            else:
                args = assignment_focus(a)
                try:
                    if not args:
                        raise ParseError("no link to its detail page")
                    if fetched and delay_s:
                        time.sleep(delay_s)
                    fetched += 1
                    a.mark = parse_assignment_marks(
                        client.assignment_details(args, agu_header=out.agu_header))
                except (ParentVueError, ParseError) as e:
                    out.mark_errors.append(f"{c.course.title}: {a.name}: {e}")
            if a.mark and a.status is AssignmentStatus.DUE:
                a.status = AssignmentStatus.GRADED


def _collect_classes(
    client: ParentVueClient, child: Child, *, delay_s: float
) -> StudentCollection:
    focus = client.get_focus_args(child.agu)
    sc = parse_school_classes(
        client.load_control(
            "Gradebook_SchoolClasses", focus.as_parameters(), agu_header=focus.agu_header
        )
    )
    student = Student(
        agu=child.agu, name=child.name, school=child.school or sc.school_name,
        initials=initials_of(child.name),
    )
    out = StudentCollection(student=student, school_classes=sc,
                            agu_header=focus.agu_header)

    focus_rows = [r for r in sc.rows if r.focus.get("FocusArgs")]
    if not focus_rows:
        # Elementary subject view / no per-class focus: the bootstrap's default
        # class is the only one addressable (the Phase 0 path).
        class_gu = str(focus.args.get("classID", ""))
        row = next(
            (r for r in sc.rows
             if r.teacher_id and r.teacher_id == str(focus.args.get("teacherID"))),
            sc.rows[0] if sc.rows else None,
        )
        teacher = row.teacher if row else ""
        # Best available human name for the default class: the row's own title
        # (rare in the elementary subject view), else the teacher's class,
        # else the GUID as a last resort.
        title = ((row.title if row else "")
                 or (f"{teacher}'s class" if teacher else f"Class {class_gu}"))
        try:
            cd = parse_class_details(
                client.class_details(focus.as_parameters(),
                                     agu_header=focus.agu_header),
                course_gu=class_gu,
            )
        except (ParentVueError, ParseError) as e:
            out.errors.append(f"default class {class_gu}: {e}")
            return out
        out.classes.append(CollectedClass(
            course=Course(edupoint_gu=class_gu, title=title,
                          teacher=teacher, term=sc.current_term,
                          mark=cd.mark, percent=cd.percent),
            details=cd,
        ))
        return out

    seen_classes: set[str] = set()
    for row in focus_rows:
        merged = focus.as_parameters()
        merged.update(row.focus["FocusArgs"])
        class_gu = str(merged.get("classID", ""))
        # The live fragment renders each class row twice (screen + print
        # variants); fetch each class once.
        if class_gu in seen_classes:
            continue
        if seen_classes and delay_s:
            time.sleep(delay_s)
        seen_classes.add(class_gu)
        label = row.title or f"Class {class_gu}"
        try:
            cd = parse_class_details(
                client.class_details(merged, agu_header=focus.agu_header),
                course_gu=class_gu,
            )
        except (ParentVueError, ParseError) as e:
            out.errors.append(f"{label}: {e}")
            continue
        out.classes.append(CollectedClass(
            course=Course(edupoint_gu=class_gu, title=label, teacher=row.teacher,
                          term=sc.current_term, mark=cd.mark, percent=cd.percent),
            details=cd,
        ))
    return out
