"""Central configuration.

Every value comes from an environment variable, optionally seeded from an env
file (via python-dotenv): a ``.env`` in the working directory (checkout /
Docker workflow), else the installed default (``lastbell setup`` writes it —
see :mod:`lastbell.paths`). No personal values are hard-coded in the source —
this is what keeps a real username or district out of the public repo.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from . import paths

try:  # the env file is optional; the process env alone is enough (e.g. in Docker).
    from dotenv import load_dotenv

    _env_file = paths.active_env_file()
    if _env_file is not None:
        load_dotenv(_env_file)
except ImportError:  # pragma: no cover
    pass


class ConfigError(RuntimeError):
    """Raised when a required setting is missing or malformed."""


# Good-neighbor floor: however LASTBELL_POLL_MINUTES is set, the portal is
# never hit more often than this. Grades don't change minute-to-minute, and
# the README's portal-load promises are enforced here, not just documented.
POLL_FLOOR_MINUTES = 15


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
    # Phase 4: a course-percent drop of at least this many points upgrades the
    # change to a GRADE_DROP alert (separately subscribable, louder wording).
    grade_drop_points: float

    @property
    def base_url(self) -> str:
        host = self.district.replace("https://", "").replace("http://", "").strip("/")
        return f"https://{host}"


def load() -> Config:
    """Build a Config from the environment. Raises ConfigError on missing values."""
    username = _get("LASTBELL_USERNAME", required=True)
    if username == "your_parentvue_username":
        raise ConfigError(
            "LASTBELL_USERNAME is still the placeholder from .env.example. "
            "Edit .env and set your real ParentVUE username. (Careful not to re-run "
            "`cp .env.example .env` afterward — it overwrites your edits.)"
        )
    poll_minutes = int(_get("LASTBELL_POLL_MINUTES", "180"))
    if poll_minutes < POLL_FLOOR_MINUTES:
        print(f"LASTBELL_POLL_MINUTES={poll_minutes} is below the "
              f"{POLL_FLOOR_MINUTES}-minute good-neighbor floor; "
              f"polling every {POLL_FLOOR_MINUTES} minutes instead.",
              file=sys.stderr)
        poll_minutes = POLL_FLOOR_MINUTES
    return Config(
        district=_get("LASTBELL_DISTRICT", required=True),
        username=username,
        secret_backend=_get("LASTBELL_SECRET_BACKEND", "keyring"),
        poll_minutes=poll_minutes,
        # Defaults live in the platform user-data dir (an installed copy has
        # no checkout to hold data/); a checkout's .env pins them explicitly.
        db_path=Path(_get("LASTBELL_DB_PATH") or paths.data_dir() / "lastbell.db"),
        snapshot_dir=Path(_get("LASTBELL_SNAPSHOT_DIR") or paths.data_dir() / "snapshots"),
        snapshot_retention_days=int(_get("LASTBELL_SNAPSHOT_RETENTION_DAYS", "90")),
        notify_channel=_get("LASTBELL_NOTIFY_CHANNEL", "console"),
        lookahead_days=int(_get("LASTBELL_LOOKAHEAD_DAYS", "7")),
        ungraded_grace_days=int(_get("LASTBELL_UNGRADED_GRACE_DAYS", "3")),
        dashboard_host=_get("LASTBELL_DASHBOARD_HOST", "127.0.0.1"),
        dashboard_port=int(_get("LASTBELL_DASHBOARD_PORT", "8321")),
        grade_drop_points=float(_get("LASTBELL_GRADE_DROP_POINTS", "5")),
    )
