# Changelog

Plain-words notes for each release. The heading's version is what
`release.yml` looks up to fill the GitHub Release page, so keep the
`## <version> — <date>` shape.

## 0.2.11 — 2026-09-04

**0.2.10's self-restart is removed.** For one release the poller and the
dashboard watched the version on disk and restarted themselves when it
changed. That made a running service act on something other than a
person's decision, and the owner would rather it didn't: an upgrade is a
choice, and the restart belongs to that choice. So it is gone, entirely.
`pipx upgrade lastbell` moves the files and nothing more; `lastbell
upgrade` moves them and restarts the poller and the dashboard, which is
the path to use. The footer badge and `lastbell status` say "restart to
use it" again, as they did in 0.2.9. Nothing else changed.

## 0.2.10 — 2026-09-04

The poller and the dashboard restarted themselves within a minute of a
newer version appearing on disk. Withdrawn in 0.2.11; see above.

## 0.2.9 — 2026-09-04

**A heartbeat, a demo you can click, and the rest of the public-repo
list.**

- **Heartbeat URL.** A stopped machine can't email anyone. Set
  `LASTBELL_HEARTBEAT_URL` to a healthchecks.io check or an Uptime Kuma
  push monitor and Last Bell fetches it once after every successful poll,
  never otherwise; that service raises the alarm when the pings stop. A
  ping that fails is one warning in the log, never a failed poll. It is
  off unless set, listed in the README's outbound-HTTP inventory, and
  `lastbell status` names the host it pings (never the URL, which is a
  secret).
- **The watcher-health notices wear the email frame.** "Can't check the
  gradebook" and "Checking again" now carry the same HTML twin the alerts
  and summaries do, with commands as code; the plain text every other
  channel gets is unchanged.
- **A static demo.** `scripts/build_demo_site.py` renders every dashboard
  page from `seed-demo` data, and a workflow publishes it to
  https://noestudios.github.io/lastbell/ whenever the dashboard changes.
- **README.** Badges; a link to the demo; the district question now leads
  with `pipx run lastbell preflight …`, which installs nothing; screenshots
  of the three emails (digest, daily summary, the health notice), made by
  `scripts/render_emails.py`; a half-page threat model; a Contributing
  section. SECURITY.md documents the PEP 740 attestations each release
  carries, and CONTRIBUTING.md is new.

## 0.2.8 — 2026-09-04

**Three commands for the second week: `status`, `upgrade`, `backup`.**
Once it runs, the questions change from "how do I set it up" to "is it
still working", "how do I update it", and "what if the SD card dies".
Each now has a one-word answer.

- **`lastbell status`** puts the install on one screen: the version
  running and, if different, the version installed but not yet restarted;
  the platform and whether the clock is on UTC; the settings file and
  where the password lives (the keyring by name, or the owner-only
  settings file — never the password); the poll interval, alert channel,
  and Canvas mode; whether the service is installed and running (both
  user units on Linux, the launchd agent on macOS); whether the dashboard
  is listening and whether its network key exists; the log's size and
  age; the last successful check, whether checking is failing and since
  when, and when the next check is due (or that it is overdue, which is
  how a stopped service looks); students as initials; every watcher with
  their channels and who they are subscribed to; alerts sent and queued.
  Home directories print as `~`, so the whole thing can be pasted into an
  issue. It creates nothing, reads no secret, and touches no network.
- **`lastbell upgrade`** runs `pipx upgrade lastbell` and then restarts
  the poller and, where one is set up, the dashboard — the step people
  forgot often enough to earn a footer badge in 0.2.5. It reports the
  version before and after and what it restarted. `--no-restart` upgrades
  the files only; `--restart-only` skips pipx. Without pipx on PATH it
  says how to upgrade a venv or checkout instead.
- **`lastbell backup [path]`** writes one owner-only zip: the database
  copied through SQLite's backup API (a copy taken mid-poll is still
  whole, and the write-ahead log is folded in, which a plain `cp` can
  miss) and the settings file with every secret left out — passwords,
  channel tokens, the dashboard key — each replaced by a line saying so.
  A README inside says what it holds and how to restore it.
- **`lastbell restore <file>`** checks the archive, refuses to replace an
  existing database unless `--force` (and then keeps the old one beside
  it as `.before-restore`, with its log folded in first), merges the
  settings without touching the secrets already on the machine, and
  reminds you to store the password again.


