"""``lastbell setup`` — the interactive first-run wizard (install Phase 2).

Kills the actual scariest step of self-hosting: hand-editing a dotfile. A
terminal Q&A that writes the env file and the OS keyring itself, verifies the
district with the preflight before and after credentials, sets up one
notification channel with a live test message, and offers the baseline
collection — so a parent goes install → first test push without opening an
editor.

Re-runnable by design: every answer is saved as soon as it's given (a failed
step resumes where it left off), and existing values are offered as defaults.
Plain ``input()``/``getpass()`` — no new dependencies.
"""
from __future__ import annotations

import os
import secrets as pysecrets
import sys
from pathlib import Path

from . import config as cfg
from . import notify, paths, preflight, service
from . import secrets as secretstore

MCPS_HOST = "md-mcps-psv.edupoint.com"

_FRESH_HEADER = [
    "# Last Bell settings — written by `lastbell setup`; safe to edit by hand.",
    "# Passwords live in the OS keyring, not here — unless setup was told this",
    "# is an always-on box with no usable keyring (LASTBELL_SECRET_BACKEND=env),",
    "# in which case this file holds them and is mode 0600 (owner-only).",
    "",
]


# ── terminal I/O (module-level so tests can monkeypatch) ──────────────


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _say(text: str = "") -> None:
    print(text)


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def _ask_yn(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = input(f"{prompt} [{hint}]: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _getpass(prompt: str) -> str:
    import getpass

    return getpass.getpass(prompt)


# ── env-file bookkeeping ──────────────────────────────────────────────


def read_env(path: Path) -> dict:
    """KEY=VALUE lines from an env file; comments and blanks skipped."""
    values: dict = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def write_env(path: Path, updates: dict) -> None:
    """Update KEY=VALUE lines in place, preserving comments and unknown keys;
    new keys are appended, and a value of ``None`` removes the key. Also
    mirrors the changes into this process's environment so later wizard
    steps (preflight, config.load) see them."""
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
    else:
        lines = list(_FRESH_HEADER)
    remaining = dict(updates)
    out = []
    for line in lines:
        stripped = line.strip()
        key = None
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
        if key in remaining:
            value = remaining.pop(key)
            if value is not None:
                out.append(f"{key}={value}")
        else:
            out.append(line)
    for key, value in remaining.items():
        if value is not None:
            out.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:  # owner-only: usually no secrets, but with the env backend there are
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover — e.g. Windows
        pass
    for key, value in updates.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


# ── probes (module-level so tests can stub the network) ───────────────


def _anonymous_check(district: str) -> tuple:
    """(ok, detail) from the credential-free portal probe."""
    report = preflight.run_anonymous(district, preflight._base_url(district))
    check = next(c for c in report.checks if c.id == "login_page")
    return check.status == preflight.PASS, check.detail


def _full_check(district: str, username: str, password: str) -> preflight.Report:
    return preflight.run_full(district, preflight._base_url(district),
                              username, password)


def _test_send(channel_name: str, address: dict) -> None:
    notify.send_test(channel_name, address)


# ── steps ─────────────────────────────────────────────────────────────


def _step_district(env_path: Path, env: dict) -> str:
    _say("Step 1 of 5 — your district's ParentVUE portal.")
    default = env.get("LASTBELL_DISTRICT") or MCPS_HOST
    while True:
        district = _ask("Portal hostname (MCPS parents: keep the default)", default)
        _say(f"  checking {district} — public pages only, no credentials sent …")
        try:
            ok, detail = _anonymous_check(district)
        except Exception as e:  # DNS typo, no network, …
            ok, detail = False, f"couldn't reach it ({e.__class__.__name__})"
        if ok:
            _say(f"  ✓ that's a Synergy ParentVUE portal ({detail})")
            write_env(env_path, {"LASTBELL_DISTRICT": district})
            return district
        _say(f"  ✗ {detail}")
        if not _ask_yn("  Try a different hostname?", default=True):
            _say("  keeping it anyway — the full check may still tell you more.")
            write_env(env_path, {"LASTBELL_DISTRICT": district})
            return district
        default = district


def _keyring_available() -> bool:
    return secretstore.keyring_available()


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _choose_backend(env: dict) -> str:
    """'keyring' or 'env'. The keyring is the default everywhere it works —
    except on a Linux box that will run Last Bell unattended: a service
    started at boot runs outside the login session and can't unlock the
    desktop keyring, so there the settings file is the honest choice."""
    current = env.get("LASTBELL_SECRET_BACKEND", "keyring")
    if not _keyring_available():
        _say("  This machine has no usable OS keyring (no Secret Service —")
        _say("  typical for a headless Pi or server). The alternative is the")
        _say("  settings file: owner-only (mode 0600), but on disk in plain text.")
        if _ask_yn("  Keep the password in the settings file instead?", default=True):
            return "env"
        return "none"
    if _is_linux():
        unattended = _ask_yn("  Will Last Bell run as a background service on "
                             "this machine (starts at boot, no one logged in)?",
                             default=(current == "env"))
        if unattended:
            _say("  A boot-time service can't unlock the desktop keyring, so the")
            _say("  password goes in the settings file: owner-only (mode 0600),")
            _say("  but on disk in plain text — the trade-off for always-on.")
            if _ask_yn("  Use the settings file?", default=True):
                return "env"
    return "keyring"


def _step_credentials(env_path: Path, env: dict) -> tuple:
    _say("")
    _say("Step 2 of 5 — your ParentVUE login.")
    username = ""
    while not username:
        username = _ask("Username", env.get("LASTBELL_USERNAME", ""))
    write_env(env_path, {"LASTBELL_USERNAME": username})

    backend = _choose_backend(env)
    if backend == "none":
        _say("  okay — nowhere to keep the password, so stopping here. Re-run")
        _say("  `lastbell setup` after setting up a keyring, or answer yes to")
        _say("  the settings-file option.")
        return username, ""

    stored: str | None = None
    try:
        stored = secretstore.get_password(username, backend)
    except secretstore.SecretError:
        stored = None
    if backend == "env":
        prompt = ("Password (hidden — saved to the owner-only settings file, "
                  "sent only to your district)")
    else:
        prompt = ("Password (hidden — goes straight into your OS keyring, "
                  "never onto disk)")
    if stored:
        prompt += " — press Enter to keep the stored one"
    password = _getpass(prompt + ": ")
    if password:
        if backend == "env":
            write_env(env_path, {"LASTBELL_SECRET_BACKEND": "env",
                                 "LASTBELL_PASSWORD": password})
            _say(f"  ✓ saved in {env_path} (owner-only) for {username!r}")
        else:
            secretstore.set_password(username, password)
            # A switch from a previous env-backend run must not leave the old
            # password behind in the file.
            write_env(env_path, {"LASTBELL_SECRET_BACKEND": "keyring",
                                 "LASTBELL_PASSWORD": None})
            _say(f"  ✓ stored in the OS keyring for {username!r}")
    elif stored:
        password = stored
        write_env(env_path, {"LASTBELL_SECRET_BACKEND": backend})
        _say("  keeping the already-stored password")
    else:
        _say("  no password given — re-run `lastbell setup` when you have it.")
        return username, ""
    return username, password


def _step_verify(district: str, username: str, password: str) -> bool:
    _say("")
    _say("Step 3 of 5 — verifying against the portal (login + data path + parsers).")
    try:
        report = _full_check(district, username, password)
    except Exception as e:
        _say(f"  ✗ the check itself failed ({e.__class__.__name__}: {e})")
        _say("  Your answers so far are saved — re-running `lastbell setup` resumes here.")
        return False
    _say(preflight.render_text(report))
    if report.verdict == "go":
        return True
    _say("")
    _say("  Your answers so far are saved — after fixing the failing check, "
        "re-run `lastbell setup` to resume from here.")
    return False


def _step_notifications(env_path: Path, env: dict) -> tuple | None:
    """Returns the chosen (channel, address-dict) for the default watcher,
    or None for console/dashboard-only."""
    _say("")
    _say("Step 4 of 5 — how alerts reach you.")
    _say("  1) email — to any inbox, sent from an email account you own (recommended)")
    _say("  2) ntfy — free push-notification app, no account; managed from the")
    _say("     terminal rather than the dashboard")
    _say("  3) none — just the dashboard and terminal output")
    choice = ""
    while choice not in ("1", "2", "3"):
        choice = _ask("Pick one", "1")

    if choice == "1":
        return _setup_email(env_path, env)
    if choice == "2":
        return _setup_ntfy(env_path)
    write_env(env_path, {"LASTBELL_NOTIFY_CHANNEL": "console"})
    _say("  okay — alerts print to the terminal; the dashboard has everything.")
    return None


def _setup_ntfy(env_path: Path) -> tuple | None:
    # The topic name is the only secret — generate an unguessable one.
    topic = f"lastbell-{pysecrets.token_urlsafe(12)}"
    _say("")
    _say("  ntfy sends pushes to a topic; knowing the topic name is the only")
    _say("  key, so Last Bell generated a long random one for you:")
    _say(f"      {topic}")
    _say("  On your phone: 1. install the free 'ntfy' app (App Store / Play Store)")
    _say("                 2. tap + and subscribe to exactly that topic name")
    while True:
        if not _ask_yn("  Subscribed? Ready for a test push?", default=True):
            _say(f"  skipping the test — your topic is {topic} (also shown at the end).")
            return ("ntfy", {"topic": topic})
        try:
            _test_send("ntfy", {"topic": topic})
        except Exception as e:
            _say(f"  ✗ sending failed ({e.__class__.__name__}: {e}) — "
                 "check the network and try again.")
            continue
        if _ask_yn("  Test sent — did it appear on your phone?", default=True):
            _say("  ✓ notifications are working")
            return ("ntfy", {"topic": topic})
        _say("  double-check the topic name in the ntfy app matches exactly, "
             "then we'll resend.")


def _setup_email(env_path: Path, env: dict) -> tuple | None:
    """Email over an SMTP account the parent owns. (Text message via the
    carriers' email-to-SMS gateways was withdrawn in 0.1.5: T-Mobile's and
    AT&T's are shut down and Verizon's is being retired, so some people
    would simply never get the message.)"""
    _say("")
    _say("  Alerts are sent from an email account you own, over SMTP —")
    _say("  your provider's docs have the host/port (for Gmail use an App Password).")
    values = {
        "LASTBELL_NOTIFY_CHANNEL": "email",   # the Phase-1 fallback transport
        "LASTBELL_SMTP_HOST": _ask("  SMTP host", env.get("LASTBELL_SMTP_HOST", "")),
        "LASTBELL_SMTP_PORT": _ask("  SMTP port", env.get("LASTBELL_SMTP_PORT", "587")),
        "LASTBELL_SMTP_USER": _ask("  SMTP username",
                                   env.get("LASTBELL_SMTP_USER", "")),
    }
    values["LASTBELL_SMTP_FROM"] = _ask(
        "  From address", env.get("LASTBELL_SMTP_FROM")
        or values["LASTBELL_SMTP_USER"])
    recipient = ""
    while not recipient:
        try:
            recipient = notify.validate_address(
                "email", _ask("  Send alerts to (email address)",
                              env.get("LASTBELL_SMTP_TO", "")))
        except ValueError as e:
            _say(f"  ✗ {e}")
    values["LASTBELL_SMTP_TO"] = recipient
    if env.get("LASTBELL_SECRET_BACKEND") == "env":
        smtp_password = _getpass("  SMTP password (hidden — saved to the "
                                 "owner-only settings file, like the portal "
                                 "password); Enter keeps any stored one: ")
        if smtp_password:
            values["LASTBELL_PASSWORD_SMTP"] = smtp_password
    else:
        smtp_password = _getpass("  SMTP password (hidden — stored in the OS "
                                 "keyring, not the settings file); Enter keeps "
                                 "any stored one: ")
        if smtp_password:
            secretstore.set_smtp_password(smtp_password)
    write_env(env_path, values)
    chosen = ("email", {"to": recipient})
    while True:
        if not _ask_yn("  Send a test email now?", default=True):
            return chosen
        try:
            _test_send("email", {"to": recipient})
        except Exception as e:
            _say(f"  ✗ sending failed ({e.__class__.__name__}: {e})")
            if not _ask_yn("  Re-enter the SMTP settings?", default=True):
                return chosen
            return _setup_email(env_path, read_env(env_path))
        if _ask_yn("  Test sent — did it arrive (check spam too)?", default=True):
            _say("  ✓ notifications are working")
            return chosen


def _baseline_run() -> None:
    """One collection pass — same code path as ``lastbell run``."""
    from . import cli, store
    from .client import ParentVueClient

    conf = cfg.load()
    password = secretstore.get_password(conf.username, conf.secret_backend)
    client = ParentVueClient(conf.base_url, conf.username, password)
    conn = store.connect(conf.db_path)
    try:
        store.ensure_schema(conn)
        cli._run_once(client, conn, notify.get("console"), conf)
    finally:
        conn.close()


def _attach_channel(username: str, chosen: tuple) -> bool:
    """Point the auto-created default watcher at the chosen channel — and
    only that one. The first run seeds a placeholder (console, or an
    ``email`` channel from LASTBELL_SMTP_TO even when the wizard chose text
    message), which would otherwise double up the delivery."""
    from . import store, watchers

    channel_name, address = chosen
    conn = store.connect(cfg.load().db_path)
    try:
        store.ensure_schema(conn)
        w = watchers.get_watcher(conn, username)
        if w is None:
            return False
        updates = {name: None for name in w.channels if name != channel_name}
        updates[channel_name] = address
        watchers.set_channels(conn, username, updates)
        return True
    finally:
        conn.close()


def _offer_service(unattended: bool) -> bool:
    """Offer `lastbell install-service` from the wizard; True when installed."""
    if service.platform_name() not in ("linux", "darwin"):
        if service.platform_name() == "windows":
            _say("  To keep it running on Windows, `lastbell install-service`")
            _say("  prints a Task Scheduler command you can paste.")
        return False
    _say("")
    _say("  Last Bell only alerts while `lastbell run --loop` is running. A")
    _say("  background service starts it at boot and restarts it if it stops.")
    if not _ask_yn("  Install it as a background service now?", default=unattended):
        _say("  skipped — `lastbell install-service` does it later (`--print`"
             " shows what it would write).")
        return False
    try:
        return service.install(say=_say) == 0
    except Exception as e:
        _say(f"  ✗ couldn't install the service ({e.__class__.__name__}: {e})")
        _say("  `lastbell install-service` retries it; nothing else is affected.")
        return False


def _step_first_run(username: str, chosen: tuple | None) -> None:
    _say("")
    _say("Step 5 of 5 — the first collection.")
    _say("  The first run learns the current state of every gradebook; alerts")
    _say("  start with the next change after that.")
    ran = False
    if _ask_yn("  Run it now?", default=True):
        try:
            _baseline_run()
            ran = True
        except Exception as e:
            _say(f"  ✗ the first run failed ({e.__class__.__name__}: {e})")
            _say("  Everything you configured is saved — `lastbell run` retries it.")
    if chosen is not None:
        channel_name, address = chosen
        address_str = next(iter(address.values()), "")
        if ran and _attach_channel(username, chosen):
            _say(f"  ✓ your alerts will arrive by {channel_name}")
        elif channel_name == "ntfy":
            # Email needs no fix-up (the first run seeds it from LASTBELL_SMTP_TO),
            # but an ntfy topic lives on the watcher, created by the first run.
            _say("  after your first `lastbell run`, attach your topic with:")
            _say(f"      lastbell watcher set-channel {username} ntfy={address_str}")


def main(argv: list | None = None) -> int:
    if not _interactive():
        print("lastbell setup is interactive — run it in a terminal.",
              file=sys.stderr)
        return 2

    env_path = paths.active_env_file() or paths.default_env_file()
    env = read_env(env_path)
    _say("Last Bell setup — five quick steps; re-run any time, answers are")
    _say(f"saved as you go (settings file: {env_path}).")
    _say("")

    district = _step_district(env_path, env)
    username, password = _step_credentials(env_path, env)
    if not password:
        return 1
    if not _step_verify(district, username, password):
        return 1
    chosen = _step_notifications(env_path, read_env(env_path))
    _step_first_run(username, chosen)
    unattended = read_env(env_path).get("LASTBELL_SECRET_BACKEND") == "env"
    installed = _offer_service(unattended)

    conf = cfg.load()
    _say("")
    if installed:
        _say("Done. Last Bell is running as a background service; the command")
        _say("that matters:")
    else:
        _say("Done. The two commands that matter:")
        _say("    lastbell run --loop     # keep watching and alerting")
    _say("    lastbell dashboard      # browse everything at http://127.0.0.1:8321")
    _say(f"Your data lives in {conf.db_path.parent}; settings in {env_path}.")
    if chosen is not None and chosen[0] == "ntfy":
        _say(f"Your ntfy topic (treat it like a password): {chosen[1]['topic']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
