# Install plan — from "clone and venv" to "paste one line, answer questions"

> **Status (2026-09-01): both phases implemented.** Phase 1: package audit
> done (wheel carries all package data; suite passes 3.9→3.14, one f-string
> fixed for the 3.9 floor; `twine check` passes), metadata + classifiers +
> absolute README links done, data/env defaults moved to platform user dirs
> (`lastbell/paths.py`; checkout `.env` still wins), release + CI workflows
> in `.github/workflows/`. Phase 2: `lastbell setup` wizard shipped with
> tests. **Remaining, human-only:** create the GitHub repo and push (no
> remote configured yet), manual first `twine upload` to claim `lastbell` on
> PyPI, then enable trusted publishing for `release.yml` (env `pypi`).
> The stretch `install-service` item is still open, deliberately.

Goal: a non-techie MCPS parent gets Last Bell running without editing any
file by hand. Two phases, each shippable alone. (Decided against for now: a
signed desktop app — real engineering + notarization overhead; and a hosted
service — forbidden by the trust story, credentials never leave the house.)

## Phase 1 — publish to PyPI (`pipx install lastbell`)

Turns the venv/pip/clone dance into one paste. The name was free at rename
time; the entry points (`lastbell`, `parentvue-preflight`) already exist.

1. **Package audit.** `python -m build` a wheel + sdist and install into a
   clean venv: confirm `lastbell/mcps_schools.json`, `style.css`, `app.js`,
   and `schema.sql` are inside the wheel (package-data — the likely gotcha;
   `[tool.setuptools.package-data]` as needed) and every command runs from
   the installed copy, not the repo. `requires-python`: verify 3.9 truly
   passes the test suite or raise the floor honestly.
2. **Metadata.** `[project.urls]` (done), classifiers, a PyPI-appropriate
   long description (README renders fine, but check relative links — badge
   and doc links must be absolute to render on pypi.org).
3. **Data location.** Installed-from-PyPI means no repo checkout: default
   `LASTBELL_DB_PATH`/snapshots must move from `data/` (cwd-relative) to a
   platform user-data dir (`~/.local/share/lastbell` /
   `~/Library/Application Support/lastbell` / `%APPDATA%`), overridable as
   today. `.env` likewise gains a default home (`~/.config/lastbell/env` or
   similar) — the wizard (Phase 2) writes it there.
4. **Release mechanics.** Git tag → GitHub Actions with PyPI *trusted
   publishing* (no long-lived token). Manual first release to claim the
   name.
5. **Docs.** Quickstart becomes: `pipx install lastbell` (with a one-line
   "get pipx" pointer per OS) → `lastbell setup` → `lastbell run --loop`.

## Phase 2 — `lastbell setup` wizard

Kills the actual scariest step: hand-editing a dotfile. Interactive Q&A in
the terminal, writing `.env` + keyring itself. Re-runnable (shows current
values as defaults); plain `getpass`/`input()` — no new dependencies.

