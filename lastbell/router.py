"""Event routing (Phase 3): diff events -> per-watcher deliveries.

``plan`` is pure DB + matching (easy to test); ``dispatch`` does the actual
sends through the channel registry. A subscription row is
(watcher, student, alert_type, channel) where either filter may be ``'*'``;
a ``'*'`` channel expands to every channel the watcher has an address for.

One delivery per (watcher, channel) per poll: a watcher subscribed to three
alert types still gets *one* message listing all of that student's events.
Transports are built once per dispatch and reused across deliveries.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import notify, watchers
from .differ import Event
from .models import URGENT_ALERT_TYPES


@dataclass
class Delivery:
    watcher_name: str
    channel: str
    to: dict
    events: list[Event] = field(default_factory=list)
    # Phase 4 scheduling context:
    watcher_id: str = ""
    quiet_hours: dict = field(default_factory=dict)
    send_at: str | None = None   # HH:MM digest time; None = immediate


def plan(conn: sqlite3.Connection, student_id: str,
         events: list[Event]) -> tuple[list[Delivery], list[str]]:
    """Match this student's events against subscriptions.

    Returns (deliveries, warnings). Each delivery carries its schedule: an
    event matched by both an immediate and a digest subscription goes once,
    immediately (the sooner schedule wins). An empty deliveries list with no
    subscriptions at all means "fall back to the global notifier"; the caller
    can tell that apart via ``has_subscriptions``.
    """
    warnings: list[str] = []
    # (watcher_id, channel) -> {event id: (event, best send_at)}; None beats
    # any HH:MM, and earlier HH:MM beats later (zero-padded strings compare).
    matched_map: dict[tuple[str, str], dict[int, tuple[Event, str | None]]] = {}
    context: dict[str, watchers.Watcher] = {}

    for watcher, alert_type, channel_name, send_at, urgent_now in \
            watchers.subscriptions_for_student(conn, student_id):
        matched = [e for e in events
                   if alert_type == watchers.ALL or e.type.value == alert_type]
        if not matched:
            continue
        context[watcher.id] = watcher
        if channel_name == watchers.ALL:
            channel_names = list(watcher.channels) or ["console"]
        else:
            channel_names = [channel_name]
        for ch in channel_names:
            if watcher.channels.get(ch) is None and ch != "console":
                warnings.append(
                    f"watcher {watcher.name!r} is subscribed via {ch!r} but has no "
                    f"{ch} address — set one with: lastbell watcher set-channel "
                    f"{watcher.name} {ch}=…")
                continue
            slot = matched_map.setdefault((watcher.id, ch), {})
            for e in matched:
                # An urgent-flagged row sends its urgent types now, not at
                # the digest hour (quiet hours still defer downstream).
                effective = None if (urgent_now and e.type in URGENT_ALERT_TYPES) \
                    else send_at
                prev = slot.get(id(e))
                if prev is None or _sooner(effective, prev[1]):
                    slot[id(e)] = (e, effective)

    deliveries: list[Delivery] = []
    for (watcher_id, ch), slot in matched_map.items():
        w = context[watcher_id]
        by_schedule: dict[str | None, list[Event]] = {}
        for e, send_at in slot.values():
            by_schedule.setdefault(send_at, []).append(e)
        for send_at, evs in by_schedule.items():
            deliveries.append(Delivery(
                watcher_name=w.name, channel=ch, to=w.channels.get(ch) or {},
                events=evs, watcher_id=watcher_id,
                quiet_hours=w.quiet_hours, send_at=send_at))
    return deliveries, warnings


def _sooner(a: str | None, b: str | None) -> bool:
    """Is schedule ``a`` strictly sooner than ``b``? (None = immediate.)"""
    if a is None:
        return b is not None
    return b is not None and a < b


def has_subscriptions(conn: sqlite3.Connection, student_id: str) -> bool:
    """Any subscription row at all — including daily_summary, which routes no
    events but still means "this household opted into targeted delivery", so
    the global fallback must stay quiet."""
    return conn.execute(
        "SELECT 1 FROM subscriptions WHERE student_id = ? LIMIT 1", (student_id,)
    ).fetchone() is not None


def dispatch(deliveries: list[Delivery], student_initials: str,
             channel_factory=notify.channel) -> tuple[int, list[str]]:
    """Send every delivery; a failing channel is a warning, not a crash —
    one watcher's dead bot token must not silence the others."""
    sent = 0
    warnings: list[str] = []
    transports: dict[str, notify.Channel] = {}
    for d in deliveries:
        try:
            ch = transports.get(d.channel)
            if ch is None:
                ch = transports[d.channel] = channel_factory(d.channel)
            ch.send(d.to, subject(student_initials, d.events),
                    "\n".join(f"• {e.detail}" for e in d.events))
            sent += 1
        except Exception as e:
            warnings.append(
                f"couldn't deliver to {d.watcher_name!r} via {d.channel} ({e}) "
                f"— this message won't be retried, but the alerts stay in the "
                f"dashboard's alert log")
    return sent, warnings


def subject(student_initials: str, events: list[Event]) -> str:
    n = len(events)
    return f"[Last Bell] {n} update{'s' if n != 1 else ''} for {student_initials}"
