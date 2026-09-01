# MCPSGradeWatch

A self-hosted **ParentVUE grade & assignment monitor**. It watches your own
students' gradebooks and pushes alerts — missing assignments, new or changed
grades, long-term work that's still ungraded or coming due — to **any number of
watchers** (guardians *and* the students themselves), on whatever device they
already use.

> Not affiliated with Edupoint. It uses **your** credentials to read **your**
> students' data, and everything runs on hardware you control.

**Status: all roadmap phases complete.** `mcpsgradewatch run` sweeps **every class** per
student (via each class row's own `data-focus` payload, the same drill-down
the portal UI performs), persists a snapshot keyed on the Edupoint assignment
GUID, diffs against the previous run, and alerts on **score changes,
missing-flags, work still ungraded past its due date, deadlines entering the
look-ahead window, and course grades dropping past a threshold** — plus a
one-shot **final-grades summary** when a marking period closes (the persisted
per-student term is the dedup; the closing term's last-seen marks are its
finals, the new term starts as a quiet fresh baseline, and the dashboard and
daily summaries scope themselves to the current term). Delivery is
per-watcher: **subscriptions** filtered by alert type over **channels**
(email/SMS-gateway, ntfy, Telegram, Pushover), each optionally batched into a
**daily digest**, held during **quiet hours**, or replaced by a generated
**daily summary** — plus a web **dashboard** with **shared ack** (one person
marks an alert handled for the whole household). Data path verified live
against MCPS (`md-mcps-psv.edupoint.com`, 2026-08-31).

---

## Why scraping (and not the SOAP API)

The legacy Edupoint SOAP mobile API is disabled on a growing number of districts
(MCPS returns `UPD5304-00`, Loudoun `D5517`). MCPSGradeWatch talks to the PXP2 **web
portal** instead: an ASP.NET form login, then the `PXP2_Gradebook.aspx/LoadControl`
page method the gradebook UI itself calls. Run `mcpsgradewatch preflight` to see what
your district allows.

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

cp .env.example .env          # then edit: district + username (NOT the password)
mcpsgradewatch set-password       # stores the password in your OS keyring
mcpsgradewatch preflight          # district go/no-go check (values redacted)

mcpsgradewatch run                # one pass: snapshot, diff, alert (first run = baseline)
mcpsgradewatch run --loop         # keep polling every MCPSGRADEWATCH_POLL_MINUTES
mcpsgradewatch collect            # read-only JSON dump of what a run would persist
```

Then route alerts to the people who should get them (Phase 3):

```bash
mcpsgradewatch watcher add Mom --kind guardian --channel email=mom@example.com
mcpsgradewatch watcher add Jasper --kind student --channel ntfy=some-long-secret-topic
mcpsgradewatch subscribe Mom jasper                 # all alert types, all her channels
mcpsgradewatch subscribe Jasper jasper \
    --types assignment_missing,upcoming_deadline    # students see nudges, not grades
mcpsgradewatch subscriptions                        # who gets what
mcpsgradewatch dashboard                            # web UI on 127.0.0.1:8321
```

Students are referenced by AGU or any unique name/initials prefix; watchers by
the name you gave them. You start with one automatically: the first `run`
creates a guardian watcher named after the credential holder, subscribed to
every student — email seeded from `MCPSGRADEWATCH_SMTP_TO` when set, console
otherwise. Its delivery follows the considerate default: one daily digest at
4pm, with urgent alert types (missing assignment, upcoming deadline, grade
drop) sent immediately. Rename or remove it freely; it's only re-created if
the watcher list is ever empty again.

And shape *when and how much* each person hears (Phase 4):

```bash
mcpsgradewatch subscribe Mom jasper --at 17:00      # batch her alerts into a 5pm digest
mcpsgradewatch subscribe Mom jasper --types daily_summary --at 07:00   # morning report
mcpsgradewatch watcher quiet-hours Jasper 21:00-07:00   # held overnight, never dropped
mcpsgradewatch alerts                               # the log, with ack state
mcpsgradewatch ack 6e383bac --by Mom                # "handled" — for everyone
mcpsgradewatch flush                                # send due digests/summaries now
```

In `run --loop`, the portal is polled every `POLL_MINUTES` but the outbox and
summaries are checked **every minute**, so a 17:00 digest goes out at 17:00 —
not at the next three-hour poll. Time-based deliveries use the host's local
clock. An event subscribed both immediately and in a digest is sent once,
immediately. A summary reports *standing state* (overall marks, missing work,
what's due soon, unacked alerts); a digest batches the *events* that fired.

## Configuration & secrets

All non-secret settings live in a **git-ignored `.env`** (`.env.example` is the
template). Passwords never go in `.env`, the database, or the source tree — only
a *reference* to where the secret lives:

| Install        | Secret store                                                        |
|----------------|---------------------------------------------------------------------|
| Bare-metal     | OS keyring — macOS Keychain / Windows Credential Manager / Secret Service (`mcpsgradewatch set-password`) |
| Docker / CI    | `MCPSGRADEWATCH_PASSWORD`, injected from Docker secrets or a CI secret store |

Cross-platform by construction: plain Python (Windows/macOS/Linux), no OS-native
hooks. SQLite by default; ship it as a container to run identically on a Pi, NAS,
or VPS. It needs an **always-on host** to poll and push.

## How alerts reach people

Push-**out**, not pull-in: nobody signs into MCPSGradeWatch to receive an alert.
A *watcher* is just a name plus addresses; *subscriptions* say which student's
events reach them, over which channels, filtered by alert type. One poll, one
message per watcher-channel — a watcher subscribed to three alert types gets a
single message listing everything.

| Channel    | Watcher address        | Transport setup (env)                      |
|------------|------------------------|--------------------------------------------|
| `email`    | `email=who@example.com`| `MCPSGRADEWATCH_SMTP_*` (any SMTP account) |
| *SMS*      | carrier gateway addr, e.g. `email=3015551234@vtext.com` | same as email |
| `ntfy`     | `ntfy=secret-topic`    | none (public ntfy.sh) or `NTFY_SERVER/TOKEN` |
| `telegram` | `telegram=<chat_id>`   | `MCPSGRADEWATCH_TELEGRAM_TOKEN` (@BotFather bot) |
| `pushover` | `pushover=<user_key>`  | `MCPSGRADEWATCH_PUSHOVER_TOKEN` (app token) |
| `console`  | —                      | none; prints to the run's stdout           |

The web dashboard (`mcpsgradewatch dashboard`) is for looking things up on
demand — students, assignments, alert log, grade history, watcher routing —
never required to get a notification. It's stdlib-only and binds `127.0.0.1`
unless you deliberately widen it; unlike alert payloads it shows full names,
so the bind address is the access control. Every page is a read; the one
write is the **shared ack** button on /alerts (Phase 4): any watcher marking
an alert handled marks it for the whole household, and summaries stop
nagging about it. Alert payloads stay **low-PII** (initials + course +
score, never a child's full name — safe for an SMS preview on a lock
screen).

## Will it work for *my* district?

Probably, if your district runs the Synergy PXP2 web portal — and the
**preflight** (Phase 5) answers definitively, without installing anything else
or touching a `.env`:

```bash
# Anonymous: public endpoints only, no credentials sent anywhere
mcpsgradewatch preflight --district your-host.example --report

