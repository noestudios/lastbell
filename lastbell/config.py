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
        # interpolate=False: a password is never a template. Without it,
        # dotenv expands ${VAR} inside values and a password containing that
        # sequence would silently change between `lastbell setup` and the
        # service that reads the file back.
        load_dotenv(_env_file, interpolate=False)
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


def _number(name: str, default: str, kind=int):
    raw = _get(name, default) or default
    try:
        return kind(raw)
    except ValueError:
        raise ConfigError(
            f"{name}={raw!r} isn't a number. Fix it in the settings file "
            f"(or unset it to use the default, {default}).") from None


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
    # Extra hostnames the dashboard answers to (pi.example.net, a Tailscale
    # name). Loopback, IP literals, and .local names are always allowed; any
    # other Host header is refused — see dashboard.server.host_allowed.
    # Phase 4: a course-percent drop of at least this many points upgrades the
    # change to a GRADE_DROP alert (separately subscribable, louder wording).
    grade_drop_points: float
    # Canvas layer: "auto" follows the portal's own Canvas link (or uses a
    # stored token when LASTBELL_CANVAS_HOST is set); "off" never touches it.
    canvas: str = "auto"
    canvas_host: str = ""
    canvas_skip: tuple = ()     # course-name fragments never given their own row
    dashboard_hostnames: tuple = ()

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
    poll_minutes = _number("LASTBELL_POLL_MINUTES", "180")
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
        snapshot_retention_days=_number("LASTBELL_SNAPSHOT_RETENTION_DAYS", "90"),
        notify_channel=_get("LASTBELL_NOTIFY_CHANNEL", "console"),
        lookahead_days=_number("LASTBELL_LOOKAHEAD_DAYS", "7"),
        ungraded_grace_days=_number("LASTBELL_UNGRADED_GRACE_DAYS", "3"),
        dashboard_host=_get("LASTBELL_DASHBOARD_HOST", "127.0.0.1"),
        dashboard_port=_number("LASTBELL_DASHBOARD_PORT", "8321"),
        grade_drop_points=_number("LASTBELL_GRADE_DROP_POINTS", "5", float),
        canvas=(_get("LASTBELL_CANVAS", "auto") or "auto").strip().lower(),
        canvas_host=(_get("LASTBELL_CANVAS_HOST", "") or "").strip()
        .replace("https://", "").replace("http://", "").strip("/"),
        canvas_skip=tuple(f.strip() for f in (_get("LASTBELL_CANVAS_SKIP", "") or "").split(",")
                          if f.strip()),
        dashboard_hostnames=tuple(
            h.strip() for h in (_get("LASTBELL_DASHBOARD_HOSTNAMES", "") or "").split(",")
            if h.strip()),
    )
