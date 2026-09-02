"""`lastbell install-service` (install Phase 3b): generated unit/plist text,
and install/uninstall flows against a fake home with every subprocess
recorded instead of run."""
from __future__ import annotations

import plistlib
import shutil
import subprocess
import sys

import pytest

from lastbell import cli, service

EXE = "/home/pi/.local/bin/lastbell"


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Fake home + data dir, canned executable, recorded commands."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(service, "_home", lambda: home)
    monkeypatch.setenv("LASTBELL_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("LASTBELL_SECRET_BACKEND", raising=False)
    monkeypatch.chdir(tmp_path)                       # no checkout .env here
    monkeypatch.setattr(service, "executable", lambda: EXE)
    monkeypatch.setattr(service, "_host_is_utc", lambda: False)
    monkeypatch.setattr(service, "_secret_backend", lambda: "env")
    monkeypatch.setattr(service.getpass, "getuser", lambda: "pi")
    if not hasattr(service.os, "getuid"):             # Windows CI
        monkeypatch.setattr(service.os, "getuid", lambda: 501, raising=False)

    w = {"home": home, "commands": [], "said": [], "rc": {}}

    def fake_run(cmd):
        w["commands"].append(cmd)
        rc = w["rc"].get(cmd[0] + " " + cmd[-1], w["rc"].get(cmd[0], 0))
        return subprocess.CompletedProcess(cmd, rc, "", "boom" if rc else "")
    monkeypatch.setattr(service, "_run", fake_run)
    w["say"] = w["said"].append
    w["output"] = lambda: "\n".join(w["said"])
    return w


def linux(monkeypatch):
    monkeypatch.setattr(service, "platform_name", lambda: "linux")


def darwin(monkeypatch):
    monkeypatch.setattr(service, "platform_name", lambda: "darwin")


# ── generated text ────────────────────────────────────────────────────


def test_systemd_unit_content():
    text = service.systemd_unit(EXE)
    assert f"ExecStart={EXE} run --loop" in text
    assert "Restart=on-failure" in text
    assert "RestartSec=60" in text
    assert "After=network-online.target" in text
    assert "WantedBy=default.target" in text          # user units, not multi-user
    assert "WorkingDirectory" not in text
    assert "WorkingDirectory=/srv/lastbell" in service.systemd_unit(
        EXE, service.Path("/srv/lastbell"))


def test_launchd_plist_content_and_validity(tmp_path):
    text = service.launchd_plist(EXE, tmp_path / "lastbell.log")
    entry = plistlib.loads(text.encode())
    assert entry["Label"] == "com.noestudios.lastbell"
    assert entry["ProgramArguments"] == [EXE, "run", "--loop"]
    assert entry["RunAtLoad"] is True and entry["KeepAlive"] is True
    assert entry["StandardOutPath"] == str(tmp_path / "lastbell.log")
    assert "WorkingDirectory" not in entry
    if shutil.which("plutil"):                        # macOS: the real linter
        f = tmp_path / "x.plist"
        f.write_text(text)
        subprocess.run(["plutil", "-lint", str(f)], check=True,
                       capture_output=True)


def test_schtasks_commands_quote_the_executable():
    create, delete = service.schtasks_commands(r"C:\Users\p\bin\lastbell.exe")
    assert create.startswith("schtasks /Create") and "ONLOGON" in create
    assert r'\"C:\Users\p\bin\lastbell.exe\" run --loop' in create
    assert delete.startswith("schtasks /Delete")


# ── Linux ─────────────────────────────────────────────────────────────


def test_linux_install_writes_user_unit_and_enables(world, monkeypatch):
    linux(monkeypatch)
    assert service.install(say=world["say"]) == 0
    unit = world["home"] / ".config" / "systemd" / "user" / "lastbell.service"
    assert unit.is_file()
    assert f"ExecStart={EXE} run --loop" in unit.read_text()
    assert world["commands"] == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "lastbell.service"],
        ["loginctl", "enable-linger", "pi"],
    ]
    out = world["output"]()
    assert "lingering enabled" in out and "journalctl" in out
    assert "⚠" not in out


