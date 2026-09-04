"""``lastbell backup`` / ``lastbell restore`` — one file to keep, one
command to come back from.

The database is copied through SQLite's own backup API, so a copy taken
while the poller is mid-commit is still a consistent database and the
write-ahead log is folded in (a plain ``cp`` of a WAL database can miss
the last hour). The settings file goes in with every secret left out:
the portal and SMTP passwords, channel tokens, the dashboard key. A
backup is meant to sit on a NAS or in a cloud folder, and the one thing
that must never travel that way is a password. The archive is created
owner-only; it still holds names and grades.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from . import __version__, paths
from . import config as cfg
from .service import Say

DB_MEMBER = "lastbell.db"
ENV_MEMBER = "settings.env"
NOTE_MEMBER = "README.txt"

# A settings key with any of these in its name is a secret and stays out.
# LASTBELL_PASSWORD_FILE and LASTBELL_DASHBOARD_KEY_FILE name *where* a
# secret is, not the secret; those are ordinary settings and travel.
SECRET_MARKS = ("PASSWORD", "TOKEN", "KEY")


class BackupError(RuntimeError):
    """A plain-language reason the backup or restore couldn't be made."""


def is_secret_key(key: str) -> bool:
    k = key.strip().upper()
    return any(m in k for m in SECRET_MARKS) and not k.endswith("_FILE")


def redact_env(text: str) -> tuple[str, list[str]]:
    """The settings text with every secret line replaced by a note saying
    it was left out. Comments, order, and everything else stay."""
    out, removed = [], []
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            key = s.partition("=")[0].strip()
            if is_secret_key(key):
                removed.append(key)
                out.append(f"# {key} was left out of this backup")
                continue
        out.append(line)
    return "\n".join(out) + "\n", removed


def _paths() -> tuple[Path, Path | None]:
    """(database, settings file or None) — from the loaded settings when
    they load, else from the platform defaults (a fresh install restoring)."""
    try:
        conf = cfg.load()
        db = Path(conf.db_path).expanduser()
    except cfg.ConfigError:
        db = Path(os.environ.get("LASTBELL_DB_PATH") or paths.data_dir() / "lastbell.db")
    return db, paths.active_env_file()


def _copy_db(src: Path, dest: Path) -> None:
    """A consistent copy of ``src`` at ``dest`` via the SQLite backup API."""
    source = sqlite3.connect(str(src))
    try:
        target = sqlite3.connect(str(dest))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _counts(db: Path) -> dict:
    conn = sqlite3.connect(str(db))
    try:
        out = {}
        for table in ("students", "assignments", "alerts", "watchers", "subscriptions"):
            try:
                out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                out[table] = 0
        return out
    finally:
        conn.close()


def default_name(now: datetime | None = None) -> str:
    now = now or datetime.now()
    return now.strftime("lastbell-backup-%Y-%m-%d-%H%M.zip")


def backup(target: Path | None = None, say: Say = print, now: datetime | None = None) -> Path:
    db, env_file = _paths()
    if not db.is_file():
        raise BackupError(f"there is no database yet at {db} — nothing to back up "
                          "(the first `lastbell run` makes one)")
    out = Path(target).expanduser() if target else Path(default_name(now))
    if out.is_dir():
        out = out / default_name(now)
    if out.suffix.lower() != ".zip":
        out = out.with_name(out.name + ".zip")

    counts = _counts(db)
    removed: list[str] = []
    env_text = None
    if env_file is not None:
        env_text, removed = redact_env(env_file.read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory() as tmpdir:
        snapshot = Path(tmpdir) / DB_MEMBER
        _copy_db(db, snapshot)
        stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
        note = "\n".join([
            f"Last Bell backup, {stamp}, made by Last Bell {__version__}.",
            "",
            f"{DB_MEMBER}    the database: {counts['students']} students, "
            f"{counts['assignments']} assignments, {counts['alerts']} alerts, "
            f"{counts['watchers']} watchers, {counts['subscriptions']} subscriptions",
            (f"{ENV_MEMBER}   the settings, without: {', '.join(removed)}"
             if removed else f"{ENV_MEMBER}   the settings") if env_text is not None
            else "(no settings file was in use; settings came from the environment)",
            "",
            "Restore on a machine with Last Bell installed:",
            f"    lastbell restore {out.name}",
            "then store the secrets again (`lastbell setup`, or `lastbell "
            "set-password`) and restart the service.",
            "",
            "This file holds names and grades. Keep it somewhere private.",
            "",
        ])
        try:
            fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise BackupError(f"{out} already exists — pick another name") from None
        except OSError as exc:
            raise BackupError(f"couldn't create {out} ({exc.__class__.__name__}: "
                              f"{exc})") from None
        with os.fdopen(fd, "wb") as fh, zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snapshot, DB_MEMBER)
            if env_text is not None:
                z.writestr(ENV_MEMBER, env_text)
            z.writestr(NOTE_MEMBER, note)

    size = out.stat().st_size
    say(f"Backed up to {out} ({size / 1024:.0f} KB)")
    say(f"  {DB_MEMBER}   {counts['students']} students, {counts['assignments']} "
        f"assignments, {counts['alerts']} alerts, {counts['watchers']} watchers")
    if env_text is not None:
        say(f"  {ENV_MEMBER}  your settings"
            + (f", without {', '.join(removed)}" if removed else ""))
    else:
        say("  (no settings file in use; nothing but the database was saved)")
    say(f"Restore with: lastbell restore {out}   — then store the password again.")
    say("It holds names and grades: keep it somewhere private.")
    return out