Flow:
1. **District.** Ask for the portal hostname with MCPS as the offered
   default (this project's audience); anyone else pastes theirs. Run the
   anonymous preflight probe immediately — instant "that's a Synergy portal
   ✓" feedback before asking anything personal.
2. **Credentials.** Username, then password via hidden prompt straight into
   the OS keyring (reuse `set-password`); never echoed, never written to
   disk — say so in the prompt, it's the trust story out loud.
3. **Verify.** Full preflight (login + data path + parsers) with the
   human-readable verdict. On `go`: continue. On failure: the
   plain-language error + what to fix, and the partial `.env` still saved
   so re-running resumes.
4. **Notifications.** Pick a channel, easiest first:
   - **ntfy (recommended):** zero signup — wizard generates a long random
     topic, prints it plus the two-step phone instruction (install ntfy
     app, subscribe to the topic), sends a test push.
   - **email/text:** asks SMTP host/port/user/password (password to a
     keyring slot, not `.env`) and the to-address; explains the carrier
     gateway for SMS. This is the techie path and is labeled as such.
   - **console:** "just the dashboard, no pushes."
   Then send a test message and confirm receipt before finishing.
5. **First run.** Offer to run the baseline collection now, explaining
   "first run learns the current state; alerts start with the next change."
   Finish by printing the two commands that matter (`lastbell run --loop`,
   `lastbell dashboard`) and where the data lives.

Stretch (post-wizard, likely worth it): `lastbell install-service` writing
the launchd plist / systemd unit / Task Scheduler entry for `run --loop`,
so "keep it running" stops being the user's problem. Separate decision —
it touches system config and needs its own care.

## Acceptance

A test parent on a clean machine goes from nothing → first test
notification in under 10 minutes using only: install pipx, `pipx install
lastbell`, `lastbell setup`. No file edited by hand, no README required
beyond the quickstart block.

## Phase 3 — the always-on box (Raspberry Pi, headless or desktop)

*Handoff written 2026-09-01 for a fresh session. State at handoff: Phases 1–2
shipped; `lastbell` 0.1.0 is on PyPI (tag → `release.yml`, trusted
publisher); repo is private at `noestudios/lastbell` (`origin`), suite is
224 tests green except one known UTC/local-date flake in
`test_recent_view_groups_by_day…` being fixed on branch
`claude/awesome-fermi-ca8e9c` in another session — check whether it merged.
Commit trailer in use: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
Demo server for eyeballing UI: launch config `dashboard-demo-2` (port 8329,
`data/demo.db` from `lastbell seed-demo --db data/demo.db`).*

Motivation: the owner wants this on a Pi that also runs Pi-hole (with a
desktop session). Two things stand in the way.

### 3a. `lastbell setup` must survive a machine with no usable keyring

Today `setup_wizard._step_credentials` calls `secretstore.get_password`
(catching only `SecretError`) and `secretstore.set_password` (which calls
`keyring.set_password` directly). On a box with no Secret Service backend,
`keyring` raises `keyring.errors.NoKeyringError` (or reports the
`fail.Keyring` backend) → the wizard dies with a traceback. Even where a
desktop keyring exists, a daemon started at boot runs outside the login
session and cannot unlock it — so for an always-on install the keyring is
the wrong store regardless.

Build:
1. A probe in `secrets.py` (`keyring_available() -> bool`): try
   `keyring.get_keyring()` and reject the fail backend / `NoKeyringError`.
2. In the wizard's credentials step: if the probe fails, **or** the user says
   the service will run unattended on Linux (ask: "Will Last Bell run as a
   background service on this machine?"), offer the env-file fallback —
   explain in one sentence that the password then lives in the settings
   file (mode 0600) instead of a keyring, write
   `LASTBELL_SECRET_BACKEND=env` + `LASTBELL_PASSWORD=…` via `write_env`
   (which already chmods 0600). Same treatment for the SMTP password
   (`LASTBELL_PASSWORD_SMTP`, read by `secrets.get_smtp_password`).
3. `secrets.get_password`'s error text should mention the `env` backend.
4. Tests in `tests/test_setup.py` use the existing `Script` fixture pattern
   (scripted `_ask/_ask_yn/_getpass`, stubbed keyring); add cases for the
   fallback path and assert the password never lands in the file when the
   keyring path is taken.
5. README "Configuration & secrets" table: add the always-on/Pi row with the
   stated trade-off (the trust story must stay honest).

### 3b. `lastbell install-service`

"Keep it running" is still the user's problem. Build a subcommand that
writes and enables the right thing, with `--print` (show, don't install)
and `--uninstall`:

- **Linux** → a *user* unit at `~/.config/systemd/user/lastbell.service`
  (no sudo), then `systemctl --user enable --now lastbell` and
  `loginctl enable-linger $USER` so it runs at boot without a login.
  `ExecStart` is the resolved `lastbell` executable
  (`shutil.which` / `sys.argv[0]`; under pipx it's `~/.local/bin/lastbell`)
  with `run --loop`; `Restart=on-failure`, `RestartSec=60`,
  `After=network-online.target`. Warn (don't block) when the host timezone
  is UTC — digests use the local clock (Pi-hole images often ship UTC;
  fix is `timedatectl set-timezone …`).
- **macOS** → launchd agent `~/Library/LaunchAgents/com.noestudios.lastbell.plist`
  (`RunAtLoad`, `KeepAlive`), loaded with `launchctl bootstrap gui/$UID`.
  Validate generated plists with `plutil -lint` in a test.
- **Windows** → print the `schtasks` command rather than run it (keep scope
  small).
- Tests: generate unit/plist text and assert on content; installation paths
  go through a `LASTBELL_HOME`-style override or monkeypatched
  `Path.home()` so nothing touches the real machine. Subprocess calls
  (`systemctl`, `launchctl`) behind a small runner that tests replace.
- Wizard step 5 gains "Install it as a background service now?" calling the
  same code; README quickstart becomes install pipx → `pipx install
  lastbell` → `lastbell setup` (which offers the service).

### Release afterwards
Bump `pyproject.toml` and `lastbell/__init__.py` to 0.1.1, tag `v0.1.1`,
push the tag — `release.yml` publishes. The 0.1.1 README on PyPI will also
pick up the screenshot tour (0.1.0's predates it).
