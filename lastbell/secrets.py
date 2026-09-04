"""Password resolution.

Passwords never live in config files, the database, or the source tree — only a
*reference* to where the secret is stored. Two backends:

  keyring  the OS keyring (macOS Keychain, Windows Credential Manager,
           Linux Secret Service) via the ``keyring`` library. Recommended for
           bare-metal installs.
  env      read from ``LASTBELL_PASSWORD`` — or from the file named by
           ``LASTBELL_PASSWORD_FILE`` (a Docker/Podman secret mounted under
           /run/secrets) — where the value is injected by a secret store
           (Docker secrets, CI) — or, for an always-on box with
           no usable keyring (a headless Pi, a boot-time service that can't
           unlock the desktop keyring), written by ``lastbell setup`` into the
           mode-0600 settings file. That trade-off is stated to the user at
           the moment it's made, never silently.
"""
from __future__ import annotations

import getpass
import os
from pathlib import Path

SERVICE = "lastbell"


class SecretError(RuntimeError):
    """Raised when a password can't be resolved."""


def _from_env(key: str) -> str:
    """``key`` from the environment; else the contents of the file named by
    ``<key>_FILE`` — the Docker/Podman secrets convention, where compose
    mounts each secret at /run/secrets/<name> and nothing puts it in the
    environment. A trailing newline (every editor adds one) is dropped.
    Empty when neither is set."""
    value = os.environ.get(key)
    if value:
        return value
    path = os.environ.get(key + "_FILE")
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").rstrip("\r\n")
    except OSError as exc:
        raise SecretError(
            f"{key}_FILE={path} couldn't be read "
            f"({exc.__class__.__name__}: {exc})") from exc


def get_password(username: str, backend: str = "keyring") -> str:
    if backend == "env":
        value = _from_env("LASTBELL_PASSWORD")
        if not value:
            raise SecretError(
                "LASTBELL_SECRET_BACKEND=env but LASTBELL_PASSWORD is unset. "
                "Run `lastbell setup` (it writes it to the settings file), "
                "export LASTBELL_PASSWORD, or point LASTBELL_PASSWORD_FILE at a "
                "secret file (Docker: /run/secrets/<name>)."
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


def backend() -> str:
    """The install's secret backend (``LASTBELL_SECRET_BACKEND``). An install
    on the ``env`` backend has said "this box has no usable keyring": nothing
    may touch one, not even optionally — on a headless Linux box the keyring
    library can block forever waiting for a desktop prompt, with no error and
    no timeout (this hung every poll on a Pi in 0.2.0/0.2.1)."""
    return (os.environ.get("LASTBELL_SECRET_BACKEND") or "keyring").strip().lower()


def _optional_from_keyring(account: str) -> str:
    if backend() == "env":
        return ""
    try:
        import keyring

        return keyring.get_password(SERVICE, account) or ""
    except Exception:  # no keyring backend: fall back to "nothing stored"
        return ""


# The SMTP account password gets its own keyring slot so `lastbell setup` can
# keep it out of the env file too. The env var (Docker/CI injection) wins.
SMTP_ACCOUNT = "smtp"


def get_smtp_password() -> str:
    return _from_env("LASTBELL_PASSWORD_SMTP") or _optional_from_keyring(SMTP_ACCOUNT)


def set_smtp_password(password: str) -> str:
    """Store the SMTP password where this install keeps secrets — the OS
    keyring, or on the ``env`` backend the owner-only settings file, like
    the portal password. Returns a one-line description of where it went."""
    return _store_secret(SMTP_ACCOUNT, "LASTBELL_PASSWORD_SMTP", "SMTP password",
                         password)


def _store_secret(account: str, env_key: str, label: str, value: str) -> str:
    if backend() == "env":
        from . import paths
        from .setup_wizard import write_env

        env_path = paths.active_env_file() or paths.default_env_file()
        write_env(env_path, {env_key: value})
        return f"the settings file ({env_path})"
    import keyring

    try:
        keyring.set_password(SERVICE, account, value)
    except Exception as exc:  # NoKeyringError, PasswordSetError, …
        raise SecretError(
            f"Couldn't store the {label} in the OS keyring "
            f"({exc.__class__.__name__}: {exc}). Set {env_key} in the "
            f"environment instead, or re-run `lastbell setup` and choose the "
            f"settings-file store.") from exc
    return "the OS keyring"


# A Canvas personal access token is optional: the poll can ride the portal's
# own Canvas link instead. When the district offers tokens (Canvas → Account
# → Settings → New Access Token), storing one here skips that hand-off.
CANVAS_ACCOUNT = "canvas"


def get_canvas_token() -> str:
    return _from_env("LASTBELL_CANVAS_TOKEN") or _optional_from_keyring(CANVAS_ACCOUNT)


def set_canvas_token(token: str) -> str:
    """Store the token where this install keeps secrets: the OS keyring, or —
    on the ``env`` backend — the owner-only settings file, like the portal
    password. Returns a one-line description of where it went."""
    return _store_secret(CANVAS_ACCOUNT, "LASTBELL_CANVAS_TOKEN", "Canvas token",
                         token)


# The dashboard's network key. Not a portal credential: it gates the
# dashboard's own pages when a request arrives from another machine
# (requests from the machine itself need nothing). It lives in the settings
# file on every backend, because the dashboard must find the same key after
# a restart; LASTBELL_DASHBOARD_KEY (or _FILE) in the environment wins.
DASHBOARD_KEY = "LASTBELL_DASHBOARD_KEY"


def dashboard_key() -> tuple[str, str]:
    """(key, where) — the existing key, or a fresh one persisted to the
    settings file. ``where`` says where it is kept; when persisting fails
    the key is ephemeral and ``where`` says so (set LASTBELL_DASHBOARD_KEY
    to make it stable — the Docker case)."""
    import secrets as pysecrets

    existing = _from_env(DASHBOARD_KEY)
    if existing:
        return existing, "the settings file or environment"
    key = pysecrets.token_urlsafe(24)
    from . import paths
    from .setup_wizard import write_env

    env_path = paths.active_env_file() or paths.default_env_file()
    try:
        write_env(env_path, {DASHBOARD_KEY: key})
    except OSError as exc:
        return key, (f"nowhere — couldn't write {env_path} "
                     f"({exc.__class__.__name__}); this key lasts until the "
                     f"dashboard restarts. Set {DASHBOARD_KEY} to keep one.")
    return key, f"the settings file ({env_path})"
