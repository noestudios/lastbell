"""Config sanity checks — including that no personal defaults leak from source."""
from __future__ import annotations

import pytest

from gradewatch import config as cfg


def test_missing_required_raises(monkeypatch):
    monkeypatch.delenv("GRADEWATCH_DISTRICT", raising=False)
    monkeypatch.delenv("GRADEWATCH_USERNAME", raising=False)
    # Prevent a stray .env on the dev box from satisfying the requirement.
    monkeypatch.setattr(cfg, "load_dotenv", lambda *a, **k: None, raising=False)
    with pytest.raises(cfg.ConfigError):
        cfg.load()


def test_base_url_normalizes_host(monkeypatch):
    monkeypatch.setenv("GRADEWATCH_DISTRICT", "https://md-mcps-psv.edupoint.com/")
    monkeypatch.setenv("GRADEWATCH_USERNAME", "someone")
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
    # preflight must be config-driven
    assert "cfg.load()" in (pkg / "preflight.py").read_text()
