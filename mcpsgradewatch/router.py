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


@dataclass
class Delivery:
    watcher_name: str
    channel: str
    to: dict
    events: list[Event] = field(default_factory=list)


def plan(conn: sqlite3.Connection, student_id: str,
         events: list[Event]) -> tuple[list[Delivery], list[str]]:
    """Match this student's events against subscriptions.

    Returns (deliveries, warnings). An empty deliveries list with no
    subscriptions at all means "fall back to the global notifier"; the caller
    can tell that apart via ``has_subscriptions``.
    """
    warnings: list[str] = []
    deliveries: dict[tuple[str, str], Delivery] = {}

    for watcher, alert_type, channel_name in watchers.subscriptions_for_student(conn, student_id):
        matched = [e for e in events
                   if alert_type == watchers.ALL or e.type.value == alert_type]
        if not matched:
            continue
        if channel_name == watchers.ALL:
            channel_names = list(watcher.channels) or ["console"]
        else:
            channel_names = [channel_name]
        for ch in channel_names:
            to = watcher.channels.get(ch)
            if to is None and ch != "console":
                warnings.append(
                    f"watcher {watcher.name!r} is subscribed via {ch!r} but has no "
                    f"{ch} address — set one with: mcpsgradewatch watcher set-channel "
                    f"{watcher.name} {ch}=…")
                continue
            d = deliveries.setdefault(
                (watcher.id, ch),
                Delivery(watcher_name=watcher.name, channel=ch, to=to or {}))
            for e in matched:
                if e not in d.events:
                    d.events.append(e)
    return list(deliveries.values()), warnings


def has_subscriptions(conn: sqlite3.Connection, student_id: str) -> bool:
    return bool(watchers.subscriptions_for_student(conn, student_id))


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
                f"delivery to {d.watcher_name!r} via {d.channel} failed: {e}")
    return sent, warnings


def subject(student_initials: str, events: list[Event]) -> str:
    n = len(events)
    return f"[MCPSGradeWatch] {n} update{'s' if n != 1 else ''} for {student_initials}"
