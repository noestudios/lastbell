"""Telegram channel — messages from your own bot.

One-time setup: create a bot with @BotFather, put its token in
LASTBELL_TELEGRAM_TOKEN, have each watcher message the bot once (bots
can't initiate), and read their chat id from ``getUpdates``. The watcher's
address is ``{"chat_id": "123456789"}``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import requests


@dataclass
class TelegramChannel:
    name = "telegram"
    token: str

    @classmethod
    def from_env(cls) -> "TelegramChannel":
        token = os.environ.get("LASTBELL_TELEGRAM_TOKEN")
        if not token:
            raise ValueError(
                "LASTBELL_TELEGRAM_TOKEN is required for the telegram channel "
                "(create a bot with @BotFather).")
        return cls(token=token)

    def send(self, to: dict, subject: str, body: str) -> None:
        chat_id = to.get("chat_id")
        if not chat_id:
            raise ValueError("telegram channel needs an address: {'chat_id': '…'}")
        resp = requests.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json={"chat_id": chat_id, "text": f"{subject}\n{body}"},
            timeout=30,
        )
        resp.raise_for_status()
