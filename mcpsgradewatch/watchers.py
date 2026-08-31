"""Watcher accounts and subscriptions (Phase 3).

A *watcher* is a person who receives alerts — a guardian or the student
themselves. Watchers are not logins: nobody signs into mcpsgradewatch. A watcher
is a name plus a set of reachable addresses (``channels`` JSON, e.g. an email
address or an ntfy topic), and *subscriptions* say which student's events reach
them, filtered by alert type, over which channel. ``'*'`` means "all".

Everything here is plain SQLite against the tables schema.sql reserved for this
phase: ``watchers``, ``watcher_student``, ``subscriptions``.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Optional

from .models import AlertType, WatcherKind

ALL = "*"  # wildcard for alert_type / channel on a subscription row

VALID_ALERT_TYPES = {t.value for t in AlertType}


class WatcherError(RuntimeError):
    """A watcher/subscription operation that can't proceed (bad name, dupes…)."""


@dataclass
class Watcher:
    id: str
    name: str
    kind: WatcherKind
    channels: dict = field(default_factory=dict)   # channel name -> address dict
    quiet_hours: dict = field(default_factory=dict)


@dataclass
class Subscription:
    id: str
    watcher_id: str
    watcher_name: str
    student_id: str
    student_name: str
    alert_type: str   # AlertType value or '*'
    channel: str      # channel name or '*'


# ── watchers ──────────────────────────────────────────────────────────


def _row_to_watcher(r: sqlite3.Row) -> Watcher:
    return Watcher(
        id=r["id"], name=r["name"], kind=WatcherKind(r["kind"]),
        channels=json.loads(r["channels"] or "{}"),
        quiet_hours=json.loads(r["quiet_hours"] or "{}"),
    )


def add_watcher(conn: sqlite3.Connection, name: str, kind: WatcherKind,
                channels: Optional[dict] = None) -> Watcher:
    if get_watcher(conn, name) is not None:
        raise WatcherError(f"a watcher named {name!r} already exists")
    channels = {k: v for k, v in (channels or {}).items() if v is not None}
    w = Watcher(id=uuid.uuid4().hex, name=name, kind=kind, channels=channels)
    conn.execute(
        "INSERT INTO watchers (id, name, kind, channels) VALUES (?, ?, ?, ?)",
        (w.id, w.name, w.kind.value, json.dumps(w.channels)),
    )
    conn.commit()
    return w


