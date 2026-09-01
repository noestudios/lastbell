"""Config sanity checks — including that no personal defaults leak from source."""
from __future__ import annotations

import pytest

from lastbell import config as cfg


def test_missing_required_raises(monkeypatch):
    monkeypatch.delenv("LASTBELL_DISTRICT", raising=False)
    monkeypatch.delenv("LASTBELL_USERNAME", raising=False)
    # Prevent a stray .env on the dev box from satisfying the requirement.
    monkeypatch.setattr(cfg, "load_dotenv", lambda *a, **k: None, raising=False)
    with pytest.raises(cfg.ConfigError):
        cfg.load()


def test_base_url_normalizes_host(monkeypatch):
    monkeypatch.setenv("LASTBELL_DISTRICT", "https://md-mcps-psv.edupoint.com/")
    monkeypatch.setenv("LASTBELL_USERNAME", "someone")
    conf = cfg.load()
    assert conf.base_url == "https://md-mcps-psv.edupoint.com"


def test_no_hardcoded_credentials_in_source():
    """Guard against re-introducing a real username or district as a default.

    Deliberately references no real values, so this test itself leaks nothing.
    The connection code must take district/username from config, never literals.
    """
    import pathlib

    pkg = pathlib.Path(cfg.__file__).parent
    for name in ("preflight.py", "client.py"):
        src = (pkg / name).read_text()
        assert ".edupoint.com" not in src, f"{name}: no district host may be hard-coded"
        assert 'default="' not in src, f"{name}: no argparse credential defaults"
    # preflight is a general tool (Phase 5): district/username come from flags
    # or the LASTBELL_* environment, never literals in source.
    src = (pkg / "preflight.py").read_text()
    assert "LASTBELL_DISTRICT" in src
    assert "LASTBELL_USERNAME" in src


def test_poll_floor_enforced(monkeypatch, capsys):
    """The good-neighbor floor: a too-eager LASTBELL_POLL_MINUTES is clamped,
    with a warning, so the README's portal-load promise holds by construction."""
    monkeypatch.setenv("LASTBELL_DISTRICT", "md-mcps-psv.edupoint.com")
    monkeypatch.setenv("LASTBELL_USERNAME", "someone")
    monkeypatch.setenv("LASTBELL_POLL_MINUTES", "1")
    conf = cfg.load()
    assert conf.poll_minutes == cfg.POLL_FLOOR_MINUTES
    assert "good-neighbor floor" in capsys.readouterr().err

    monkeypatch.setenv("LASTBELL_POLL_MINUTES", "60")
    assert cfg.load().poll_minutes == 60
    assert capsys.readouterr().err == ""


def test_placeholder_username_rejected(monkeypatch):
    monkeypatch.setenv("LASTBELL_DISTRICT", "md-mcps-psv.edupoint.com")
    monkeypatch.setenv("LASTBELL_USERNAME", "your_parentvue_username")
    with pytest.raises(cfg.ConfigError, match="placeholder"):
        cfg.load()
