# Handoff: Last Bell 0.3.0 — "runs anywhere" and "settings that live in the app"

A self-contained brief for the session that builds 0.3.0. Read it whole
before touching code. It states what to build, what is already there, what
the owner has decided, and what "done" means. Where it says *decide*, the
choice is yours; where it says *decided*, it is not.

## Ground rules (decided)

- **Nothing runs on its own.** No self-restart, no auto-upgrade, no
  phone-home. An upgrade is a person's choice; the restart belongs to
  `lastbell upgrade`. (Owner's call 2026-09-04; 0.2.10 tried otherwise and
  was withdrawn in 0.2.11.)
- **Email is the only notification channel in the web UI.** Text-message
  gateways are dead; no push apps for parents. Don't add channels.
- **Storage stays verbatim.** The portal's strings are stored raw; anything
  that interprets them lives in one helper (`models.course_grade` is the
  model). Don't "clean" data on the way in.
- **Commit and push finished, tested work to `main` without asking.** Tag a
  release only when the work is release-worthy (this whole brief is one
  release). Every tag needs a `## X.Y.Z — date` section in `CHANGELOG.md`
  written in plain words for parents first; `release.yml` lifts it into the
  GitHub Release. Bump `pyproject.toml` and `lastbell/__init__.py` together.
- **Tests and lint pass before every commit:** `.venv/bin/python -m pytest
  tests -q` and `.venv/bin/ruff check .`. Python floor is 3.9.
- **Verify in a browser, not by reading code.** `.claude/launch.json` has
  `dashboard-demo` (port 8323, `data/demo.db` from `lastbell seed-demo`).
  Restart it after editing Python; it does not reload modules.
- The owner runs the real thing on a Raspberry Pi (Bookworm, user
  `dcAgent`, two systemd user units: `lastbell.service` and
  `lastbell-dashboard.service`), upgraded with `lastbell upgrade`. You
  cannot reach it; give the owner commands to paste. PyPI's index is cached
  for 10 minutes, so an upgrade run right after a publish finds the
  previous version.

## Why 0.3.0

Every 0.2.x release made the product behave better. 0.3.0 changes what it
is: it can be obtained without Python (a published container image), and
it keeps its first household setting inside the app instead of a shell
variable. Together with the backlog item on upgrade restarts, that is the
release. Grade-trend charts are **not** in scope; they wait for a marking
period of real data (0.4.0). NAS/Umbrel/Home Assistant *packaging* is not
in scope either; the image is their prerequisite, and they come after.

## Workstream A — runs anywhere (a published, multi-arch container image)

### What exists

- `Dockerfile` (python:3.12-slim, installs the source tree with the
  `[service]` extra, sets `LASTBELL_DB_PATH=/data/lastbell.db`,
  `LASTBELL_SNAPSHOT_DIR=/data/snapshots`, `LASTBELL_SECRET_BACKEND=env`,
  `VOLUME /data`, entrypoint `lastbell`, default command `preflight`).
- `docker-compose.yml` with two services built from `.`: the poller
  (`run --loop`) and a loopback-published dashboard (`dashboard --host
  0.0.0.0`, `127.0.0.1:8321:8321`), secrets as files under `./secrets/`,
  settings from a git-ignored `.env`.
- `.github/workflows/release.yml`: tag → build wheel/sdist → PyPI trusted
  publishing → GitHub Release with notes from `CHANGELOG.md`.
- `lastbell/paths.py`: platform data/config dirs; `LASTBELL_HOME` overrides
  both; a checkout's `.env` takes precedence.
- `lastbell setup` (the wizard) already has an env-file fallback for boxes
  with no usable keyring, which is exactly a container.
- `lastbell upgrade` (`lastbell/upgrade.py`) runs `pipx upgrade lastbell`
  then restarts the systemd/launchd units; `lastbell status`
  (`lastbell/status.py`) reports version, service, last check, dashboard.
- README "The dashboard" section documents the network key
  (`lastbell dashboard --show-key`) and the hostname check.

Nothing publishes the image today; the compose file only builds locally,
and the wizard's config file would land inside the container, not on the
volume.

### Build this

