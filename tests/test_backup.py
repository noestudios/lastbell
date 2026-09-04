"""`lastbell backup` / `restore` (0.2.8): one zip, secrets left out, the
database copied through SQLite's backup API, restore that never eats the
current database."""
from __future__ import annotations

import os
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from lastbell import backup, store, watchers
from lastbell.backup import BackupError
from lastbell.models import WatcherKind

WHEN = datetime(2026, 9, 4, 10, 15)

ENV = """# Last Bell settings
LASTBELL_DISTRICT=x.example
LASTBELL_USERNAME=parent1
LASTBELL_SECRET_BACKEND=env
LASTBELL_PASSWORD="hunter2 #1"
LASTBELL_PASSWORD_SMTP=smtp-secret
LASTBELL_SMTP_HOST=smtp.example
LASTBELL_NTFY_TOKEN=tk_abc
LASTBELL_DASHBOARD_KEY=dk_xyz
LASTBELL_PASSWORD_FILE=/run/secrets/pw
"""


@pytest.fixture
def world(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("LASTBELL_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    for key in ("LASTBELL_DB_PATH", "LASTBELL_SNAPSHOT_DIR"):
        monkeypatch.delenv(key, raising=False)
    # write_env mirrors restored keys into os.environ (monkeypatch.delenv
    # records nothing for a key that was absent), so put them back by hand.
    keys = [line.partition("=")[0] for line in ENV.splitlines()
            if line and not line.startswith("#")] + ["LASTBELL_POLL_MINUTES"]
    before = {k: os.environ.get(k) for k in keys}
    monkeypatch.setenv("LASTBELL_DISTRICT", "x.example")
    monkeypatch.setenv("LASTBELL_USERNAME", "parent1")
    (home / "env").write_text(ENV)
    conn = store.connect(home / "lastbell.db")
    store.ensure_schema(conn)
    conn.execute("INSERT INTO students (id, agu, name, initials) VALUES ('s1', '1', 'Kid One', 'K.O.')")
    conn.commit()
    watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN, {"email": {"to": "m@example.com"}})
    conn.close()
    yield {"home": home, "db": home / "lastbell.db", "env": home / "env", "cwd": tmp_path}
    for k, v in before.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_secret_keys():
    assert backup.is_secret_key("LASTBELL_PASSWORD")
    assert backup.is_secret_key("LASTBELL_PASSWORD_SMTP")
    assert backup.is_secret_key("LASTBELL_TELEGRAM_TOKEN")
    assert backup.is_secret_key("LASTBELL_DASHBOARD_KEY")
    assert not backup.is_secret_key("LASTBELL_PASSWORD_FILE")
    assert not backup.is_secret_key("LASTBELL_DASHBOARD_KEY_FILE")
    assert not backup.is_secret_key("LASTBELL_SMTP_HOST")


def test_redact_env_keeps_everything_else():
    text, removed = backup.redact_env(ENV)
    assert removed == ["LASTBELL_PASSWORD", "LASTBELL_PASSWORD_SMTP",
                       "LASTBELL_NTFY_TOKEN", "LASTBELL_DASHBOARD_KEY"]
    assert "hunter2" not in text and "smtp-secret" not in text
    assert "tk_abc" not in text and "dk_xyz" not in text
    assert "# LASTBELL_PASSWORD was left out of this backup" in text
    assert "LASTBELL_SMTP_HOST=smtp.example" in text
    assert "LASTBELL_PASSWORD_FILE=/run/secrets/pw" in text
    assert text.startswith("# Last Bell settings\n")


def test_backup_writes_one_private_zip(world, capsys):
    said = []
    out = backup.backup(say=said.append, now=WHEN)
    assert out == Path("lastbell-backup-2026-09-04-1015.zip")
    assert out.is_file()
    if sys.platform != "win32":
        assert oct(out.stat().st_mode & 0o777) == "0o600"
    with zipfile.ZipFile(out) as z:
        assert sorted(z.namelist()) == ["README.txt", "lastbell.db", "settings.env"]
        settings = z.read("settings.env").decode()
        assert "hunter2" not in settings and "LASTBELL_DISTRICT=x.example" in settings
        note = z.read("README.txt").decode()
        assert "1 students" in note and "without: LASTBELL_PASSWORD, LASTBELL_PASSWORD_SMTP" in note
        assert "lastbell restore lastbell-backup-2026-09-04-1015.zip" in note
        z.extract("lastbell.db", world["cwd"] / "x")
    copy = sqlite3.connect(str(world["cwd"] / "x" / "lastbell.db"))
    assert copy.execute("SELECT name FROM watchers").fetchone()[0] == "Mom"
    assert copy.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    copy.close()
    text = "\n".join(said)
    assert "Backed up to lastbell-backup-2026-09-04-1015.zip" in text
    assert "1 students, 0 assignments, 0 alerts, 1 watchers" in text
    assert "without LASTBELL_PASSWORD, LASTBELL_PASSWORD_SMTP, LASTBELL_NTFY_TOKEN, LASTBELL_DASHBOARD_KEY" in text
    assert "keep it somewhere private" in text


def test_backup_includes_the_wal_tail(world):
    """Rows committed but not yet checkpointed live only in the -wal file;
    a byte copy of the .db would miss them. The backup API doesn't."""
    conn = store.connect(world["db"])
    conn.execute("PRAGMA wal_autocheckpoint = 0")
    conn.execute("INSERT INTO students (id, agu, name) VALUES ('s2', '2', 'Kid Two')")
    conn.commit()
    assert world["db"].with_name("lastbell.db-wal").stat().st_size > 0
    out = backup.backup(world["cwd"] / "b.zip", say=lambda s: None, now=WHEN)
    with zipfile.ZipFile(out) as z:
        (world["cwd"] / "y").mkdir()
        z.extract("lastbell.db", world["cwd"] / "y")
    copy = sqlite3.connect(str(world["cwd"] / "y" / "lastbell.db"))
    assert copy.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 2
    copy.close()
    conn.close()


def test_backup_target_forms(world):
    quiet = lambda s: None  # noqa: E731
    d = world["cwd"] / "backups"
    d.mkdir()
    assert backup.backup(d, say=quiet, now=WHEN) == d / "lastbell-backup-2026-09-04-1015.zip"
    assert backup.backup(d / "named", say=quiet, now=WHEN) == d / "named.zip"
    with pytest.raises(BackupError, match="already exists"):
        backup.backup(d / "named.zip", say=quiet, now=WHEN)
    with pytest.raises(BackupError, match="couldn't create"):
        backup.backup(d / "missing-dir" / "x.zip", say=quiet, now=WHEN)


def test_backup_without_a_database(world):
    world["db"].unlink()
    with pytest.raises(BackupError, match="no database yet"):
        backup.backup(say=lambda s: None)


def test_backup_without_a_settings_file(world):
    world["env"].unlink()
    said = []
    out = backup.backup(say=said.append, now=WHEN)
    with zipfile.ZipFile(out) as z:
        assert "settings.env" not in z.namelist()
    assert "nothing but the database was saved" in "\n".join(said)


# ── restore ───────────────────────────────────────────────────────────


def _take_backup(world) -> Path:
    return backup.backup(world["cwd"] / "keep.zip", say=lambda s: None, now=WHEN)


def test_restore_onto_a_fresh_machine(world, monkeypatch):
    archive = _take_backup(world)
    fresh = world["cwd"] / "fresh"
    monkeypatch.setenv("LASTBELL_HOME", str(fresh))
    monkeypatch.delenv("LASTBELL_USERNAME")               # settings won't load
    said = []
    assert backup.restore(archive, say=said.append) == 0
    db = sqlite3.connect(str(fresh / "lastbell.db"))
    assert db.execute("SELECT name FROM watchers").fetchone()[0] == "Mom"
    db.close()
    env = (fresh / "env").read_text()
    assert "LASTBELL_DISTRICT=x.example" in env and "LASTBELL_SMTP_HOST=smtp.example" in env
    assert "hunter2" not in env and "LASTBELL_PASSWORD=" not in env
    if sys.platform != "win32":
        assert oct((fresh / "env").stat().st_mode & 0o777) == "0o600"
    text = "\n".join(said)
    assert "database restored to" in text and "1 students" in text
    assert "settings written to" in text
    assert "store the password" in text


def test_restore_refuses_to_eat_the_current_database(world):
    archive = _take_backup(world)
    with pytest.raises(BackupError, match="--force"):
        backup.restore(archive, say=lambda s: None)
    db = sqlite3.connect(str(world["db"]))
    assert db.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 1
    db.close()


def test_restore_with_force_keeps_the_old_one_whole(world):
    archive = _take_backup(world)
    conn = store.connect(world["db"])
    conn.execute("PRAGMA wal_autocheckpoint = 0")
    conn.execute("INSERT INTO students (id, agu, name) VALUES ('s2', '2', 'Kid Two')")
    conn.commit()
    conn.close()
    world["env"].write_text(ENV + "LASTBELL_POLL_MINUTES=60\n")
    said = []
    assert backup.restore(archive, force=True, say=said.append) == 0
    kept = world["db"].with_name("lastbell.db.before-restore")
    assert kept.is_file()
    old = sqlite3.connect(str(kept))
    assert old.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 2   # WAL folded in
    old.close()
    assert not world["db"].with_name("lastbell.db-wal").exists()
    new = sqlite3.connect(str(world["db"]))
    assert new.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 1
    new.close()
    env = world["env"].read_text()
    assert 'LASTBELL_PASSWORD="hunter2 #1"' in env                      # secrets untouched
    assert "LASTBELL_PASSWORD_SMTP=smtp-secret" in env
    assert "LASTBELL_POLL_MINUTES=60" in env                            # unknown keys kept
    text = "\n".join(said)
    assert "kept the current database as lastbell.db.before-restore" in text
    assert "settings merged into" in text and "secrets stay as they were" in text


def test_restore_rejects_what_isnt_a_backup(world):
    quiet = lambda s: None  # noqa: E731
    with pytest.raises(BackupError, match="doesn't exist"):
        backup.restore(world["cwd"] / "nope.zip", say=quiet)
    junk = world["cwd"] / "junk.zip"
    junk.write_text("not a zip")
    with pytest.raises(BackupError, match="not a zip"):
        backup.restore(junk, say=quiet)
    other = world["cwd"] / "other.zip"
    with zipfile.ZipFile(other, "w") as z:
        z.writestr("hello.txt", "hi")
    with pytest.raises(BackupError, match="lastbell.db missing"):
        backup.restore(other, say=quiet)
    bad = world["cwd"] / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("lastbell.db", b"SQLite format 3\0" + os.urandom(2000))
    with pytest.raises(BackupError, match="won't open|damaged"):
        backup.restore(bad, say=quiet)


def test_cli_backup_and_restore(world, monkeypatch, capsys):
    import argparse

    from lastbell import cli
    assert cli._cmd_backup(argparse.Namespace(path=None)) == 0
    out = capsys.readouterr().out
    name = out.split("Backed up to ")[1].split(" ")[0]
    assert Path(name).is_file()
    assert cli._cmd_restore(argparse.Namespace(archive=name, force=True)) == 0
    assert "database restored" in capsys.readouterr().out
