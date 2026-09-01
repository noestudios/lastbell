"""Daily student summaries (Phase 4).

A summary is *generated* content — where things stand right now — not a batch
of queued events (that's the outbox's digest). Subscribe with
``--types daily_summary --at 07:00``; the sender fires once per day per
subscription, on the first tick after the scheduled time. ``last_sent_on``
on the subscription row is the whole dedup story.

Summaries respect the low-PII rule like every other payload: initials, never
a child's full name.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from typing import Optional

from . import notify, watchers


def build(conn: sqlite3.Connection, student_id: str, initials: str,
          *, lookahead_days: int = 7, today: Optional[date] = None) -> str:
    """The summary body for one student: overall marks, open problems,
    what's coming, and recent alerts nobody has acked yet."""
    today = today or date.today()
    lines: list[str] = []

    from .models import format_percent

    # Scope to the current marking period once one is known — a closed
    # quarter's courses and leftover statuses stay out of the daily picture.
    row = conn.execute("SELECT current_term FROM students WHERE id = ?",
                       (student_id,)).fetchone()
    term = (row["current_term"] if row else "") or ""
    term_sql = " AND c.term = ?" if term else ""
    term_args: tuple = (term,) if term else ()

    courses = conn.execute(
        "SELECT * FROM courses c WHERE c.student_id = ?" + term_sql + " ORDER BY title",
        (student_id,) + term_args).fetchall()

    def one_course(c) -> str:
        pct = format_percent(c["percent"])
        shown = f"{pct}%" if pct is not None else (c["percent"] or c["mark"] or "—")
        suffix = f" ({c['mark']})" if shown != c["mark"] and c["mark"] else ""
        return f"{c['title']} {shown}{suffix}"

    overall = "; ".join(one_course(c) for c in courses)
    lines.append(f"Overall: {overall or 'no courses yet'}")

    def open_items(status: str) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT a.name, a.due_date, c.title FROM assignments a "
            "JOIN courses c ON c.id = a.course_id "
            "WHERE c.student_id = ? AND a.status = ?" + term_sql +
            " ORDER BY a.due_date IS NULL, a.due_date",
            (student_id, status) + term_args
        ).fetchall()

    missing = open_items("missing")
    if missing:
        lines.append(f"Missing ({len(missing)}):")
        lines.extend(f"  • {r['title']}: “{r['name']}”" for r in missing)

    past_due = open_items("ungraded_past_due")
    if past_due:
        lines.append(f"Ungraded past due ({len(past_due)}):")
        lines.extend(
            f"  • {r['title']}: “{r['name']}”"
            + (f" (was due {r['due_date']})" if r["due_date"] else "")
            for r in past_due)

    horizon = (today + timedelta(days=lookahead_days)).isoformat()
    upcoming = conn.execute(
        "SELECT a.name, a.due_date, c.title FROM assignments a "
        "JOIN courses c ON c.id = a.course_id "
        "WHERE c.student_id = ? AND a.status = 'due' "
        "AND a.due_date IS NOT NULL AND a.due_date >= ? AND a.due_date <= ?"
        + term_sql + " ORDER BY a.due_date",
        (student_id, today.isoformat(), horizon) + term_args
    ).fetchall()
    if upcoming:
        lines.append(f"Due in the next {lookahead_days} days ({len(upcoming)}):")
        lines.extend(f"  • {r['title']}: “{r['name']}” due {r['due_date']}"
                     for r in upcoming)

    if not (missing or past_due or upcoming):
        lines.append("Nothing missing, nothing overdue, nothing due soon. 🎉")

    unacked = conn.execute(
        "SELECT type, body, created_at FROM alerts "
        "WHERE student_id = ? AND acked_at IS NULL "
        "AND created_at >= datetime('now', '-7 days') "
        "ORDER BY created_at DESC LIMIT 10", (student_id,)
    ).fetchall()
    if unacked:
        lines.append(f"Unacked alerts this week ({len(unacked)}):")
        for r in unacked:
            try:
                detail = json.loads(r["body"]).get("detail", "")
            except Exception:
                detail = r["body"]
            lines.append(f"  • {detail}")

    return "\n".join(lines)


def send_due(conn: sqlite3.Connection, *, lookahead_days: int = 7,
             now: Optional[datetime] = None,
             channel_factory=notify.channel) -> tuple[int, list[str]]:
    """Send every daily_summary subscription whose time has come today.

    Explicitly scheduled, so quiet hours don't apply — a 07:00 summary is a
    choice, not an interruption.
    """
    now = now or datetime.now()
    today_str = now.date().isoformat()
    sent = 0
    warnings: list[str] = []
    transports: dict[str, notify.Channel] = {}

    for sub in watchers.summary_subscriptions(conn):
        send_at = sub["send_at"] or "07:00"
        h, m = (int(x) for x in send_at.split(":"))
        if (now.hour, now.minute) < (h, m) or sub["last_sent_on"] == today_str:
            continue
        addresses = json.loads(sub["watcher_channels"] or "{}")
        channel_names = (list(addresses) or ["console"]) \
            if sub["channel"] == watchers.ALL else [sub["channel"]]
        body = build(conn, sub["student_id"], sub["initials"],
                     lookahead_days=lookahead_days, today=now.date())
        subject = f"[MCPSGradeWatch] Daily summary for {sub['initials'] or sub['agu']}"
        delivered = False
        for ch_name in channel_names:
            to = addresses.get(ch_name)
            if to is None and ch_name != "console":
                warnings.append(
                    f"summary for {sub['watcher_name']!r}: no {ch_name} address")
                continue
            try:
                ch = transports.get(ch_name)
                if ch is None:
                    ch = transports[ch_name] = channel_factory(ch_name)
                ch.send(to or {}, subject, body)
                delivered = True
                sent += 1
            except Exception as e:
                warnings.append(
                    f"summary to {sub['watcher_name']!r} via {ch_name} failed "
                    f"(will retry next tick): {e}")
        if delivered:
            # Partial success counts as sent — retrying would double-send the
            # channels that worked; the failure is warned and visible.
            watchers.mark_summary_sent(conn, sub["sub_id"], today_str)
    return sent, warnings