def test_linux_install_honors_xdg_config_home(world, monkeypatch, tmp_path):
    linux(monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert service.install(say=world["say"]) == 0
    assert (tmp_path / "xdg" / "systemd" / "user" / "lastbell.service").is_file()


def test_linux_install_from_checkout_pins_working_directory(world, monkeypatch, tmp_path):
    linux(monkeypatch)
    (tmp_path / ".env").write_text("LASTBELL_DISTRICT=x\n")
    assert service.install(say=world["say"]) == 0
    text = service.unit_path().read_text()
    assert f"WorkingDirectory={tmp_path}" in text


def test_linux_warnings_are_non_blocking(world, monkeypatch):
    linux(monkeypatch)
    monkeypatch.setattr(service, "_host_is_utc", lambda: True)
    monkeypatch.setattr(service, "_secret_backend", lambda: "keyring")
    assert service.install(say=world["say"]) == 0
    out = world["output"]()
    assert "timedatectl set-timezone" in out
    assert "keyring" in out and "lastbell setup" in out
    assert service.unit_path().is_file()


def test_linux_enable_failure_reports_and_returns_1(world, monkeypatch):
    linux(monkeypatch)
    world["rc"]["systemctl lastbell.service"] = 1
    assert service.install(say=world["say"]) == 1
    assert "enable --now failed: boom" in world["output"]()
    assert ["loginctl", "enable-linger", "pi"] not in world["commands"]


def test_linux_linger_failure_warns_with_sudo_hint(world, monkeypatch):
    linux(monkeypatch)
    world["rc"]["loginctl"] = 1
    assert service.install(say=world["say"]) == 0
    assert "sudo loginctl enable-linger pi" in world["output"]()


def test_linux_print_only_touches_nothing(world, monkeypatch):
    linux(monkeypatch)
    monkeypatch.setattr(service, "_host_is_utc", lambda: True)
    assert service.install(print_only=True, say=world["say"]) == 0
    assert not service.unit_path().exists()
    assert world["commands"] == []
    out = world["output"]()
    assert "[Service]" in out and "systemctl --user enable --now" in out
    assert "note:" in out                          # warnings still shown


def test_linux_uninstall(world, monkeypatch):
    linux(monkeypatch)
    service.install(say=world["say"])
    world["commands"].clear()
    assert service.uninstall(say=world["say"]) == 0
    assert not service.unit_path().exists()
    assert world["commands"] == [
        ["systemctl", "--user", "disable", "--now", "lastbell.service"],
        ["systemctl", "--user", "daemon-reload"],
    ]
    assert "disable-linger pi" in world["output"]()   # left on, told how to undo
    # a second uninstall is a no-op, not an error
    world["commands"].clear()
    assert service.uninstall(say=world["say"]) == 0
    assert world["commands"] == []


def test_linux_uninstall_print_only(world, monkeypatch):
    linux(monkeypatch)
    service.install(say=world["say"])
    world["commands"].clear()
    assert service.uninstall(print_only=True, say=world["say"]) == 0
    assert service.unit_path().is_file() and world["commands"] == []


# ── macOS ─────────────────────────────────────────────────────────────


def test_darwin_install_writes_agent_and_bootstraps(world, monkeypatch):
    darwin(monkeypatch)
    monkeypatch.setattr(service.os, "getuid", lambda: 501)
    assert service.install(say=world["say"]) == 0
    plist = world["home"] / "Library" / "LaunchAgents" / "com.noestudios.lastbell.plist"
    assert plist.is_file()
    entry = plistlib.loads(plist.read_bytes())
    assert entry["ProgramArguments"] == [EXE, "run", "--loop"]
    assert entry["StandardOutPath"] == str(service.log_path())
    assert service.log_path().parent.is_dir()
    assert world["commands"] == [
        ["launchctl", "bootout", "gui/501/com.noestudios.lastbell"],
        ["launchctl", "bootstrap", "gui/501", str(plist)],
    ]


def test_darwin_bootout_failure_is_ignored_but_bootstrap_failure_is_not(world, monkeypatch):
    darwin(monkeypatch)
    monkeypatch.setattr(service.os, "getuid", lambda: 501)
    world["rc"]["launchctl gui/501/com.noestudios.lastbell"] = 3   # not loaded yet
    assert service.install(say=world["say"]) == 0
    world["rc"]["launchctl " + str(service.plist_path())] = 5
    assert service.install(say=world["say"]) == 1
    assert "bootstrap failed" in world["output"]()


def test_darwin_uninstall(world, monkeypatch):
    darwin(monkeypatch)
    monkeypatch.setattr(service.os, "getuid", lambda: 501)
    service.install(say=world["say"])
    world["commands"].clear()
    assert service.uninstall(say=world["say"]) == 0
    assert not service.plist_path().exists()
    assert world["commands"] == [
        ["launchctl", "bootout", "gui/501/com.noestudios.lastbell"]]


# ── Windows: print, never run ─────────────────────────────────────────


def test_windows_prints_schtasks_only(world, monkeypatch):
    monkeypatch.setattr(service, "platform_name", lambda: "windows")
    assert service.install(say=world["say"]) == 0
    assert service.uninstall(say=world["say"]) == 0
    assert world["commands"] == []
    out = world["output"]()
    assert "schtasks /Create" in out and "schtasks /Delete" in out
    assert not list(world["home"].rglob("*"))


# ── resolving the launcher ────────────────────────────────────────────


def test_executable_prefers_path_then_argv0(monkeypatch, tmp_path):
    monkeypatch.setattr(service.shutil, "which", lambda name: "bin/lastbell")
    assert service.executable().endswith("/bin/lastbell")
    assert service.executable().startswith("/")

    monkeypatch.setattr(service.shutil, "which", lambda name: None)
    fake = tmp_path / "lastbell"
    fake.write_text("")
    monkeypatch.setattr(sys, "argv", [str(fake)])
    assert service.executable() == str(fake.resolve())

    monkeypatch.setattr(sys, "argv", ["pytest"])
    with pytest.raises(service.ServiceError, match="pipx ensurepath"):
        service.executable()


# ── CLI wiring ────────────────────────────────────────────────────────


def test_cli_install_service_print(world, monkeypatch, capsys):
    linux(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["lastbell", "install-service", "--print"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Would write" in out and "run --loop" in out
    assert world["commands"] == []


def test_cli_uninstall_print(world, monkeypatch, capsys):
    darwin(monkeypatch)
    monkeypatch.setattr(service.os, "getuid", lambda: 501)
    monkeypatch.setattr(sys, "argv",
                        ["lastbell", "install-service", "--uninstall", "--print"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert "launchctl bootout" in capsys.readouterr().out
