"""`lastbell upgrade` (0.2.8): pipx upgrade, then restart the running copies."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lastbell import cli, service, updates, upgrade
from lastbell.service import ServiceError


@pytest.fixture
def world(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(service, "_home", lambda: home)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(service, "platform_name", lambda: "linux")
    monkeypatch.setattr(service.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(upgrade.shutil, "which", lambda name: "/usr/bin/pipx")
    versions = iter(["0.2.7", "0.2.8"])
    monkeypatch.setattr(updates, "installed_version", lambda: next(versions, "0.2.8"))
    w = {"home": home, "commands": [], "said": [], "rc": {}, "stdout": {}}

    def fake_run(cmd):
        w["commands"].append(cmd)
        rc = w["rc"].get(cmd[0], 0)
        return subprocess.CompletedProcess(cmd, rc, w["stdout"].get(cmd[0], ""),
                                           "boom" if rc else "")
    monkeypatch.setattr(service, "_run", fake_run)
    w["say"] = w["said"].append
    w["output"] = lambda: "\n".join(w["said"])
    return w


def _units(home: Path, *names: str) -> None:
    d = home / ".config" / "systemd" / "user"
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_text("[Unit]")


def test_no_pipx_is_a_plain_error(world, monkeypatch):
    monkeypatch.setattr(upgrade.shutil, "which", lambda name: None)
    with pytest.raises(ServiceError, match="pip install -U lastbell"):
        upgrade.run(say=world["say"])
    assert world["commands"] == []


def test_upgrade_then_restart_both_units(world):
    _units(world["home"], "lastbell.service", "lastbell-dashboard.service")
    world["stdout"]["pipx"] = "upgraded package lastbell from 0.2.7 to 0.2.8 (location: /home/pi/.local/pipx/venvs/lastbell)\n"
    assert upgrade.run(say=world["say"]) == 0
    out = world["output"]()
    assert "✓ upgraded 0.2.7 → 0.2.8" in out
    assert "✓ restarted the poller (lastbell.service)" in out
    assert "✓ restarted the dashboard (lastbell-dashboard.service)" in out
    assert world["commands"] == [
        ["pipx", "upgrade", "lastbell"],
        ["systemctl", "--user", "restart", "lastbell.service"],
        ["systemctl", "--user", "restart", "lastbell-dashboard.service"],
    ]
    assert "/home/pi" not in out


def test_already_latest_still_restarts_the_poller_only(world, monkeypatch):
    _units(world["home"], "lastbell.service")
    monkeypatch.setattr(updates, "installed_version", lambda: "0.2.8")
    world["stdout"]["pipx"] = "lastbell is already at latest version 0.2.8 (location: /x)\n"
    assert upgrade.run(say=world["say"]) == 0
    out = world["output"]()
    assert "✓ lastbell is already at latest version 0.2.8" in out
    assert "(location" not in out
    assert world["commands"][1:] == [["systemctl", "--user", "restart", "lastbell.service"]]


def test_pipx_failure_stops_before_any_restart(world):
    _units(world["home"], "lastbell.service")
    world["rc"]["pipx"] = 1
    assert upgrade.run(say=world["say"]) == 1
    assert "✗ pipx upgrade lastbell failed: boom" in world["output"]()
    assert len(world["commands"]) == 1


def test_no_restart_flag(world):
    _units(world["home"], "lastbell.service")
    assert upgrade.run(say=world["say"], no_restart=True) == 0
    assert "not restarting anything" in world["output"]()
    assert len(world["commands"]) == 1


def test_no_service_installed_says_so(world):
    assert upgrade.run(say=world["say"]) == 0
    assert "no service installed here" in world["output"]()
    assert len(world["commands"]) == 1


def test_restart_failure_is_reported_per_unit(world):
    _units(world["home"], "lastbell.service")
    world["rc"]["systemctl"] = 5
    assert upgrade.restart(say=world["say"]) == 0
    assert "✗ couldn't restart the poller (lastbell.service): boom" in world["output"]()


def test_darwin_kickstarts_the_agent(world, monkeypatch):
    monkeypatch.setattr(service, "platform_name", lambda: "darwin")
    assert upgrade.restart(say=world["say"]) == 0
    assert "no launchd agent installed" in world["output"]()
    plist = service.plist_path()
    plist.parent.mkdir(parents=True)
    plist.write_text("<plist/>")
    assert upgrade.restart(say=world["say"]) == 1
    assert world["commands"] == [["launchctl", "kickstart", "-k", "gui/501/com.noestudios.lastbell"]]
    assert "restarted the poller (launchd agent" in world["output"]()
    assert "dashboard, if one is running, needs restarting by hand" in world["output"]()


def test_windows_asks_for_a_manual_restart(world, monkeypatch):
    monkeypatch.setattr(service, "platform_name", lambda: "windows")
    assert upgrade.restart(say=world["say"]) == 0
    assert "by hand" in world["output"]()


def test_cli_restart_only(world, monkeypatch, capsys):
    import argparse
    _units(world["home"], "lastbell.service")
    assert cli._cmd_upgrade(argparse.Namespace(restart_only=True, no_restart=False)) == 0
    assert world["commands"] == [["systemctl", "--user", "restart", "lastbell.service"]]
    assert "restarted the poller" in capsys.readouterr().out
