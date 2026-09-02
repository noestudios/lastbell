"""Password resolution.

Passwords never live in config files, the database, or the source tree — only a
*reference* to where the secret is stored. Two backends:

  keyring  the OS keyring (macOS Keychain, Windows Credential Manager,
           Linux Secret Service) via the ``keyring`` library. Recommended for
           bare-metal installs.
  env      read from ``LASTBELL_PASSWORD``, where the value is injected by a
           secret store (Docker secrets, CI). For containerized installs.
"""
from __future__ import annotations

import getpass
import os

SERVICE = "lastbell"


class SecretError(RuntimeError):
    """Raised when a password can't be resolved."""


def get_password(username: str, backend: str = "keyring") -> str:
    if backend == "env":
        value = os.environ.get("LASTBELL_PASSWORD")
        if not value:
            raise SecretError(
                "LASTBELL_SECRET_BACKEND=env but LASTBELL_PASSWORD is unset."
            )
        return value

    if backend == "keyring":
        try:
            import keyring
        except ImportError as exc:  # pragma: no cover
            raise SecretError(
                "keyring is not installed. `pip install keyring`, or set "
                "LASTBELL_SECRET_BACKEND=env."
            ) from exc
        value = keyring.get_password(SERVICE, username)
        if not value:
            raise SecretError(
                f"No password stored for {username!r}. Run: lastbell set-password"
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


# The SMTP account password gets its own keyring slot so `lastbell setup` can
# keep it out of the env file too. The env var (Docker/CI injection) wins.
SMTP_ACCOUNT = "smtp"


def get_smtp_password() -> str:
    value = os.environ.get("LASTBELL_PASSWORD_SMTP")
    if value:
        return value
    try:
        import keyring

        return keyring.get_password(SERVICE, SMTP_ACCOUNT) or ""
    except Exception:  # no keyring backend: fall back to "no password"
        return ""


def set_smtp_password(password: str) -> None:
    import keyring

    keyring.set_password(SERVICE, SMTP_ACCOUNT, password)
