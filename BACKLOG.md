# Backlog

Owner-approved items parked for later, so nothing lives only in a chat log.
Dashboard-UX specifics stay in [UX-PLAN.md](UX-PLAN.md); this is the
repo-level list. Newest at the bottom.

## Grade trends over time
The data model (`course_history`) and first sparklines shipped; the
leftovers are in
[UX-PLAN.md → Grade trends over time](UX-PLAN.md): per-course chart on the
scoped course view, sparklines on the overview's student cards, per-course
sparklines in the course strip's reserved slot, and whether the term-final
alert links to the closing term's trajectory. (Backlogged 2026-09-01.)

## Public-repo readiness sweep
Everything a healthy public repo carries beyond the code (added
2026-09-01): CONTRIBUTING.md, SECURITY.md (private-report contact + what
counts — this project's whole pitch is trust), CODE_OF_CONDUCT, issue/PR
templates beyond the existing district report, CI (GitHub Actions: pytest
across supported Pythons, lint), dependabot/renovate, branch protection and
repo settings, release/tag conventions, badges. Decide what's genuinely
useful for a small self-hosted tool vs. ceremony.

## Credits completeness pass
The README credits Purity UI (styles), dmc5179/ParentVUE (prior art), and
StudentVue/docs. Audit everything else pulled in or drawn on and credit it
properly (added 2026-09-01): the icon set used in the dashboard (feather
icons), the motion-tier timings noted in UX-PLAN as borrowed, the MCPS
school directory as the source behind `mcps_schools.json`, ntfy et al. as
services (mention, not license), and anything a fresh sweep of the code
turns up. Licenses verified, wording consistent with the existing Credits
section.

## Data-freshness indicator (catch-up already works) — shipped 2026-09-03
The "Last checked …" line and the stale notice are in 0.2.0; what's left of
this item is the "catching up after downtime" line in the loop log.

Verified 2026-09-01: the poll loop's wall-clock deadline means a machine
that was off/asleep polls once within a minute of waking — no hammering,
and queued digests flush late rather than drop. What's missing is the
*indication*: a "Last checked …" freshness line on the dashboard,
escalating to a visible notice when the newest poll is older than ~2× the
poll interval ("Data from Monday 4pm — the watcher looks like it isn't
running"), plus a "catching up after downtime" line in the loop log.

## Score cutoff as a real setting
`LASTBELL_SCORE_CUTOFF` (default 70 = "below a C" on the MCPS scale; `0`
disables) tints graded rows in the dashboard, and that is all it does. It
is env-only, so a pipx install can't discover it: the wizard never asks,
the README doesn't list it, and `.env.example` is the only mention — a
file PyPI users never see (found 2026-09-02). Plan when it's picked up:
move it into the database as a household setting with a small "Display"
card on the Settings page (default 70, env var honored as the seed), so it
obeys the page's own rule of showing only what it can change; per-student
override as one optional column on the same card. Decide separately, and
on purpose, whether a "score fell below the line" alert type is wanted —
today nothing alerts on it and the digest already carries the grade.
Interim fix if needed sooner: two README lines in the configuration table.
