"""On-demand "is there a newer Last Bell?" — one request to PyPI, only when
a person clicks Check for updates.

Never automatic, never scheduled, never cached across requests: the README
promises there is no phone-home, and the only outbound HTTP is the portal,
the preflight, the alert channels you configured — plus this, when you ask
for it. Keep it that way. PyPI learns nothing but that some address fetched
a public page.
"""
from __future__ import annotations

import re
from typing import Tuple

from . import __version__

PYPI_JSON = "https://pypi.org/pypi/lastbell/json"
UPGRADE_HINT = "on the machine running Last Bell: pipx upgrade lastbell, then restart it"


class UpdateCheckError(RuntimeError):
    """PyPI couldn't be reached or didn't answer sensibly."""


def _parse(version: str) -> Tuple[list, bool]:
    """Numeric parts plus whether a pre-release tail (rc1, .dev0) follows."""
    parts = re.match(r"^(\d+(?:\.\d+)*)(.*)$", version.strip())
    if not parts:
        return [-1], False
    return [int(n) for n in parts.group(1).split(".")], bool(parts.group(2).strip())


def compare(current: str, latest: str) -> str:
    """'newer' when PyPI has a newer release, 'current' when equal, 'ahead'
    when this copy is newer than anything published (a checkout). Numeric,
    zero-padded (0.2 == 0.2.0), and a pre-release sorts just below the
    release it precedes — enough without pulling in `packaging`."""
    a, pre_a = _parse(current)
    b, pre_b = _parse(latest)
    width = max(len(a), len(b))
    key_a = tuple(a + [0] * (width - len(a))) + (-1 if pre_a else 0,)
    key_b = tuple(b + [0] * (width - len(b))) + (-1 if pre_b else 0,)
    if key_b > key_a:
        return "newer"
    if key_b < key_a:
        return "ahead"
    return "current"


def latest_version(timeout: float = 5.0) -> str:
    import requests

    try:
        response = requests.get(PYPI_JSON, timeout=timeout,
                                headers={"User-Agent": f"lastbell/{__version__}"})
        response.raise_for_status()
    except Exception as exc:
        raise UpdateCheckError(
            f"couldn't reach PyPI ({exc.__class__.__name__}) — check the network"
        ) from exc
    try:
        latest = response.json()["info"]["version"]
    except Exception:
        latest = None
    if not isinstance(latest, str) or not latest:
        raise UpdateCheckError("PyPI's answer had no version in it")
    return latest


def check() -> Tuple[str, str]:
    """(status, latest) — status from ``compare``. Raises UpdateCheckError."""
    latest = latest_version()
    return compare(__version__, latest), latest


def describe(status: str, latest: str) -> str:
    if status == "newer":
        return (f"Last Bell {latest} is available (this is {__version__}) — "
                f"{UPGRADE_HINT}")
    if status == "ahead":
        return (f"This is {__version__}, newer than the latest release on PyPI "
                f"({latest}) — nothing to do")
    return f"You're on the latest version ({__version__})"
