"""Watcher health: never fail silently.

Last Bell's promise is "you don't have to check". A watcher that can't sign
in (the parent changed their ParentVUE password) or can't reach the portal
would otherwise fail every three hours forever, and the only sign would be
a stale footer on a dashboard nobody opens. So the run loop keeps a small
record of consecutive failed polls here and, once a failure has lasted long
enough to be a fact rather than a blip, tells the guardians once — one
message, on their own channels, saying what is wrong and what to do — and
tells them again when checking resumes.

The same record slows the loop down while sign-in is being rejected: a
district may lock an account after repeated bad logins, and eight tries a
day with a stale password is eight more than needed to learn it is stale.

Everything lives in the ``meta`` table so a service restart keeps the
story straight ("since Tuesday", not "since just now").
"""
from __future__ import annotations

import socket
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from . import store

FAILURES_KEY = "poll_failures"          # consecutive failed polls
SINCE_KEY = "poll_failure_since"        # UTC of the first failure in this run
KIND_KEY = "poll_failure_kind"          # login | portal | other
DETAIL_KEY = "poll_failure_detail"      # one line, for the dashboard
NOTIFIED_KEY = "poll_failure_notified"  # UTC when guardians were told, if they were

# How many consecutive failures before guardians hear about it. A rejected
# sign-in doesn't fix itself, so two (six hours at the default interval) is
# enough; a portal that can't be reached usually comes back, so a full day.
NOTIFY_AFTER = {"login": 2, "portal": 8, "other": 3}

# While sign-in is being rejected, back off to once a day after this many
# consecutive rejections — enough to notice a fix, few enough to stay clear
# of any district lockout policy. Other failures keep the normal cadence.
LOGIN_BACKOFF_AFTER = 3
LOGIN_BACKOFF_MINUTES = 24 * 60

EXPLAIN = {
    "login": ("the portal rejected the sign-in. If you changed your ParentVUE "
              "password, run `lastbell setup` (or `lastbell set-password`) on "
              "{host} and restart the service. Until then Last Bell tries once "
              "a day, so a locked-out account isn't made worse."),
    "portal": ("the portal couldn't be reached. If {host} is online and the "
               "portal opens in a browser, run `lastbell preflight --report` "
               "there to see what changed."),
    "other": ("a poll couldn't finish. The log on {host} has the details "
              "(`lastbell run` once, by hand, shows them on screen)."),
}


@dataclass(frozen=True)
class Health:
    failures: int = 0
    since: str | None = None       # UTC "YYYY-MM-DD HH:MM:SS"
    kind: str = ""
    detail: str = ""
    notified: str | None = None

    @property
    def failing(self) -> bool:
        return self.failures > 0

    @property
    def notice_due(self) -> bool:
        """Guardians should hear now: past the threshold and not yet told."""
        return (self.failures >= NOTIFY_AFTER.get(self.kind, NOTIFY_AFTER["other"])
                and not self.notified)


def classify(exc: BaseException) -> str:
    """login | portal | other — what kind of failure this exception is."""
    from .client import LoginError

    if isinstance(exc, LoginError):
        return "login"
    try:
        import requests

        if isinstance(exc, requests.RequestException):
            return "portal"
    except ImportError:  # pragma: no cover
        pass
    return "other"


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def current(conn: sqlite3.Connection) -> Health:
    raw = store.get_meta(conn, FAILURES_KEY)
    try:
        failures = int(raw or 0)
    except ValueError:
        failures = 0
    return Health(failures=failures,
                  since=store.get_meta(conn, SINCE_KEY),
                  kind=store.get_meta(conn, KIND_KEY) or "",
                  detail=store.get_meta(conn, DETAIL_KEY) or "",
                  notified=store.get_meta(conn, NOTIFIED_KEY))


def record_failure(conn: sqlite3.Connection, kind: str, detail: str) -> Health:
    """One more consecutive failure. The kind is the *latest* failure's:
    a portal outage that turns into a login rejection is a login problem."""
    before = current(conn)
    store.set_meta(conn, FAILURES_KEY, str(before.failures + 1))
    if not before.failing or not before.since:
        store.set_meta(conn, SINCE_KEY, _now_utc())
    store.set_meta(conn, KIND_KEY, kind)
    store.set_meta(conn, DETAIL_KEY, (detail or "")[:200])
    return current(conn)


