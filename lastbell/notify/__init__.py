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

"sms" is legacy: it rode the carriers' email→SMS gateways, which are gone —
T-Mobile's shut down in December 2024, AT&T's in June 2025, and Verizon is
retiring its by March 2027 with deliveries already being dropped. Nothing
offers the channel any more (withdrawn in 0.1.5); rows created earlier keep
delivering over the email transport rather than silently breaking.
"""
from __future__ import annotations

from typing import Protocol


class Notifier(Protocol):
    def send(self, subject: str, body: str) -> None: ...


class Channel(Protocol):
    name: str

    def send(self, to: dict, subject: str, body: str) -> None: ...


# The key a bare CLI shorthand value fills in, per channel:
#   --channel email=kid@example.com  ->  {"to": "kid@example.com"}
# "sms" stays only so pre-0.1.5 rows still render, update, and deliver.
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

    email (and legacy sms) ride SMTP, so the address must look like
    user@host. A carrier email-to-text gateway is refused outright: those
    are shut down or being retired, and accepting one would mean alerts that
    silently never arrive — the worst failure this tool can have.
    Raises ValueError with the fix; returns the stripped address.
    """
    address = address.strip()
    if channel_name in ("email", "sms"):
        local, sep, domain = address.partition("@")
        if not sep or not local or "." not in domain:
            raise ValueError(
                f"{address!r} doesn't look like an email address (name@example.com)")
        dead = DEAD_GATEWAYS.get(domain.lower())
        if dead:
            raise ValueError(
                f"{address!r} won't deliver: {dead}. Use an email address instead.")
    return address


# Carrier email-to-text gateways, all gone or going. Kept as a refusal list so
# nobody is left waiting for a text that will never come.
DEAD_GATEWAYS = {
    "txt.att.net": "AT&T shut down its email-to-text gateway in June 2025",
    "mms.att.net": "AT&T shut down its email-to-text gateway in June 2025",
    "tmomail.net": "T-Mobile shut down its email-to-text gateway in December 2024",
    "vtext.com": "Verizon is retiring its email-to-text gateway (by March 2027) "
                 "and already drops messages without notice",
    "vzwpix.com": "Verizon is retiring its email-to-text gateway (by March 2027) "
                  "and already drops messages without notice",
}

TEST_SUBJECT = "Last Bell test"
TEST_BODY = ("This is your Last Bell test message. Alerts about your students "
             "will arrive here.")


def send_test(channel_name: str, address: dict) -> None:
    """Send the one-line test message a person can recognize on their phone.
    Used by the setup wizard, `lastbell watcher test`, and the dashboard's
    Test button, so all three prove the same thing the same way. Raises
    whatever the transport raises (missing SMTP settings, network, …)."""
    channel(channel_name).send(address, TEST_SUBJECT, TEST_BODY)


def send_sample(channel_name: str, address: dict) -> None:
    """A realistic alert message with made-up courses — shows what the real
    thing will look like on this channel (HTML on email), with no one's data."""
    from . import render

    subject, body = render.sample()
    channel(channel_name).send(address, subject, body)


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
