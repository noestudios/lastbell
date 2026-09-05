"""Household settings the app keeps itself.

A setting lives in the database's ``meta`` table under a ``setting.`` key
(no table of its own, no migration) so the Settings page can change it and
every install — pipx, container, a Pi — finds it the same way. Each one has
a typed accessor here with a fixed precedence:

    the database value, if the page ever saved one;
    else the environment variable, as the **seed** a shell-configured
    install starts from;
    else the default.

Saving from the page writes the database, and from then on the environment
variable is ignored. Only the dashboard process reads these; the poller
has no display settings.

The first (and so far only) setting is the score cutoff: graded assignments
under this percent are tinted on the student pages. Display only — nothing
alerts on it.
"""
from __future__ import annotations

import os
import sqlite3

from . import store

PREFIX = "setting."

SCORE_CUTOFF_KEY = PREFIX + "score_cutoff"
SCORE_CUTOFF_ENV = "LASTBELL_SCORE_CUTOFF"
SCORE_CUTOFF_DEFAULT = 70            # "below a C" on the MCPS scale
SCORE_CUTOFF_MAX = 100

# Where the effective value came from — ``lastbell status`` says so.
FROM_DATABASE, FROM_ENVIRONMENT, FROM_DEFAULT = "database", "environment", "default"


class SettingError(ValueError):
    """A value the setting can't take; the message is for the person."""


def parse_score_cutoff(text: str | None) -> int:
    """The cutoff a person typed: a whole number from 0 to 100, where 0 (or
    nothing at all) turns the tint off. Anything else is a ``SettingError``
    worded for the Settings banner."""
    raw = (text or "").strip().rstrip("%").strip()
    if not raw:
        return 0
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if value != int(value) or not 0 <= value <= SCORE_CUTOFF_MAX:
        raise SettingError(
            f"Tint scores below needs a whole number from 0 to {SCORE_CUTOFF_MAX} "
            f"(0 turns the tint off), not {text.strip()!r}")
    return int(value)


def _seed_score_cutoff() -> tuple[int, str]:
    """The env var as the seed (a mis-set one falls back to the default —
    a setting that reads as garbage shouldn't silently switch the tint off)."""
    raw = os.environ.get(SCORE_CUTOFF_ENV)
    if raw is None:
        return SCORE_CUTOFF_DEFAULT, FROM_DEFAULT
    try:
        return parse_score_cutoff(raw), FROM_ENVIRONMENT
    except SettingError:
        return SCORE_CUTOFF_DEFAULT, FROM_DEFAULT


def score_cutoff_with_source(conn: sqlite3.Connection) -> tuple[int, str]:
    """(effective cutoff, where it came from). 0 means the tint is off."""
    saved = store.get_meta(conn, SCORE_CUTOFF_KEY)
    if saved is not None:
        try:
            return parse_score_cutoff(saved), FROM_DATABASE
        except SettingError:            # a hand-edited row; not this page's doing
            pass
    return _seed_score_cutoff()


def score_cutoff(conn: sqlite3.Connection) -> int:
    """The percent graded scores tint below; 0 = off. Database, then the
    environment seed, then the default."""
    return score_cutoff_with_source(conn)[0]


def set_score_cutoff(conn: sqlite3.Connection, value: int) -> None:
    """Save the cutoff. From here on the environment variable is ignored."""
    if not 0 <= int(value) <= SCORE_CUTOFF_MAX:
        raise SettingError(f"cutoff out of range: {value!r}")
    store.set_meta(conn, SCORE_CUTOFF_KEY, str(int(value)))


def describe_score_cutoff(value: int) -> str:
    """The toast's words for a cutoff."""
    return f"Scores below {value}% are tinted" if value else "Score tint is off"


def display(conn: sqlite3.Connection) -> dict:
    """Every display setting the pages read, fetched once per request and
    carried in the page context — cell renderers never open a connection."""
    return {"score_cutoff": score_cutoff(conn)}
