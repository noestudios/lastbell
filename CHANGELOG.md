# Changelog

Plain-words notes for each release. The heading's version is what
`release.yml` looks up to fill the GitHub Release page, so keep the
`## <version> — <date>` shape.

## 0.2.3 — 2026-09-03

**Canvas no longer invents classes.** Two kinds of Canvas course were
showing up on the Students page as if they were classes. A school's
all-students course ("Olney ES", "Sherwood HS") got a row of its own
whenever a school-wide notice had points attached — elementary students,
whose gradebook has one homeroom "class" for Canvas to match against, saw
it every time. And a teacher who runs one Canvas page for two sections
names it for one of them ("Theatre HS 2A" holding the 1A work), so it
showed up as a second Theatre class with no grade. Now a Canvas course
only stands on its own when it is named like a class (the district's
`Subject-Teacher-S1-2027` shape) and lives in a real term, and a course
that matches no class by name is folded into the one class the student
has with that teacher — the same rule a parent would use. Rows the old
versions created are removed on the next check; nothing else is touched.

## 0.2.2 — 2026-09-03

**A poll can no longer hang on Canvas.** On an install that keeps its
secrets in the settings file (`LASTBELL_SECRET_BACKEND=env` — a headless
Pi), 0.2.0 and 0.2.1 still asked the OS keyring for the optional Canvas
token before each poll, and on a headless Linux box that call can block
forever waiting for a desktop prompt that never comes: no error, no CPU,
no timeout. The gradebook half never ran. Now nothing touches a keyring the
install has opted out of (the SMTP password lookup had the same latent
hole), `lastbell set-canvas-token` writes to the settings file on that
backend, and the whole Canvas step runs under a wall-clock cap — two
minutes to sign in, five per student — past which the poll warns and
carries on with the gradebook alone. The Linux service now writes its own
log file (`~/.local/share/lastbell/logs/lastbell.log`, same as macOS)
because journald on an appliance image often keeps nothing; re-run
`lastbell install-service` to pick that up.

## 0.2.1 — 2026-09-03

**Says what it is.** The README's opening and disclaimer and the package
description on PyPI now name both sources — ParentVUE + Canvas — and the
disclaimer covers Instructure (Canvas) as well as Edupoint. No code changes.

## 0.2.0 — 2026-09-03

**Canvas is the leading source.** ParentVUE only shows work once it is in
the gradebook, days after the fact; Canvas (myMCPS Classroom) is where
assignments, due dates, submissions, and the "missing" flag appear first.
`lastbell run` now folds Canvas into every poll: it follows the Canvas tile
on the portal's own home page — a SAML hand-off for which Synergy is the
identity provider, so the ParentVUE session is the only credential — and
reads the observer-scoped REST API (students, courses, assignments with
your student's submission). Canvas work attaches to the matching gradebook
course, keyed `canvas:<id>`; once the same item reaches the gradebook under
the same name the gradebook row is the record and the Canvas twin leaves
the counts, lists, and alerts — but stays updated, and when the two
disagree (Canvas has a score where the gradebook shows a 0, a different
score, or a missing flag) the row shows *Canvas says 9/10* and a new
`source_conflict` alert asks you to check with the teacher; plain sync lag
(gradebook still ungraded) is hinted, never alerted. A new **turned in** status covers work
handed in and not yet graded. Alert lines from Canvas end in `[Canvas]`;
dashboard rows wear a small *Canvas* mark. New commands: `lastbell canvas`
(read-only check of the sign-in path and what each course contributes) and
`lastbell set-canvas-token` (optional personal access token, with
`LASTBELL_CANVAS_HOST`, to skip the hop). `LASTBELL_CANVAS=off` disables the
layer; when Canvas is unreachable the poll warns once and proceeds with the
gradebook alone. Existing databases gain a `source` column on upgrade.

**Email looks like a message, not a log line.** Every email now carries an
HTML version alongside the plain text: a card with the updates grouped by
what they mean — Needs attention, Slipping, Coming up, Grades posted — each
line showing the course, the assignment in bold, and what happened, with a
small *Canvas* pill where that applies; the daily summary gets an Overall
table and the same groups. Subjects count by kind ("J.P.H.: 1 missing, 2
due soon") instead of "2 updates". Plain-text channels (ntfy, Telegram,
Pushover, console) get the same grouping in text. `lastbell watcher test
NAME --sample` sends a realistic sample with made-up courses so you can see
the new look in your own inbox.

**The home page says when it last checked.** A "Last checked today at
4:12 PM" line closes the Students page; past twice the poll interval it
becomes a notice that the watcher looks like it isn't running. A quiet poll
with no changes counts — every finished poll is recorded.

## 0.1.6 — 2026-09-02

**Demo family uses email.** `lastbell seed-demo` no longer gives a demo
watcher a text-message address, so the sample dashboard matches what 0.1.5
offers. The README's Settings screenshot is re-shot from it and the
quickstart gains an "Upgrading" block: `pipx upgrade lastbell`, then restart
the running copy (the command per OS is there).

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
