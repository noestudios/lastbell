"""SQLite persistence.

Skeleton: initializes the schema and provides a home for snapshot writes and the
diff-driving history. Snapshot upserts land here in Phase 1, once the gate fetch
gives us real assignment data to persist.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


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
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


# TODO(Phase 1): upsert_snapshot(conn, snapshot) -> writes courses/assignments,
# appends grade_history rows for any changed fields, keyed on edupoint_gu.