def _retire(db: Path) -> Path:
    """Move the current database aside as ``<name>.before-restore`` with
    its write-ahead log folded in first, so the retired copy is whole and
    no stale -wal/-shm is left for the restored file to pick up."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError as exc:
        raise BackupError(f"the current database is busy ({exc}); stop the "
                          "service (`systemctl --user stop lastbell`) and try "
                          "again") from None
    finally:
        conn.close()
    kept = db.with_name(db.name + ".before-restore")
    if kept.exists():
        kept.unlink()
    os.replace(db, kept)
    for sidecar in (db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        if sidecar.exists():
            sidecar.unlink()
    return kept


def restore(archive: Path, force: bool = False, say: Say = print) -> int:
    archive = Path(archive).expanduser()
    if not archive.is_file():
        raise BackupError(f"{archive} doesn't exist")
    if not zipfile.is_zipfile(archive):
        raise BackupError(f"{archive} isn't a Last Bell backup (not a zip file)")
    with zipfile.ZipFile(archive) as z:
        names = set(z.namelist())
        if DB_MEMBER not in names:
            raise BackupError(f"{archive} isn't a Last Bell backup ({DB_MEMBER} missing)")
        db, env_file = _paths()
        with tempfile.TemporaryDirectory() as tmpdir:
            staged = Path(tmpdir) / DB_MEMBER
            staged.write_bytes(z.read(DB_MEMBER))
            conn = sqlite3.connect(str(staged))
            try:
                ok = conn.execute("PRAGMA quick_check").fetchone()[0]
            except sqlite3.DatabaseError as exc:
                raise BackupError(f"the database inside {archive.name} won't open "
                                  f"({exc})") from None
            finally:
                conn.close()
            if ok != "ok":
                raise BackupError(f"the database inside {archive.name} is damaged: {ok}")
            counts = _counts(staged)
            if db.is_file():
                if not force:
                    raise BackupError(
                        f"{db} already exists. Pass --force to replace it (the "
                        "current one is kept beside it as "
                        f"{db.name}.before-restore); stop the service first.")
                kept = _retire(db)
                say(f"  kept the current database as {kept.name}")
            db.parent.mkdir(parents=True, exist_ok=True)
            tmp = db.with_name(db.name + ".restoring")
            tmp.write_bytes(staged.read_bytes())
            os.replace(tmp, db)
            say(f"  ✓ database restored to {db}: {counts['students']} students, "
                f"{counts['assignments']} assignments, {counts['alerts']} alerts, "
                f"{counts['watchers']} watchers")
        if ENV_MEMBER in names:
            from .setup_wizard import read_env, write_env

            text = z.read(ENV_MEMBER).decode("utf-8")
            with tempfile.TemporaryDirectory() as tmpdir:
                staged_env = Path(tmpdir) / ENV_MEMBER
                staged_env.write_text(text, encoding="utf-8")
                values = {k: v for k, v in read_env(staged_env).items()
                          if not is_secret_key(k)}
            target = env_file or paths.default_env_file()
            existing = read_env(target) if target.is_file() else {}
            write_env(target, values)
            say(f"  ✓ settings {'merged into' if existing else 'written to'} {target}"
                + (" (secrets stay as they were)" if existing else ""))
    say("Now store the password (`lastbell setup`, or `lastbell set-password`) "
        "if this is a fresh machine, and restart the service.")
    return 0
