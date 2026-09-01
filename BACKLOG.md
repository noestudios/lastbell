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

## Favicon + wordmark icon
A favicon, and the same mark rendered small in the nav bar to the left of
the "LAST BELL" title (added 2026-09-01). One SVG asset should serve both
(served locally — no external hosts, per the no-internet-LAN-box rule).
Bell motif is the obvious direction; avoid anything trademark-adjacent
(Edupoint's VUE marks).

## Data-freshness indicator (catch-up already works)
Verified 2026-09-01: the poll loop's wall-clock deadline means a machine
that was off/asleep polls once within a minute of waking — no hammering,
and queued digests flush late rather than drop. What's missing is the
*indication*: a "Last checked …" freshness line on the dashboard,
escalating to a visible notice when the newest poll is older than ~2× the
poll interval ("Data from Monday 4pm — the watcher looks like it isn't
running"), plus a "catching up after downtime" line in the loop log.
