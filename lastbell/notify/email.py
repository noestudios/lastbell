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

import imaplib
import logging
import os
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr

log = logging.getLogger("lastbell")

# Gmail sometimes answers the end of a message with "451 4.3.0 temporarily
# rejected" after it has already accepted and delivered it (seen 2026-09-10
# and 2026-11-09). A retry then lands a second copy: Gmail does not merge two
# submissions with the same Message-ID. So after a 4xx from Gmail's SMTP
# server, Last Bell asks Gmail's IMAP server — same account, same app
# password — whether that Message-ID is in Sent Mail, and only retries when
# it isn't. Read-only: it searches one folder for one header, nothing else.
GMAIL_SMTP_HOSTS = ("smtp.gmail.com", "smtp.googlemail.com")
GMAIL_IMAP_HOST = "imap.gmail.com"
SENT_CHECK_TRIES = 3          # Sent Mail can lag the submission by seconds
SENT_CHECK_WAIT_S = 5.0


def _need(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise ValueError(f"{key} is required for the email channel.")
    return val


def build_message(sender: str, recipient: str, subject: str, body: str) -> EmailMessage:
    """Plain text always; when the body is a ``render.Message`` carrying an
    HTML twin, send both as multipart/alternative so a mail client shows the
    styled version and everything else falls back to the text."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(str(body))
    html = getattr(body, "html", "")
    if html:
        msg.add_alternative(html, subtype="html")
    # Every message carries a Message-ID of its own, so a submission can be
    # looked up afterwards (see GMAIL_SMTP_HOSTS). A digest or summary
    # passes a stable token, so each retry of it is the same message.
    domain = parseaddr(sender)[1].rpartition("@")[2] or "lastbell.invalid"
    token = getattr(body, "message_id", "")
    msg["Message-ID"] = f"<{token}@{domain}>" if token else make_msgid("lastbell", domain)
    return msg


def _sent_folder(imap: imaplib.IMAP4) -> str | None:
    """The mailbox flagged \\Sent (its name is localized: "[Gmail]/Sent
    Mail", "[Gmail]/Gesendet", …), quoted as LIST printed it."""
    _, lines = imap.list()
    for line in lines or []:
        if not isinstance(line, bytes):
            continue
        m = re.match(rb'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$', line)
        if m and b"\\Sent" in m.group("flags").split():
            return m.group("name").decode("utf-8", "replace")
    return None


def gmail_sent_has(user: str, password: str, message_id: str, *,
                   tries: int | None = None, wait_s: float | None = None,
                   imap_factory=None) -> bool:
    """Whether Gmail's Sent Mail holds a message with this Message-ID.
    Raises on any IMAP trouble — the caller treats that as "don't know"."""
    tries = SENT_CHECK_TRIES if tries is None else tries
    wait_s = SENT_CHECK_WAIT_S if wait_s is None else wait_s
    context = ssl.create_default_context()
    factory = imap_factory or (lambda: imaplib.IMAP4_SSL(
        GMAIL_IMAP_HOST, 993, ssl_context=context, timeout=30))
    imap = factory()
    try:
        imap.login(user, password)
        folder = _sent_folder(imap)
        if folder is None:
            raise imaplib.IMAP4.error("no mailbox is flagged as Sent")
        for attempt in range(tries):
            if attempt:
                time.sleep(wait_s)
            imap.select(folder, readonly=True)   # re-select: see new arrivals
            _, data = imap.search(None, "HEADER", "Message-ID", message_id)
            if data and data[0] and data[0].split():
                return True
        return False
    finally:
        try:
            imap.logout()
        except Exception:
            pass


@dataclass
class SmtpTransport:
    host: str
    port: int
    user: str
    password: str
    sender: str

    @classmethod
    def from_env(cls) -> SmtpTransport:
        from .. import secrets as secretstore

        return cls(
            host=_need("LASTBELL_SMTP_HOST"),
            port=int(os.environ.get("LASTBELL_SMTP_PORT", "587")),
            user=os.environ.get("LASTBELL_SMTP_USER", ""),
            password=secretstore.get_smtp_password(),
            sender=_need("LASTBELL_SMTP_FROM"),
        )

    def deliver(self, recipient: str, subject: str, body: str) -> None:
        msg = build_message(self.sender, recipient, subject, body)
        # A verifying context, explicitly: smtplib's default for STARTTLS is
        # the *unverified* stdlib context, which would let anyone between
        # this box and the mail server present any certificate and read the
        # SMTP password. Port 465 is implicit TLS (SMTPS); everything else
        # upgrades with STARTTLS. Both verify the server against the OS roots.
        context = ssl.create_default_context()
        if self.port == 465:
            smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=30, context=context)
        else:
            smtp = smtplib.SMTP(self.host, self.port, timeout=30)
        with smtp:
            if self.port != 465:
                smtp.starttls(context=context)
            if self.user:
                smtp.login(self.user, self.password)
            try:
                smtp.send_message(msg)
            except smtplib.SMTPDataError as e:
                if not (400 <= e.smtp_code < 500 and self.can_check_sent):
                    raise
                if not self._sent_anyway(msg["Message-ID"]):
                    raise
                log.warning(f"Gmail answered {e.smtp_code} for {subject!r} but the "
                            f"message is in Sent Mail — delivered; not resending")

    @property
    def can_check_sent(self) -> bool:
        return self.host.lower() in GMAIL_SMTP_HOSTS and bool(self.user and self.password)

    def _sent_anyway(self, message_id: str) -> bool:
        """After a 4xx from Gmail: did the message go out regardless? Any
        doubt (IMAP off, a network error) answers no, so it is retried."""
        try:
            return gmail_sent_has(self.user, self.password, message_id)
        except Exception as e:
            log.warning(f"couldn't check Gmail's Sent Mail after a temporary "
                        f"rejection ({e.__class__.__name__}: {e}); retrying the send")
            return False


@dataclass
class EmailNotifier:
    """Global fallback: one fixed recipient from LASTBELL_SMTP_TO."""

    transport: SmtpTransport
    recipient: str

    @classmethod
    def from_env(cls) -> EmailNotifier:
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
    def from_env(cls) -> EmailChannel:
        return cls(transport=SmtpTransport.from_env())

    def send(self, to: dict, subject: str, body: str) -> None:
        recipient = to.get("to")
        if not recipient:
            raise ValueError("email channel needs an address: {'to': '…'}")
        self.transport.deliver(recipient, subject, body)
