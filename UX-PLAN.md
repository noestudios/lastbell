# Dashboard UX plan

Status: **planning — decisions locked 2026-08-31, awaiting design inputs (Figma).**
No implementation yet beyond what's already shipped (one-decimal percents,
score-as-percent with hover, teacher-named elementary classes, term grouping).

## Decisions (locked)

1. **Design source**: a Figma dashboard template file supplied by the owner;
   styles/colors/typography are extracted from it into design tokens.
2. **Nav**: brand → overview; student names as direct links in the nav at
   desktop size. On mobile the student links collapse to iconography — tap to
   reveal names in a menu. The redundant top-level "Students" item goes away.
3. **Default watcher**: whoever installs with a username/password IS a watcher
   by default. Setup (or the first `run` with zero watchers) creates a guardian
   watcher for the credential holder, subscribes them to all discovered
   students, and seeds their email channel from `LASTBELL_SMTP_TO` when
   set. Ack and the viewer identity picker therefore always have someone to
   attribute to; watcher CRUD is in both the CLI and (since the Phase B
   settings build-out) the dashboard's Settings page.
   **Implemented (2026-08-31):** `watchers.ensure_default_watcher`, called at
   the end of every `run` pass — a no-op once any watcher exists. Console
   channel when SMTP_TO is unset, mirroring the old no-watcher fallback.
4. **Score cutoff**: one global display threshold
   (`LASTBELL_SCORE_CUTOFF`), used to tint graded rows below it.
   Per-student cutoffs are a later maybe.
   **Implemented (2026-09-01, Phase C):** default 70, `0` disables;
   documented in `.env.example`. Read at render time by the dashboard only.

## Phases

### A. Foundation — tokens + theming (blocked on Figma file)
- Extract CSS out of `dashboard.py` into a served stylesheet built on CSS
  custom properties: status ramp (ok/info/warn/bad/muted), surfaces, text,
  spacing, type scale. Light + dark derived from the same tokens.
- Values come from the Figma template. Fonts: bundle locally or use a system
  stack — the dashboard must work on a LAN box with no internet.
- Keep the ethos: server-rendered HTML, no framework, no build step; small
  vanilla JS only where interaction demands it (mobile nav menu, ack, hover).

### B. Structure — nav + settings
- Nav per decision 2 (responsive: names on desktop, icons+menu on mobile).
- Fold Watchers into a **Settings** page: watcher/subscription/quiet-hours
  management as real web forms (owner's call 2026-08-31, superseding the
  earlier read-only-with-CLI-hints deferral). Env-owned config (poll
  cadence, thresholds) is deliberately absent: if it can't be changed from
  the page, it isn't shown there (owner's call 2026-09-01). The write paths
  follow ack's trust model: no auth of their own, the bind address is the
  access control.
