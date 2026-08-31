# gradewatch

A self-hosted **ParentVUE grade & assignment monitor**. It watches your own
students' gradebooks and pushes alerts — missing assignments, new or changed
grades, long-term work that's still ungraded or coming due — to **any number of
watchers** (guardians *and* the students themselves), on whatever device they
already use.

> Not affiliated with Edupoint. It uses **your** credentials to read **your**
> students' data, and everything runs on hardware you control.

**Status: scaffold.** The connection layer is proven against MCPS
(`md-mcps-psv.edupoint.com`); the gradebook parser sits behind one unverified
step — see [The Phase 0 gate](#the-phase-0-gate).

---

## Why scraping (and not the SOAP API)

The legacy Edupoint SOAP mobile API is disabled on a growing number of districts
(MCPS returns `UPD5304-00`, Loudoun `D5517`). gradewatch talks to the PXP2 **web
portal** instead: an ASP.NET form login, then the `PXP2_Gradebook.aspx/LoadControl`
page method the gradebook UI itself calls. Run `gradewatch preflight` to see what
your district allows.

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

cp .env.example .env          # then edit: district + username (NOT the password)
gradewatch set-password       # stores the password in your OS keyring
gradewatch preflight          # district go/no-go check (values redacted)
```

## Configuration & secrets

All non-secret settings live in a **git-ignored `.env`** (`.env.example` is the
template). Passwords never go in `.env`, the database, or the source tree — only
a *reference* to where the secret lives:

| Install        | Secret store                                                        |
|----------------|---------------------------------------------------------------------|
| Bare-metal     | OS keyring — macOS Keychain / Windows Credential Manager / Secret Service (`gradewatch set-password`) |
| Docker / CI    | `GRADEWATCH_PASSWORD`, injected from Docker secrets or a CI secret store |

Cross-platform by construction: plain Python (Windows/macOS/Linux), no OS-native
hooks. SQLite by default; ship it as a container to run identically on a Pi, NAS,
or VPS. It needs an **always-on host** to poll and push.

## How alerts reach people

Push-**out**, not pull-in: nobody signs into gradewatch to receive an alert.
**Email** is the universal default; **ntfy / Telegram / Pushover / SMS** are
opt-in per watcher (Phase 3). The web dashboard is for looking things up on
demand — never required to get a notification. Alert payloads are **low-PII**
(initials + course + score, never a child's full name).

## The Phase 0 gate

We reverse-engineered the data path from the portal's own JavaScript and know
the exact `LoadControl` contract, but an end-to-end fetch returning assignment
data isn't verified yet (the empty-parameter probe returns HTTP 500 without the
per-term focus GUIDs). **Passing that fetch is the build's go/no-go.** The
gradebook parsers in [`gradewatch/gradebook.py`](gradewatch/gradebook.py) are
deliberately stubs until a real fragment is captured.

```bash
gradewatch preflight          # reports whether the gate passes for your district
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

## License

MIT — see [LICENSE](LICENSE). Because the connector is original code (not a fork),
the license is a free choice; MIT is the permissive default for maximum forkability.
Set your name/handle in `LICENSE` before publishing.
