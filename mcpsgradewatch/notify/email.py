"""Email notifier over SMTP (stdlib only).

Email is the universal default channel: every device on every OS already has it,
no extra app to install. Configure via the MCPSGRADEWATCH_SMTP_* environment vars.
"""
from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage


@dataclass
class EmailNotifier:
    host: str
    port: int
    user: str
    password: str
    sender: str
    recipient: str

    @classmethod
    def from_env(cls) -> "EmailNotifier":
        def need(key: str) -> str:
            val = os.environ.get(key)
            if not val:
                raise ValueError(f"{key} is required for the email channel.")
            return val

        return cls(
            host=need("MCPSGRADEWATCH_SMTP_HOST"),
            port=int(os.environ.get("MCPSGRADEWATCH_SMTP_PORT", "587")),
            user=os.environ.get("MCPSGRADEWATCH_SMTP_USER", ""),
            password=os.environ.get("MCPSGRADEWATCH_PASSWORD_SMTP", ""),
            sender=need("MCPSGRADEWATCH_SMTP_FROM"),
            recipient=need("MCPSGRADEWATCH_SMTP_TO"),
        )

    def send(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.sender
        msg["To"] = self.recipient
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
            smtp.starttls()
            if self.user:
                smtp.login(self.user, self.password)
            smtp.send_message(msg)
