"""SQLite persistence.

The persisted state *is* the previous snapshot: each run loads what the last
run wrote, diffs against the fresh collection, then upserts the new state.
Rows are keyed deterministically on natural keys (student AGU, course GUID +
term, assignment GUID) so re-running is idempotent, and every field-level
change is appended to ``grade_history`` — the audit trail behind "what changed
and when".
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date
from pathlib import Path
from typing import Optional

from .differ import Event
from .models import Assignment, AssignmentStatus, Course, Snapshot, Student

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Assignment fields whose changes are worth an audit row.
_TRACKED_FIELDS = ("name", "kind", "due_date", "graded_at", "score", "points", "status")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path) -> None:
    """Create tables if they don't exist."""
    conn = connect(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


# ── deterministic ids ─────────────────────────────────────────────────


def _course_id(student_id: str, course: Course) -> str:
    return f"{student_id}:{course.edupoint_gu}:{course.term}"


def _assignment_key(a: Assignment) -> str:
    # A missing GUID shouldn't collide every keyless row onto one id; the name
    # is the best remaining stand-in (the differ still ignores keyless rows).
    return a.edupoint_gu or f"name:{a.name}"


# ── reading the previous state ────────────────────────────────────────


def load_snapshot(conn: sqlite3.Connection, student_agu: str) -> Optional[Snapshot]:
    """Rebuild the last persisted Snapshot, or None if this student is new
    (None means "baseline run" to the differ — no alerts)."""
    student = conn.execute(
        "SELECT id FROM students WHERE agu = ?", (student_agu,)
    ).fetchone()
    if student is None:
        return None
    student_id = student["id"]

    courses = []
    course_gu_by_id: dict[str, str] = {}
    for r in conn.execute(
        "SELECT * FROM courses WHERE student_id = ?", (student_id,)
    ):
        course_gu_by_id[r["id"]] = r["edupoint_gu"]
        courses.append(Course(
            edupoint_gu=r["edupoint_gu"], title=r["title"], teacher=r["teacher"],
            term=r["term"], mark=r["mark"], percent=r["percent"],
        ))

    assignments = []
    for r in conn.execute(
        "SELECT a.* FROM assignments a JOIN courses c ON c.id = a.course_id "
        "WHERE c.student_id = ?",
        (student_id,),
    ):
        assignments.append(Assignment(
            edupoint_gu=r["edupoint_gu"],
            course_gu=course_gu_by_id.get(r["course_id"], ""),
            name=r["name"],
            kind=r["kind"],
            due_date=_from_iso(r["due_date"]),
            graded_at=_from_iso(r["graded_at"]),
            score=r["score"],
            points=r["points"],
            status=AssignmentStatus(r["status"]),
        ))

    return Snapshot(student_agu=student_agu, courses=courses, assignments=assignments)


def _from_iso(value: Optional[str]) -> Optional[date]:
    return date.fromisoformat(value) if value else None


def _to_iso(value: Optional[date]) -> Optional[str]:
    return value.isoformat() if value else None


# ── writing the new state ─────────────────────────────────────────────


def persist_snapshot(conn: sqlite3.Connection, student: Student, snap: Snapshot) -> None:
    """Upsert the student's courses + assignments and log field-level changes.

    Assignments that vanished from the portal are kept (they're history, and
    teachers do temporarily hide items); the differ only looks at what the
    current snapshot asserts.
    """
    student_id = student.agu  # AGU is already the portal's stable student key
    conn.execute(
        "INSERT INTO students (id, agu, name, initials, school) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
        "initials=excluded.initials, school=excluded.school",
        (student_id, student.agu, student.name, student.initials, student.school),
    )

    course_ids: dict[str, str] = {}  # edupoint_gu -> row id
    for c in snap.courses:
        cid = _course_id(student_id, c)
        course_ids[c.edupoint_gu] = cid
        conn.execute(
            "INSERT INTO courses (id, edupoint_gu, student_id, title, teacher, term, mark, percent) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET title=excluded.title, teacher=excluded.teacher, "
            "mark=excluded.mark, percent=excluded.percent",
            (cid, c.edupoint_gu, student_id, c.title, c.teacher, c.term, c.mark, c.percent),
        )

    for a in snap.assignments:
        cid = course_ids.get(a.course_gu)
        if cid is None:  # defensive: an assignment without its course row
            continue
        aid = f"{cid}:{_assignment_key(a)}"
        new = {
            "name": a.name, "kind": a.kind,
            "due_date": _to_iso(a.due_date), "graded_at": _to_iso(a.graded_at),
            "score": a.score, "points": a.points, "status": a.status.value,
        }
        old = conn.execute("SELECT * FROM assignments WHERE id = ?", (aid,)).fetchone()
        if old is not None:
            for field_name in _TRACKED_FIELDS:
                if old[field_name] != new[field_name]:
                    conn.execute(
                        "INSERT INTO grade_history (assignment_id, field, old_value, new_value) "
                        "VALUES (?, ?, ?, ?)",
                        (aid, field_name,
                         None if old[field_name] is None else str(old[field_name]),
                         None if new[field_name] is None else str(new[field_name])),
                    )
        conn.execute(
            "INSERT INTO assignments (id, edupoint_gu, course_id, name, kind, assigned, "
            "due_date, graded_at, score, points, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, kind=excluded.kind, "
            "assigned=excluded.assigned, due_date=excluded.due_date, "
            "graded_at=excluded.graded_at, score=excluded.score, "
            "points=excluded.points, status=excluded.status",
            (aid, a.edupoint_gu, cid, a.name, a.kind, _to_iso(a.assigned),
             new["due_date"], new["graded_at"], a.score, a.points, new["status"]),
        )

    conn.commit()


def record_alert(conn: sqlite3.Connection, student_agu: str, event: Event) -> None:
    """Log a delivered alert (the shared-ack story in Phase 4 builds on this)."""
    conn.execute(
        "INSERT INTO alerts (id, student_id, type, body) VALUES (?, ?, ?, ?)",
        (uuid.uuid4().hex, student_agu, event.type.value,
         json.dumps({"course": event.course_title, "detail": event.detail})),
    )
    conn.commit()
