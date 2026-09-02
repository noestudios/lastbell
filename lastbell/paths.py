"""Where Last Bell keeps its files on an installed (non-checkout) machine.

A `pipx install lastbell` has no repo directory to put `data/` in, so state
defaults to the platform's user-data directory and the `.env` gains a default
home in the user-config directory:

    macOS     ~/Library/Application Support/lastbell   (data and config)
    Linux     $XDG_DATA_HOME/lastbell    (~/.local/share/lastbell)
              $XDG_CONFIG_HOME/lastbell  (~/.config/lastbell)
    Windows   %APPDATA%\\lastbell         (data and config)

Everything remains overridable exactly as before (LASTBELL_DB_PATH,
LASTBELL_SNAPSHOT_DIR, or a `.env` in the working directory — which is how a
git checkout keeps its familiar `data/` layout). LASTBELL_HOME overrides both
directories at once, which is what the tests use and what a "keep everything
on this USB stick" install would want.

Deliberately stdlib-only and dependency-free: `platformdirs` would add a
transitive dependency to save thirty lines.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP = "lastbell"


def _override() -> Path | None:
    home = os.environ.get("LASTBELL_HOME")
    return Path(home).expanduser() if home else None


def data_dir() -> Path:
    """Platform user-data directory for the database, snapshots, and dumps.

    Returned, never created — callers mkdir when they actually write.
    """
    override = _override()
    if override is not None:
        return override
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP


def config_dir() -> Path:
    """Platform user-config directory — the default home of the env file."""
    override = _override()
    if override is not None:
        return override
    if sys.platform == "darwin" or os.name == "nt":
        return data_dir()  # both platforms use one app folder for either role
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP


def default_env_file() -> Path:
    """Where `lastbell setup` writes settings when no `.env` exists yet."""
    return config_dir() / "env"


def active_env_file() -> Path | None:
    """The env file this process should load, or None when there isn't one.

    A `.env` in the working directory wins — that's the git-checkout and
    Docker-compose workflow, unchanged — then the installed default.
    """
    for candidate in (Path(".env"), default_env_file()):
        if candidate.is_file():
            return candidate
    return None
