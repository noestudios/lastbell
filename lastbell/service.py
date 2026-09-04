"""``lastbell install-service`` — keep ``run --loop`` running (install Phase 3b).

"Keep it running" used to be the user's problem. This writes and enables the
right thing for the host, without sudo, and can show or undo it:

    Linux    a *user* systemd unit at ~/.config/systemd/user/lastbell.service,
             enabled now and at boot, plus ``loginctl enable-linger`` so it
             starts without anyone logging in (the Raspberry Pi / Pi-hole box).
    macOS    a launchd agent at ~/Library/LaunchAgents/com.noestudios.lastbell.plist
             (RunAtLoad + KeepAlive), bootstrapped into the user's GUI session
             — which is also what lets it read the login keychain.
    Windows  prints the Task Scheduler command rather than running it; the
             scope stays small and nothing on that platform is touched.

``--print`` shows exactly what would be written and run; ``--uninstall``
reverses it. Every subprocess goes through ``_run`` and every path through
``_home``/``paths`` so the tests never touch the real machine.
"""
from __future__ import annotations

import getpass
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from . import paths

LABEL = "com.noestudios.lastbell"
UNIT = "lastbell.service"
# A dashboard kept running as its own user unit (the README shows how) —
# not installed by us, but restarted by `lastbell upgrade` when it exists.
DASHBOARD_UNIT = "lastbell-dashboard.service"
TASK_NAME = "Last Bell"

Say = Callable[[str], None]


class ServiceError(RuntimeError):
    """A plain-language reason the service couldn't be set up."""


# ── host facts (module-level so tests can stub them) ───────────────────


def platform_name() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if os.name == "nt":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def _home() -> Path:
    return Path.home()


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a system command, never raising: a missing binary comes back as a
    failed CompletedProcess so the caller reports it in one line."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: command not found")


def _host_is_utc() -> bool:
    """Pi-hole and other appliance images often ship on UTC; digests use the
    local clock, so a 4pm digest would land at 11am in Maryland."""
    return time.timezone == 0 and time.tzname[0].upper() in ("UTC", "GMT")


def _secret_backend() -> str:
    """The backend the service would run with (env file + process env)."""
    from .setup_wizard import read_env  # lazy: setup_wizard imports this module

    env_file = paths.active_env_file()
    values = read_env(env_file) if env_file else {}
    return os.environ.get("LASTBELL_SECRET_BACKEND") or values.get(
        "LASTBELL_SECRET_BACKEND", "keyring")


def _workdir() -> Path | None:
    """A checkout's `.env` only wins from its own directory, so a service
    installed from inside a checkout keeps running from there."""
    return Path.cwd() if Path(".env").is_file() else None


def executable() -> str:
    """The `lastbell` launcher the service should run — under pipx that's
    ~/.local/bin/lastbell; in a venv, its bin/. Absolute, since the service
    has no PATH to speak of."""
    found = shutil.which("lastbell")
    if found:
        return os.path.abspath(found)
    argv0 = Path(sys.argv[0] or "")
    if argv0.name.startswith("lastbell") and argv0.is_file():
        return str(argv0.resolve())
    raise ServiceError(
        "couldn't find the `lastbell` launcher on PATH. If you installed with "
        "pipx, run `pipx ensurepath`, open a new terminal, and try again.")


def log_path() -> Path:
    return paths.data_dir() / "logs" / "lastbell.log"


# ── the generated files ───────────────────────────────────────────────


def unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(_home() / ".config")
    return Path(base) / "systemd" / "user" / UNIT