1. **Image from the release artifact, published on tag.** Add a job to
   `release.yml` after `publish` that downloads the `dist` artifact and
   builds the image from the wheel (so the image is the PyPI release, not
   the tree), then pushes to `ghcr.io/noestudios/lastbell` with tags
   `X.Y.Z` and `latest`. Platforms: `linux/amd64` and `linux/arm64` are
   required (the owner's Pi is a 64-bit Bookworm); add `linux/arm/v7` only
   if the build stays reasonable. Use `docker/setup-qemu-action`,
   `docker/setup-buildx-action`, `docker/login-action` with
   `GITHUB_TOKEN` (`permissions: packages: write`), `docker/build-push-action`.
   Add OCI labels (`org.opencontainers.image.source`, `.version`,
   `.licenses`, `.description`). Make the Dockerfile accept either a wheel
   (release) or the tree (local `docker compose build`) — a build arg or a
   two-stage layout, your call. The package must become public on GHCR
   (first push makes it private; the owner flips it in the package
   settings — tell them).
2. **One volume holds everything.** Set `LASTBELL_HOME=/data` in the image
   so the database, snapshots, *and* the wizard's settings/env file all
   live on the mounted volume. Verify against `paths.py` that
   `LASTBELL_HOME` really governs both dirs and that the existing
   `LASTBELL_DB_PATH`/`LASTBELL_SNAPSHOT_DIR` lines become redundant (drop
   them if so). Run as a non-root user in the image; document the volume
   ownership line for a NAS.
3. **A first-run path that is three commands.** `docker compose run --rm
   lastbell setup` must work: the wizard detects no keyring, takes the
   env-file path, writes to the volume, runs preflight and the first
   collection, and *skips* the install-service step (say why in one
   line). Then `docker compose up -d`. Then the dashboard key:
   `docker compose exec dashboard lastbell dashboard --show-key`. Change
   `docker-compose.yml` to `image: ghcr.io/noestudios/lastbell:latest`
   with the `build: .` line kept as a comment for contributors. Keep the
   secrets-as-files design; the wizard may write the password into the
   env file on the volume (that is the env backend's documented trade-off)
   — decide whether the compose's secret-file route stays the recommended
   one and document one path, not two.
4. **The CLI knows it is in a container.** Set `LASTBELL_CONTAINER=1` in
   the image. `lastbell upgrade` there must not look for pipx or systemd:
   it prints the two compose commands (`docker compose pull` then
   `docker compose up -d`) and exits 0. `lastbell status` says "container
   image X.Y.Z" on its version line and the same upgrade hint.
   `lastbell install-service` says it is not applicable in a container.
   The footer's "X installed — restart to use it" badge must not appear in
   a container (there is no separate installed copy). Tests for each.
5. **Dashboard behind the bridge.** Inside the container every request
   comes from the Docker bridge, so the key is needed once and the
   hostname check must accept whatever name the host uses. Confirm the
   existing `LASTBELL_DASHBOARD_HOSTNAMES` covers it and document the one
   line a NAS user sets. A `HEALTHCHECK` on the dashboard service
   (TCP connect to 8321 is enough) and none on the poller (the heartbeat
   URL is its liveness).
6. **CI builds the image without pushing** on every push/PR (a
   `docker/build-push-action` step with `push: false`, amd64 only) so a
   broken Dockerfile fails before a tag does.
7. **README.** Keep pipx first (a parent on a Mac). Add a "Run it as a
   container" section right after: the three commands, the volume, the
   key, how to upgrade, and a one-line note that NAS/Umbrel/HA packaging
   comes later. Update the existing paragraph that mentions the
   Dockerfile. `INSTALL-PLAN.md` gets a Phase 4 note pointing here.

### Done means

- A tag builds and pushes a multi-arch image; `docker run --rm
  ghcr.io/noestudios/lastbell:X.Y.Z --version` prints the version on both
  an amd64 machine and the owner's Pi (they run it; you give the command).
- From an empty directory with only `docker-compose.yml`, the three
  commands produce a running poller and a dashboard reachable on the
  host's loopback with the key link.
- `lastbell upgrade`/`status`/`install-service` behave as in step 4, with
  tests.
- CI has the no-push image build.

## Workstream B — settings that live in the app (score cutoff first)

### What exists

- `LASTBELL_SCORE_CUTOFF` (default 70 = "below a C" on the MCPS scale, `0`
  disables) tints graded assignment rows in the dashboard. It is read in
  `lastbell/dashboard/render.py` (`_score_cutoff()` → `_low_class()`), from
  the environment, per call. It is display-only: nothing alerts on it.
- It is env-only, so a pipx install can't discover it: the wizard never
  asks, the README doesn't list it, `.env.example` is its only mention
  (line ~128). The backlog entry "Score cutoff as a real setting" in
  `BACKLOG.md` records the plan; this brief supersedes it.
- The database has a `meta` table (`key TEXT PRIMARY KEY, value TEXT`)
  with `store.set_meta` / `store.get_meta`, used today for the last-poll
  stamp.
- The Settings page (`lastbell/dashboard/settings.py`) renders cards:
  Watchers, Subscriptions, each a `card tablecard` with an `<h2>`. Since
  0.2.14 every manage table has one Save changes / Discard bar built by
  `_section_save(fid, action, label)`; fields bind to it via `form=` and
  `app.js` shows the bar when any field differs from what the server
  rendered (`.dirty` on `form.sectionform`). POST handlers live in
  `lastbell/dashboard/server.py` `_handle_settings_post`; success
  redirects to `/settings?ok=…` (a toast), failures to `?err=…` (a banner).
  The page's rule: **show only what the page can change**.

### Build this (decided unless marked)

1. **Storage.** A `settings` module (`lastbell/settings.py` or inside
   `store.py`, your call) with typed accessors over `meta` keys under a
   `setting.` prefix, e.g. `setting.score_cutoff`. Precedence: the
   database value if set; else the environment variable as the **seed**;
   else the default. Saving from the page writes the database and, from
   then on, the environment variable is ignored — `lastbell status` and
   the README say so in one line. No new table, no migration; an existing
   install picks its env value up as the seed on the first Settings render.
2. **The page.** A **Display** card on Settings, above Watchers or below
   Subscriptions (*decide* — above reads as "how the pages look", which is
   the more general thing). One field: "Tint scores below" with a number
   input (0–100, step 1; 0 or blank = off) and a one-line explanation
   ("Graded assignments under this percent are tinted on the student
   pages. 70 is a C on the MCPS scale. Nothing alerts on it."). It uses the
   same Save changes / Discard bar (`_section_save`) and a `display-save`
   action; the toast names the new value ("Scores below 65% are tinted" /
   "Score tint is off").
3. **The read path.** `_score_cutoff()` in `render.py` currently reads the
   environment with no connection in hand. Give the page context a
   `display` dict built once per request from the settings accessor and
   pass it to `_low_class`; do not open a second connection inside a cell
   renderer. Only the dashboard process reads it.
4. **Per-student override (optional; decide with the owner before
   building).** The backlog imagined one optional column on the same card.
   If you do it: keys `setting.score_cutoff.<student_id>`, a small
   per-student row under the household value, blank = inherit. If you
   don't, say so in the changelog entry ("household-wide for now").
5. **Alerting on it: no.** The backlog explicitly separates "a score fell
   below the line" as an alert type to be decided on purpose later. Do
   not add it here.
6. **Docs.** README configuration table gains the setting with "Settings
   page → Display; the env var only seeds it". `.env.example` keeps the
   line with that note. Remove the backlog entry (or mark shipped).

### Done means

- Changing the value on the page changes the tint on the student page on
  the next load, survives a dashboard restart, and ignores a later change
  to the env var. Tests for precedence (db > env > default), for the POST
  handler (valid, 0/blank, out-of-range → banner), and for the page markup
  (card present, bound to its section form).
- Verified in the demo dashboard in a browser.

## Workstream C — the backlog item that rides along

`lastbell upgrade` restarts the poller and dashboard after every pipx
call, even when pipx says "already at latest" (`upgrade.run` in
`lastbell/upgrade.py` only branches on the message). Restart only when the
installed version after pipx is newer than before, **or** when the running
copy is older than what is installed (`updates.restart_pending`, the case
`lastbell status` already reports); otherwise say "nothing to restart" and
stop. `--restart-only` stays the explicit override. Tests in
`tests/test_upgrade.py` (monkeypatch pipx and the unit restarts as the
existing tests do). Combine with A.4's container branch.

## Release checklist for 0.3.0

1. All three workstreams merged to `main`, suite and lint green, demo site
   builds (`scripts/build_demo_site.py --out /tmp/site --base /lastbell`).
2. `CHANGELOG.md` `## 0.3.0 — <date>`: one paragraph that says what the
   release *is* (obtainable without Python; the first setting the app keeps
   itself; upgrade only restarts when there is something to restart), then
   bullets. Note for existing installs: nothing changes on disk; the score
   cutoff env value is picked up as the seed.
3. README install section reflects both paths; screenshots that show
   Settings are refreshed (`scripts/render_emails.py` is for emails; the
   dashboard screenshots are taken by hand from the demo — ask the owner
   or take them via the browser tools) and the static demo will rebuild on
   push (`pages.yml`).
4. Bump both version strings, commit "Release 0.3.0: …", tag `v0.3.0`,
   push branch and tag, watch `release.yml` (`gh run watch`) through the
   new image job, confirm the GHCR package is public.
5. Give the owner the Pi command (`lastbell upgrade`, minding the 10-minute
   index cache) and the container smoke-test command; they verify on
   their side and report back.
6. Then, and only then, the public post plan in the owner's notes can move
   (GHCR image was its gate).

## Things that look like scope and aren't

- Grade-trend charts, overview sparklines (0.4.0).
- Umbrel / Home Assistant add-on / NAS app packaging (after the image).
- Any new alert type, any new channel, anything that acts without a person.
- Per-assignment anchors on the student page (owner declined 2026-09-05;
  if ever wanted, extend the existing `?status=…#hit` server-side
  highlight with `?item=<gu>`, don't add per-row ids).
