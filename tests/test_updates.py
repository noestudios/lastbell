"""The on-demand update check (0.1.4): version comparison and the one PyPI
request, with the network stubbed."""
from __future__ import annotations

import pytest

from lastbell import __version__, updates


@pytest.mark.parametrize("current,latest,expected", [
    ("0.1.3", "0.1.4", "newer"),
    ("0.1.3", "0.1.3", "current"),
    ("0.1.10", "0.1.9", "ahead"),          # numeric, not lexical
    ("0.1.3", "0.2", "newer"),
    ("0.2.0", "0.2", "current"),
    ("0.1.4.dev0", "0.1.4", "newer"),      # a checkout ahead of its release
    ("0.1.4", "0.1.4rc1", "ahead"),
])
def test_compare(current, latest, expected):
    assert updates.compare(current, latest) == expected


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_latest_version_fetches_pypi_once_with_a_version_ua(monkeypatch):
    import requests
    calls = []

    def fake_get(url, timeout, headers):
        calls.append((url, timeout, headers))
        return _Resp({"info": {"version": "9.9.9"}})
    monkeypatch.setattr(requests, "get", fake_get)
    assert updates.check() == ("newer", "9.9.9")
    (url, timeout, headers), = calls
    assert url == updates.PYPI_JSON and timeout == 5.0
    assert headers["User-Agent"] == f"lastbell/{__version__}"


def test_latest_version_failures_are_one_error_type(monkeypatch):
    import requests

    def down(url, timeout, headers):
        raise requests.ConnectionError("nope")
    monkeypatch.setattr(requests, "get", down)
    with pytest.raises(updates.UpdateCheckError, match="couldn't reach PyPI"):
        updates.latest_version()

    monkeypatch.setattr(requests, "get", lambda url, timeout, headers: _Resp({}, 503))
    with pytest.raises(updates.UpdateCheckError):
        updates.latest_version()

    monkeypatch.setattr(requests, "get",
                        lambda url, timeout, headers: _Resp({"info": {}}))
    with pytest.raises(updates.UpdateCheckError, match="no version"):
        updates.latest_version()


def test_describe_wording():
    assert "pipx upgrade lastbell" in updates.describe("newer", "9.9.9")
    assert __version__ in updates.describe("current", __version__)
    assert "nothing to do" in updates.describe("ahead", "0.0.1")


def test_restart_hint_names_both_processes_per_platform():
    linux = updates.restart_hint("linux")
    assert "systemctl --user restart lastbell" in linux and "lastbell-dashboard" in linux
    assert "lastbell install-service" in updates.restart_hint("darwin")
    assert "poller and the dashboard" in updates.restart_hint("windows")
    assert "lastbell-dashboard" in updates.describe("newer", "9.9.9", plat="linux")


def test_upgraded_but_not_restarted_is_told_to_restart_not_upgrade():
    """pipx upgrade replaced the files under this still-running process:
    the on-disk version is newer than the loaded one."""
    msg = updates.describe("newer", "9.9.9", installed="9.9.9", plat="linux")
    assert msg.startswith("Last Bell 9.9.9 is installed, but this dashboard is still running")
    assert __version__ in msg and "systemctl --user restart lastbell" in msg
    assert "pipx upgrade" not in msg
    # Disk newer than running, PyPI newer still: restart first, mention the rest.
    assert "(9.9.10 is also out on PyPI)" in updates.describe(
        "newer", "9.9.10", installed="9.9.9", plat="linux")
    assert updates.restart_pending("9.9.9")


def test_a_checkout_whose_dist_info_lags_is_not_a_pending_restart():
    """`pip install -e .` leaves a dist-info that goes stale as the version
    bumps; older on disk than in memory means nothing to restart."""
    assert not updates.restart_pending("0.0.1")
    assert not updates.restart_pending(None)
    assert "pipx upgrade lastbell" in updates.describe("newer", "9.9.9", installed="0.0.1")


def test_installed_version_reads_the_disk_each_call(monkeypatch):
    import importlib.metadata as metadata

    seen = iter(["1.0.0", "2.0.0"])
    monkeypatch.setattr(metadata, "version", lambda name: next(seen))
    assert updates.installed_version() == "1.0.0"
    assert updates.installed_version() == "2.0.0"

    def missing(name):
        raise metadata.PackageNotFoundError(name)
    monkeypatch.setattr(metadata, "version", missing)
    assert updates.installed_version() is None


def test_settings_footer_flags_a_pending_restart():
    from lastbell import dashboard

    html = dashboard.render_settings([], [], installed="9.9.9")
    assert "9.9.9 installed — restart to use it" in html
    assert "installed — restart" not in dashboard.render_settings([], [], installed="0.0.1")
    assert "installed — restart" not in dashboard.render_settings([], [])
