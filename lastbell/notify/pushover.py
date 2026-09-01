"""Pushover channel — https://pushover.net (one-time $5 app, no subscription).

LASTBELL_PUSHOVER_TOKEN holds the application token (register one app for
your install); each watcher's address is their own user key:
``{"user_key": "u…"}``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import requests


@dataclass
class PushoverChannel:
    name = "pushover"
    token: str

    @classmethod
    def from_env(cls) -> "PushoverChannel":
        token = os.environ.get("LASTBELL_PUSHOVER_TOKEN")
        if not token:
            raise ValueError(
                "LASTBELL_PUSHOVER_TOKEN is required for the pushover channel "
                "(register an application at pushover.net).")
        return cls(token=token)

    def send(self, to: dict, subject: str, body: str) -> None:
        user_key = to.get("user_key")
        if not user_key:
            raise ValueError("pushover channel needs an address: {'user_key': '…'}")
        resp = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={"token": self.token, "user": user_key,
                  "title": subject, "message": body},
            timeout=30,
        )
        resp.raise_for_status()
