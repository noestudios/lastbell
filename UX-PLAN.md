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
- Row tint + leading icon per status, from the same tokens as badges:
  missing = bad, due soon = warn, ungraded past due = warn-strong,
  graded below the global cutoff = bad-tinted score.
- Overview badges ("2 due soon", "1 missing") click through to
  `/student/<agu>?status=…`, which highlights and scrolls to matching rows.
- Alerts list: local-time **dates** only (today/yesterday for recent), full
  local timestamp on hover. (Stored timestamps are UTC; the current
  "When (UTC)" column is the anti-pattern this replaces.)
- **Styled tooltips**: the score hover (raw points behind the percentage) —
  and any other hover reveal, e.g. the alert timestamp above — renders as a
  design-system tooltip (tokens: surface, radius, shadow), not the browser's
  tiny native `title` bubble. CSS-only where possible (positioned
  pseudo-element/`data-tip` attribute), no JS dependency.

### D. Interaction — ack
- One-time viewer identity picker (choose your watcher), remembered in
  localStorage; ack becomes a single checkmark click attributed to that
  person. Acked rows show ✓ + who (+ when on hover). Identity switchable.
- With decision 3, a fresh install always has at least one watcher, so the
  ack UI never silently disappears (its current failure mode).

### Open design question — density at end-of-quarter scale
Today each class holds a handful of assignments; by quarter's end it will be
dozens per class (× 7 classes, × students, plus a term of alerts/history).
The overview-vs-detail split must be decided against *that* volume, not
week-one data. Before locking Phase C layouts:

- Build a **demo-data seeder** (e.g. `lastbell seed-demo` into a
  throwaway DB, or a script) that fabricates a realistic quarter-end state:
  ~25–40 assignments per class with plausible types/dates/scores, a few
  missing/past-due, weeks of grade history and alerts, two terms.
- Design against it, then decide:
  - what an overview card earns at that scale (currently full course table —
    probably: overall grade + open-issue flags + maybe a trend, nothing more);
  - student detail: default sort (due date vs. recently-graded first),
    whether graded backlog collapses ("last 5 + view all"), per-course
    collapse, and whether closed terms collapse by default;
  - whether alerts/history need paging.
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
- Decide: sparkline on overview cards vs. detail-only charts; whether the
  term-final summary alert links to/embeds the closing term's trajectory.

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
