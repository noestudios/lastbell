"""Platform data/config dirs and env-file resolution (install Phase 1)."""
from __future__ import annotations

import sys
from pathlib import Path

from lastbell import paths


def test_lastbell_home_overrides_both(monkeypatch, tmp_path):
    monkeypatch.setenv("LASTBELL_HOME", str(tmp_path / "stick"))
    assert paths.data_dir() == tmp_path / "stick"
    assert paths.config_dir() == tmp_path / "stick"
    assert paths.default_env_file() == tmp_path / "stick" / "env"


def test_platform_dirs_are_absolute_and_app_named(monkeypatch):
    monkeypatch.delenv("LASTBELL_HOME", raising=False)
    for d in (paths.data_dir(), paths.config_dir()):
        assert d.is_absolute()
        assert d.name == "lastbell"


def test_xdg_overrides_on_linux(monkeypatch, tmp_path):
    if sys.platform == "darwin":
        # macOS ignores XDG and uses Application Support for both roles.
        monkeypatch.delenv("LASTBELL_HOME", raising=False)
        assert "Application Support" in str(paths.data_dir())
        assert paths.config_dir() == paths.data_dir()
        return
    monkeypatch.delenv("LASTBELL_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    assert paths.data_dir() == tmp_path / "xdg-data" / "lastbell"
    assert paths.config_dir() == tmp_path / "xdg-config" / "lastbell"


def test_active_env_file_prefers_cwd_dotenv(monkeypatch, tmp_path):
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setenv("LASTBELL_HOME", str(home))
    monkeypatch.chdir(cwd)

    assert paths.active_env_file() is None

    installed = paths.default_env_file()
    installed.parent.mkdir(parents=True)
    installed.write_text("LASTBELL_DISTRICT=x\n")
    assert paths.active_env_file() == installed

    (cwd / ".env").write_text("LASTBELL_DISTRICT=y\n")
    assert paths.active_env_file() == Path(".env")