def plist_path() -> Path:
    return _home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def systemd_unit(exe: str, workdir: Path | None = None,
                 log: Path | None = None) -> str:
    log = log or log_path()
    lines = [
        "[Unit]",
        "Description=Last Bell — ParentVUE + Canvas grade & assignment monitor",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        f"ExecStart={exe} run --loop",
    ]
    if workdir is not None:
        lines.append(f"WorkingDirectory={workdir}")
    lines += [
        # journald on an appliance image is often volatile or absent for user
        # units ("No journal files were found"); the log file always exists.
        f"StandardOutput=append:{log}",
        f"StandardError=append:{log}",
        "Environment=PYTHONUNBUFFERED=1",
        "Restart=on-failure",
        "RestartSec=60",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def launchd_plist(exe: str, log: Path, workdir: Path | None = None) -> str:
    entry = {
        "Label": LABEL,
        "ProgramArguments": [exe, "run", "--loop"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 60,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }
    if workdir is not None:
        entry["WorkingDirectory"] = str(workdir)
    return plistlib.dumps(entry, sort_keys=False).decode("utf-8")


def schtasks_commands(exe: str) -> list[str]:
    return [
        f'schtasks /Create /F /SC ONLOGON /TN "{TASK_NAME}" '
        f'/TR "\\"{exe}\\" run --loop"',
        f'schtasks /Delete /F /TN "{TASK_NAME}"',
    ]


# ── install / uninstall ───────────────────────────────────────────────


def _warnings(plat: str) -> list[str]:
    notes = []
    if _host_is_utc():
        notes.append(
            "this machine's clock is on UTC — digests and quiet hours use the "
            "local clock, so set your timezone"
            + (" (e.g. `sudo timedatectl set-timezone America/New_York`)"
               if plat == "linux" else "") + ".")
    if plat == "linux" and _secret_backend() != "env":
        notes.append(
            "the password is in the OS keyring, which a boot-time service "
            "can't unlock. Re-run `lastbell setup` and answer yes to the "
            "background-service question so it moves to the settings file.")
    return notes


def _fail(say: Say, what: str, proc: subprocess.CompletedProcess) -> int:
    detail = (proc.stderr or proc.stdout or "").strip() or f"exit status {proc.returncode}"
    say(f"  ✗ {what} failed: {detail}")
    return 1


def install(print_only: bool = False, say: Say = print) -> int:
    plat = platform_name()
    exe = executable()
    if plat == "windows":
        create, delete = schtasks_commands(exe)
        say("Windows: paste this into a terminal to start Last Bell at logon:")
        say(f"    {create}")
        say(f"(and to remove it later: {delete})")
        return 0
    if plat not in ("linux", "darwin"):
        raise ServiceError(f"no service recipe for this platform ({plat}); "
                           f"run `lastbell run --loop` under your own supervisor.")

    workdir = _workdir()
    if plat == "linux":
        target, content = unit_path(), systemd_unit(exe, workdir)
        commands = [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", UNIT],
            ["loginctl", "enable-linger", getpass.getuser()],
        ]
    else:
        target, content = plist_path(), launchd_plist(exe, log_path(), workdir)
        domain = f"gui/{os.getuid()}"
        commands = [
            ["launchctl", "bootout", f"{domain}/{LABEL}"],  # may fail: not loaded yet
            ["launchctl", "bootstrap", domain, str(target)],
        ]

    if print_only:
        say(f"Would write {target}:")
        say("")
        for line in content.rstrip("\n").splitlines():
            say(f"    {line}")
        say("")
        say("and then run:")
        for cmd in commands:
            say("    " + " ".join(cmd))
        for note in _warnings(plat):
            say(f"note: {note}")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    log_path().parent.mkdir(parents=True, exist_ok=True)
    say(f"  wrote {target}")

    if plat == "linux":
        reload_, enable, linger = commands
        if (proc := _run(reload_)).returncode != 0:
            return _fail(say, "systemctl --user daemon-reload", proc)
        if (proc := _run(enable)).returncode != 0:
            return _fail(say, "systemctl --user enable --now", proc)
        say("  ✓ service enabled and started (systemctl --user status lastbell)")
        if (proc := _run(linger)).returncode != 0:
            say("  ⚠ couldn't enable lingering, so the service only runs while "
                "you're logged in. Fix: sudo loginctl enable-linger "
                f"{getpass.getuser()}")
        else:
            say("  ✓ lingering enabled — it starts at boot, no login needed")
        say(f"  logs: tail -f {log_path()}  (also journalctl --user -u lastbell)")
    else:
        bootout, bootstrap = commands
        _run(bootout)  # a previous copy, if any; failure just means none was loaded
        if (proc := _run(bootstrap)).returncode != 0:
            return _fail(say, "launchctl bootstrap", proc)
        say("  ✓ agent loaded — it starts at login and restarts if it stops")
        say(f"  logs: {log_path()}")
    for note in _warnings(plat):
        say(f"  ⚠ {note}")
    return 0


def uninstall(print_only: bool = False, say: Say = print) -> int:
    plat = platform_name()
    if plat == "windows":
        say("Windows: remove the scheduled task with:")
        say(f"    {schtasks_commands('lastbell')[1]}")
        return 0
    if plat not in ("linux", "darwin"):
        raise ServiceError(f"no service recipe for this platform ({plat}).")

    if plat == "linux":
        target = unit_path()
        commands = [
            ["systemctl", "--user", "disable", "--now", UNIT],
            ["systemctl", "--user", "daemon-reload"],
        ]
    else:
        target = plist_path()
        commands = [["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"]]

    if print_only:
        say("Would run:")
        for cmd in commands:
            say("    " + " ".join(cmd))
        say(f"and remove {target}")
        return 0

    if not target.is_file():
        say(f"  nothing installed ({target} doesn't exist)")
        return 0
    for cmd in commands:
        proc = _run(cmd)
        if proc.returncode != 0:
            say(f"  ⚠ {' '.join(cmd[:2])} reported: "
                f"{(proc.stderr or proc.stdout).strip() or proc.returncode}")
    target.unlink()
    say(f"  ✓ stopped and removed {target}")
    if plat == "linux":
        say("  (login lingering was left on — other user services may rely on it; "
            f"`loginctl disable-linger {getpass.getuser()}` turns it off)")
    return 0
