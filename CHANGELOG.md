# Changelog

Plain-words notes for each release. The heading's version is what
`release.yml` looks up to fill the GitHub Release page, so keep the
`## <version> — <date>` shape.

## 0.1.5 — 2026-09-02

**Text message is withdrawn; email is the default.** The "text message"
channel relied on the carriers' free email-to-text gateways, and those are
gone or going: T-Mobile's shut down in December 2024, AT&T's in June 2025,
and Verizon's is being retired by March 2027 and already drops messages
without telling anyone — a daily summary sent to a Verizon address was
accepted by Gmail and never arrived. A channel that reaches some people and
never reaches others is worse than none, so the wizard and the dashboard now
offer email (plus ntfy for the terminal-minded), and any carrier-gateway
address is refused at entry with the reason. Rows created earlier with a
text-message address keep delivering over the same email transport rather
than silently breaking; switch them to an email address in Settings.

## 0.1.4 — 2026-09-02

**The dashboard knows what version it is.** The Settings page footer now
starts with the version you're running, linked to its release notes, and adds
**What's new** and **Report a problem** links.

**Check for updates, on your click only.** Next to those, **Check for
updates** asks PyPI whether a newer Last Bell exists and tells you the upgrade
command. It runs when you click it and never otherwise — no timer, no
background check — so the "no phone-home" promise stays true; the README says
so in the same sentence.

## 0.1.3 — 2026-09-02

**Test any address, any time.** Every address row on the dashboard's Settings
page has a **test** button, and `lastbell watcher test <name>` does the same
from the terminal — the one-line message the setup wizard sends, so "does this
actually reach Grandma's phone?" no longer means walking the wizard again.

**AT&T's email-to-text gateway is gone, and Last Bell says so.** AT&T shut it
down in 2025; a `@txt.att.net` address is now refused at entry, in the wizard
and the dashboard, with the fix (AT&T customers use email). Before this it was
accepted and the texts silently never arrived.

## 0.1.2 — 2026-09-01

**Text message is now the first choice in setup.** `lastbell setup` offers
text message, then email, then ntfy, and text message is the default — the
same two channels the dashboard's Settings page offers, in the same words.
A phone number typed on its own is caught with the carrier-gateway fix
instead of silently never delivering, and the watcher the wizard creates is
labeled "text message" in the dashboard rather than "email".

**The dashboard's Recent view groups grades by your local day.** A grade that
landed this evening no longer shows under tomorrow's date because the
database keeps time in UTC.

**README leads with the dashboard.** After the three-command quickstart it
now points at the Settings page for adding people and choosing alerts; the
terminal commands are still there, folded into a "Doing it from the terminal
instead" section. The channel table leads with text message and email and
marks ntfy, Telegram, and Pushover as terminal-only.

## 0.1.1 — 2026-09-01

**`lastbell install-service` keeps it running for you.** One command turns a
Raspberry Pi, an old laptop, or a Mac into the always-on watcher: Linux gets a
user-level systemd service that starts at boot with nobody logged in, macOS
gets a launchd agent that starts at login and restarts if it stops, Windows is
shown the Task Scheduler command to paste. `--print` shows exactly what would
be written without touching anything; `--uninstall` reverses it. It warns,
without stopping, if the machine's clock is on UTC (digests follow the local
clock) or if the password is somewhere a boot-time service can't reach.
`lastbell setup` offers to install it as its last step.

**Setup works on machines with no keyring.** A headless Pi has no place to
keep a password the way a Mac's Keychain does, and a service that starts at
boot can't unlock a desktop keyring either. The wizard now notices both
cases, explains the trade-off in one sentence — the password lives in the
owner-only settings file, on disk in plain text — and only proceeds if you
say yes. Switching back to the keyring later scrubs it from the file.

**Friendlier errors.** Keyring problems come back as one plain-language line
that says what to do next, never a traceback.

## 0.1.0 — 2026-09-01

First public release of Last Bell, a self-hosted ParentVUE grade and
assignment monitor for Montgomery County (MCPS) parents — and anyone else on
an Edupoint Synergy portal.

- **Install in three commands:** `pipx install lastbell`, `lastbell setup`,
  `lastbell run --loop`. The setup wizard confirms your district before
  asking anything personal, puts your password in the OS keyring, verifies
  login and parsers against the live portal, sets up one notification
  channel with a real test message, and runs the first collection.
- **Alerts for the things that matter:** missing assignments, upcoming
  deadlines, grade drops, new grades, and still-ungraded work — one daily
  digest at 4pm by default, with the urgent ones sent immediately.
- **Any number of watchers,** including the students themselves: each person
  gets their own channel, alert types, delivery time, and quiet hours.
- **A local web dashboard** (`lastbell dashboard`) for students, assignments,
  grade history, the alert log, and all the routing settings. Binds to your
  own machine only.
- **Your data stays home.** Credentials go to your district and nowhere else;
  everything collected sits in a local SQLite file; alert payloads carry
  initials, not names. There is no telemetry.
- **Polite to the portal:** a 15-minute polling floor, one login per pass.
- `lastbell seed-demo` fabricates a fake family so you can see the dashboard
  populated before pointing it at your own kids.
