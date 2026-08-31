"""Password resolution.

Passwords never live in config files, the database, or the source tree — only a
*reference* to where the secret is stored. Two backends:

  keyring  the OS keyring (macOS Keychain, Windows Credential Manager,
           Linux Secret Service) via the ``keyring`` library. Recommended for
           bare-metal installs.
  env      read from ``GRADEWATCH_PASSWORD``, where the value is injected by a
           secret store (Docker secrets, CI). For containerized installs.
"""
from __future__ import annotations

import getpass
import os

SERVICE = "gradewatch"


class SecretError(RuntimeError):
    """Raised when a password can't be resolved."""


def get_password(username: str, backend: str = "keyring") -> str:
    if backend == "env":
        value = os.environ.get("GRADEWATCH_PASSWORD")
        if not value:
            raise SecretError(
                "GRADEWATCH_SECRET_BACKEND=env but GRADEWATCH_PASSWORD is unset."
            )
        return value

    if backend == "keyring":
        try:
            import keyring
        except ImportError as exc:  # pragma: no cover
            raise SecretError(
                "keyring is not installed. `pip install keyring`, or set "
                "GRADEWATCH_SECRET_BACKEND=env."
            ) from exc
        value = keyring.get_password(SERVICE, username)
        if not value:
            raise SecretError(
                f"No password stored for {username!r}. Run: gradewatch set-password"
            )
        return value

    raise SecretError(f"Unknown secret backend: {backend!r}")


def set_password(username: str, password: str) -> None:
    """Store a password in the OS keyring."""
    import keyring

    keyring.set_password(SERVICE, username, password)


def prompt_password(prompt: str = "ParentVUE password (hidden): ") -> str:
    """Read a password from the terminal without echoing it."""
    return getpass.getpass(prompt)
