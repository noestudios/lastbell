"""The `lastbell setup` wizard (install Phase 2) — scripted end-to-end runs
with the network, keyring, and terminal stubbed out."""
from __future__ import annotations

import pytest

from lastbell import preflight
from lastbell import secrets as secretstore
from lastbell import setup_wizard as wiz


# ── env-file bookkeeping ──────────────────────────────────────────────


def test_read_env_parses_and_strips_quotes(tmp_path):
    f = tmp_path / "env"
    f.write_text("# comment\n\nLASTBELL_DISTRICT=host.example\n"
                 'LASTBELL_USERNAME="quoted"\nnot a pair\n')
    assert wiz.read_env(f) == {"LASTBELL_DISTRICT": "host.example",
                               "LASTBELL_USERNAME": "quoted"}
    assert wiz.read_env(tmp_path / "missing") == {}


def test_write_env_updates_in_place_preserving_comments(tmp_path, monkeypatch):
    monkeypatch.delenv("LASTBELL_DISTRICT", raising=False)
    f = tmp_path / "env"
    f.write_text("# keep me\nLASTBELL_DISTRICT=old.example\n"
                 "# LASTBELL_PASSWORD=   <- commented template line, untouched\n")
    wiz.write_env(f, {"LASTBELL_DISTRICT": "new.example",
                      "LASTBELL_USERNAME": "parent1"})
    text = f.read_text()
    assert "# keep me" in text
    assert "LASTBELL_DISTRICT=new.example" in text
    assert "old.example" not in text
    assert "# LASTBELL_PASSWORD=" in text            # commented lines survive
    assert text.rstrip().endswith("LASTBELL_USERNAME=parent1")  # appended
    # …and the process env sees the values immediately (later steps rely on it)
    import os
    assert os.environ["LASTBELL_DISTRICT"] == "new.example"


def test_write_env_creates_fresh_file_with_header(tmp_path):
    f = tmp_path / "cfg" / "env"
    wiz.write_env(f, {"LASTBELL_DISTRICT": "host.example"})
    text = f.read_text()
    assert text.startswith("# Last Bell settings")
    assert "LASTBELL_DISTRICT=host.example" in text


# ── scripted wizard runs ──────────────────────────────────────────────


class Script:
    """Canned terminal: pops answers in order, records prompts and output."""

    def __init__(self, asks, yns, passwords):
        self.asks, self.yns, self.passwords = list(asks), list(yns), list(passwords)
        self.ask_log: list = []   # (prompt, default) pairs
        self.said: list = []

    def install(self, monkeypatch):
        def _ask(prompt, default=""):
            self.ask_log.append((prompt, default))
            answer = self.asks.pop(0)
            return answer if answer is not None else default
        monkeypatch.setattr(wiz, "_ask", _ask)
        monkeypatch.setattr(wiz, "_ask_yn", lambda p, default=True: self.yns.pop(0))
        monkeypatch.setattr(wiz, "_getpass", lambda p: self.passwords.pop(0))
        monkeypatch.setattr(wiz, "_say", lambda t="": self.said.append(t))
        monkeypatch.setattr(wiz, "_interactive", lambda: True)

    @property
    def output(self):
        return "\n".join(self.said)


def go_report(district="host.example"):
    r = preflight.Report(district=district, mode="full")
    for cid in ("web_login", "gate_fetch", "parse_classes", "parse_assignments"):
        r.add(cid, cid, preflight.PASS, "ok")
    assert r.verdict == "go"
    return r


