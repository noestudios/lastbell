"""Pluggable notification channels.

Delivery is push-*out*: a channel sends to a person's inbox/phone. No one signs
into mcpsgradewatch to receive an alert. Payloads are low-PII by default (initials +
course + score, never a child's full name).

Add a channel by implementing ``Notifier.send`` and registering it in ``get``.
"""
from __future__ import annotations

from typing import Protocol


class Notifier(Protocol):
    def send(self, subject: str, body: str) -> None: ...


def get(channel: str) -> Notifier:
    if channel == "console":
        from .console import ConsoleNotifier

        return ConsoleNotifier()
    if channel == "email":
        from .email import EmailNotifier

        return EmailNotifier.from_env()
    # Phase 3 channels: ntfy, telegram, pushover, sms
    raise ValueError(f"Unknown notify channel: {channel!r}")
