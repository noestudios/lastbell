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

# Course fields whose changes are worth an audit row (the grade trajectory).
_COURSE_TRACKED_FIELDS = ("mark", "percent")


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
    _migrate(conn)
    conn.commit()


# Columns added after a table first shipped: CREATE IF NOT EXISTS won't touch
# an existing table, so they're patched in with ALTER on upgrade.
_MIGRATIONS = [
    ("alerts", "acked_at", "TEXT"),
    ("subscriptions", "last_sent_on", "TEXT"),
    ("students", "current_term", "TEXT NOT NULL DEFAULT ''"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in _MIGRATIONS:
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


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
        "SELECT id, current_term FROM students WHERE agu = ?", (student_agu,)
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

    return Snapshot(student_agu=student_agu, courses=courses,
                    assignments=assignments, term=student["current_term"] or "")


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
    if snap.term:
        # Remember the marking period this pass saw; a change against the
        # remembered value is what the differ reports as a term rollover. An
        # empty term (a partial/failed parse) preserves the last known one.
        conn.execute("UPDATE students SET current_term = ? WHERE id = ?",
                     (snap.term, student_id))

    course_ids: dict[str, str] = {}  # edupoint_gu -> row id
    for c in snap.courses:
        cid = _course_id(student_id, c)
        course_ids[c.edupoint_gu] = cid
        old_course = conn.execute(
            "SELECT mark, percent FROM courses WHERE id = ?", (cid,)
        ).fetchone()
        if old_course is not None:
            for field_name in _COURSE_TRACKED_FIELDS:
                new_value = getattr(c, field_name)
                if old_course[field_name] != new_value:
                    conn.execute(
                        "INSERT INTO course_history (course_id, field, old_value, new_value) "
                        "VALUES (?, ?, ?, ?)",
                        (cid, field_name, old_course[field_name], new_value),
                    )
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
    """Log a delivered alert; ``ack_alert`` marks it handled for everyone."""
    conn.execute(
        "INSERT INTO alerts (id, student_id, type, body) VALUES (?, ?, ?, ?)",
        (uuid.uuid4().hex, student_agu, event.type.value,
         json.dumps({"course": event.course_title, "detail": event.detail})),
    )
    conn.commit()


# ── shared ack (Phase 4) ──────────────────────────────────────────────
#
# An ack is *shared*: one watcher marking an alert handled marks it for the
# whole household — summaries and the dashboard show who took it.


class AckError(RuntimeError):
    pass


def list_alerts(conn: sqlite3.Connection, *, only_open: bool = False,
                limit: int = 50) -> list[sqlite3.Row]:
    where = "WHERE al.acked_at IS NULL" if only_open else ""
    return conn.execute(
        f"SELECT al.*, st.initials, st.name AS student_name, w.name AS acked_by_name "
        f"FROM alerts al JOIN students st ON st.id = al.student_id "
        f"LEFT JOIN watchers w ON w.id = al.acked_by {where} "
        f"ORDER BY al.created_at DESC, al.rowid DESC LIMIT ?", (limit,)
    ).fetchall()


def ack_alert(conn: sqlite3.Connection, alert_id_prefix: str, watcher_id: str) -> sqlite3.Row:
    """Ack one alert by id (any unique prefix). Returns the alert row."""
    rows = conn.execute(
        "SELECT * FROM alerts WHERE id LIKE ? ORDER BY created_at",
        (alert_id_prefix + "%",),
    ).fetchall()
    if not rows:
        raise AckError(f"no alert with id starting {alert_id_prefix!r} "
                       f"(see `mcpsgradewatch alerts`)")
    if len(rows) > 1:
        ids = ", ".join(r["id"][:8] for r in rows[:5])
        raise AckError(f"{alert_id_prefix!r} matches {len(rows)} alerts ({ids}…) — "
                       f"use more characters")
    (row,) = rows
    if row["acked_at"] is None:
        conn.execute(
            "UPDATE alerts SET acked_by = ?, acked_at = datetime('now') WHERE id = ?",
            (watcher_id, row["id"]))
        conn.commit()
    return conn.execute("SELECT * FROM alerts WHERE id = ?", (row["id"],)).fetchone()