# Full: login + data path + this repo's actual parsers against your fragments
mcpsgradewatch preflight --district your-host.example --username you --report
```

It checks, in order: the PXP2 login form exists → the legacy SOAP API's status
(the deprecation code your district returns is kept verbatim — useful
cross-district data) → web login → students on the credential → the
`LoadControl` data path → and finally whether the **parsers understand your
district's fragments**, which is the question that actually decides
compatibility. Verdicts: `go`, `partial` (data path answers but a parser
needs a tweak — the most fixable kind of report), `no-go`, `anonymous-ok`.
Exit codes match (0 go, 1 not yet, 2 couldn't run) so it scripts cleanly.

`--report` prints Markdown that is **redacted by construction** — no student
names, grades, or usernames can appear in it — ready to paste into a
[district report issue](.github/ISSUE_TEMPLATE/district-report.md). `--json`
is for scripts; `--show-values` reveals names locally only, and is never
included in exported output; `--dump` saves raw fragments to `data/debug/`
(personal data — stays local, git-ignored) for parser development. It also
installs standalone as `parentvue-preflight`.

## The Phase 0 gate — PASSED

The data path was reverse-engineered from the portal's own JavaScript and then
verified end-to-end against MCPS: `POST service/PXP2Communication.asmx/LoadControl`
with the page's verbatim `PXP.GBCurrentFocus` FocusArgs (and an `AGU` header)
returns server-rendered fragments; assignments arrive as a DevExpress grid
`dataSource` JSON array (`Date`, `GBAssignment`, `GBScore`, `GBPoints`, … with
LinkColumn cells wrapping display text and a ready-made
`Gradebook_AssignmentDetails` drill-down focus). The parsers in
[`mcpsgradewatch/gradebook.py`](mcpsgradewatch/gradebook.py) are wired against
real captured fragments from both school types.

```bash
mcpsgradewatch preflight --dump   # go/no-go check; saves fragments to data/debug/
mcpsgradewatch collect            # normalized JSON for every student and class
```

Phase 1 built the watch loop on top of that path: each class row's `data-focus`
attribute carries the ready-made `{LoadParams, FocusArgs}` the portal's own
`GB.LoadControl` click handler sends, so `run` sweeps every class exactly the
way a human clicking through them would (with a polite delay between calls,
and duplicate screen/print row variants fetched once).

## Roadmap

| Phase | What ships |
|------:|------------|
| **0** | ✅ Pass the gate; harden the connector into normalized courses + assignments |
| **1** | ✅ All-class sweep, persisted snapshots (keyed on the Edupoint assignment GUID), diff + first alert (`run` / `run --loop`) |
| **2** | ✅ Missing, ungraded-past-due, future-deadline look-ahead, score changes (`LOOKAHEAD_DAYS` / `UNGRADED_GRACE_DAYS`) |
| **3** | ✅ Watcher accounts (guardians & students), subscriptions, dashboard, channels |
| **4** | ✅ Daily student summaries, digests, quiet hours, grade-drop thresholds, shared ack |
| **5** | ✅ Publish the preflight as a redacted, general district tool |

## Credits

The dashboard's visual design (colors, type, card and badge styling in
[`mcpsgradewatch/style.css`](mcpsgradewatch/style.css)) is derived from
[Purity UI Dashboard](https://github.com/creativetimofficial/purity-ui-dashboard)
— Copyright (c) 2021 Creative Tim, released under the MIT license; its
copyright and permission notice applies to those derived styles.

The web-portal approach (ASP.NET form login, embedded child-list JSON) was
first demonstrated by [dmc5179/ParentVUE](https://github.com/dmc5179/ParentVUE)
(GPLv3), which served as prior art and reference during this project's district
recon. MCPSGradeWatch's code is written independently against the portal itself, but
that repo deserves the credit for proving the post-SOAP path first. Community
documentation of the (now largely deprecated) SOAP API lives at
[StudentVue/docs](https://github.com/StudentVue/docs).

## License

MIT — see [LICENSE](LICENSE). Because the connector is original code (not a fork),
the license is a free choice; MIT is the permissive default for maximum forkability.
Set your name/handle in `LICENSE` before publishing.