def get_watcher(conn: sqlite3.Connection, name: str) -> Optional[Watcher]:
    r = conn.execute(
        "SELECT * FROM watchers WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()
    return _row_to_watcher(r) if r else None


def require_watcher(conn: sqlite3.Connection, name: str) -> Watcher:
    w = get_watcher(conn, name)
    if w is None:
        known = ", ".join(x.name for x in list_watchers(conn)) or "none yet"
        raise WatcherError(f"no watcher named {name!r} (known: {known})")
    return w


def list_watchers(conn: sqlite3.Connection) -> list[Watcher]:
    return [_row_to_watcher(r) for r in
            conn.execute("SELECT * FROM watchers ORDER BY name")]


def remove_watcher(conn: sqlite3.Connection, name: str) -> None:
    w = require_watcher(conn, name)
    # subscriptions and watcher_student rows cascade
    conn.execute("DELETE FROM watchers WHERE id = ?", (w.id,))
    conn.commit()


def set_channels(conn: sqlite3.Connection, name: str, updates: dict) -> Watcher:
    """Merge ``updates`` into the watcher's channel map. A ``None`` value
    removes that channel (an empty dict is valid — console has no address)."""
    w = require_watcher(conn, name)
    for channel_name, address in updates.items():
        if address is not None:
            w.channels[channel_name] = address
        else:
            w.channels.pop(channel_name, None)
    conn.execute("UPDATE watchers SET channels = ? WHERE id = ?",
                 (json.dumps(w.channels), w.id))
    conn.commit()
    return w


# ── student resolution ────────────────────────────────────────────────


def resolve_student(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    """Find a student by AGU, or by a case-insensitive prefix of name/initials.

    Students appear in the DB after the first ``run`` pass; a clear error says
    so instead of a bare miss.
    """
    rows = conn.execute("SELECT * FROM students").fetchall()
    if not rows:
        raise WatcherError(
            "no students in the database yet — run `mcpsgradewatch run` once "
            "(it establishes the baseline and records each student)."
        )
    exact = [r for r in rows if r["agu"] == ref]
    if len(exact) == 1:
        return exact[0]
    needle = ref.lower()
    matches = [r for r in rows
               if r["name"].lower().startswith(needle)
               or r["initials"].lower().replace(".", "").startswith(needle.replace(".", ""))]
    if len(matches) == 1:
        return matches[0]
    listing = ", ".join(f"{r['name']} (agu {r['agu']})" for r in rows)
    if not matches:
        raise WatcherError(f"no student matching {ref!r}. Known: {listing}")
    raise WatcherError(f"{ref!r} is ambiguous. Known: {listing}")


# ── subscriptions ─────────────────────────────────────────────────────


def subscribe(conn: sqlite3.Connection, watcher: Watcher, student_id: str,
              alert_types: Optional[list[str]] = None,
              channels: Optional[list[str]] = None) -> int:
    """Create subscription rows (and the watcher_student link). Returns the
    number of rows actually added; existing identical rows are left alone."""
    types = alert_types or [ALL]
    chans = channels or [ALL]
    for t in types:
        if t != ALL and t not in VALID_ALERT_TYPES:
            raise WatcherError(
                f"unknown alert type {t!r} (valid: {', '.join(sorted(VALID_ALERT_TYPES))})")

    conn.execute(
        "INSERT OR IGNORE INTO watcher_student (watcher_id, student_id) VALUES (?, ?)",
        (watcher.id, student_id),
    )
    added = 0
    for t in types:
        for ch in chans:
            dup = conn.execute(
                "SELECT 1 FROM subscriptions WHERE watcher_id=? AND student_id=? "
                "AND alert_type=? AND channel=?",
                (watcher.id, student_id, t, ch),
            ).fetchone()
            if dup:
                continue
            conn.execute(
                "INSERT INTO subscriptions (id, watcher_id, student_id, alert_type, channel) "
                "VALUES (?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, watcher.id, student_id, t, ch),
            )
            added += 1
    conn.commit()
    return added


def unsubscribe(conn: sqlite3.Connection, watcher: Watcher,
                student_id: Optional[str] = None) -> int:
    """Drop the watcher's subscriptions (for one student, or all of them)."""
    if student_id is None:
        cur = conn.execute("DELETE FROM subscriptions WHERE watcher_id=?", (watcher.id,))
        conn.execute("DELETE FROM watcher_student WHERE watcher_id=?", (watcher.id,))
    else:
        cur = conn.execute(
            "DELETE FROM subscriptions WHERE watcher_id=? AND student_id=?",
            (watcher.id, student_id))
        conn.execute(
            "DELETE FROM watcher_student WHERE watcher_id=? AND student_id=?",
            (watcher.id, student_id))
    conn.commit()
    return cur.rowcount


def list_subscriptions(conn: sqlite3.Connection) -> list[Subscription]:
    rows = conn.execute(
        "SELECT s.id, s.watcher_id, w.name AS watcher_name, s.student_id, "
        "       st.name AS student_name, s.alert_type, s.channel "
        "FROM subscriptions s "
        "JOIN watchers w ON w.id = s.watcher_id "
        "JOIN students st ON st.id = s.student_id "
        "ORDER BY w.name, st.name, s.alert_type, s.channel"
    ).fetchall()
    return [Subscription(**dict(r)) for r in rows]


def subscriptions_for_student(conn: sqlite3.Connection, student_id: str) -> list[tuple[Watcher, str, str]]:
    """(watcher, alert_type, channel) triples that target this student."""
    rows = conn.execute(
        "SELECT w.*, s.alert_type, s.channel FROM subscriptions s "
        "JOIN watchers w ON w.id = s.watcher_id WHERE s.student_id = ?",
        (student_id,),
    ).fetchall()
    return [(_row_to_watcher(r), r["alert_type"], r["channel"]) for r in rows]
