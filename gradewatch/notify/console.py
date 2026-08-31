"""Console notifier — prints to stdout. The zero-config default for dev."""
from __future__ import annotations


class ConsoleNotifier:
    def send(self, subject: str, body: str) -> None:
        print(f"\n── notify ──────────────────────────\n{subject}\n{body}\n")
