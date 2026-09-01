"""ntfy channel — push notifications to any phone via https://ntfy.sh or a
self-hosted ntfy server. Zero-signup: a watcher just subscribes to a topic in
the ntfy app, and their address here is ``{"topic": "my-secret-topic"}``.

Topic names are effectively passwords (anyone who knows one can read it), so
pick unguessable topics. LASTBELL_NTFY_SERVER overrides the public server;
LASTBELL_NTFY_TOKEN adds auth for protected self-hosted instances.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import requests


@dataclass
class NtfyChannel:
    name = "ntfy"
    server: str
    token: str = ""

    @classmethod
    def from_env(cls) -> "NtfyChannel":
        return cls(
            server=os.environ.get("LASTBELL_NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
            token=os.environ.get("LASTBELL_NTFY_TOKEN", ""),
        )

    def send(self, to: dict, subject: str, body: str) -> None:
        topic = to.get("topic")
        if not topic:
            raise ValueError("ntfy channel needs an address: {'topic': '…'}")
        server = (to.get("server") or self.server).rstrip("/")
        # HTTP headers are latin-1; the body is UTF-8 and carries the detail,
        # so a lossy title is cosmetic only.
        headers = {
            "Title": subject.encode("latin-1", "replace").decode("latin-1"),
            "Tags": "school",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        resp = requests.post(f"{server}/{topic}", data=body.encode("utf-8"),
                             headers=headers, timeout=30)
        resp.raise_for_status()
