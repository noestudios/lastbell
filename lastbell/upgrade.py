"""``lastbell upgrade`` — the two steps people forget are one command.

Upgrading has always been ``pipx upgrade lastbell`` *and then* restarting
every long-running copy, because the poller and the dashboard each keep
the old code in memory until restarted; the second half was forgotten
often enough to earn a footer badge in 0.2.5. This does both and says
which version was running, which is installed now, and what it restarted.
"""
from __future__ import annotations

import os
import shutil

from . import __version__, service, updates
from .service import ServiceError, Say

PIPX = ["pipx", "upgrade", "lastbell"]


def _pipx_summary(proc) -> str:
    """pipx's own one-liner, minus the install path it appends (a home
    directory is nothing a terminal needs repeated)."""
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith(("upgraded package", "lastbell is already")):
            return line.split(" (location:", 1)[0]
    return ""


def restart(say: Say = print, plat: str | None = None) -> int:
    """Restart what `install-service` (or the owner) set up. On Linux both
    known user units, when their files exist; on macOS the launchd agent;
    elsewhere a note. Returns the number of things restarted."""
    plat = plat or service.platform_name()
    if plat == "linux":
        restarted = 0
        for unit, what in ((service.UNIT, "poller"), (service.DASHBOARD_UNIT, "dashboard")):
            if not service.unit_path().with_name(unit).is_file():
                continue
            proc = service._run(["systemctl", "--user", "restart", unit])
            if proc.returncode == 0:
                say(f"  ✓ restarted the {what} ({unit})")
                restarted += 1
            else:
                detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
                say(f"  ✗ couldn't restart the {what} ({unit}): {detail}")
        if not restarted and not service.unit_path().is_file():
            say("  no service installed here — restart `lastbell run --loop` and "
                "the dashboard yourself (or run `lastbell install-service`)")
        return restarted
    if plat == "darwin":
        if not service.plist_path().is_file():
            say("  no launchd agent installed here — restart `lastbell run --loop` "
                "and the dashboard yourself (or run `lastbell install-service`)")
            return 0
        proc = service._run(["launchctl", "kickstart", "-k",
                             f"gui/{os.getuid()}/{service.LABEL}"])
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
            say(f"  ✗ couldn't restart the poller: {detail} — `lastbell "
                "install-service` reloads it")
            return 0
        say(f"  ✓ restarted the poller (launchd agent {service.LABEL})")
        say("  the dashboard, if one is running, needs restarting by hand")
        return 1
    say("  restart every running Last Bell process by hand — the poller and "
        "the dashboard")
    return 0


def run(say: Say = print, no_restart: bool = False) -> int:
    before = updates.installed_version()
    if shutil.which("pipx") is None:
        raise ServiceError(
            "pipx isn't on PATH, so this copy wasn't installed with `pipx install "
            "lastbell` (or pipx isn't set up for this shell). Upgrade the way you "
            "installed: `pip install -U lastbell` inside its venv, or `git pull` "
            "in a checkout — then restart the poller and the dashboard.")
    say(f"Last Bell {__version__} is running here"
        + (f"; {before} is installed" if before and before != __version__ else "")
        + ". Asking pipx for a newer release…")
    proc = service._run(PIPX)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        say(f"  ✗ pipx upgrade lastbell failed: {detail}")
        return 1
    after = updates.installed_version() or before
    if before and after and updates.compare(before, after) == "newer":
        say(f"  ✓ upgraded {before} → {after}")
    else:
        say("  ✓ " + (_pipx_summary(proc) or f"already the latest release ({after})"))
    if no_restart:
        say("  not restarting anything (--no-restart); the running copies keep "
            f"{__version__} until they are")
        return 0
    say("Restarting so the running copies pick it up:")
    restart(say)
    return 0
