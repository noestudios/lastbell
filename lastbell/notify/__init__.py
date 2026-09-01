"""Pluggable notification channels.

Delivery is push-*out*: a channel sends to a person's inbox/phone. No one signs
into lastbell to receive an alert. Payloads are low-PII by default (initials +
course + score, never a child's full name).

Two layers:

* ``Channel`` (Phase 3) — a transport built once from environment secrets
  (SMTP host, bot token, …) that can deliver to *any* watcher's address:
  ``send(to, subject, body)`` where ``to`` is the watcher's address dict
  (``{"to": "a@b.c"}``, ``{"topic": "…"}``, ``{"chat_id": "…"}``, …).
* ``Notifier`` (Phase 1) — the single global fallback used when no watcher is
  subscribed to a student; kept so a bare single-user install needs zero setup.

SMS rides the email channel: every US carrier exposes an email→SMS gateway
(e.g. ``3015551234@vtext.com``), so a watcher's "sms" is just an email address.
"""
from __future__ import annotations

from typing import Optional, Protocol


class Notifier(Protocol):
    def send(self, subject: str, body: str) -> None: ...


class Channel(Protocol):
    name: str

    def send(self, to: dict, subject: str, body: str) -> None: ...


# The key a bare CLI shorthand value fills in, per channel:
#   --channel email=kid@example.com  ->  {"to": "kid@example.com"}
# "sms" IS the email transport (carrier email→SMS gateway address) but keeps
# its own name so a phone number and an inbox stay two separate fields.
ADDRESS_KEY = {
    "email": "to",
    "sms": "to",
    "ntfy": "topic",
    "telegram": "chat_id",
    "pushover": "user_key",
    "console": None,   # console needs no address
}

CHANNEL_NAMES = tuple(ADDRESS_KEY)


def validate_address(channel_name: str, address: str) -> str:
    """Sanity-check a channel address at entry time (dashboard and CLI).

    email and sms ride SMTP, so the address must look like user@host. The
    sms case is the one people get wrong: a bare phone number can't be
    delivered — texting goes through the carrier's email→SMS gateway.
    Raises ValueError with the fix; returns the stripped address.
    """
    address = address.strip()
    if channel_name in ("email", "sms"):
        local, sep, domain = address.partition("@")
        if not sep or not local or "." not in domain:
            if channel_name == "sms":
                raise ValueError(
                    f"{address!r} can't receive texts — use your carrier's "
                    f"email-to-SMS gateway address, e.g. 5551234567@vtext.com "
                    f"(Verizon), 5551234567@txt.att.net (AT&T), or "
                    f"5551234567@tmomail.net (T-Mobile)")
            raise ValueError(
                f"{address!r} doesn't look like an email address (name@example.com)")
    return address


def channel(name: str) -> Channel:
    """Build a per-watcher channel from environment config. Raises ValueError
    with a setup hint when the transport's secrets are missing."""
    if name == "console":
        from .console import ConsoleNotifier

        return ConsoleChannel(ConsoleNotifier())
    if name in ("email", "sms"):   # sms = email over the carrier gateway
        from .email import EmailChannel

        return EmailChannel.from_env()
    if name == "ntfy":
        from .ntfy import NtfyChannel

        return NtfyChannel.from_env()
    if name == "telegram":
        from .telegram import TelegramChannel

        return TelegramChannel.from_env()
    if name == "pushover":
        from .pushover import PushoverChannel

        return PushoverChannel.from_env()
    raise ValueError(
        f"Unknown channel {name!r} (valid: {', '.join(CHANNEL_NAMES)})")


class ConsoleChannel:
    """Console as a routable channel — handy for dry-running subscriptions."""

    name = "console"

    def __init__(self, inner) -> None:
        self._inner = inner

    def send(self, to: dict, subject: str, body: str) -> None:
        self._inner.send(subject, body)


def get(channel_name: str) -> Notifier:
    """The Phase-1 global fallback notifier (console/email via *_TO env)."""
    if channel_name == "console":
        from .console import ConsoleNotifier

        return ConsoleNotifier()
    if channel_name == "email":
        from .email import EmailNotifier

        return EmailNotifier.from_env()
    raise ValueError(
        f"Unknown notify channel: {channel_name!r} (the global fallback supports "
        f"console and email; per-watcher channels are configured with "
        f"`lastbell watcher` + `subscribe`)")
