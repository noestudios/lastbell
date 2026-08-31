"""Deferred delivery (Phase 4): one outbox behind digests *and* quiet hours.

A planned delivery either goes now or gets a ``send_after`` timestamp:

* subscription ``send_at`` (digest) -> the next occurrence of that HH:MM;
* watcher quiet hours -> pushed past the end of the quiet window;
* both -> digest time first, then nudged out of the quiet window.

Deferred events are queued as rows and the flusher — called every loop tick
and by ``mcpsgradewatch flush`` — groups what's due into one message per
(watcher, channel). Addresses are resolved at *flush* time, so a watcher who
changes their email mid-day gets the digest at the new address. A failed send
stays unsent and simply retries next tick.

All times are naive local time: this runs on the household's own box, and
school deadlines already live in local time.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, time, timedelta
from typing import Optional

from . import notify
from .router import Delivery


def _hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def in_quiet_hours(now: datetime, quiet: dict) -> bool:
    if not quiet.get("start") or not quiet.get("end"):
        return False
    start, end = _hhmm(quiet["start"]), _hhmm(quiet["end"])
    t = now.time()
    if start < end:                    # same-day window, e.g. 13:00–15:00
        return start <= t < end
    return t >= start or t < end       # crosses midnight, e.g. 21:00–07:00


def quiet_end(now: datetime, quiet: dict) -> datetime:
    """The moment the current quiet window ends (call only when inside it)."""
    end = _hhmm(quiet["end"])
    candidate = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def next_occurrence(now: datetime, hhmm: str) -> datetime:
    t = _hhmm(hhmm)
    candidate = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    if candidate < now:
        candidate += timedelta(days=1)
    return candidate


def compute_send_after(now: datetime, send_at: Optional[str],
                       quiet: dict) -> Optional[datetime]:
    """When this delivery may go out. None means "right now"."""
    moment = now if send_at is None else next_occurrence(now, send_at)
    if in_quiet_hours(moment, quiet):
        moment = quiet_end(moment, quiet)
    return None if moment <= now else moment


# ── queue + flush ─────────────────────────────────────────────────────


def enqueue(conn: sqlite3.Connection, delivery: Delivery,
            send_after: datetime) -> int:
    """Queue a delivery's events as outbox rows. Duplicate unsent rows (same
    watcher/channel/detail) are skipped — a digest-subscribed event would
    otherwise re-queue on every poll until the digest goes out."""
    added = 0
    for e in delivery.events:
        dup = conn.execute(
            "SELECT 1 FROM outbox WHERE watcher_id=? AND channel=? AND detail=? "
            "AND sent_at IS NULL",
            (delivery.watcher_id, delivery.channel, e.detail),
        ).fetchone()
        if dup:
            continue
        conn.execute(
            "INSERT INTO outbox (id, watcher_id, channel, student_id, alert_type, "
            "detail, send_after) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, delivery.watcher_id, delivery.channel,
             e.student_agu, e.type.value, e.detail,
             send_after.isoformat(sep=" ", timespec="seconds")),
        )
        added += 1
    conn.commit()
    return added


def flush_due(conn: sqlite3.Connection, now: Optional[datetime] = None,
              channel_factory=notify.channel) -> tuple[int, list[str]]:
    """Send every due outbox group. Returns (messages sent, warnings)."""
    now = now or datetime.now()
    rows = conn.execute(
        "SELECT o.*, st.initials FROM outbox o "
        "JOIN students st ON st.id = o.student_id "
        "WHERE o.sent_at IS NULL AND o.send_after <= ? "
        "ORDER BY o.queued_at, o.rowid",
        (now.isoformat(sep=" ", timespec="seconds"),),
    ).fetchall()
    if not rows:
        return 0, []

    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault((r["watcher_id"], r["channel"]), []).append(r)

    sent = 0
    warnings: list[str] = []
    transports: dict[str, notify.Channel] = {}
    for (watcher_id, channel_name), group in groups.items():
        w = conn.execute("SELECT * FROM watchers WHERE id = ?", (watcher_id,)).fetchone()
        if w is None:
            continue  # watcher deleted since queueing; cascade already cleaned up
        addresses = json.loads(w["channels"] or "{}")
        to = addresses.get(channel_name)
        if to is None and channel_name != "console":
            warnings.append(
                f"outbox: watcher {w['name']!r} has no {channel_name} address; "
                f"{len(group)} item(s) held")
            continue
        try:
            ch = transports.get(channel_name)
            if ch is None:
                ch = transports[channel_name] = channel_factory(channel_name)
            ch.send(to or {}, _subject_for(group), _body_for(group))
            ids = [r["id"] for r in group]
            conn.executemany(
                "UPDATE outbox SET sent_at = datetime('now') WHERE id = ?",
                [(i,) for i in ids])
            conn.commit()
            sent += 1
        except Exception as e:
            warnings.append(
                f"outbox delivery to {w['name']!r} via {channel_name} failed "
                f"(will retry): {e}")
    return sent, warnings


def pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT o.*, w.name AS watcher_name, st.initials FROM outbox o "
        "JOIN watchers w ON w.id = o.watcher_id "
        "JOIN students st ON st.id = o.student_id "
        "WHERE o.sent_at IS NULL ORDER BY o.send_after").fetchall()


def _subject_for(group: list[sqlite3.Row]) -> str:
    initials = sorted({r["initials"] or r["student_id"] for r in group})
    n = len(group)
    if len(initials) == 1:
        return f"[MCPSGradeWatch] {n} update{'s' if n != 1 else ''} for {initials[0]}"
    return f"[MCPSGradeWatch] digest: {n} updates for {', '.join(initials)}"


def _body_for(group: list[sqlite3.Row]) -> str:
    by_student: dict[str, list[sqlite3.Row]] = {}
    for r in group:
        by_student.setdefault(r["initials"] or r["student_id"], []).append(r)
    if len(by_student) == 1:
        (rows,) = by_student.values()
        return "\n".join(f"• {r['detail']}" for r in rows)
    parts = []
    for initials, rows in sorted(by_student.items()):
        parts.append(initials)
        parts.extend(f"• {r['detail']}" for r in rows)
    return "\n".join(parts)
