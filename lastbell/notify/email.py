"""Email notifier over SMTP (stdlib only).

Email is the universal default channel: every device on every OS already has it,
no extra app to install. Configure via the LASTBELL_SMTP_* environment vars.
It also carries SMS for free — send to a carrier's email→SMS gateway address
(e.g. ``3015551234@vtext.com``).

Two faces on one transport:

* ``EmailNotifier`` — Phase-1 global fallback with a fixed LASTBELL_SMTP_TO.
* ``EmailChannel`` — Phase-3 per-watcher channel; the recipient comes from the
  watcher's address (``{"to": "kid@example.com"}``) at send time.
"""
from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage


def _need(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise ValueError(f"{key} is required for the email channel.")
    return val


@dataclass
class SmtpTransport:
    host: str
    port: int
    user: str
    password: str
    sender: str

    @classmethod
    def from_env(cls) -> "SmtpTransport":
        return cls(
            host=_need("LASTBELL_SMTP_HOST"),
            port=int(os.environ.get("LASTBELL_SMTP_PORT", "587")),
            user=os.environ.get("LASTBELL_SMTP_USER", ""),
            password=os.environ.get("LASTBELL_PASSWORD_SMTP", ""),
            sender=_need("LASTBELL_SMTP_FROM"),
        )

    def deliver(self, recipient: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.sender
        msg["To"] = recipient
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
            smtp.starttls()
            if self.user:
                smtp.login(self.user, self.password)
            smtp.send_message(msg)


@dataclass
class EmailNotifier:
    """Global fallback: one fixed recipient from LASTBELL_SMTP_TO."""

    transport: SmtpTransport
    recipient: str

    @classmethod
    def from_env(cls) -> "EmailNotifier":
        return cls(transport=SmtpTransport.from_env(),
                   recipient=_need("LASTBELL_SMTP_TO"))

    def send(self, subject: str, body: str) -> None:
        self.transport.deliver(self.recipient, subject, body)


@dataclass
class EmailChannel:
    """Per-watcher channel: recipient comes from the watcher's address dict."""

    name = "email"
    transport: SmtpTransport

    @classmethod
    def from_env(cls) -> "EmailChannel":
        return cls(transport=SmtpTransport.from_env())

    def send(self, to: dict, subject: str, body: str) -> None:
        recipient = to.get("to")
        if not recipient:
            raise ValueError("email channel needs an address: {'to': '…'}")
        self.transport.deliver(recipient, subject, body)
