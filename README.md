# MCPSGradeWatch

A self-hosted **ParentVUE grade & assignment monitor**. It watches your own
students' gradebooks and pushes alerts — missing assignments, new or changed
grades, long-term work that's still ungraded or coming due — to **any number of
watchers** (guardians *and* the students themselves), on whatever device they
already use.

> Not affiliated with Edupoint. It uses **your** credentials to read **your**
> students' data, and everything runs on hardware you control.

**Status: Phase 3 complete.** `mcpsgradewatch run` sweeps **every class** per
student (via each class row's own `data-focus` payload, the same drill-down
the portal UI performs), persists a snapshot keyed on the Edupoint assignment
GUID, diffs against the previous run, and alerts on **score changes,
missing-flags, work still ungraded past its due date, and deadlines entering
the look-ahead window** (the time-based rules are status *derivations*, so a
crossed threshold is just another persisted transition — alerted exactly
once). Phase 3 adds the fan-out: **watcher accounts** (guardians *and*
students), per-watcher **subscriptions** filtered by alert type, **channels**
(email/SMS-gateway, ntfy, Telegram, Pushover), and a read-only **web
dashboard**. Data path verified live against MCPS
(`md-mcps-psv.edupoint.com`, 2026-08-31).

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
mcpsgradewatch dashboard                            # read-only web UI on 127.0.0.1:8321
```

Students are referenced by AGU or any unique name/initials prefix; watchers by
the name you gave them. With **no** watchers configured, `run` falls back to
the single global `MCPSGRADEWATCH_NOTIFY_CHANNEL` exactly as before.

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
never required to get a notification. It's read-only, stdlib-only, and binds
`127.0.0.1` unless you deliberately widen it; unlike alert payloads it shows
full names, so the bind address is the access control. Alert payloads stay
**low-PII** (initials + course + score, never a child's full name — safe for
an SMS preview on a lock screen).

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
| **4** | Daily student summaries, digests, quiet hours, grade-drop thresholds, shared ack |
| **5** | Publish the preflight as a redacted, general district tool |

## Credits

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
