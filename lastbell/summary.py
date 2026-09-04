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

from . import notify, store, watchers
from .differ import MISSING_PHRASE, compose, due_phrase, past_due_phrase
from .models import format_percent
from .notify import render


def build(conn: sqlite3.Connection, student_id: str, initials: str,
          *, lookahead_days: int = 7, today: date | None = None,
          grace_days: int = 3):
    """The summary body for one student: overall marks, open problems,
    what's coming, and the week's recent alerts. A ``render.Message``: plain
    text for every channel, with the HTML twin email attaches."""
    today = today or date.today()
    lines: list[str] = []
    html_groups: list[tuple[str, str, list[dict]]] = []

    def line(course: str, item: str, what: str) -> dict:
        return {"course": course, "item": item, "what": what, "via": "",
                "detail": compose(course, what, item)}

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
    overall_rows = [(c["title"], one_course(c)[len(c["title"]) + 1:]) for c in courses]

    def open_items(status: str) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT a.name, a.due_date, c.title FROM assignments a "
            "JOIN courses c ON c.id = a.course_id "
            "WHERE c.student_id = ? AND a.status = ? AND " + store.NOT_SUPERSEDED_SQL
            + term_sql +
            " ORDER BY a.due_date IS NULL, a.due_date",
            (student_id, status) + term_args
        ).fetchall()

    missing = open_items("missing")
    if missing:
        lines.append(f"Missing ({len(missing)}):")
        # Under a "Missing" heading the sentence's verb goes without saying.
        lines.extend(f"  • {compose(r['title'], '', r['name'])}" for r in missing)
        html_groups.append((f"Missing ({len(missing)})", "Needs attention",
                            [line(r["title"], r["name"], MISSING_PHRASE) for r in missing]))

    past_due = open_items("ungraded_past_due")
    if past_due:
        lines.append(f"Ungraded past due ({len(past_due)}):")
        for r in past_due:
            when = f"(was due {r['due_date']})" if r["due_date"] else ""
            lines.append(f"  • {compose(r['title'], when, r['name'])}")
        html_groups.append((f"Ungraded past due ({len(past_due)})", "Slipping",
                            [line(r["title"], r["name"], past_due_phrase(r["due_date"]))
                             for r in past_due]))

    horizon = (today + timedelta(days=lookahead_days)).isoformat()
    upcoming = conn.execute(
        "SELECT a.name, a.due_date, c.title FROM assignments a "
        "JOIN courses c ON c.id = a.course_id "
        "WHERE c.student_id = ? AND a.status = 'due' "
        "AND a.due_date IS NOT NULL AND a.due_date >= ? AND a.due_date <= ? AND "
        + store.NOT_SUPERSEDED_SQL + term_sql + " ORDER BY a.due_date",
        (student_id, today.isoformat(), horizon) + term_args
    ).fetchall()
    if upcoming:
        lines.append(f"Due in the next {lookahead_days} days ({len(upcoming)}):")
        details = [line(r["title"], r["name"], due_phrase(r["due_date"])) for r in upcoming]
        lines.extend(f"  • {d['detail']}" for d in details)
        html_groups.append((f"Due in the next {lookahead_days} days ({len(upcoming)})",
                            "Coming up", details))

    # Work that was due in the last few days and is still ungraded sits in
    # the grace window: not yet "past due" to the time rules, but gone from
    # "due soon". The dashboard shows it as "yesterday"; so does the summary.
    recent_due = conn.execute(
        "SELECT a.name, a.due_date, c.title FROM assignments a "
        "JOIN courses c ON c.id = a.course_id "
        "WHERE c.student_id = ? AND a.status = 'due' "
        "AND a.due_date IS NOT NULL AND a.due_date < ? AND a.due_date >= ? AND "
        + store.NOT_SUPERSEDED_SQL + term_sql + " ORDER BY a.due_date",
        (student_id, today.isoformat(),
         (today - timedelta(days=grace_days)).isoformat()) + term_args
    ).fetchall()
    if recent_due:
        lines.append(f"Due recently, not yet graded ({len(recent_due)}):")
        for r in recent_due:
            when = f"(was due {r['due_date']})"
            lines.append(f"  • {compose(r['title'], when, r['name'])}")
        html_groups.append((f"Due recently, not yet graded ({len(recent_due)})", "Slipping",
                            [line(r["title"], r["name"], past_due_phrase(r["due_date"]))
                             for r in recent_due]))

    all_clear = not (missing or past_due or upcoming or recent_due)
    if all_clear:
        lines.append("Nothing missing, nothing overdue, nothing due soon. 🎉")

    recent = conn.execute(
        "SELECT type, body, created_at FROM alerts "
        "WHERE student_id = ? "
        "AND created_at >= datetime('now', '-7 days') "
        "ORDER BY created_at DESC LIMIT 10", (student_id,)
    ).fetchall()
    recent_details: list = []
    if recent:
        lines.append(f"Recent alerts this week ({len(recent)}):")
        for r in recent:
            try:
                parts = json.loads(r["body"])
            except ValueError:
                parts = {"detail": r["body"]}
            if not isinstance(parts, dict):
                parts = {"detail": r["body"]}
            lines.append(f"  • {parts.get('detail', '')}")
            recent_details.append(parts)

    return render.Message(
        "\n".join(lines),
        render.summary_html(initials, today, overall_rows, html_groups,
                            recent_details, all_clear))


