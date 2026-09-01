# Install plan — from "clone and venv" to "paste one line, answer questions"

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
