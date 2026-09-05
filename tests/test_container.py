"""The container image (0.3.0): the facts the CLI's container branches rely
on, pinned where they are declared — the Dockerfile, the compose file, and
the two workflows — plus the one runtime claim the image makes, that
LASTBELL_HOME=/data alone puts the database, the snapshots, and the settings
file on the volume. Docker itself is not needed here; ci.yml builds and
runs the image without pushing on every push."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from lastbell import config as cfg
from lastbell import paths

ROOT = Path(__file__).resolve().parents[1]
GHCR = "ghcr.io/noestudios/lastbell"


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return (ROOT / "Dockerfile").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose() -> str:
    return (ROOT / "docker-compose.yml").read_text(encoding="utf-8")


def _uncommented(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _env_block(dockerfile: str) -> str:
    """Every ENV line of the runtime stage, joined."""
    runtime = dockerfile.split("AS runtime", 1)[1].replace("\\\n", " ")  # join continuations
    return " ".join(re.findall(r"^ENV\s+(.*)$", runtime, re.M))


def test_one_volume_holds_everything(monkeypatch, tmp_path):
    """LASTBELL_HOME governs both dirs, so the image's single ENV line puts
    the database, snapshots, *and* the wizard's settings file on /data —
    the explicit LASTBELL_DB_PATH / LASTBELL_SNAPSHOT_DIR lines are gone."""
    monkeypatch.chdir(tmp_path)                            # no checkout .env
    for key in ("LASTBELL_DB_PATH", "LASTBELL_SNAPSHOT_DIR", "XDG_DATA_HOME",
                "XDG_CONFIG_HOME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LASTBELL_HOME", "/data")
    monkeypatch.setenv("LASTBELL_DISTRICT", "x.example")
    monkeypatch.setenv("LASTBELL_USERNAME", "parent")
    monkeypatch.setenv("LASTBELL_SECRET_BACKEND", "env")
    conf = cfg.load()
    assert conf.db_path == Path("/data/lastbell.db")
    assert conf.snapshot_dir == Path("/data/snapshots")
    assert paths.default_env_file() == Path("/data/env")
    assert paths.data_dir() == paths.config_dir() == Path("/data")


def test_dockerfile_declares_what_the_cli_assumes(dockerfile):
    env = _env_block(dockerfile)
    assert "LASTBELL_HOME=/data" in env
    assert "LASTBELL_CONTAINER=1" in env
    assert "LASTBELL_SECRET_BACKEND=env" in env
    assert "LASTBELL_DB_PATH" not in dockerfile and "LASTBELL_SNAPSHOT_DIR" not in dockerfile
    assert 'VOLUME ["/data"]' in dockerfile
    assert re.search(r"^USER lastbell$", dockerfile, re.M)           # unprivileged
    assert "--uid 1000" in dockerfile                                 # the documented chown id
    assert re.search(r"^ARG SOURCE=tree$", dockerfile, re.M)         # tree by default…
    assert "FROM build-${SOURCE} AS build" in dockerfile              # …or the wheel
    assert "COPY dist/ /tmp/dist/" in dockerfile
    assert 'ENTRYPOINT ["lastbell"]' in dockerfile
    assert "HEALTHCHECK" not in dockerfile          # compose puts it on the dashboard only
    for label in ("org.opencontainers.image.source=\"https://github.com/noestudios/lastbell\"",
                  "org.opencontainers.image.licenses=\"MIT\"",
                  "org.opencontainers.image.version=", "org.opencontainers.image.description="):
        assert label in dockerfile, label


def test_dockerignore_admits_only_what_the_dockerfile_copies():
    lines = [line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()
             if line.strip() and not line.startswith("#")]
    assert lines[0] == "*"
    assert {"!pyproject.toml", "!README.md", "!lastbell/", "!dist/"} <= set(lines)


def test_compose_pulls_the_published_image_and_keeps_the_build_line_as_a_comment(compose):
    live = _uncommented(compose)
    assert live.count(f"image: {GHCR}:latest") == 2
    assert "build:" not in live and "# build: ." in compose
    assert "env_file" not in live                    # settings live in data/env, on the volume
    assert "secrets:" not in live                     # one documented path, not two
    assert "LASTBELL_DASHBOARD_KEY" not in live       # no shared default key
    assert "127.0.0.1:8321:8321" in live              # loopback publish, widened on purpose
    assert "TZ:" in live
    assert "LASTBELL_DASHBOARD_HOSTNAMES" in compose  # the one NAS line, shown
    poller, dashboard = live.split("dashboard:", 1)
    assert "healthcheck" not in poller and "healthcheck" in dashboard
    assert "8321" in dashboard.split("healthcheck", 1)[1]
    assert "docker compose run --rm lastbell setup" in compose
    assert "chown -R 1000:1000" in compose            # the volume ownership line


def test_release_workflow_pushes_a_multi_arch_image_from_the_wheel():
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    job = text.split("\n  image:", 1)[1]
    assert "needs: publish" in job
    assert "packages: write" in job
    assert "docker/setup-qemu-action" in job and "docker/setup-buildx-action" in job
    assert "docker/login-action" in job and "registry: ghcr.io" in job
    assert f"images: {GHCR}" in job
    assert "linux/amd64,linux/arm64" in job
    assert "SOURCE=wheel" in job
    assert "push: true" in job
    assert "name: dist" in job                        # the artifact the PyPI job published


def test_ci_builds_the_image_without_pushing():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    job = text.split("\n  image:", 1)[1]
    assert "push: false" in job and "push: true" not in job
    assert "platforms:" not in job                    # amd64 only, the runner's own
    assert "SOURCE=wheel" in job                      # both ways the release can build
    assert "docker run --rm" in job                   # …and each result runs once
