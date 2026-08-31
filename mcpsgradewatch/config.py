"""Central configuration.

Every value comes from an environment variable, optionally seeded from a
git-ignored ``.env`` file (via python-dotenv). No personal values are hard-coded
in the source — copy ``.env.example`` to ``.env`` and fill it in. This is what
keeps a real username or district out of the public repo.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:  # .env is optional; the process env alone is enough (e.g. in Docker).
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


class ConfigError(RuntimeError):
    """Raised when a required setting is missing or malformed."""


def _get(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise ConfigError(
            f"{name} is required. Copy .env.example to .env and set it "
            f"(or export {name} in the environment)."
        )
    return value


@dataclass(frozen=True)
class Config:
    district: str
    username: str
    secret_backend: str
    poll_minutes: int
    db_path: Path
    snapshot_dir: Path
    snapshot_retention_days: int
    notify_channel: str
    # Phase 2 time-based rules:
    lookahead_days: int        # alert when a due date enters this window
    ungraded_grace_days: int   # days past due before "still ungraded" fires
    # Phase 3 dashboard: localhost-only unless deliberately opened up.
    dashboard_host: str
    dashboard_port: int

    @property
    def base_url(self) -> str:
        host = self.district.replace("https://", "").replace("http://", "").strip("/")
        return f"https://{host}"


def load() -> Config:
    """Build a Config from the environment. Raises ConfigError on missing values."""
    username = _get("MCPSGRADEWATCH_USERNAME", required=True)
    if username == "your_parentvue_username":
        raise ConfigError(
            "MCPSGRADEWATCH_USERNAME is still the placeholder from .env.example. "
            "Edit .env and set your real ParentVUE username. (Careful not to re-run "
            "`cp .env.example .env` afterward — it overwrites your edits.)"
        )
    return Config(
        district=_get("MCPSGRADEWATCH_DISTRICT", required=True),
        username=username,
        secret_backend=_get("MCPSGRADEWATCH_SECRET_BACKEND", "keyring"),
        poll_minutes=int(_get("MCPSGRADEWATCH_POLL_MINUTES", "180")),
        db_path=Path(_get("MCPSGRADEWATCH_DB_PATH", "data/mcpsgradewatch.db")),
        snapshot_dir=Path(_get("MCPSGRADEWATCH_SNAPSHOT_DIR", "data/snapshots")),
        snapshot_retention_days=int(_get("MCPSGRADEWATCH_SNAPSHOT_RETENTION_DAYS", "90")),
        notify_channel=_get("MCPSGRADEWATCH_NOTIFY_CHANNEL", "console"),
        lookahead_days=int(_get("MCPSGRADEWATCH_LOOKAHEAD_DAYS", "7")),
        ungraded_grace_days=int(_get("MCPSGRADEWATCH_UNGRADED_GRACE_DAYS", "3")),
        dashboard_host=_get("MCPSGRADEWATCH_DASHBOARD_HOST", "127.0.0.1"),
        dashboard_port=int(_get("MCPSGRADEWATCH_DASHBOARD_PORT", "8321")),
    )