def send_due(conn: sqlite3.Connection, *, lookahead_days: int = 7,
             now: datetime | None = None,
             channel_factory=notify.channel, grace_days: int = 3) -> tuple[int, list[str]]:
    """Send every daily_summary subscription whose time has come today.

    Explicitly scheduled, so quiet hours don't apply — a 07:00 summary is a
    choice, not an interruption.
    """
    now = now or datetime.now()
    today_str = now.date().isoformat()
    yesterday_str = (now.date() - timedelta(days=1)).isoformat()
    sent = 0
    warnings: list[str] = []
    transports: dict[str, notify.Channel] = {}

    for sub in watchers.summary_subscriptions(conn):
        send_at = sub["send_at"] or "07:00"
        h, m = (int(x) for x in send_at.split(":"))
        last = sub["last_sent_on"]
        # Its slot today has passed, or a whole day was skipped (a poll that
        # ran across midnight past a 23:55 slot, a service that was down):
        # one summary is owed either way, and the day is never silently lost.
        due_today = (now.hour, now.minute) >= (h, m)
        owed = bool(last) and last < yesterday_str
        if last == today_str or not (due_today or owed):
            continue
        addresses = json.loads(sub["watcher_channels"] or "{}")
        channel_names = (list(addresses) or ["console"]) \
            if sub["channel"] == watchers.ALL else [sub["channel"]]
        body = build(conn, sub["student_id"], sub["initials"],
                     lookahead_days=lookahead_days, today=now.date(),
                     grace_days=grace_days)
        subject = f"[Last Bell] Daily summary for {sub['initials'] or sub['agu']}"
        delivered = False
        for ch_name in channel_names:
            to = addresses.get(ch_name)
            if to is None and ch_name != "console":
                warnings.append(
                    f"{sub['watcher_name']!r} has no {ch_name} address for "
                    f"their daily summary — add one with: lastbell watcher "
                    f"set-channel {sub['watcher_name']} {ch_name}=…")
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
                    f"couldn't send {sub['watcher_name']!r}'s daily summary "
                    f"via {ch_name} ({e}) — retries within a minute unless "
                    f"another channel already delivered it")
        if delivered:
            # Partial success counts as sent — retrying would double-send the
            # channels that worked; the failure is warned and visible.
            watchers.mark_summary_sent(conn, sub["sub_id"], today_str)
    return sent, warnings
