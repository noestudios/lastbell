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
    # Write-ahead logging: the dashboard keeps reading while the poll
    # persists, instead of hitting "database is locked" mid-commit. Sticky
    # per file, so the first connection to set it does it for all.
    conn.execute("PRAGMA journal_mode = WAL")
    # WAL's standard durability setting: a commit is an append, not an fsync
    # of the whole file. A poll makes many small commits; on an SD card each
    # FULL-sync commit is tens of milliseconds the dashboard waits behind.
    # The exposure is a power cut losing the last commit or two, which the
    # next poll re-derives from the portal.
    conn.execute("PRAGMA synchronous = NORMAL")
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
    ("subscriptions", "last_sent_on", "TEXT"),
    ("students", "current_term", "TEXT NOT NULL DEFAULT ''"),
    ("subscriptions", "urgent_now", "INTEGER NOT NULL DEFAULT 0"),
    ("courses", "source", "TEXT NOT NULL DEFAULT 'parentvue'"),
    ("assignments", "source", "TEXT NOT NULL DEFAULT 'parentvue'"),
    ("assignments", "superseded_by", "TEXT NOT NULL DEFAULT ''"),
    ("outbox", "body", "TEXT NOT NULL DEFAULT ''"),
]

# A Canvas assignment is the leading copy of work that later reaches the
# gradebook under the same name. Once the ParentVUE row exists it is the
# record, so readers hide the Canvas twin: the count, the list, and the
# daily summary all say each piece of work once. (The row itself is kept —
# it is history and it keeps being updated — the merge step marks it
# ``superseded_by`` the twin's GUID, and the differ speaks for it only when
# the two disagree.) The name rule is kept alongside the mark so rows hidden
# by older versions stay hidden. Use as `... AND <NOT_SUPERSEDED_SQL>` with
# the assignments table aliased `a`.
NOT_SUPERSEDED_SQL = (
    "NOT (a.source = 'canvas' AND (a.superseded_by != '' OR EXISTS ("
    "  SELECT 1 FROM assignments b WHERE b.course_id = a.course_id "
    "  AND b.source = 'parentvue' AND lower(trim(b.name)) = lower(trim(a.name)))))")

