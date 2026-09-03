"""Password resolution.

Passwords never live in config files, the database, or the source tree — only a
*reference* to where the secret is stored. Two backends:

  keyring  the OS keyring (macOS Keychain, Windows Credential Manager,
           Linux Secret Service) via the ``keyring`` library. Recommended for
           bare-metal installs.
  env      read from ``LASTBELL_PASSWORD``, where the value is injected by a
           secret store (Docker secrets, CI) — or, for an always-on box with
           no usable keyring (a headless Pi, a boot-time service that can't
           unlock the desktop keyring), written by ``lastbell setup`` into the
           mode-0600 settings file. That trade-off is stated to the user at
           the moment it's made, never silently.
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
                "LASTBELL_SECRET_BACKEND=env but LASTBELL_PASSWORD is unset. "
                "Run `lastbell setup` (it writes it to the settings file) or "
                "export LASTBELL_PASSWORD."
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
        try:
            value = keyring.get_password(SERVICE, username)
        except Exception as exc:  # NoKeyringError, InitError, a locked daemon …
            raise SecretError(
                f"The OS keyring isn't usable here ({exc.__class__.__name__}: "
                f"{exc}). On a headless or always-on box, re-run `lastbell "
                f"setup` and choose the settings-file store, or set "
                f"LASTBELL_SECRET_BACKEND=env and LASTBELL_PASSWORD."
            ) from exc
        if not value:
            raise SecretError(
                f"No password stored for {username!r}. Run: lastbell set-password "
                f"(or `lastbell setup`; use LASTBELL_SECRET_BACKEND=env on a "
                f"machine without a keyring)"
            )
        return value

    raise SecretError(f"Unknown secret backend: {backend!r} "
                      f"(expected 'keyring' or 'env')")


def keyring_available() -> bool:
    """True when this machine has a keyring backend that can actually store
    something. False for the ``fail`` backend (no Secret Service on a headless
    Linux box, no D-Bus session, …) and for any error just *loading* the
    backend — the wizard then offers the env-file store instead of dying with
    a traceback."""
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:  # pragma: no cover
        return False
    try:
        backend = keyring.get_keyring()
    except Exception:  # NoKeyringError, InitError, …
        return False
    if isinstance(backend, fail.Keyring):
        return False
    # keyring's chainer/null backends report a priority; anything that ends
    # up wrapping only the fail backend also has nowhere to write.
    return getattr(type(backend), "priority", 1) > 0


def set_password(username: str, password: str) -> None:
    """Store a password in the OS keyring."""
    import keyring

    try:
        keyring.set_password(SERVICE, username, password)
    except Exception as exc:  # NoKeyringError, PasswordSetError, …
        raise SecretError(
            f"Couldn't store the password in the OS keyring "
            f"({exc.__class__.__name__}: {exc}). On a headless or always-on "
            f"box, run `lastbell setup` and choose the settings-file store "
            f"(LASTBELL_SECRET_BACKEND=env)."
        ) from exc


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


# A Canvas personal access token is optional: the poll can ride the portal's
# own Canvas link instead. When the district offers tokens (Canvas → Account
# → Settings → New Access Token), storing one here skips that hand-off.
CANVAS_ACCOUNT = "canvas"


def get_canvas_token() -> str:
    value = os.environ.get("LASTBELL_CANVAS_TOKEN")
    if value:
        return value
    try:
        import keyring

        return keyring.get_password(SERVICE, CANVAS_ACCOUNT) or ""
    except Exception:  # no keyring backend: fall back to "no token"
        return ""


def set_canvas_token(token: str) -> None:
    import keyring

    try:
        keyring.set_password(SERVICE, CANVAS_ACCOUNT, token)
    except Exception as exc:
        raise SecretError(
            f"Couldn't store the Canvas token in the OS keyring "
            f"({exc.__class__.__name__}: {exc}). Set LASTBELL_CANVAS_TOKEN in "
            f"the environment instead.") from exc
