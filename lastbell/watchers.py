"""Watcher accounts and subscriptions (Phase 3).

A *watcher* is a person who receives alerts — a guardian or the student
themselves. Watchers are not logins: nobody signs into lastbell. A watcher
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
    send_at: Optional[str] = None   # HH:MM digest/summary time; None = immediate
    urgent_now: bool = False        # urgent alert types skip the digest wait


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


def ensure_default_watcher(conn: sqlite3.Connection, username: str,
                           email: Optional[str] = None) -> Optional[Watcher]:
    """Whoever installs with a username/password IS a watcher (UX decision 3).

    With zero watchers, create a guardian named after the credential holder
    and subscribe them to every student in the database — so a fresh install
    always has someone to notify. The email channel comes from ``email``
    (LASTBELL_SMTP_TO) when set; otherwise
    console, matching the old no-watcher fallback. Delivery defaults to the
    considerate cadence: one 16:00 daily digest, with urgent alert types
    (missing / due soon / grade drop) sent immediately. Returns the new
    watcher, or None when any watcher already exists (a no-op later).
    """
    if list_watchers(conn):
        return None
    channels = {"email": {"to": email}} if email else {"console": {}}
    w = add_watcher(conn, username, WatcherKind.GUARDIAN, channels)
    for r in conn.execute("SELECT id FROM students"):
        subscribe(conn, w, r["id"], send_at="16:00", urgent_now=True)
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
            "no students in the database yet — run `lastbell run` once "
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


def validate_hhmm(value: str) -> str:
    """Normalize an ``HH:MM`` string (zero-padded, 24h) or raise WatcherError."""
    parts = value.split(":")
    try:
        h, m = int(parts[0]), int(parts[1])
        if len(parts) != 2 or not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
    except (ValueError, IndexError):
        raise WatcherError(f"{value!r} is not a valid time — use 24h HH:MM, e.g. 17:30")
    return f"{h:02d}:{m:02d}"


def set_quiet_hours(conn: sqlite3.Connection, name: str,
                    start: Optional[str], end: Optional[str]) -> Watcher:
    """Set (or clear, with None) the watcher's quiet window. Alerts landing
    inside it are held in the outbox until the window ends — deferred, never
    dropped."""
    w = require_watcher(conn, name)
    if start is None or end is None:
        w.quiet_hours = {}
    else:
        start, end = validate_hhmm(start), validate_hhmm(end)
        if start == end:
            raise WatcherError("quiet hours start and end are the same — "
                               "use `--clear` to remove the window instead")
        w.quiet_hours = {"start": start, "end": end}
    conn.execute("UPDATE watchers SET quiet_hours = ? WHERE id = ?",
                 (json.dumps(w.quiet_hours), w.id))
    conn.commit()
    return w


def subscribe(conn: sqlite3.Connection, watcher: Watcher, student_id: str,
              alert_types: Optional[list[str]] = None,
              channels: Optional[list[str]] = None,
              send_at: Optional[str] = None,
              urgent_now: bool = False) -> list[str]:
    """Create subscription rows (and the watcher_student link). Returns the
    ids of the rows actually added; existing identical rows are left alone.

    ``send_at`` (HH:MM) makes the rows *scheduled*: event alerts batch into a
    daily digest delivered after that time; a ``daily_summary`` row generates
    the standing report then (and defaults to 07:00 if no time is given).
    """
    types = alert_types or [ALL]
    chans = channels or [ALL]
    for t in types:
        if t != ALL and t not in VALID_ALERT_TYPES:
            raise WatcherError(
                f"unknown alert type {t!r} (valid: {', '.join(sorted(VALID_ALERT_TYPES))})")
    if send_at is not None:
        send_at = validate_hhmm(send_at)
    if AlertType.DAILY_SUMMARY.value in types and send_at is None:
        send_at = "07:00"

    conn.execute(
        "INSERT OR IGNORE INTO watcher_student (watcher_id, student_id) VALUES (?, ?)",
        (watcher.id, student_id),
    )
    added: list[str] = []
    for t in types:
        for ch in chans:
            dup = conn.execute(
                "SELECT 1 FROM subscriptions WHERE watcher_id=? AND student_id=? "
                "AND alert_type=? AND channel=? AND send_at IS ?",
                (watcher.id, student_id, t, ch, send_at),
            ).fetchone()
            if dup:
                continue
            sub_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO subscriptions (id, watcher_id, student_id, alert_type, "
                "channel, send_at, urgent_now) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sub_id, watcher.id, student_id, t, ch, send_at, int(urgent_now)),
            )
            added.append(sub_id)
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


def set_subscription_group(conn: sqlite3.Connection, ids: list[str],
                           alert_types: list[str], channel: str,
                           send_at: Optional[str],
                           urgent_now: bool = False) -> None:
    """Rewrite a displayed subscription row (the dashboard's per-row edit).

    A dashboard row is a GROUP of single-type subscription rows sharing
    (watcher, student, channel, send_at, urgent) — ``ids`` are the group's
    current rows, ``alert_types`` the newly selected set. Types are
    reconciled (insert the new, delete the de-selected, update the kept);
    the watcher⇒student pair is the row's identity and stays."""
    rows = [r for i in ids
            for r in [conn.execute("SELECT * FROM subscriptions WHERE id = ?",
                                   (i,)).fetchone()] if r is not None]
    if not rows:
        raise WatcherError("no such subscription (already removed?)")
    watcher_id, student_id = rows[0]["watcher_id"], rows[0]["student_id"]
    if any(r["watcher_id"] != watcher_id or r["student_id"] != student_id
           for r in rows):
        raise WatcherError("those subscriptions belong to different rows")
    types = list(dict.fromkeys(alert_types)) or [ALL]
    if ALL in types:
        types = [ALL]
    for t in types:
        if t != ALL and t not in VALID_ALERT_TYPES:
            raise WatcherError(
                f"unknown alert type {t!r} (valid: {', '.join(sorted(VALID_ALERT_TYPES))})")
    if send_at is not None:
        send_at = validate_hhmm(send_at)

    existing = {r["alert_type"]: r["id"] for r in rows}
    try:
        for t, sub_id in existing.items():
            if t not in types:
                conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
        for t in types:
            row_send_at = send_at
            if t == AlertType.DAILY_SUMMARY.value and row_send_at is None:
                row_send_at = "07:00"
            dup = conn.execute(
                "SELECT id FROM subscriptions WHERE watcher_id = ? AND student_id = ? "
                "AND alert_type = ? AND channel = ? AND send_at IS ?",
                (watcher_id, student_id, t, channel, row_send_at)).fetchone()
            if dup and dup["id"] not in existing.values():
                raise WatcherError("an identical subscription already exists")
            if t in existing:
                conn.execute(
                    "UPDATE subscriptions SET channel = ?, send_at = ?, urgent_now = ? "
                    "WHERE id = ?",
                    (channel, row_send_at, int(urgent_now), existing[t]))
            else:
                conn.execute(
                    "INSERT INTO subscriptions (id, watcher_id, student_id, alert_type, "
                    "channel, send_at, urgent_now) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (uuid.uuid4().hex, watcher_id, student_id, t, channel,
                     row_send_at, int(urgent_now)))
    except WatcherError:
        conn.rollback()
        raise
    conn.commit()


