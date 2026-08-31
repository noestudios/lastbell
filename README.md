# MCPSGradeWatch

A self-hosted **ParentVUE grade & assignment monitor**. It watches your own
students' gradebooks and pushes alerts — missing assignments, new or changed
grades, long-term work that's still ungraded or coming due — to **any number of
watchers** (guardians *and* the students themselves), on whatever device they
already use.

> Not affiliated with Edupoint. It uses **your** credentials to read **your**
> students' data, and everything runs on hardware you control.

**Status: Phase 0 complete — the gate is passed.** Verified live against MCPS
(`md-mcps-psv.edupoint.com`, 2026-08-31): login, multi-student discovery, and
the full `LoadControl` drill-down returning real class and assignment data for
both an elementary (subject view) and a high-school student.
`mcpsgradewatch collect` emits normalized JSON today; persistence + diffing
(Phase 1) is next.

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
```

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
**Email** is the universal default; **ntfy / Telegram / Pushover / SMS** are
opt-in per watcher (Phase 3). The web dashboard is for looking things up on
demand — never required to get a notification. Alert payloads are **low-PII**
(initials + course + score, never a child's full name).

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
mcpsgradewatch collect            # normalized JSON for every student
```

## Roadmap

| Phase | What ships |
|------:|------------|
| **0** | Pass the gate; harden the connector into normalized courses + assignments |
| **1** | Persist snapshots (keyed on the Edupoint assignment GUID) + diff + first alert |
| **2** | Missing, ungraded-past-due, future-deadline look-ahead, score changes |
| **3** | Watcher accounts (guardians & students), subscriptions, dashboard, channels |
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