# The hidden twin's grade, for a "Canvas says …" hint on the gradebook row.
# Select alongside `a.*`; NULL when there is no twin or it has no score.
CANVAS_TWIN_SQL = (
    "(SELECT b.score FROM assignments b WHERE b.course_id = a.course_id "
    "  AND b.source = 'canvas' AND b.superseded_by = a.edupoint_gu "
    "  AND b.score IS NOT NULL LIMIT 1) AS canvas_score, "
    "(SELECT b.points FROM assignments b WHERE b.course_id = a.course_id "
    "  AND b.source = 'canvas' AND b.superseded_by = a.edupoint_gu "
    "  AND b.score IS NOT NULL LIMIT 1) AS canvas_points")


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that shipped after a table did. The poller and the
    dashboard both run this at startup, and after an upgrade they are
    restarted together: if both see the column missing, the second ALTER
    fails with "duplicate column name" — the other process already did the
    work, which is the outcome we wanted."""
    for table, column, decl in _MIGRATIONS:
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise


# ── deterministic ids ─────────────────────────────────────────────────


def _course_id(student_id: str, course: Course) -> str:
    return f"{student_id}:{course.edupoint_gu}:{course.term}"


def _assignment_key(a: Assignment) -> str:
    # A missing GUID shouldn't collide every keyless row onto one id; the name
    # is the best remaining stand-in (the differ still ignores keyless rows).
    return a.edupoint_gu or f"name:{a.name}"


# ── reading the previous state ────────────────────────────────────────


def load_snapshot(conn: sqlite3.Connection, student_agu: str) -> Snapshot | None:
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
            source=r["source"],
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
            source=r["source"],
            superseded_by=r["superseded_by"],
        ))

    return Snapshot(student_agu=student_agu, courses=courses,
                    assignments=assignments, term=student["current_term"] or "")


def _from_iso(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _to_iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


# ── writing the new state ─────────────────────────────────────────────


def persist_snapshot(conn: sqlite3.Connection, student: Student, snap: Snapshot,
                     *, prune_canvas: bool = False) -> None:
    """Upsert the student's courses + assignments and log field-level changes.

    Assignments that vanished from the portal are kept (they're history, and
    teachers do temporarily hide items); the differ only looks at what the
    current snapshot asserts. Canvas-only course rows are the exception:
    with ``prune_canvas`` (the Canvas layer ran this poll) any the snapshot
    no longer asserts are dropped, work and all — they are rebuilt from
    Canvas every poll, so a row that stopped qualifying would otherwise
    linger on the dashboard forever.
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

    # What the last pass wrote, read once: every course and assignment row
    # this student has, keyed by id, so the upsert loop below never queries.
    old_courses = {r["id"]: r for r in conn.execute(
        "SELECT id, mark, percent FROM courses WHERE student_id = ?", (student_id,))}
    old_assignments = {r["id"]: r for r in conn.execute(
        "SELECT a.* FROM assignments a JOIN courses c ON c.id = a.course_id "
        "WHERE c.student_id = ?", (student_id,))}

    course_ids: dict[str, str] = {}  # edupoint_gu -> row id
    for c in snap.courses:
        cid = _course_id(student_id, c)
        course_ids[c.edupoint_gu] = cid
        old_course = old_courses.get(cid)
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
            "INSERT INTO courses (id, edupoint_gu, student_id, title, teacher, term, "
            "mark, percent, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET title=excluded.title, teacher=excluded.teacher, "
            "mark=excluded.mark, percent=excluded.percent, source=excluded.source",
            (cid, c.edupoint_gu, student_id, c.title, c.teacher, c.term, c.mark,
             c.percent, c.source),
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
        old = old_assignments.get(aid)
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
            "due_date, graded_at, score, points, status, source, superseded_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, kind=excluded.kind, "
            "assigned=excluded.assigned, due_date=excluded.due_date, "
            "graded_at=excluded.graded_at, score=excluded.score, "
            "points=excluded.points, status=excluded.status, source=excluded.source, "
            "superseded_by=excluded.superseded_by",
            (aid, a.edupoint_gu, cid, a.name, a.kind, _to_iso(a.assigned),
             new["due_date"], new["graded_at"], a.score, a.points, new["status"],
             a.source, a.superseded_by),
        )

    if prune_canvas:
        keep = list(course_ids.values())
        conn.execute(
            "DELETE FROM courses WHERE student_id = ? AND source = 'canvas' "
            f"AND id NOT IN ({','.join('?' * len(keep)) or 'NULL'})",
            (student_id, *keep),
        )

    conn.commit()


def record_alert(conn: sqlite3.Connection, student_agu: str, event: Event) -> None:
    """Log a delivered alert."""
    conn.execute(
        "INSERT INTO alerts (id, student_id, type, body) VALUES (?, ?, ?, ?)",
        (uuid.uuid4().hex, student_agu, event.type.value,
         json.dumps(event.as_dict())),
    )
    conn.commit()


def list_alerts(conn: sqlite3.Connection, *, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT al.*, st.initials, st.name AS student_name "
        "FROM alerts al JOIN students st ON st.id = al.student_id "
        "ORDER BY al.created_at DESC, al.rowid DESC LIMIT ?", (limit,)
    ).fetchall()


# ── install facts ─────────────────────────────────────────────────────

LAST_POLL_KEY = "last_poll_at"


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def record_poll(conn: sqlite3.Connection, when: str | None = None) -> None:
    """Note that a poll finished (UTC, the same clock as every other stored
    timestamp). Called after every student is persisted — a quiet poll with
    no changes leaves no other trace, and "last checked" must count it."""
    set_meta(conn, LAST_POLL_KEY, when or
             conn.execute("SELECT datetime('now')").fetchone()[0])


def last_poll(conn: sqlite3.Connection) -> str | None:
    return get_meta(conn, LAST_POLL_KEY)
