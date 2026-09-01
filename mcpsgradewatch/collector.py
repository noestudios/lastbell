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

from .client import Child, ParentVueClient, ParentVueError
from .gradebook import (
    ClassDetails,
    ParseError,
    SchoolClasses,
    parse_class_details,
    parse_school_classes,
)
from .models import Course, Snapshot, Student

# Pause between per-class LoadControl calls; the portal serves a human clicking
# through classes, so a sweep shouldn't hit it faster than a person would.
CLASS_FETCH_DELAY_S = 0.5


def initials_of(name: str) -> str:
    """'JASPER P. HAYS' -> 'J.P.H.' — the low-PII handle used in alerts."""
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

    @property
    def snapshot(self) -> Snapshot:
        return Snapshot(
            student_agu=self.student.agu,
            courses=[c.course for c in self.classes],
            assignments=[a for c in self.classes for a in c.details.assignments],
            term=self.school_classes.current_term,
        )


def collect_student(
    client: ParentVueClient, child: Child, *, delay_s: float = CLASS_FETCH_DELAY_S
) -> StudentCollection:
    """Fetch and normalize the current term's gradebook for one student.

    A class whose fragment fails to fetch or parse is skipped and reported in
    ``errors`` rather than failing the whole pass — one reshaped class page
    shouldn't silence alerts for every other class.
    """
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
    out = StudentCollection(student=student, school_classes=sc)

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
                client.load_control(
                    "Gradebook_ClassDetails", focus.as_parameters(),
                    agu_header=focus.agu_header,
                ),
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
                client.load_control(
                    "Gradebook_ClassDetails", merged, agu_header=focus.agu_header
                ),
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
