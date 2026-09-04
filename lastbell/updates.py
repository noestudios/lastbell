"""On-demand "is there a newer Last Bell?" — one request to PyPI, only when
a person clicks Check for updates.

Never automatic, never scheduled, never cached across requests: the README
promises there is no phone-home, and the only outbound HTTP is the portal,
the preflight, the alert channels you configured — plus this, when you ask
for it. Keep it that way. PyPI learns nothing but that some address fetched
a public page.
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from typing import Callable

from . import __version__
from .service import platform_name

PYPI_JSON = "https://pypi.org/pypi/lastbell/json"
UPGRADE_HINT = "on the machine running Last Bell: pipx upgrade lastbell, then"


def restart_hint(plat: str | None = None) -> str:
    """What "restart it" actually means on this host. A poll loop and a
    dashboard are separate long-running processes, and both keep the old
    code in memory until restarted — an upgraded box whose dashboard was
    left running keeps reporting the old version from its own footer."""
    plat = plat or platform_name()
    if plat == "linux":
        return ("restart the poller and, if you run one, the dashboard: "
                "systemctl --user restart lastbell (and lastbell-dashboard)")
    if plat == "darwin":
        return ("run `lastbell install-service` again to reload the agent, and "
                "restart the dashboard if one is running")
    return "restart every running Last Bell process — the poller and the dashboard"


def installed_version() -> str | None:
    """The version installed on disk *right now*, re-read on every call.
    After ``pipx upgrade`` replaces the files under a still-running process,
    this says the new version while ``__version__`` (what's loaded) still
    says the old — the one signal that tells "not upgraded" from "not
    restarted". None when not installed as a distribution."""
    import importlib
    import importlib.metadata as metadata

    importlib.invalidate_caches()   # importlib.metadata caches directory listings
    try:
        return metadata.version("lastbell")
    except metadata.PackageNotFoundError:
        return None


def restart_pending(installed: str | None) -> bool:
    """Newer files on disk than in this process. (A source checkout's
    dist-info can lag *behind* its code; that is not a pending restart.)"""
    return bool(installed) and compare(__version__, installed) == "newer"


# ── restarting on our own ─────────────────────────────────────────────
#
# ``pipx upgrade`` replaces the files under a running poller and a running
# dashboard, and both keep the old code in memory until restarted — the
# step people forgot often enough to earn a footer badge (0.2.5) and a
# command (0.2.8). Since 0.2.10 the two long-running processes notice on
# their own: every minute they look at the version on disk and, when it is
# newer, replace themselves with a fresh process running the same command
# line. ``exec`` keeps the PID, so systemd and launchd see nothing happen;
# by hand, the terminal just carries on with the new version.

SELF_RESTART_ENV = "LASTBELL_SELF_RESTART"
# Set on the re-exec'd process to the version it restarted for. If that
# process *still* sees the same version pending, the files on disk aren't
# the files that load (a checkout whose dist-info runs ahead of its code):
# restarting again would loop every minute, so it doesn't.
REEXEC_ENV = "LASTBELL_REEXEC_FOR"
CHECK_EVERY = 60.0


def self_restart_enabled() -> bool:
    return (os.environ.get(SELF_RESTART_ENV, "1").strip().lower()
            not in ("0", "no", "off", "false"))


def pending_upgrade() -> str | None:
    """The version on disk when it is newer than this process — the signal
    to restart — else None (also None when self-restart is switched off)."""
    if not self_restart_enabled():
        return None
    installed = installed_version()
    if not restart_pending(installed):
        return None
    if os.environ.get(REEXEC_ENV) == installed:
        return None
    return installed


_exec = os.execv   # module-level so tests can stub it


def reexec(what: str, installed: str, say: Callable[[str], None] = print) -> None:
    """Replace this process with one running the same command line on the
    new files. Doesn't return. If the exec itself fails, exits 75 so a
    service manager with Restart=on-failure brings the process back."""
    argv = [sys.executable, "-m", "lastbell.cli", *sys.argv[1:]]
    say(f"Last Bell {installed} is installed; restarting the {what} to use it")
    os.environ[REEXEC_ENV] = installed      # inherited by the new process
    # exec discards Python's buffers: the line above must reach the log first.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    try:
        _exec(sys.executable, argv)
    except OSError as exc:
        say(f"couldn't restart in place ({exc.__class__.__name__}: {exc}); "
            f"exiting so the service manager starts the {what} again")
    raise SystemExit(75)


def watch_for_upgrade(on_pending: Callable[[str], None],
                      every: float = CHECK_EVERY) -> threading.Thread:
    """A daemon thread that calls ``on_pending(version)`` once, the first
    time a newer version is on disk. The dashboard uses it to stop serving
    (from the thread) and re-exec (from the main thread, once the server
    loop has returned) — so no request is cut off mid-response."""
    def run() -> None:
        while True:
            time.sleep(every)
            newer = pending_upgrade()
            if newer:
                on_pending(newer)
                return
    thread = threading.Thread(target=run, name="lastbell-upgrade-watch", daemon=True)
    thread.start()
    return thread


class UpdateCheckError(RuntimeError):
    """PyPI couldn't be reached or didn't answer sensibly."""


def _parse(version: str) -> tuple[list, bool]:
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


def check() -> tuple[str, str]:
    """(status, latest) — status from ``compare``. Raises UpdateCheckError."""
    latest = latest_version()
    return compare(__version__, latest), latest


def describe(status: str, latest: str, installed: str | None = None,
             plat: str | None = None) -> str:
    """One line for the Check for updates click. ``installed`` (from
    ``installed_version``) takes precedence: an upgrade that only lacks a
    restart must not be told to upgrade again."""
    if restart_pending(installed):
        also = (f" ({latest} is also out on PyPI)"
                if compare(installed, latest) == "newer" else "")
        if self_restart_enabled():
            return (f"Last Bell {installed} is installed; this dashboard is still "
                    f"running {__version__} and restarts itself within a minute{also}")
        return (f"Last Bell {installed} is installed, but this dashboard is still "
                f"running {__version__} — {restart_hint(plat)}{also}")
    if status == "newer":
        if self_restart_enabled():
            return (f"Last Bell {latest} is available (this is {__version__}) — on the "
                    f"machine running Last Bell: pipx upgrade lastbell (or lastbell "
                    f"upgrade); the poller and the dashboard restart themselves "
                    f"within a minute")
        return (f"Last Bell {latest} is available (this is {__version__}) — "
                f"{UPGRADE_HINT} {restart_hint(plat)}")
    if status == "ahead":
        return (f"This is {__version__}, newer than the latest release on PyPI "
                f"({latest}) — nothing to do")
    return f"You're on the latest version ({__version__})"