**It no longer fails silently, it can forget you, and three audits'
findings are fixed.** A read of the credential path in 0.2.6 left six
things on the list; this release does all of them.

- **Guardians are told when checking stops.** A watcher that can't sign
  in (the ParentVUE password changed) or can't reach the portal used to
  fail every three hours forever, and the only sign was a stale footer
  on a dashboard nobody opens. Now, after a rejected sign-in has lasted
  two polls (a day for an unreachable portal), every guardian gets one
  message on their own channels saying what is wrong and what to do; one
  more arrives when checking resumes. The home page footer names the
  failure meanwhile. While sign-in is being rejected the poller also
  backs off to once a day, so a stale password can't trip a district's
  lockout policy.
- **`lastbell forget`** removes everything Last Bell keeps on a machine:
  the background service, the database with every snapshot and alert,
  the settings file, and the keyring entries. It lists what it will do
  and asks first; `--yes` is for scripts. (The program itself stays;
  `pipx uninstall lastbell` removes that.)
- **Preflight: the shareable report is redacted on the failure paths
  too.** A LoadControl error from the portal is post-login text that
  could name anyone; it now stays local and the report carries a fixed
  sentence. A request failure after login is reported by exception name
  only (its text carries a URL, and the gradebook URL carries the
  student's id). A pasted portal URL is reduced to its hostname before it
  is printed anywhere. Table cells can't break the table. The command
  never prints a traceback.
- **Dashboard: the alerts page's type filter was reflected unescaped.**
  A crafted link could run script in the dashboard's own origin. The
  filter is now escaped and, before that, only ever one of the types
  actually present.
- **Email addresses mean one mailbox.** The validator accepted
  `kid@example.com, other@evil.com` and display-name and group forms,
  which SMTP would fan out to every address named. One bare address, or
  a plain refusal.
- **Clocks.** History buckets (the six-week trend, "this week") were
  grouped by UTC date while everything around them used local days, so
  an evening change didn't count until midnight. A daily summary whose
  slot fell inside a poll that ran past midnight was skipped for the day;
  a missed day is now caught up. Work in the grace window (due in the
  last few days, still ungraded) had dropped out of the summary; it has
  its own line now, matching the dashboard. `run --loop` warns at start
  when the host clock is on UTC.
- **Locks.** The poller and the dashboard restart together after an
  upgrade and both run migrations; the loser of that race died on a
  duplicate-column error. A settings save while the poller held the
  database past the busy timeout dropped the connection, and the page's
  fallback re-submitted the form. Both now end well: the migration
  tolerates the race, the save answers "busy, try again". WAL runs with
  `synchronous=NORMAL`, the standard setting, so a poll's many small
  commits aren't each a full fsync on the SD card.
- **Housekeeping for a public repo:** `SECURITY.md` with a private
  reporting path, Dependabot for pip and Actions, and a `pip-audit` job
  that also runs weekly against fresh advisories.

## 0.2.6 — 2026-09-04

**A hardening pass over the parts a careful reader checks first.** Seven
changes; none alters what Last Bell watches or sends, and each closes a
gap between what the README promised and what the code guaranteed.

- **Dashboard: beyond loopback, other devices need a key.** `--host
  0.0.0.0` used to put full names and the watcher list on the network
  with no login at all. Now requests from the machine itself still need
  nothing, and every other device needs the dashboard key once: a long
  random string generated on first widened start, kept in the settings
  file, printed as a link (`http://raspberrypi.local:8321/?key=…`) that
  sets a cookie. `lastbell dashboard --show-key` prints it again. Binding
  to a public address is refused (the key would cross the internet in the
  clear) unless `LASTBELL_DASHBOARD_PUBLIC=1`; the right shape there is
  loopback behind a TLS proxy with its own login.

- **Email: the mail server's certificate is now verified.** `starttls()`
  with no context uses Python's *unverified* default, so anyone between
  the box and the mail server could have presented any certificate and
  read the SMTP password. Both STARTTLS and, new, implicit TLS on port
  465 now verify against the OS trust store.
- **Dashboard: DNS rebinding is refused.** Binding to 127.0.0.1 kept the
  network out but not the reader's own browser: a web page could point a
  hostname it controls at 127.0.0.1 and read the dashboard, and the
  Origin check couldn't tell because Origin and Host then agree. The
  dashboard now answers only to loopback, IP literals, `.local` names,
  the bound address, and `LASTBELL_DASHBOARD_HOSTNAMES`; anything else
  gets a 421 page saying why and how to allow a name of your own.
- **Settings file: passwords with `#`, `$`, quotes, or edge spaces now
  survive the round trip.** The wizard wrote values bare; python-dotenv
  reads `abc #def` as `abc` and expands `${VAR}`. The wizard's own check
  used the in-memory value, so it said "go" and the boot-time service
  then failed to sign in. Values that need it are now quoted, the loader
  no longer interpolates, and the wizard reads the password back through
  dotenv before calling it saved.
- **Settings file: owner-only from the first byte.** It was written under
  the umask and chmod-ed to 0600 afterwards. It is now written to a
  0600 temp file and moved into place, so a crash mid-write can't leave
  a truncated settings file either.
- **Docker: the compose file works.** It mounted a secret that nothing
  read. `LASTBELL_PASSWORD_FILE` (and `_SMTP_FILE`, `LASTBELL_CANVAS_TOKEN_FILE`)
  now name a secret file, and the compose file runs the poller plus a
  loopback-only dashboard.
- **Preflight: the legacy SOAP probe no longer carries your password.**
  The deprecation code districts return (UPD5304-00 and friends) comes
  back before any login, so a placeholder learns the same thing. The
  README's "sent to one destination" is now literally true.

## 0.2.5 — 2026-09-04

**The dashboard now knows the difference between "not upgraded" and
"not restarted".** After `pipx upgrade`, the poller and the dashboard
keep running the old code until each is restarted — and the dashboard
kept reporting its own old version and telling you to upgrade again, as
if the upgrade hadn't taken. Now Check for updates reads the version
installed on disk as well as the one running: when the disk copy is
newer it says "0.2.5 is installed, but this dashboard is still running
0.2.4" and gives the restart command for your platform. The Settings
footer shows the same flag next to the version. The upgrade hint itself
now names both processes (on Linux: `systemctl --user restart lastbell`,
plus `lastbell-dashboard` if you set that up) instead of "restart it".

## 0.2.4 — 2026-09-04

Fixes from a code review of the whole package. Nothing changes in what
you see day to day; several things change in what can go wrong.

**A Canvas read that runs out of time can no longer touch the poll.**
The Canvas step runs under a time limit so a stuck read can't hang a
poll. Before, a read that hit the limit was abandoned but kept running
in the background — and it was writing into the very gradebook snapshot
the poll went on to save. Now the abandoned read holds nothing but its
own Canvas data; the merge into the snapshot happens only when the read
comes back in time, on the poll's own thread. The Canvas sign-in hop
likewise gets its own connection instead of sharing the portal's.

**The dashboard refuses settings changes that come from other sites.**
Binding to 127.0.0.1 stops other machines, not your own browser: any web
page you visit could quietly submit a form to the dashboard and add a
stranger's address as a watcher on every student. Settings posts now
have to come from the dashboard's own pages. Nothing changes for you.

**The dashboard reads while the poll writes.** The database now uses
write-ahead logging, so a page load during a poll no longer hits the
"couldn't read its database just now" screen.

**A lighter History page.** History used to render every change ever
recorded into the page — 700 KB after one season. Each section now
shows its newest 300 with a link to the full list.

**The service log has timestamps.** `lastbell.log` lines from the poll
now start with the date and time, so "couldn't reach the portal" can be
placed.

**Smaller fixes.** A typo in a numeric setting (`LASTBELL_POLL_MINUTES=soon`)
is a one-line error, not a traceback. `lastbell setup` stores the SMTP
password in the settings file on an install that keeps secrets there,
and a keyring failure while storing it is a plain error. A portal error
while looking for the Canvas link is reported as one, not as "no Canvas
link". Alert lines are stored with their parts (course, assignment, what
happened) so the HTML email lays them out without re-parsing the
sentence; rows written by older versions still render. The dashboard
module is now a package (queries, render, settings, server) and a linter
runs in CI; the two `spike_*.py` shims at the repo root are gone —
`lastbell preflight` and `lastbell canvas` are what they forwarded to.

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
