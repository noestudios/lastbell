"""``lastbell status`` — one screen that answers "is it working?"

Every question a parent asks in the first week ("did it check today?",
"is the service actually running?", "where did my password go?", "who is
getting alerts?") has an answer somewhere: the log, the dashboard footer,
``systemctl``, the settings file. This gathers them into one report that
is safe to paste into an issue: students appear as initials, the password
never appears, home directories are shortened to ``~`` so the account name
stays off the page, and nothing here touches the network (the one local
probe is a connection to the dashboard's own port).

Nothing here creates anything either. A database that doesn't exist yet
is reported as such, not made; the keyring is only *named*, never read.
"""
from __future__ import annotations

import os
import platform
import socket
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__, paths, service, updates
from . import config as cfg
from .health import _local as when_local

ISSUES_URL = "https://github.com/noestudios/lastbell/issues/new"


# ── small formatters ──────────────────────────────────────────────────


def tilde(path) -> str:
    """A path with the home directory written as ``~`` — a pasted report
    shouldn't carry the account name."""
    p = Path(path).expanduser()
    home = Path.home()
    if p == home:
        return "~"
    try:
        return "~/" + p.relative_to(home).as_posix()
    except ValueError:
        return str(p)


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"  # pragma: no cover


def _utc(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def ago(stamp: str | None, now: datetime | None = None) -> str:
    """``2 hours ago`` for a UTC ``YYYY-MM-DD HH:MM:SS`` stamp."""
    dt = _utc(stamp)
    if dt is None:
        return ""
    now = now or datetime.now(timezone.utc)
    secs = max(0, int((now - dt).total_seconds()))
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    hours = secs // 3600
    if hours < 48:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    return f"{hours // 24} days ago"


def _clock(dt: datetime) -> str:
    local = dt.astimezone()
    return local.strftime("%I:%M %p").lstrip("0")


# ── host probes (module-level so tests can stub them) ─────────────────


def keyring_name() -> str:
    """Which keyring the ``keyring`` library would use here, in words.
    Only *named*: reading a password would prompt on macOS and can hang on
    a headless box, so status never does that."""
    try:
        import keyring

        backend = keyring.get_keyring()
    except Exception as exc:  # NoKeyringError, InitError, …
        return f"none usable ({exc.__class__.__name__})"
    module = type(backend).__module__
    for needle, name in (("macOS", "macOS Keychain"), ("Windows", "Windows Credential Manager"),
                         ("SecretService", "Secret Service"), ("kwallet", "KWallet"),
                         ("fail", "none usable"), ("null", "none usable"),
                         ("chainer", "several, chained")):
        if needle.lower() in module.lower():
            return name
    return type(backend).__name__


def service_state(plat: str) -> list[str]:
    """One line per managed process: installed or not, running or not."""
    if plat == "linux":
        lines = []
        for unit, what in ((service.UNIT, "poller"), (service.DASHBOARD_UNIT, "dashboard")):
            path = service.unit_path().with_name(unit)
            if not path.is_file():
                if what == "poller":
                    lines.append("not installed — `lastbell install-service` keeps "
                                 "`run --loop` running at boot")
                continue
            proc = service._run(["systemctl", "--user", "is-active", unit])
            state = (proc.stdout or proc.stderr or "").strip().splitlines()
            state = state[0] if state else f"exit {proc.returncode}"
            if proc.returncode == 127:
                state = "unknown (systemctl not found)"
            lines.append(f"{what}: systemd user unit {unit} — {state}")
        return lines
    if plat == "darwin":
        if not service.plist_path().is_file():
            return ["not installed — `lastbell install-service` keeps `run --loop` "
                    "running at login"]
        proc = service._run(["launchctl", "print", f"gui/{os.getuid()}/{service.LABEL}"])
        if proc.returncode != 0:
            return [f"poller: launchd agent {service.LABEL} — installed but not loaded "
                    "(`lastbell install-service` reloads it)"]
        state = "loaded"
        for line in (proc.stdout or "").splitlines():
            if line.strip().startswith("state ="):
                state = line.split("=", 1)[1].strip()
                break
        return [f"poller: launchd agent {service.LABEL} — {state}"]
    if plat == "windows":
        return ["Task Scheduler (not checked here; `schtasks /Query /TN \"Last Bell\"`)"]
    return [f"not managed on this platform ({plat})"]


def dashboard_listening(host: str, port: int) -> bool:
    """Is something answering on the dashboard's port? A local TCP connect
    with a short timeout; nothing is sent."""
    target = host
    if host in ("", "0.0.0.0"):
        target = "127.0.0.1"
    elif host == "::":
        target = "::1"
    try:
        with socket.create_connection((target, port), timeout=0.5):
            return True
    except OSError:
        return False


# ── the report ────────────────────────────────────────────────────────


def _password_line(conf: cfg.Config, env_file: Path | None) -> str:
    backend = conf.secret_backend
    if backend == "env":
        from .setup_wizard import read_env

        in_file = env_file is not None and "LASTBELL_PASSWORD" in read_env(env_file)
        if in_file:
            return "the settings file (LASTBELL_PASSWORD, owner-only)"
        if os.environ.get("LASTBELL_PASSWORD"):
            return "the environment (LASTBELL_PASSWORD)"
        if os.environ.get("LASTBELL_PASSWORD_FILE"):
            return "a secret file (LASTBELL_PASSWORD_FILE)"
        return ("NOT SET — LASTBELL_SECRET_BACKEND=env but LASTBELL_PASSWORD is "
                "empty; run `lastbell setup`")
    if backend == "keyring":
        return f"the OS keyring ({keyring_name()})"
    return f"unknown backend {backend!r} (expected keyring or env)"


def _canvas_line(conf: cfg.Config) -> str:
    if conf.canvas == "off":
        return "off"
    token = bool(os.environ.get("LASTBELL_CANVAS_TOKEN")
                 or os.environ.get("LASTBELL_CANVAS_TOKEN_FILE"))
    if conf.canvas_host:
        how = "a token in the environment" if token else "a stored token, if any"
        return f"{conf.canvas} — {conf.canvas_host} via {how}"
    return f"{conf.canvas} — the portal's own Canvas link (SAML hand-off)"


def _alerts_line(conf: cfg.Config) -> str:
    chan = conf.notify_channel
    if chan == "email":
        host = os.environ.get("LASTBELL_SMTP_HOST") or "SMTP host NOT SET"
        port = os.environ.get("LASTBELL_SMTP_PORT") or "587"
        return f"email via {host}:{port}"
    return chan


def _db_section(conf: cfg.Config, now: datetime) -> list[str]:
    from . import health, outbox, store, watchers
    from .collector import initials_of
    from .models import WatcherKind

    db = Path(conf.db_path).expanduser()
    if not db.is_file():
        return [f"Database: not created yet ({tilde(db)}) — the first "
                "`lastbell run` makes it"]
    lines = [f"Database: {tilde(db)} ({_size(db.stat().st_size)})"]
    try:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        return lines + [f"  couldn't open it: {exc}"]
    try:
        try:
            last = store.last_poll(conn)
            state = health.current(conn)
        except sqlite3.OperationalError:      # a database from before 0.1.5
            last, state = None, health.Health()
            lines.append("  (older layout — the next `lastbell run` brings it up to date)")
        if last:
            lines.append(f"Last successful check: {when_local(last)} ({ago(last, now)})")
        else:
            lines.append("Last successful check: never — run `lastbell run` once by hand")
        if state.failing:
            what = {"login": "the portal rejected the sign-in",
                    "portal": "the portal couldn't be reached"}.get(
                        state.kind, "the poll couldn't finish")
            told = (f"; guardians were told {ago(state.notified, now)}"
                    if state.notified else "; guardians not told yet")
            lines.append(f"Checking: FAILING — {state.failures} in a row since "
                         f"{when_local(state.since)}: {what}{told}")
            if state.detail:
                lines.append(f"  last error: {state.detail}")
            delay = health.next_delay_minutes(state, conf.poll_minutes)
            lines.append("Next try: " + ("once a day while sign-in is rejected"
                                         if delay >= 24 * 60 else f"within {delay} min")
                         + " (if the service is running)")
        else:
            lines.append("Checking: OK")
            due = _utc(last)
            if due is not None:
                due += timedelta(minutes=conf.poll_minutes)
                if due < now - timedelta(minutes=conf.poll_minutes):
                    lines.append(f"Next check: was due {ago(due.strftime('%Y-%m-%d %H:%M:%S'), now)}"
                                 " — the service doesn't look like it's running")
                else:
                    lines.append(f"Next check: about {_clock(due)} (if the service is running)")

        rows = conn.execute("SELECT id, name, initials FROM students ORDER BY name").fetchall()
        initials = {r["id"]: (r["initials"] or initials_of(r["name"])) for r in rows}
        n = len(rows)
        lines.append(f"Students: {n} ({', '.join(initials.values())})" if n
                     else "Students: none yet (they appear after the first check)")

        subs: dict = {}
        for s in watchers.list_subscriptions(conn):
            subs.setdefault(s.watcher_id, []).append(initials.get(s.student_id, "?"))
        ws = watchers.list_watchers(conn)
        if not ws:
            lines.append("Watchers: none yet — the first check adds one for the "
                         "credential holder")
        else:
            lines.append(f"Watchers: {len(ws)}")
            for w in ws:
                chans = ", ".join(f"{k}={next(iter(v.values()), '·') if v else '·'}"
                                  for k, v in w.channels.items()) or "no channels"
                to = sorted(set(subs.get(w.id, [])))
                kind = "guardian" if w.kind is WatcherKind.GUARDIAN else "student"
                lines.append(f"  {w.name} ({kind}): {chans} → "
                             + (", ".join(to) if to else "no subscriptions"))
        queued = len(outbox.pending(conn))
        total = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        latest = conn.execute("SELECT created_at FROM alerts ORDER BY created_at DESC "
                              "LIMIT 1").fetchone()
        lines.append(f"Alerts: {total} sent"
                     + (f", last {ago(latest[0], now)}" if latest else "")
                     + (f"; {queued} queued for later" if queued else ""))
    except sqlite3.Error as exc:
        lines.append(f"  couldn't read it: {exc}")
    finally:
        conn.close()
    return lines


def report(now: datetime | None = None) -> list[str]:
    """The status lines. Never raises: an install with no settings yet
    still gets the version, the platform, and what to do next."""
    now = now or datetime.now(timezone.utc)
    plat = service.platform_name()
    installed = updates.installed_version()
    head = f"Last Bell {__version__}"
    if updates.restart_pending(installed):
        head += f" running; {installed} installed — restart to use it"
    lines = [head,
             f"Python {platform.python_version()} on {platform.system()} "
             f"{platform.release()}"]
    tz = f"{time.tzname[0] or 'unknown'} (UTC{time.strftime('%z')})"
    lines.append(f"Clock: {tz}" + (" — UTC! due dates, digests, and quiet hours "
                                   "will run hours early; set the timezone"
                                   if service._host_is_utc() else ""))
    try:
        exe = service.executable()
        lines.append(f"Launcher: {tilde(exe)}")
    except service.ServiceError:
        lines.append(f"Launcher: not on PATH (running {tilde(sys.argv[0] or 'python')})")

    env_file = paths.active_env_file()
    lines.append("Settings file: " + (tilde(env_file) if env_file else
                                      "none — `lastbell setup` writes one"))
    try:
        conf = cfg.load()
    except cfg.ConfigError as exc:
        lines.append(f"Settings: NOT LOADED — {exc}")
        conf = None

    lines.append("")
    if conf is not None:
        lines.append(f"District: {conf.district}")
        lines.append(f"Password: {_password_line(conf, env_file)}")
        lines.append(f"Checks every {conf.poll_minutes} min; alerts: {_alerts_line(conf)}")
        lines.append(f"Canvas: {_canvas_line(conf)}")
        if conf.heartbeat_url:
            from urllib.parse import urlparse

            lines.append(f"Heartbeat: {urlparse(conf.heartbeat_url).netloc or 'set'} "
                         "(pinged after every successful check)")
        lines.append("")

    for i, line in enumerate(service_state(plat)):
        lines.append(("Service: " if i == 0 else "         ") + line)
    if conf is not None:
        listening = dashboard_listening(conf.dashboard_host, conf.dashboard_port)
        key_set = bool(os.environ.get("LASTBELL_DASHBOARD_KEY")
                       or os.environ.get("LASTBELL_DASHBOARD_KEY_FILE"))
        lines.append(f"Dashboard: {conf.dashboard_host}:{conf.dashboard_port} — "
                     + ("listening" if listening else "not listening")
                     + ("; network key set" if key_set else
                        "; network key not made yet (the first start beyond loopback makes one)"))
        # A phone can't open a network-bound dashboard without the key, and
        # this screen gets pasted into issues — so say where to get it, never
        # what it is.
        if conf.dashboard_host not in ("127.0.0.1", "localhost", "::1"):
            lines.append("           other devices need the key once: "
                         "lastbell dashboard --show-key prints the link")
    log = service.log_path()
    if log.is_file():
        st = log.stat()
        written = datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"Log: {tilde(log)} ({_size(st.st_size)}, last written {ago(written, now)})")
    else:
        lines.append(f"Log: none yet ({tilde(log)} once the service runs)")

    lines.append("")
    if conf is not None:
        lines += _db_section(conf, now)
        lines.append("")
    lines.append(f"Paste this when reporting a problem: {ISSUES_URL}")
    lines.append("(students are initials only and no password appears; the "
                 "addresses above are yours to trim first)")
    return lines