@pytest.fixture
def wizard_world(monkeypatch, tmp_path):
    """Isolated home, no cwd .env, stubbed network/keyring/run/attach."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("LASTBELL_HOME", str(tmp_path / "home"))
    for var in ("LASTBELL_DISTRICT", "LASTBELL_USERNAME", "LASTBELL_DB_PATH",
                "LASTBELL_SNAPSHOT_DIR", "LASTBELL_NOTIFY_CHANNEL"):
        monkeypatch.delenv(var, raising=False)

    world = {"keyring": {}, "sends": [], "baseline_runs": 0, "attached": []}
    monkeypatch.setattr(wiz, "_anonymous_check",
                        lambda d: (True, "login form found"))
    monkeypatch.setattr(wiz, "_full_check",
                        lambda d, u, p: go_report(d))
    monkeypatch.setattr(wiz, "_test_send",
                        lambda name, addr: world["sends"].append((name, addr)))
    monkeypatch.setattr(wiz, "_baseline_run",
                        lambda: world.__setitem__("baseline_runs",
                                                  world["baseline_runs"] + 1))
    monkeypatch.setattr(wiz, "_attach_channel",
                        lambda user, chosen: world["attached"].append((user, chosen)) or True)

    def fake_get(username, backend="keyring"):
        if username not in world["keyring"]:
            raise secretstore.SecretError("none stored")
        return world["keyring"][username]
    monkeypatch.setattr(wiz.secretstore, "get_password", fake_get)
    monkeypatch.setattr(wiz.secretstore, "set_password",
                        lambda u, p: world["keyring"].__setitem__(u, p))
    return world


def test_happy_path_ntfy(wizard_world, monkeypatch):
    script = Script(
        asks=[None,          # district — accept the MCPS default
              "parent1",     # username
              "1"],          # channel menu: ntfy
        yns=[True,           # ready for test push
             True,           # push arrived
             True],          # run the baseline now
        passwords=["hunter2"])
    script.install(monkeypatch)

    assert wiz.main() == 0

    # district default was offered, answers hit the keyring and the env file
    assert script.ask_log[0][1] == wiz.MCPS_HOST
    assert wizard_world["keyring"]["parent1"] == "hunter2"
    saved = wiz.read_env(wiz.paths.default_env_file())
    assert saved["LASTBELL_DISTRICT"] == wiz.MCPS_HOST
    assert saved["LASTBELL_USERNAME"] == "parent1"

    # one test push to a generated unguessable topic, baseline ran, channel attached
    (name, addr), = wizard_world["sends"]
    assert name == "ntfy" and addr["topic"].startswith("lastbell-")
    assert len(addr["topic"]) > len("lastbell-") + 10
    assert wizard_world["baseline_runs"] == 1
    assert wizard_world["attached"] == [("parent1", ("ntfy", addr))]
    assert "lastbell run --loop" in script.output


def test_rerun_offers_saved_values_as_defaults(wizard_world, monkeypatch):
    wiz.write_env(wiz.paths.default_env_file(),
                  {"LASTBELL_DISTRICT": "other-host.example",
                   "LASTBELL_USERNAME": "parent1"})
    wizard_world["keyring"]["parent1"] = "stored-pw"
    script = Script(
        asks=[None, None, "3"],          # accept both saved defaults; console
        yns=[True],                      # run baseline
        passwords=[""])                  # keep the stored password
    script.install(monkeypatch)

    assert wiz.main() == 0
    assert script.ask_log[0][1] == "other-host.example"   # saved district offered
    assert script.ask_log[1][1] == "parent1"              # saved username offered
    assert wizard_world["keyring"]["parent1"] == "stored-pw"   # untouched
    assert wizard_world["sends"] == []                    # console: nothing to test
    assert wiz.read_env(wiz.paths.default_env_file())["LASTBELL_NOTIFY_CHANNEL"] == "console"


def test_failed_verify_saves_progress_and_stops(wizard_world, monkeypatch):
    bad = preflight.Report(district="host.example", mode="full")
    bad.add("web_login", "Web login", preflight.FAIL, "rejected")
    monkeypatch.setattr(wiz, "_full_check", lambda d, u, p: bad)
    script = Script(asks=[None, "parent1"], yns=[], passwords=["wrong-pw"])
    script.install(monkeypatch)

    assert wiz.main() == 1
    # the resume state is on disk even though the wizard bailed
    saved = wiz.read_env(wiz.paths.default_env_file())
    assert saved["LASTBELL_USERNAME"] == "parent1"
    assert wizard_world["baseline_runs"] == 0
    assert "saved" in script.output


def test_email_path_stores_smtp_password_in_keyring(wizard_world, monkeypatch):
    smtp_slot = {}
    monkeypatch.setattr(wiz.secretstore, "set_smtp_password",
                        lambda p: smtp_slot.__setitem__("pw", p))
    script = Script(
        asks=[None, "parent1", "2",              # email channel
              "smtp.example", "587", "me@example.com", None,   # host/port/user/from
              "5551234567@vtext.com"],           # recipient (carrier gateway)
        yns=[True,   # send test email
             True,   # it arrived
             True],  # run baseline
        passwords=["hunter2", "smtp-secret"])
    script.install(monkeypatch)

    assert wiz.main() == 0
    assert smtp_slot["pw"] == "smtp-secret"
    saved = wiz.read_env(wiz.paths.default_env_file())
    assert saved["LASTBELL_SMTP_HOST"] == "smtp.example"
    assert saved["LASTBELL_SMTP_TO"] == "5551234567@vtext.com"
    assert saved["LASTBELL_SMTP_FROM"] == "me@example.com"   # defaulted from user
    # neither password ever lands in the settings file
    assert "LASTBELL_PASSWORD" not in saved
    assert "LASTBELL_PASSWORD_SMTP" not in saved
    assert "smtp-secret" not in wiz.paths.default_env_file().read_text()
    assert wizard_world["sends"] == [("email", {"to": "5551234567@vtext.com"})]


def test_non_interactive_refuses(monkeypatch, capsys):
    monkeypatch.setattr(wiz, "_interactive", lambda: False)
    assert wiz.main() == 2
    assert "interactive" in capsys.readouterr().err


# ── the watcher fix-up runs against a real database ───────────────────


def test_attach_channel_replaces_console_on_default_watcher(monkeypatch, tmp_path):
    from lastbell import store, watchers
    from lastbell.models import WatcherKind

    db = tmp_path / "t.db"
    monkeypatch.setenv("LASTBELL_DISTRICT", "host.example")
    monkeypatch.setenv("LASTBELL_USERNAME", "parent1")
    monkeypatch.setenv("LASTBELL_DB_PATH", str(db))
    conn = store.connect(db)
    store.ensure_schema(conn)
    watchers.add_watcher(conn, "parent1", WatcherKind.GUARDIAN, {"console": {}})
    conn.close()

    assert wiz._attach_channel("parent1", ("ntfy", {"topic": "lastbell-abc"}))
    conn = store.connect(db)
    w = watchers.get_watcher(conn, "parent1")
    conn.close()
    assert w.channels == {"ntfy": {"topic": "lastbell-abc"}}

    assert not wiz._attach_channel("nobody", ("ntfy", {"topic": "t"}))