def record_success(conn: sqlite3.Connection) -> Health:
    """A poll finished. Returns the state that was cleared, so the caller
    knows whether guardians had been told and deserve the all-clear."""
    before = current(conn)
    if before.failing:
        for key in (FAILURES_KEY, SINCE_KEY, KIND_KEY, DETAIL_KEY, NOTIFIED_KEY):
            conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        conn.commit()
    return before


def mark_notified(conn: sqlite3.Connection) -> None:
    store.set_meta(conn, NOTIFIED_KEY, _now_utc())


def next_delay_minutes(state: Health, poll_minutes: int) -> int:
    """How long the loop should wait before the next portal attempt."""
    if state.kind == "login" and state.failures >= LOGIN_BACKOFF_AFTER:
        return max(poll_minutes, LOGIN_BACKOFF_MINUTES)
    return poll_minutes


def _local(utc: str | None) -> str:
    if not utc:
        return "a while"
    try:
        dt = datetime.fromisoformat(utc).replace(tzinfo=timezone.utc).astimezone()
    except ValueError:
        return utc
    return dt.strftime("%a %b ") + str(dt.day) + dt.strftime(", %I:%M %p").replace(" 0", " ")


def failure_message(state: Health, host: str | None = None) -> tuple[str, str]:
    """(subject, body) for the one message guardians get. Low-PII by
    construction: no student, no course, no credential — only what is wrong
    with the watcher and what to do about it."""
    host = host or socket.gethostname()
    why = EXPLAIN.get(state.kind, EXPLAIN["other"]).format(host=host)
    n = state.failures
    body = (f"Last Bell hasn't been able to check the gradebook since "
            f"{_local(state.since)} ({n} {'try' if n == 1 else 'tries'}): "
            f"{why}\n\nNo alerts are coming from ParentVUE or Canvas until "
            f"this is fixed. You'll get one more message when checking resumes.")
    return "[Last Bell] can't check the gradebook — action needed", body


def recovery_message(cleared: Health) -> tuple[str, str]:
    body = (f"Last Bell is checking the gradebook again. It had been failing "
            f"since {_local(cleared.since)} ({cleared.failures} tries); alerts "
            f"resume from here, and anything that changed in between shows up "
            f"in the next digest.")
    return "[Last Bell] checking again", body


def deliver(conn: sqlite3.Connection, notifier, subject: str, body: str) -> tuple[int, list[str]]:
    """Send a watcher-health message to every guardian on every channel they
    have (students don't get watcher plumbing); with no guardians configured,
    the install's global notifier. Returns (sent, warnings) — a channel that
    fails is a warning, never an exception: this is the code path that runs
    when things are already going wrong."""
    from . import notify, watchers
    from .models import WatcherKind

    sent, warnings = 0, []
    guardians = [w for w in watchers.list_watchers(conn)
                 if w.kind is WatcherKind.GUARDIAN and w.channels]
    if not guardians:
        try:
            notifier.send(subject, body)
            return 1, []
        except Exception as e:
            return 0, [f"health notice via the default notifier failed: {e}"]
    for w in guardians:
        for name, address in w.channels.items():
            try:
                notify.channel(name).send(address or {}, subject, body)
                sent += 1
            except Exception as e:
                warnings.append(f"health notice to {w.name} via {name} failed: {e}")
    return sent, warnings


def dashboard_note(state: Health) -> str:
    """One plain sentence for the home page footer, or ''."""
    if not state.failing:
        return ""
    n = state.failures
    what = {"login": "the portal rejected the sign-in",
            "portal": "the portal couldn't be reached",
            "other": "the poll couldn't finish"}.get(state.kind, "the poll couldn't finish")
    return (f"The last {n} {'try' if n == 1 else 'tries'} failed: {what}"
            + (" — run `lastbell setup` to store the new password" if state.kind == "login" else "")
            + ".")