def remove_subscription(conn: sqlite3.Connection, sub_id: str) -> None:
    """Delete one subscription row by id (the dashboard's per-row remove).
    Also drops the watcher_student link when this was the pair's last row."""
    row = conn.execute(
        "SELECT watcher_id, student_id FROM subscriptions WHERE id = ?", (sub_id,)
    ).fetchone()
    if row is None:
        raise WatcherError("no such subscription (already removed?)")
    conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
    left = conn.execute(
        "SELECT 1 FROM subscriptions WHERE watcher_id = ? AND student_id = ?",
        (row["watcher_id"], row["student_id"])).fetchone()
    if left is None:
        conn.execute(
            "DELETE FROM watcher_student WHERE watcher_id = ? AND student_id = ?",
            (row["watcher_id"], row["student_id"]))
    conn.commit()


def list_subscriptions(conn: sqlite3.Connection) -> list[Subscription]:
    rows = conn.execute(
        "SELECT s.id, s.watcher_id, w.name AS watcher_name, s.student_id, "
        "       st.name AS student_name, s.alert_type, s.channel, s.send_at, "
        "       s.urgent_now "
        "FROM subscriptions s "
        "JOIN watchers w ON w.id = s.watcher_id "
        "JOIN students st ON st.id = s.student_id "
        "ORDER BY w.name, st.name, s.alert_type, s.channel"
    ).fetchall()
    return [Subscription(**{**dict(r), "urgent_now": bool(r["urgent_now"])})
            for r in rows]


def subscriptions_for_student(conn: sqlite3.Connection, student_id: str) -> list[tuple[Watcher, str, str, Optional[str], bool]]:
    """(watcher, alert_type, channel, send_at, urgent_now) tuples that target
    this student. ``daily_summary`` rows are generated content, not event
    routing — the summary sender handles them, so they're excluded here."""
    rows = conn.execute(
        "SELECT w.*, s.alert_type, s.channel, s.send_at, s.urgent_now "
        "FROM subscriptions s "
        "JOIN watchers w ON w.id = s.watcher_id "
        "WHERE s.student_id = ? AND s.alert_type != ?",
        (student_id, AlertType.DAILY_SUMMARY.value),
    ).fetchall()
    return [(_row_to_watcher(r), r["alert_type"], r["channel"], r["send_at"],
             bool(r["urgent_now"]))
            for r in rows]


def summary_subscriptions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All ``daily_summary`` rows, with watcher + student columns joined in."""
    return conn.execute(
        "SELECT s.id AS sub_id, s.channel, s.send_at, s.last_sent_on, "
        "       w.id AS watcher_id, w.name AS watcher_name, w.channels AS watcher_channels, "
        "       st.id AS student_id, st.agu, st.initials, st.school "
        "FROM subscriptions s "
        "JOIN watchers w ON w.id = s.watcher_id "
        "JOIN students st ON st.id = s.student_id "
        "WHERE s.alert_type = ?",
        (AlertType.DAILY_SUMMARY.value,),
    ).fetchall()


def mark_summary_sent(conn: sqlite3.Connection, sub_id: str, sent_on: str) -> None:
    conn.execute("UPDATE subscriptions SET last_sent_on = ? WHERE id = ?",
                 (sent_on, sub_id))
    conn.commit()