- Settings status: **done (2026-09-01)** — `/settings` replaces `/watchers`
  (301). The gear is set apart from the page links: always icon-only,
  right-aligned against the theme toggle. Add-forms sit above their tables.
  Watchers table nests one row per channel under each watcher (address
  editable in place, add/remove per row, via the HTML form= attribute);
  subscriptions add in one step ("all students" fans out) and edit in place
  per row (type/channel/time + update/remove). Validation errors redirect
  back as a banner (`?err=`). Quiet hours are descoped from the web UI
  (owner's call 2026-09-01) — CLI only.
- Student-name nav links (decision 2): **done (2026-09-01)** — the nav shows
  each student as a direct link between the brand and Alerts/History (first
  names, falling back to full names if two students share one; the full name
  rides the title attribute), and the top-level "Students" item is gone. At
  the narrow-nav breakpoint the name links collapse into a `<details>` menu
  behind the students icon (no JS to open; app.js closes it on an outside
  click, same as the alert-types popover). Every page renders the links —
  `_handle` fetches students once per request and threads them to `_page`.
- Delivery cadence (owner's call 2026-09-01): the default is ONE daily
  digest at 16:00, with an "urgent now" checkbox (per subscription,
  `urgent_now` column) that sends urgent alert types immediately —
  `URGENT_ALERT_TYPES` = missing assignment, upcoming deadline, grade drop.
  Grade changes are informational and batch. The default watcher seeds
  16:00 + urgent. Quiet hours still defer urgent sends downstream.
- Channels (owner's call 2026-09-01): the web UI offers exactly **email**
  and **text message** — sms is its own channel name riding the email
  transport (carrier email→SMS gateway address). ntfy/telegram/pushover
  stay in the codebase as CLI-only; Signal rejected (no official bot API;
  signal-cli is brittle). Addresses are validated at entry
  (`notify.validate_address`, dashboard + CLI): email/sms must look like
  user@host, and the sms error teaches the carrier gateway format — a bare
  phone number is the mistake it exists to catch.
- Settings layout (owner's call 2026-09-01): add forms sit in an inset
  panel ABOVE their tables (create vs manage zones); action buttons
  right-align in the last column so the two tables line up; Update buttons
  are display-gated (no phantom space) and fade in when the row dirties.
- No-reload settings (owner's call 2026-09-01): app.js posts settings forms
  over fetch and swaps `#settings-main` in place — no navigation, no scroll
  reset; the URL never carries `?ok/?new`. The server contract is
  unchanged (303 → fresh page; fetch follows it, outcome read from the
  final URL's params), so JS-off still works as plain POST → redirect.
- Alert types are a multiselect (owner's call 2026-09-01): the data model
  stays one row per type (routing depends on it); the dashboard groups rows
  by (watcher, student, channel, delivery, urgent) and shows the type SET
  as a checkbox dropdown (a styled `<details>` popover — no JS to open).
  `watchers.set_subscription_group` reconciles the selected set (insert new
  types, delete de-selected, update kept — rolling back on a conflict).
  app.js keeps "all alerts" exclusive, at least one box checked, and the
  summary label fresh.
- Removal confirmations (owner's call 2026-09-01): removing a watcher, or
  a watcher's LAST subscription, pops a Cancel/Remove dialog (scrim +
  surface card, toast-style enter) whose text states the consequence
  plainly. Channel and non-final subscription removals stay one click.
- Settings polish (2026-09-01): motion timings borrowed from jshq's tiers
  (fast 150ms feedback / base 300ms fades / slow 400ms movement / linger
  750ms decay, one symmetric ease). Row Update buttons appear only once the
  row is dirtied (app.js, served at /static/app.js — progressive
  enhancement, plain posts with JS off). Successes redirect with `?ok=`
  (toast, bottom-center, 3s hold then opacity-only fade) and `?new=` (row
  ids that slide down + fade in); removals fade + collapse client-side
  before the form posts, so content below slides up with no reload jump.
  Buttons have fast-tier hover transitions and a 1px press-down state.
  `prefers-reduced-motion` collapses all of it.

### C. Signal — status visibility
**Status: done (2026-09-01), together with the alerts-page ripple below.**
- Row tint + leading icon per status, from the same tokens as badges:
  missing = bad, due soon = warn, ungraded past due = warn-strong,
  graded below the global cutoff = bad-tinted score.
  **Built:** `tr.st-missing/st-late/st-due` classes + feather icons in the
  first cell (alert-circle / alert-triangle / clock), tints via `color-mix`
  so both themes derive from the same tokens. The cutoff is
  `LASTBELL_SCORE_CUTOFF` (default 70, `0` disables; display-only, nothing
  alerts on it) — the *score* tints, per decision 4.
- Overview badges ("2 due soon", "1 missing") click through to
  `/student/<agu>?status=…`, which highlights and scrolls to matching rows.
  **Built:** missing/past-due badges carry `&status=…#hit`; matching rows
  get a decaying accent pulse and the first anchors the scroll (`id='hit'`,
  `scroll-margin-top`). The due badge stays a plain `?view=due` link — the
  view *is* the answer there, highlighting every row would say nothing.
- Alerts list: local-time **dates** only (today/yesterday for recent), full
  local timestamp on hover. (Stored timestamps are UTC; the current
  "When (UTC)" column is the anti-pattern this replaces.)
  **Built:** applied to /alerts and both /history tables (same helper —
  leaving "When (UTC)" on History while fixing Alerts would be the same
  anti-pattern half-fixed). Server-local time is household-local by design
  (LAN box).
- **Styled tooltips**: the score hover (raw points behind the percentage) —
  and any other hover reveal, e.g. the alert timestamp above — renders as a
  design-system tooltip (tokens: surface, radius, shadow), not the browser's
  tiny native `title` bubble. CSS-only where possible (positioned
  pseudo-element/`data-tip` attribute), no JS dependency.
  **Built:** `.tip[data-tip]` + `::after` bubble, hover/decay on the fast
  tier; card tables get `overflow: visible` so bubbles escape (their radius
  is 0, hidden bought nothing). Native `title` remains only on controls
  (nav, buttons), where it's a label, not a data reveal.

### D. Interaction — ack
- One-time viewer identity picker (choose your watcher), remembered in
  localStorage; ack becomes a single checkmark click attributed to that
  person. Acked rows show ✓ + who (+ when on hover). Identity switchable.
- With decision 3, a fresh install always has at least one watcher, so the
  ack UI never silently disappears (its current failure mode).

### Density at end-of-quarter scale — DECIDED (owner's calls 2026-09-01)
Today each class holds a handful of assignments; by quarter's end it will be
dozens per class (× 7 classes, × students, plus a term of alerts/history).
The overview-vs-detail split must be decided against *that* volume, not
week-one data. Designed against seeded data; mockups (matching the real
tokens, dark theme) live in the "Student Page Views" artifact:
https://claude.ai/code/artifact/d7b0733e-fd0b-468d-a6d3-79bbcb40ce1a

**The decided model — "C0: structure", to build BEFORE Phase C's visual
signal work (tints/icons paint onto these views).
Status: built (2026-09-01) — everything below shipped as specified; notes
inline where reality forced a call.**

- **Four views** on the student page, server-rendered via query params
  (`?view=`, `&course=`), no JS:
  - **Problems** (the DEFAULT) — missing + ungraded-past-due. Empty case
    is an earned all-clear state (teal check, "Nothing needs attention")
    linking onward to Recent grades, with a due-soon peek below so the
    page is never a dead end.
  - **Due soon** — open items inside the lookahead window, soonest first.
    Kept separate from Problems: at crunch time it's dozens of rows of
    *normal* workload and would bury the fires.
  - **Recent grades** — graded work newest-first across courses, grouped
    by day (Today / Yesterday / dates). Sorts on `graded_at` — verified:
    the real collector does NOT populate it (only the seeder), so the
    build's `graded_on` falls back to the assignment's first score row in
    `grade_history` everywhere (a baseline-run grade has neither and stays
    out of Recent — it isn't recent). Capped at 20 rows with a link onward
    to Everything.
  - **Everything** — the archive (receipt-lookup job): per-course cards,
    open items surfaced first, graded backlog collapsed to the last ~5
    with a no-JS "show all N" `<details>` expander; closed terms collapse
    to a finals line.
- **View switcher = mini stat cards** (owner's call: cards over plain
  chips), 4-across desktop / 2×2 mobile, Purity mini-stat style; the
  active card gets the accent border. Each carries its own data story:
  - Problems: count + 6-week open-item trend sparkline (red; series
    reconstructible from `grade_history` status transitions);
  - Due soon: count + the next deadlines themselves (deliberately NOT a
    chart);
  - Recent grades: last-10 average vs term average + last-10 score
    micro-bars (the leading indicator — recent work slips before the
    course average moves);
  - Everything: term average across courses + quarter trendline (from
    `course_history`).
  Sparklines are server-rendered inline SVG, JS-free.
- **Course summary strip** under the cards, on every view: one compact
  table row per current-term course — grade + mark, 2-week delta (from
  `course_history`), open-issue chips, last-graded recency. Clicking a
  row scopes the active view to that course (`?course=`). Rows now;
  the strip is where per-course sparklines land later (trends question).
  Single-course (elementary) students skip the strip entirely.
- **Overview badges** (Phase C) become deep links into these views —
  "1 missing" → `?view=problems`. One mechanism, several doors.
  **Done with C0** (trivial once the views existed): missing/past-due
  badges → `?view=problems`, due-soon → `?view=due`. The remaining Phase C
  badge item is only the highlight-and-scroll `?status=` treatment.
- Build notes (2026-09-01): stat cards are `<a>` links that keep the
  `?course=` scope when switching views; counts stay student-wide (the
  scoped row is marked in the strip, and the view card's heading names the
  course). The strip's scoped row shows an inset accent bar; clicking it
  again clears the filter. The problems trend reconstructs per-day status
  from `grade_history` transitions, using `assigned` as the
  existed-on-that-day proxy. The 2-week delta reads `course_history`
  percent rows ("value in effect 14 days ago"). Graded-backlog expander
  and closed terms are `<details>`; the expander's continuation table
  shares column widths via `<colgroup>`. Sparkline SVGs color through
  `style='stroke:var(--…)'` so both themes work.
- **Alerts page ripple**: same treatment later — type-group chips,
  unacked surfaced, "older →" paging instead of the silent 100-row cap,
  local dates per the existing Phase C item.
  **Done (2026-09-01), shipped with Phase C:** chips are one pill per alert
  type present, with counts ("all 1305 · grade changed 662 · …"), filtering
  via `?type=`; unacked alerts sort first (stable ORDER BY, so offset paging
  stays consistent) with the strip's inset accent bar and a "N
  unacknowledged — surfaced first" note; 50 rows per page with ← newer /
  older → links (`?page=`, filter preserved). No JS anywhere in it.

Original pre-decision framing, kept for the record:

- Build a **demo-data seeder** (e.g. `lastbell seed-demo` into a
  throwaway DB, or a script) that fabricates a realistic quarter-end state:
  ~25–40 assignments per class with plausible types/dates/scores, a few
  missing/past-due, weeks of grade history and alerts, two terms.
  **Done (2026-09-01):** `lastbell seed-demo` (lastbell/seed.py) writes
  `data/demo.db` (refuses the live DB; `--force` to overwrite; `--seed` for
  reproducible screenshots). It replays two 9-week quarters of school days
  through the real pipeline — apply_time_rules → diff → record_alert →
  persist_snapshot — so statuses, rollover, history rows, and alert wording
  are production-identical; only the timestamp defaults are rewritten to the
  simulated day. Per-course grade "personalities" (steady/slipping/volatile/
  recovering, one bombed test → a real GRADE_DROP) give the trend charts
  shapes worth designing against. View: `lastbell dashboard --db data/demo.db`
  (the `--db` flag is new).
- ~~Design against it, then decide~~ (all decided above): overview card
  scale, student-detail default sort and collapses, alerts paging.
- The seeder stays useful afterward for screenshots, docs, and demoing
  without exposing real student data.

### Open design question — grade trends over time
Consider whether the overview and/or student detail show trends: e.g. a
sparkline of course percent next to each course, a larger per-course chart on
the detail page (percent line over the term, assignment scores as points).
The Purity template's chart cards are the visual precedent; server-rendered
inline SVG keeps it JS-free.

- **Data-model prerequisite — DONE (2026-08-31):** `course_history` now
  logs every course mark/percent change append-only (mirrors
  `grade_history`); shown on the dashboard History page under "Course
  grades". Data accumulates from now on; trend charts draw from this series.
- The demo-data seeder (above) should fabricate months of course-percent
  history so trend layouts can be designed at realistic density.
  **Done (2026-09-01)** — the seeder's grade personalities produce
  realistic trajectories.
- Partially decided via the density decision (above): the student page's
  stat cards carry the first sparklines (open-problem trend, last-10
  scores, term-average line), and the course strip is the reserved slot
  for per-course sparklines. Still open: per-course chart on a scoped
  course view, sparklines on the overview's student cards, and whether
  the term-final summary alert links to/embeds the closing term's
  trajectory.

### Later — school-name links (owner's note 2026-09-01)
Every rendered instance of a school name (overview cards, student page
header, anywhere else one appears) becomes a link to that school's own
website, resolved via the MCPS school directory/index. Needs a lookup
table or resolver from the school name ParentVUE reports to the school's
site URL — decide whether that's a bundled static map, a config file, or
a scrape of the MCPS directory. Not scheduled yet.

### Cross-cutting — human-readable errors
- Inventory every user-facing failure (dashboard error page, CLI poll
  warnings, channel delivery failures, collect errors) and rewrite as plain
  language + what happens next ("Couldn't reach the portal — retrying at the
  next poll"). Rides along with whichever phase touches each surface.

## Design source (resolved)
- Figma: "Purity UI Dashboard - Chakra UI Dashboard (Community)", file key
  `0q4rKhRxuflN8snhqSm3k0`. Vector screens on page 0:1 ("Free Version"):
  Dashboard `2:31`, Tables `29:2`, Billing `42:2`, Profile `63:149`,
  Sign In `88:3`, Sign Up `92:86` — the component reference for Phases B–D.
- Exact token values cross-checked against the template's open-source Chakra
  theme (creativetimofficial/purity-ui-dashboard, MIT — attribution in README
  Credits and style.css).
- Phase A status: **done** — tokens live in `lastbell/style.css`,
  served at `/static/style.css`.
