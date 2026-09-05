# syntax=docker/dockerfile:1
# Last Bell's container image — the same file builds two ways:
#
#   release   .github/workflows/release.yml downloads the wheel the tag just
#             published to PyPI into dist/ and builds with SOURCE=wheel, so
#             the image *is* the PyPI release, not the working tree.
#   local     `docker build .` or `docker compose build` (uncomment `build: .`
#             in docker-compose.yml) installs the working tree: SOURCE=tree,
#             the default.
#
# Pure Python all the way down (requests, dotenv, keyring, APScheduler), so
# the one file builds linux/amd64 and linux/arm64 — a 64-bit Raspberry Pi —
# under QEMU in minutes. Needs BuildKit (Docker 23+ / any Compose v2): only
# the stage SOURCE names is built, so a tree build never looks for dist/.
ARG SOURCE=tree
ARG PYTHON=3.12

FROM python:${PYTHON}-slim AS base
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
RUN python -m venv /opt/lastbell

# ── the two ways in ───────────────────────────────────────────────────
FROM base AS build-tree
WORKDIR /src
COPY pyproject.toml README.md ./
COPY lastbell ./lastbell
RUN /opt/lastbell/bin/pip install ".[service]"

FROM base AS build-wheel
COPY dist/ /tmp/dist/
RUN set -eu; wheel="$(ls /tmp/dist/lastbell-*.whl)"; \
    /opt/lastbell/bin/pip install "${wheel}[service]"

FROM build-${SOURCE} AS build

# ── what ships ────────────────────────────────────────────────────────
FROM python:${PYTHON}-slim AS runtime
ARG VERSION=dev
LABEL org.opencontainers.image.title="Last Bell" \
      org.opencontainers.image.description="Self-hosted ParentVUE + Canvas grade and assignment monitor for parents: email alerts and a private dashboard, run at home." \
      org.opencontainers.image.source="https://github.com/noestudios/lastbell" \
      org.opencontainers.image.url="https://github.com/noestudios/lastbell" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"

COPY --from=build /opt/lastbell /opt/lastbell

# Unprivileged, with a fixed id so a bind-mounted volume can be handed to it
# from the host: `chown -R 1000:1000 data` (the line a NAS usually needs).
RUN useradd --uid 1000 --user-group --create-home --shell /usr/sbin/nologin lastbell \
    && mkdir -p /data && chown lastbell:lastbell /data

# One volume holds everything. LASTBELL_HOME governs both the data dir (the
# database, snapshots) and the config dir (the settings file `lastbell setup`
# writes), so /data/lastbell.db, /data/snapshots, and /data/env all land on
# the mount — nothing of yours lives in the container. There is no OS keyring
# in an image, so the password lives in that owner-only settings file (the
# env backend); LASTBELL_PASSWORD_FILE still works for those who prefer a
# Docker secret. LASTBELL_CONTAINER tells the CLI that pipx, systemd, and
# "restart to use it" don't apply here.
ENV PATH="/opt/lastbell/bin:${PATH}" \
    HOME=/home/lastbell \
    PYTHONUNBUFFERED=1 \
    LASTBELL_HOME=/data \
    LASTBELL_SECRET_BACKEND=env \
    LASTBELL_CONTAINER=1
VOLUME ["/data"]
WORKDIR /data
USER lastbell

# docker-compose.yml runs two of these: `run --loop` (the poller) and
# `dashboard --host 0.0.0.0` (published on the host's loopback). Bare, the
# image answers `--version`; `docker compose run --rm lastbell setup` is the
# first run.
ENTRYPOINT ["lastbell"]
CMD ["--version"]
