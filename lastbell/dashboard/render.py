"""Page rendering: rows in, HTML out. The shell (nav, theme, icons),
the shared cell helpers, and the overview / student / alerts / history
pages. Pure functions — no database, no HTTP."""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote

from .. import schools

from .queries import (
    _ALERTS_PAGE,
    alerts_last_page,
)

# The public home of the project — the settings-footer credit links here.
_REPO_URL = "https://github.com/noestudios/lastbell"

# Screen-reader marker for the engaged option in a set of links (active view
# card, active filter chip): the visual accent alone doesn't announce.
_CURRENT = " aria-current='true'"
# …and for nav links, the current PAGE (also the visual current state's hook).
_CURRENT_PAGE = " aria-current='page'"

_STATUS_LABELS = {
    "graded": ("graded", "ok"),
    "not_due": ("not due yet", "muted"),
    "due": ("due", "info"),
    "missing": ("MISSING", "bad"),
    "ungraded_past_due": ("ungraded past due", "warn"),
    "submitted": ("turned in", "info"),      # Canvas: handed in, not yet graded
}

# The alerts page's Type cell, on the same severity ladder as the status
# badges above: red for what needs action now, amber for slipping work,
# teal for the heads-up, muted for the informational/report types.
_ALERT_TYPE_BADGE = {
    "grade_drop": ("grade drop", "bad"),
    "assignment_missing": ("assignment missing", "bad"),
    "ungraded_past_due": ("ungraded past due", "warn"),
    "upcoming_deadline": ("upcoming deadline", "info"),
    "grade_changed": ("grade changed", "muted"),
    "daily_summary": ("daily summary", "muted"),
    "term_final": ("term final", "muted"),
    "source_conflict": ("gradebook vs Canvas", "warn"),
}

# Phase C row signal: statuses that earn a tint + leading icon, so a mixed
# table scans by color before it's read. Escalation ladder: due soon is a
# light caution, ungraded-past-due a stronger one, missing is red. The icons
# are feather-style paths (rendered via _SVG below); each colors through the
# same token its badge uses.
_STATUS_ROWS = {
    "missing": ("st-missing", "var(--bad-ink)",
                "<circle cx='12' cy='12' r='10'/>"
                "<line x1='12' y1='8' x2='12' y2='12'/>"
                "<line x1='12' y1='16' x2='12.01' y2='16'/>"),
    "ungraded_past_due": (
        "st-late", "var(--warn-ink)",
        "<path d='M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0"
        " 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z'/>"
        "<line x1='12' y1='9' x2='12' y2='13'/>"
        "<line x1='12' y1='17' x2='12.01' y2='17'/>"),
    "due": ("st-due", "var(--warn-ink)",
            "<circle cx='12' cy='12' r='10'/>"
            "<polyline points='12 6 12 12 16 14'/>"),
}


def _score_cutoff() -> float | None:
    """The global display threshold (decision 4): graded scores below it tint
    bad. Display-only — nothing alerts on it. 0 or empty disables."""
    try:
        value = float(os.environ.get("LASTBELL_SCORE_CUTOFF", "70"))
    except ValueError:
        return None
    return value if value > 0 else None

# The theme lives in style.css next to this module (design tokens extracted
# from the Purity UI Dashboard template) and is served at /static/style.css.
# app.js is the page's one script: settings-form niceties (dirty tracking,
# row enter/exit motion, toast dismissal) that degrade to plain form posts.
_PKG = Path(__file__).resolve().parent.parent   # lastbell/, where the assets live
_STYLE_PATH = _PKG / "style.css"
_FAVICON_PATH = _PKG / "favicon.png"
_APPJS_PATH = _PKG / "app.js"


# ── rendering (pure: rows in, html out) ───────────────────────────────


# Theme toggle: cycles auto → light → dark, saved per browser. Icon-only,
# gear-sized (auto = half-filled circle, light = sun, dark = moon); the
# state rides the title/aria-label. All three icons ship in the button and
# style.css shows the one matching <html data-theme> — which the head
# script sets before paint — so neither the colors nor the icon ever flash
# (an innerHTML swap on DOMContentLoaded used to lag a frame behind).
def _theme_svg(kind: str, body: str) -> str:
    return (f"<svg class='ic-{kind}' viewBox='0 0 24 24' fill='none' "
            "stroke='currentColor' stroke-width='2' stroke-linecap='round' "
            f"stroke-linejoin='round' aria-hidden='true'>{body}</svg>")


_THEME_ICONS = (
    _theme_svg("auto", "<circle cx='12' cy='12' r='10'/>"
               "<path d='M12 2a10 10 0 0 0 0 20z' fill='currentColor' stroke='none'/>")
    + _theme_svg("light", "<circle cx='12' cy='12' r='5'/>"
                 "<line x1='12' y1='1' x2='12' y2='3'/><line x1='12' y1='21' x2='12' y2='23'/>"
                 "<line x1='4.22' y1='4.22' x2='5.64' y2='5.64'/>"
                 "<line x1='18.36' y1='18.36' x2='19.78' y2='19.78'/>"
                 "<line x1='1' y1='12' x2='3' y2='12'/><line x1='21' y1='12' x2='23' y2='12'/>"
                 "<line x1='4.22' y1='19.78' x2='5.64' y2='18.36'/>"
                 "<line x1='18.36' y1='5.64' x2='19.78' y2='4.22'/>")
    + _theme_svg("dark", "<path d='M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z'/>"))
_THEME_JS = """
(function(){
  var KEY='lastbell-theme', root=document.documentElement, choice=null;
  try { choice = localStorage.getItem(KEY); } catch (e) {}
  function apply(){
    if (choice==='light'||choice==='dark') root.setAttribute('data-theme', choice);
    else root.removeAttribute('data-theme');
    var b=document.getElementById('themetoggle');
    if (!b) return;
    b.setAttribute('data-tip', 'Theme: '
                   + (choice || 'auto (follows your system)'));
    b.setAttribute('aria-label', 'Theme: ' + (choice || 'auto'));
  }
  apply();
  window.addEventListener('DOMContentLoaded', function(){
    apply();
    var b=document.getElementById('themetoggle');
    if (!b) return;
    b.addEventListener('click', function(){
      choice = choice===null ? 'light' : choice==='light' ? 'dark' : null;
      try {
        if (choice) localStorage.setItem(KEY, choice);
        else localStorage.removeItem(KEY);
      } catch (e) {}
      apply();
    });
  });
})();
"""


# Feather-style 24-viewbox stroke icons; shown in place of nav labels below
# the narrow-nav breakpoint (styled entirely by CSS, stroke follows text color).
_SVG = ("<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' "
        "stroke-width='2' stroke-linecap='round' stroke-linejoin='round' "
        "aria-hidden='true'>{}</svg>")
# The students icon fronts the narrow-width student menu; the old top-level
# "Students" nav item is gone (the brand already links to the overview) and
# students appear as direct name links instead.
_STUDENTS_ICON = (
    "<path d='M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2'/><circle cx='12' cy='7' r='4'/>")
_NAV_ITEMS = (
    ("/alerts", "Alerts",
     "<path d='M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9'/>"
     "<path d='M13.7 21a2 2 0 0 1-3.4 0'/>"),
    ("/history", "History",
     "<circle cx='12' cy='12' r='10'/><polyline points='12 6 12 12 16 14'/>"),
)

# The brand mark: the Last Bell bell, drawn in currentColor so it follows
# the brand text through both themes (the source export hard-codes cream).
_BRAND_ICON = (
    "<svg class='mark' viewBox='0 0 148 147' fill='none' stroke='currentColor' "
    "stroke-width='11' stroke-linecap='round' stroke-linejoin='round' "
    "aria-hidden='true'><path d='M64.8092 115.767C63.4984 116.768 61.9474 "
    "117.408 60.3116 117.622C58.6758 117.835 57.0125 117.615 55.4884 116.984"
    "C53.9643 116.353 52.6328 115.332 51.6271 114.024C50.6215 112.717 49.9771 "
    "111.168 49.7584 109.532M106.807 66.9113C109.674 59.9892 109.674 52.2117 "
    "106.807 45.2897C103.94 38.3676 98.4401 32.8681 91.5181 30.0009C84.596 "
    "27.1337 76.8185 27.1337 69.8964 30.0009C62.9744 32.8681 57.4748 38.3676 "
    "54.6076 45.2897C41.995 75.7392 25.3416 79.0337 25.3416 79.0337L103.64 "
    "111.466C103.64 111.466 94.1942 97.3608 106.807 66.9113Z'/></svg>")

# Settings sits apart from the page links: always icon-only (no label at any
# width), right-aligned against the theme toggle.
_GEAR_ICON = _SVG.format(
    "<circle cx='12' cy='12' r='3'/>"
    "<path d='M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83"
    " 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1"
    " 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65"
    " 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06"
    "a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2"
    " 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06"
    "a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9"
    "a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65"
    " 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2"
    " 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51"
    " 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z'/>")


def _nav_names(students) -> list[str]:
    """Nav display names: first names, unless two students share one — then
    everyone gets their full name. When a first name stands in for a longer
    full name, the nav link reveals the full name on hover (see _nav_students)."""
    firsts = [(s["name"] or "").split()[0] or s["name"] for s in students]
    if len(set(firsts)) < len(firsts):
        return [s["name"] for s in students]
    return firsts


def _nav_students(students, path="") -> str:
    """Student links for the nav: names inline at desktop width; on narrow
    they give way to a <details> menu behind the students icon (no JS to
    open — app.js only adds outside-click close)."""
    if not students:
        return ""
    names = _nav_names(students)
    def cur(s):
        return _CURRENT_PAGE if path == f"/student/{s['agu']}" else ""
    inline = "".join(
        # The tooltip earns its place only when the nav abbreviates: it reveals
        # the full name behind a first name. When the shown name already IS the
        # full name, a tooltip would just echo it, so render a plain link.
        (f"<a class='tip-b'{cur(s)} href='/student/{escape(s['agu'])}' "
         f"data-tip='{escape(s['name'])}'>{escape(n)}</a>"
         if n != s["name"] else
         f"<a{cur(s)} href='/student/{escape(s['agu'])}'>{escape(n)}</a>")
        for s, n in zip(students, names))
    menu = "".join(
        f"<a href='/student/{escape(s['agu'])}'>{escape(s['name'])}</a>"
        for s in students)
    return (f"<span class='navstudents'>{inline}</span>"
            f"<details class='smenu'><summary "
            f"aria-label='Students'>{_SVG.format(_STUDENTS_ICON)}</summary>"
            f"<div class='smenu-list'>{menu}</div></details>")


def _page(title: str, body: str, nav_students=(), path="") -> str:
    def cur(href):
        return _CURRENT_PAGE if href and href == path else ""
    links = "".join(
        f"<a href='{href}'{cur(href)} aria-label='{label}'>{_SVG.format(icon)}"
        f"<span class='lbl'>{label}</span></a>"
        for href, label, icon in _NAV_ITEMS)
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{escape(title)} · Last Bell</title>"
        "<link rel='icon' type='image/png' href='/static/favicon.png'>"
        "<link rel='stylesheet' href='/static/style.css'>"
        f"<script>{_THEME_JS}</script>"
        "<script src='/static/app.js' defer></script></head><body>"
        "<a class='skip' href='#main'>Skip to content</a>"
        f"<nav><a class='brand' href='/'>{_BRAND_ICON}Last Bell</a>"
        f"{_nav_students(nav_students, path)}{links}"
        f"<a class='gear'{cur('/settings')} href='/settings' "
        f"aria-label='Settings'>{_GEAR_ICON}</a>"
        f"<button id='themetoggle' class='tip-b tip-e' "
        f"aria-label='Theme: auto'>{_THEME_ICONS}</button></nav>"
        f"<main id='main'>{body}</main>"
        "<div id='announce' class='vh' role='status' aria-live='polite'>"
        "</div></body></html>"
    )


def _badge(status: str) -> str:
    label, klass = _STATUS_LABELS.get(status, (status, "muted"))
    return f"<span class='badge {klass}'>{escape(label)}</span>"


def _twin(row) -> str:
    """"Canvas says 9/10" after a gradebook row whose hidden Canvas twin
    carries a grade the gradebook doesn't: the record stays the record, the
    disagreement is visible."""
    try:
        cscore = row["canvas_score"]
    except (IndexError, KeyError):
        return ""
    if cscore is None:
        return ""
    cpoints = row["canvas_points"]
    shown = f"{cscore:g}/{cpoints:g}" if cpoints else f"{cscore:g}"
    same = (row["score"] is not None and row["points"] and cpoints
            and abs(row["score"] / row["points"] - cscore / cpoints) < 0.005)
    if same or (row["score"] is not None and row["score"] == cscore and not cpoints):
        return ""
    return _tip(f"<span class='src twin'>Canvas says {escape(shown)}</span>",
                "Canvas has a grade the gradebook doesn't show — likely not "
                "synced yet; worth checking with the teacher")


def _src(row) -> str:
    """A small "Canvas" mark after an assignment name whose row came from
    Canvas rather than the gradebook — it tells the reader which app to open,
    and why the course grade doesn't reflect it yet."""
    try:
        source = row["source"]
    except (IndexError, KeyError):
        return ""
    if source != "canvas":
        return ""
    return _tip("<span class='src'>Canvas</span>",
                "From Canvas — not in the gradebook yet")


def _tip(inner_html: str, tip: str, extra_class: str = "") -> str:
    """Wrap already-escaped html in a design-system tooltip (CSS-only: the
    bubble is a ::after reading data-tip — never the browser's native
    ``title`` speck). The span is focusable so keyboard users can summon
    the bubble, and the tip text rides aria-label so screen readers hear
    what hover reveals — some tips (exact timestamps, raw scores) carry
    data that exists nowhere else on the page."""
    cls = f"tip {extra_class}".strip()
    return (f"<span class='{cls}' tabindex='0' data-tip='{escape(tip)}'>"
            f"{inner_html}<span class='vh'> ({escape(tip)})</span></span>")


def _school_link(name: str) -> str:
    """The school name as muted text; when its own website is known (via the
    bundled MCPS directory) it becomes a new-tab link with a 'Visit school
    website' tip. Plain escaped text on any miss — unknown school, ambiguous
    name, or absent data file. The first outbound link in the app."""
    safe = escape(name)
    if not name:
        return safe
    url = schools.school_url(name)
    if not url:
        return safe
    anchor = (f"<a class='schoollink' href='{escape(url)}' "
              f"target='_blank' rel='noopener noreferrer'>{safe}</a>")
    return _tip(anchor, "Visit school website")


def _row_mark(status: str, hl: str = "", first_hit: bool = False) -> tuple[str, str]:
    """(tr attributes, leading icon html) for an assignment row. ``hl`` is the
    ?status= highlight target; the first matching row gets id='hit' so the
    badge link's #hit fragment scrolls to it."""
    klass, color, icon = _STATUS_ROWS.get(status, ("", "", ""))
    classes = " ".join(c for c in (klass, "hit" if status == hl else "") if c)
    attrs = (f" class='{classes}'" if classes else "") + (
        " id='hit'" if first_hit and status == hl else "")
    lead = ""
    if icon:
        lead = ("<svg class='rowicon' viewBox='0 0 24 24' fill='none' "
                f"style='stroke:{color}' stroke-width='2' stroke-linecap='round' "
                f"stroke-linejoin='round' aria-hidden='true'>{icon}</svg>")
    return attrs, lead


# Stored timestamps are UTC (sqlite datetime('now')); the reader lives in
# local time. Display rule (Phase C): date words in the cell — today /
# yesterday for recent — with the full local timestamp in the tooltip.
def _when_html(utc_iso: str, today: date | None = None) -> str:
    try:
        dt = (datetime.fromisoformat(utc_iso).replace(tzinfo=timezone.utc)
              .astimezone())
    except ValueError:
        return escape(utc_iso)
    today = today or date.today()
    d = dt.date()
    if d == today:
        words = "today"
    elif d == today - timedelta(days=1):
        words = "yesterday"
    elif d.year == today.year:
        words = f"{d.strftime('%b')} {d.day}"
    else:
        words = f"{d.strftime('%b')} {d.day}, {d.year}"
    return _tip(escape(words), dt.strftime("%Y-%m-%d %H:%M %Z").strip())


def _pct(raw: str) -> str:
    """Course percent for display: one decimal place, or a dash."""
    from ..models import format_percent

    formatted = format_percent(raw)
    return formatted if formatted is not None else (raw or "—")


def _score(row) -> str:
    """Assignment score as a percentage (one decimal), raw points in a styled
    tooltip. A score below the global cutoff tints bad (Phase C signal).

    No points value (or zero, e.g. extra credit) means no denominator to
    percent against — those show the raw score.
    """
    if row["score"] is None:
        return "—"
    if not row["points"]:
        return escape(f"{row['score']:g}")
    raw = f"{row['score']:g}/{row['points']:g}"
    pct = row["score"] / row["points"] * 100
    return _tip(f"{pct:.1f}%", raw, extra_class=_low_class(pct))


def _low_class(pct: float | None) -> str:
    cutoff = _score_cutoff()
    return "low" if pct is not None and cutoff and pct < cutoff else ""


def _freshness_html(last_poll_utc: str | None, now: datetime | None = None,
                    failure_note: str = "") -> str:
    """The home page's last line: when the watcher last finished a poll, in
    the reader's local time. Past twice the poll interval it turns into a
    notice — the data on the page is what a stopped watcher last saw. A
    ``failure_note`` (the watcher is running but its polls are failing)
    makes it a notice at once, whatever the age."""
    if failure_note:
        when = f"Last checked {escape(last_poll_utc)}. " if last_poll_utc else ""
        return (f"<footer class='credit freshness stale' role='status'>{when}"
                f"{escape(failure_note)}</footer>")
    if not last_poll_utc:
        return ("<footer class='credit freshness'>Not checked yet — run "
                "<code>lastbell run</code> to take the first look.</footer>")
    try:
        dt = (datetime.fromisoformat(last_poll_utc).replace(tzinfo=timezone.utc)
              .astimezone())
    except ValueError:
        return f"<footer class='credit freshness'>Last checked {escape(last_poll_utc)}</footer>"
    now = now or datetime.now(timezone.utc).astimezone()
    d, today = dt.date(), now.date()
    if d == today:
        day = "today"
    elif d == today - timedelta(days=1):
        day = "yesterday"
    else:
        day = dt.strftime("%a %b ") + str(d.day) + ("" if d.year == today.year else f", {d.year}")
    clock = dt.strftime("%I:%M %p").lstrip("0")
    try:
        poll_minutes = int(os.environ.get("LASTBELL_POLL_MINUTES", "180"))
    except ValueError:
        poll_minutes = 180
    age = now - dt
    stale = age > timedelta(minutes=max(poll_minutes, 15) * 2)
    text = f"Last checked {escape(day)} at {escape(clock)}"
    if stale:
        hours = int(age.total_seconds() // 3600)
        ago = (f"{hours} hours" if hours < 48 else f"{hours // 24} days")
        return (f"<footer class='credit freshness stale' role='status'>{text} — "
                f"{ago} ago. The watcher looks like it isn't running; "
                "<code>lastbell run --loop</code> (or the installed service) "
                "keeps this page current.</footer>")
    return f"<footer class='credit freshness'>{text}</footer>"


def render_overview(students, courses_by_student, counts_by_student,
                    last_poll_utc: str | None = None, failure_note: str = "") -> str:
    if not students:
        return _page("Students",
                     "<h1>No students yet</h1><p>Run <code>lastbell run</code> "
                     "once to establish a baseline.</p>")
    cards = []
    for s in students:
        courses = courses_by_student.get(s["id"], [])
        counts = counts_by_student.get(s["id"], {})
        # Course names deep-link to the student page scoped to that course.
        rows = "".join(
            f"<tr><td><a href='/student/{escape(s['agu'])}"
            f"?course={quote(c['edupoint_gu'])}'>{escape(c['title'])}</a></td>"
            f"<td data-label='Teacher'>{escape(c['teacher'])}</td>"
            f"<td class='num' data-label='%'>{escape(_pct(c['percent']))}</td>"
            f"<td data-label='Mark'>{escape(c['mark'] or '—')}</td></tr>"
            for c in courses)
        # Badges deep-link into the student page's views: one mechanism,
        # several doors ("1 missing" → the Problems view).
        base = f"/student/{escape(s['agu'])}"
        flags = []
        # ?status= narrows the highlight inside the mixed Problems list; the
        # #hit fragment scrolls to the first matching row (id='hit').
        if counts.get("missing"):
            flags.append(f"<a href='{base}?view=problems&status=missing#hit'>"
                         f"<span class='badge bad'>"
                         f"{counts['missing']} missing</span></a>")
        if counts.get("past_due"):
            flags.append(
                f"<a href='{base}?view=problems&status=ungraded_past_due#hit'>"
                f"<span class='badge warn'>"
                f"{counts['past_due']} ungraded past due</span></a>")
        if counts.get("due"):
            flags.append(f"<a href='{base}?view=due'><span class='badge info'>"
                         f"{counts['due']} due soon</span></a>")
        cards.append(
            f"<div class='card'><h2 class='cardname'><a href='/student/{escape(s['agu'])}'>"
            f"{escape(s['name'])}</a></h2>"
            f"<div class='small'>{_school_link(s['school'])}</div>"
            f"<div>{' '.join(flags) or '<span class=small>all clear</span>'}</div>"
            f"<table class='courses'><tr class='head'><th scope='col'>Course</th><th scope='col'>Teacher</th>"
            f"<th scope='col'>%</th><th scope='col'>Mark</th></tr>{rows}</table></div>")
    return _page("Students", "<h1>Students</h1><div class='cards'>" + "".join(cards)
                 + "</div>" + _freshness_html(last_poll_utc, failure_note=failure_note), path="/",
                 nav_students=students)


# ── student page (C0): stat cards, course strip, four views ───────────


_CHECK_BIG = ("<svg viewBox='0 0 24 24' fill='none' style='stroke:var(--edge)' "
              "stroke-width='2' stroke-linecap='round' stroke-linejoin='round' "
              "aria-hidden='true'><path d='M22 11.08V12a10 10 0 1 1-5.93-9.14'/>"
              "<polyline points='22 4 12 14.01 9 11.01'/></svg>")
_CHEVRON = ("<svg viewBox='0 0 12 12' fill='none' stroke='currentColor' "
            "stroke-width='1.8' stroke-linecap='round' aria-hidden='true'>"
            "<path d='M2.5 4.5 L6 8 L9.5 4.5'/></svg>")
# The strip's filter affordance: a funnel after each course name says "this
# filters"; on the scoped row it becomes an × ("click to clear").
_FILTER_ICON = ("<svg class='filtericon' viewBox='0 0 24 24' fill='none' "
                "stroke='currentColor' stroke-width='2' stroke-linecap='round' "
                "stroke-linejoin='round' aria-hidden='true'>"
                "<polygon points='22 3 2 3 10 12.46 10 19 14 21 14 12.46'/></svg>")
_CLEAR_ICON = ("<svg class='filtericon' viewBox='0 0 24 24' fill='none' "
               "stroke='currentColor' stroke-width='2' stroke-linecap='round' "
               "aria-hidden='true'><line x1='18' y1='6' x2='6' y2='18'/>"
               "<line x1='6' y1='6' x2='18' y2='18'/></svg>")
_ARROWS = {"up": "M6 2 L10.5 9 L1.5 9 Z", "down": "M6 10 L10.5 3 L1.5 3 Z"}


def _short_title(title: str) -> str:
    """Course title without the schedule-period prefix ("2: Algebra 1A")."""
    return re.sub(r"^\d+:\s*", "", title or "")


def _ago(iso: str | None, today: date) -> str:
    """Recency in words: today / yesterday / N days ago / Aug 12."""
    if not iso:
        return "—"
    d = date.fromisoformat(iso[:10])
    n = (today - d).days
    if n <= 0:
        return "today"
    if n == 1:
        return "yesterday"
    if n < 7:
        return f"{n} days ago"
    return f"{d.strftime('%b')} {d.day}"


def _due_word(iso: str, today: date) -> str:
    """A deadline in words: yesterday / today / tomorrow / Thu / Sep 12.
    (Yesterday happens: an item stays 'due' through the ungraded grace
    window after its due date passes.)"""
    d = date.fromisoformat(iso[:10])
    n = (d - today).days
    if n == -1:
        return "yesterday"
    if n == 0:
        return "today"
    if n == 1:
        return "tomorrow"
    if 1 < n < 7:
        return d.strftime("%a")
    return f"{d.strftime('%b')} {d.day}"


def _day_heading(d: date, today: date) -> str:
    if d == today:
        return "Today"
    if d == today - timedelta(days=1):
        return "Yesterday"
    return f"{d.strftime('%a %b')} {d.day}"


def _spark_line(values, color: str, *, lo=None, hi=None, label="") -> str:
    """A 120×34 inline-SVG trend line with an end dot — JS-free."""
    if len(values) < 2:
        return ""
    lo = min(values) if lo is None else lo
    hi = max(values) if hi is None else hi
    span = hi - lo
    ys = [17.0 if span < 1e-9 else 5 + (1 - (v - lo) / span) * 24
          for v in values]
    step = 112 / (len(values) - 1)
    pts = " ".join(f"{4 + i * step:.1f},{y:.1f}" for i, y in enumerate(ys))
    aria = (f"role='img' aria-label='{escape(label)}'" if label
            else "aria-hidden='true'")
    return (f"<svg class='spark' viewBox='0 0 120 34' "
            f"preserveAspectRatio='none' {aria}>"
            f"<polyline points='{pts}' fill='none' style='stroke:{color}' "
            f"stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/>"
            f"<circle cx='116' cy='{ys[-1]:.1f}' r='3' style='fill:{color}'/>"
            f"</svg>")


def _spark_bars(pcts, label="") -> str:
    """Up to ten bottom-aligned score micro-bars (50–100% sets the height)."""
    rects = []
    for i, p in enumerate(pcts[:10]):
        h = 4 + (min(max(p, 50.0), 100.0) - 50.0) / 50.0 * 26
        rects.append(f"<rect x='{4 + i * 12}' y='{34 - h:.1f}' width='6' "
                     f"height='{h:.1f}' rx='2' style='fill:var(--edge)'/>")
    aria = (f"role='img' aria-label='{escape(label)}'" if label
            else "aria-hidden='true'")
    return (f"<svg class='spark' viewBox='0 0 120 34' "
            f"preserveAspectRatio='none' {aria}>"
            + "".join(rects) + "</svg>")


def _delta_html(cur, base) -> str:
    if cur is None or base is None or abs(cur - base) < 0.05:
        return "<span class='delta flat'>—</span>"
    cls = "up" if cur > base else "down"
    # The sign is the information — spoken for screen readers (the arrow is
    # decorative) and shown, since "+1.2" scans faster than arrow+1.2 anyway.
    signed = f"{cur - base:+.1f}"
    return (f"<span class='delta {cls}' "
            f"aria-label='{'up' if cls == 'up' else 'down'} "
            f"{abs(cur - base):.1f} points in 2 weeks'>"
            f"<svg viewBox='0 0 12 12' "
            f"fill='currentColor' aria-hidden='true'><path d='{_ARROWS[cls]}'/>"
            f"</svg>{signed}</span>")


def _stat_cards(student, ctx) -> str:
    """The view switcher: four mini stat cards, each a plain link carrying
    its own data story (active view = accent border)."""
    c = ctx["cards"]
    agu = quote(student["agu"])
    scope = f"&course={quote(ctx['course_gu'])}" if ctx["course_gu"] else ""

    def card(view, label, big, ctxline, extra="") -> str:
        cls = " active" if view == ctx["view"] else ""
        cur = " aria-current='true'" if view == ctx["view"] else ""
        return (f"<a class='stat{cls}'{cur} "
                f"href='/student/{agu}?view={view}{scope}'>"
                f"<span class='lbl'>{label}</span>"
                f"<span class='big'>{big}</span>"
                f"<span class='ctx'>{ctxline}</span>{extra}</a>")

    wk = c["problems_week"]
    if wk > 0:
        p_ctx = f"<b style='color:var(--bad-ink)'>+{wk}</b> this week"
    elif wk < 0:
        p_ctx = f"<b style='color:var(--ok-ink)'>−{-wk}</b> this week"
    else:
        p_ctx = "no change this week"
    ps = c["problems_series"]
    p_spark = (_spark_line(ps, "var(--bad-ink)", lo=0,
                           label=f"items needing attention over 6 weeks: "
                                 f"{ps[0]:g} then, {ps[-1]:g} now")
               if max(ps, default=0) > 0 else "")
    # Labelled to match the panel it opens; the ?view=problems key is kept
    # so existing links and bookmarks survive.
    parts = [card("problems", "Needs attention", str(c["problems_count"]),
                  p_ctx, p_spark)]

    lookahead = os.environ.get("LASTBELL_LOOKAHEAD_DAYS", "7")
    nextlines = "".join(
        f"<div class='nextline'>{escape(r['name'])} <span class='small'>· "
        f"{escape(_short_title(r['course_title']))}"
        + (f" · {_due_word(r['due_date'], ctx['today'])}"
           if r["due_date"] else "")
        + "</span></div>"
        for r in c["due_next"])
    parts.append(card("due", "Due soon", str(c["due_count"]),
                      f"next {escape(lookahead)} days", nextlines))

    pcts = c["recent_pcts"]
    if pcts:
        big = f"{sum(pcts) / len(pcts):.1f}<span class='unit'>%</span>"
        r_ctx = f"last {len(pcts)}"
        if c["term_avg"] is not None:
            r_ctx += f" · term avg {c['term_avg']:.1f}"
        extra = _spark_bars(
            list(reversed(pcts)),
            label=f"last {len(pcts)} scores as bars, oldest first, "
                  f"{min(pcts):.0f} to {max(pcts):.0f} percent")
    else:
        big, r_ctx, extra = "—", "no grades yet", ""
    parts.append(card("recent", "Recent grades", big, r_ctx, extra))

    big = (f"{c['term_avg']:.1f}<span class='unit'>%</span>"
           if c["term_avg"] is not None else "—")
    n = c["courses"]
    e_ctx = f"term average · {n} course{'s' if n != 1 else ''}"
    ts = c["term_series"]
    e_extra = (_spark_line(ts, "var(--edge)",
                           label=f"term average trend: {ts[0]:.1f} "
                                 f"to {ts[-1]:.1f} percent")
               if len(ts) >= 2 else "")
    parts.append(card("everything", "Everything", big, e_ctx, e_extra))
    return "<div class='stats'>" + "".join(parts) + "</div>"


def _course_strip(student, ctx) -> str:
    """One compact row per current-term course; clicking a row scopes the
    active view to that course (clicking the scoped row clears the filter).
    This is also where per-course sparklines land later."""
    agu = quote(student["agu"])
    rows = []
    for c in ctx["strip"]:
        gu = c["edupoint_gu"]
        active = gu == ctx["course_gu"]
        # Clearing the filter carries ?strip=open: deselecting must not
        # collapse the bar the reader is working in.
        href = f"/student/{agu}?view={ctx['view']}" + (
            "&strip=open" if active else f"&course={quote(gu)}")
        tip = ("show all courses" if active
               else f"show only {c['title']} in this view")
        pct = _pct(c["percent"]) if c["percent"] else ""
        # One wrapping span per cell: the stacked (phone) layout flexes a
        # labeled cell's children apart, and grade + mark must stay together.
        grade = (f"<span><strong>{escape(pct)}</strong> "
                 f"<span class='small'>{escape(c['mark'] or '')}</span></span>"
                 if pct else "<span class='small'>—</span>")
        chips = []
        if c["missing"]:
            chips.append(f"<span class='badge bad'>{c['missing']} missing</span>")
        if c["past_due"]:
            chips.append(f"<span class='badge warn'>{c['past_due']} past due</span>")
        row_class = " class='scoped'" if active else ""
        rows.append(
            f"<tr{row_class}>"
            f"<td><a class='tip-s'{_CURRENT if active else ''} href='{href}' "
            f"data-tip='{escape(tip)}'>"
            f"{escape(c['title'])}"
            f"{_CLEAR_ICON if active else _FILTER_ICON}</a></td>"
            f"<td data-label='Grade'>{grade}</td>"
            f"<td data-label='2 weeks'>{_delta_html(*ctx['deltas'][c['id']])}</td>"
            f"<td data-label='Open'><span>{' '.join(chips)}</span></td>"
            f"<td class='small' data-label='Last graded'>"
            f"{_ago(c['last_graded'], ctx['today'])}</td></tr>")
    label = (escape(ctx["term"]) + " — current") if ctx["term"] else ""
    # Collapsed by default (owner's call 2026-09-01) — the stat cards are the
    # page's front door. Held open while a course scope is active so the
    # marked row (and the way to clear the filter) stays visible.
    # The open/closed state is the READER's alone — only a manual toggle
    # changes it, never scoping/clearing/view switches (owner's call
    # 2026-09-01). The choice is saved per browser (localStorage, like the
    # theme) and applied by the inline script below synchronously — no
    # flash. With JS, the saved choice (default: closed) is the ONLY
    # authority; the server-rendered open (scoped, or the clear link's
    # ?strip=open) exists purely as the JS-off fallback, where losing the
    # filter marker in a collapsed strip would be worse.
    is_open = bool(ctx["course_gu"]) or ctx["strip_open"]
    # An active course filter must stay legible even with the strip closed:
    # the summary carries a funnel tag naming the scoped course — itself the
    # clear control (small ×, matching the scoped row's treatment), so the
    # filter can be dropped without opening the strip. A link inside a
    # summary activates as a link, not a toggle.
    scoped_title = next(
        (c["title"] for c in ctx["strip"]
         if c["edupoint_gu"] == ctx["course_gu"]), "")
    tag = ""
    if scoped_title:
        tag = (f" <a class='striptag' "
               f"href='/student/{agu}?view={ctx['view']}&strip=open' "
               f"aria-label='Clear the {escape(scoped_title)} filter — "
               f"show all courses' "
               f"data-tip='show all courses'>{_FILTER_ICON}"
               f"{escape(scoped_title)}{_CLEAR_ICON}</a>")
    return (
        "<h2 class='vh'>All Courses</h2>"
        f"<details class='allcourses' id='allcourses'"
        f"{' open' if is_open else ''}>"
        f"<summary>{_CHEVRON}<span class='striphead'>All Courses"
        + (f" <span class='termtag'>{label}</span>" if label else "")
        + tag + "</span></summary>"
          "<table class='strip' aria-label='All courses'>"
          "<tr class='head'><th scope='col'>Course</th><th scope='col'>Grade</th><th scope='col'>2 weeks</th>"
          "<th scope='col'>Open</th><th scope='col'>Last graded</th></tr>"
        + "".join(rows) + "</table></details>"
        "<script>(function(){"
        "var d=document.getElementById('allcourses');"
        "try{d.open=localStorage.getItem('lastbell-courses')==='open';}"
        "catch(e){}"
        "d.addEventListener('toggle',function(){"
        "try{localStorage.setItem('lastbell-courses',d.open?'open':'closed');}"
        "catch(e){}});"
        "})();</script>")


def _show_course_col(ctx) -> bool:
    return not ctx["course_gu"] and len(ctx["strip"]) > 1


def _card_h2(base: str, ctx) -> str:
    if ctx["course_gu"]:
        for c in ctx["strip"]:
            if c["edupoint_gu"] == ctx["course_gu"]:
                return f"{base} — {escape(c['title'])}"
    return base


def _assignment_table(rows, ctx) -> str:
    """Open-item listing for the Problems and Due-soon views. The Course
    column only earns its place unscoped on a multi-course student."""
    with_course = _show_course_col(ctx)
    head = ("<tr class='head'><th scope='col'>Assignment</th>"
            + ("<th scope='col'>Course</th>" if with_course else "")
            + "<th scope='col'>Due</th><th scope='col'>Status</th></tr>")
    body, hit_seen = [], False
    for r in rows:
        attrs, lead = _row_mark(r["status"], ctx["hl"], first_hit=not hit_seen)
        hit_seen = hit_seen or "id='hit'" in attrs
        body.append(
            f"<tr{attrs}><td>{lead}{escape(r['name'])}{_src(r)}{_twin(r)}</td>"
            + (f"<td class='small' data-label='Course'>"
               f"{escape(r['course_title'])}</td>" if with_course else "")
            + f"<td class='small' data-label='Due'>{escape(r['due_date'] or '—')}</td>"
            f"<td data-label='Status'>{_badge(r['status'])}</td></tr>")
    return f"<table class='openitems'>{head}{''.join(body)}</table>"


def _view_problems(student, ctx) -> str:
    rows = ctx["problems"]
    agu = quote(student["agu"])
    scope = f"&course={quote(ctx['course_gu'])}" if ctx["course_gu"] else ""
    if not rows:
        # The earned all-clear — with a due-soon peek so the page is never
        # a dead end.
        peek = ""
        if ctx["due"]:
            n = len(ctx["due"])
            more = (f"<p class='small' style='margin:0.7rem 0 0'>"
                    f"<a href='/student/{agu}?view=due{scope}'>all {n} →</a></p>"
                    if n > 5 else "")
            peek = ("<div class='card tablecard'><h2>Due soon</h2>"
                    + _assignment_table(ctx["due"][:5], ctx) + more + "</div>")
        return ("<div class='card allclear'>" + _CHECK_BIG
                + "<h2>Nothing needs attention</h2>"
                f"<p class='small'>Everything is graded or on schedule. "
                f"<a href='/student/{agu}?view=recent{scope}'>"
                f"See what came in recently →</a></p></div>" + peek)
    return ("<div class='card tablecard'>"
            f"<h2>{_card_h2('Needs attention', ctx)}</h2>"
            "<p class='small'>Marked missing by the teacher, or past due "
            "with no grade posted.</p>"
            + _assignment_table(rows, ctx) + "</div>")


def _view_due(student, ctx) -> str:
    rows = ctx["due"]
    body = (_assignment_table(rows, ctx) if rows else
            "<p class='small'>Nothing in the window — the calendar is clear.</p>")
    return ("<div class='card tablecard'>"
            f"<h2>{_card_h2('Due soon', ctx)}</h2>"
            "<p class='small'>Open work, soonest first.</p>" + body + "</div>")


def _view_recent(student, ctx) -> str:
    rows = ctx["recent"]
    if not rows:
        return ("<div class='card tablecard'>"
                f"<h2>{_card_h2('Recently graded', ctx)}</h2>"
                "<p class='small'>No grades posted yet.</p></div>")
    with_course = _show_course_col(ctx)
    ncols = 3 + with_course
    colgroup = ("<colgroup><col>"
                + ("<col class='c-course'>" if with_course else "")
                + "<col class='c-score'><col class='c-raw'></colgroup>")
    out, last = [], None
    for r in rows[:20]:
        d = date.fromisoformat(r["graded_on"][:10])
        if d != last:
            out.append(f"<tr class='dayrow'><td class='day' colspan='{ncols}'>"
                       f"{_day_heading(d, ctx['today'])}</td></tr>")
            last = d
        low = ""
        if r["points"] and r["score"] is not None:
            pval = r["score"] / r["points"] * 100
            pct, raw = f"{pval:.1f}%", f"{r['score']:g}/{r['points']:g}"
            low = _low_class(pval)
        else:
            pct = f"{r['score']:g}" if r["score"] is not None else "—"
            raw = "—"
        out.append(
            f"<tr><td>{escape(r['name'])}{_src(r)}{_twin(r)}</td>"
            + (f"<td class='small' data-label='Course'>"
               f"{escape(r['course_title'])}</td>" if with_course else "")
            + f"<td class='num{' ' + low if low else ''}' data-label='Score'>"
            f"{escape(pct)}</td>"
            f"<td class='raw' data-label='Points'>{escape(raw)}</td></tr>")
    older = ""
    if len(rows) > 20:
        agu = quote(student["agu"])
        scope = f"&course={quote(ctx['course_gu'])}" if ctx["course_gu"] else ""
        older = (f"<p class='small' style='margin:0.8rem 0 0'>"
                 f"<a href='/student/{agu}?view=everything{scope}'>"
                 f"Older grades live in Everything →</a></p>")
    return ("<div class='card tablecard'>"
            f"<h2>{_card_h2('Recently graded', ctx)}</h2>"
            "<p class='small'>Newest first"
            + (", across all courses" if with_course else "") + ".</p>"
            f"<table class='recent'>{colgroup}{''.join(out)}</table>"
            + older + "</div>")


_ASSIGN_COLGROUP = ("<colgroup><col><col class='c-type'><col class='c-due'>"
                    "<col class='c-score'><col class='c-status'></colgroup>")


def _course_card(course, rows, ctx) -> str:
    """One course in the Everything archive: open items surfaced first, the
    graded backlog collapsed to the newest five. The rest hide in a
    ``tbody.overflow`` of the SAME table with the no-JS ``details.more``
    toggle after it (history's pattern) — so expanding continues the list
    and the toggle reads "Show less" at the table's end, never mid-table."""
    head = escape(course["title"])
    pct = _pct(course["percent"]) if course["percent"] else ""
    overall = " · ".join(x for x in (pct and f"{pct}%", course["mark"]) if x)
    teacher = f" — {escape(course['teacher'])}" if course["teacher"] else ""
    header = (
        f"<h2>{head}{teacher}"
        + (f" <span class='badge muted'>{escape(overall)}</span>" if overall else "")
        + "</h2>")
    if not rows:
        return (f"<div class='card tablecard'>{header}"
                "<p class='small'>No assignments recorded.</p></div>")
    upcoming = sorted((r for r in rows if r["status"] in ("due", "not_due", "submitted")),
                      key=lambda r: (r["due_date"] or "9999", r["name"]))
    missing = sorted((r for r in rows if r["status"] == "missing"),
                     key=lambda r: r["due_date"] or "", reverse=True)
    late = sorted((r for r in rows if r["status"] == "ungraded_past_due"),
                  key=lambda r: r["due_date"] or "", reverse=True)
    graded = sorted((r for r in rows if r["status"] == "graded"),
                    key=lambda r: r["graded_on"] or r["due_date"] or "",
                    reverse=True)

    def tr(a) -> str:
        attrs, lead = _row_mark(a["status"])
        return (f"<tr{attrs}><td>{lead}{escape(a['name'])}{_src(a)}{_twin(a)}</td>"
                f"<td data-label='Type'>{escape(a['kind'] or '—')}</td>"
                f"<td data-label='Due'>{escape(a['due_date'] or '—')}</td>"
                f"<td class='num' data-label='Score'>{_score(a)}</td>"
                f"<td data-label='Status'>{_badge(a['status'])}</td></tr>")

    visible = upcoming + missing + late + graded[:5]
    table = (f"<table class='assignments' aria-label='{head}'>"
             + _ASSIGN_COLGROUP
             + "<tr class='head'><th scope='col'>Assignment</th><th scope='col'>Type</th><th scope='col'>Due</th>"
               "<th scope='col'>Score</th><th scope='col'>Status</th></tr>"
             + "".join(tr(a) for a in visible))
    more = ""
    if len(graded) > 5:
        # Course ids carry colons/spaces ("student:GU:term") — not id-safe.
        slug = "".join(ch if ch.isalnum() else "-" for ch in course["id"])
        overflow_id = f"overflow-graded-{slug}"
        table += (f"<tbody class='overflow' id='{overflow_id}'>"
                  + "".join(tr(a) for a in graded[5:]) + "</tbody>")
        more = (f"<details class='more'><summary "
                f"aria-controls='{overflow_id}'>{_CHEVRON}"
                f"<span class='when-closed'>Show all {len(graded)} graded</span>"
                f"<span class='when-open'>Show less</span>"
                f"<span class='vh'> — the remaining graded rows are added to "
                f"the table above</span></summary></details>")
    table += "</table>"
    return f"<div class='card tablecard'>{header}{table}{more}</div>"


def _closed_term(term, courses, ctx) -> str:
    """A closed marking period collapses to its finals line; the full course
    cards sit behind the <details>."""
    finals = " · ".join(
        " ".join(x for x in (escape(_short_title(c["title"])),
                             escape(_pct(c["percent"])) if c["percent"] else "",
                             escape(c["mark"] or "")) if x)
        for c, _rows in courses if c["percent"] or c["mark"])
    inner = "".join(_course_card(c, rows, ctx) for c, rows in courses)
    return (f"<h2 class='vh'>{escape(term or '(no term)')}</h2>"
            f"<details class='closedterm'><summary>{_CHEVRON}"
            f"<span><strong>{escape(term or '(no term)')}</strong>"
            + (f" <span class='small'>finals: {finals}</span>" if finals else "")
            + f"</span></summary><div class='closedbody'>{inner}</div></details>")


def _view_everything(student, ctx) -> str:
    parts = []
    multi = len(ctx["sections"]) > 1
    for term, courses in ctx["sections"]:
        if multi and term != ctx["term"]:
            parts.append(_closed_term(term, courses, ctx))
            continue
        if multi:
            label = (term or "(no term)") + (" — current" if term else "")
            parts.append(f"<h2 class='termhead'>{escape(label)}</h2>")
        parts += [_course_card(c, rows, ctx) for c, rows in courses]
    if not parts:
        parts.append("<p class='small'>No courses recorded yet.</p>")
    return "".join(parts)


def render_student(student, ctx, nav_students=()) -> str:
    """The C0 student page: stat-card view switcher, course strip (skipped
    for single-course students), then the active view's body."""
    view_body = {"problems": _view_problems, "due": _view_due,
                 "recent": _view_recent, "everything": _view_everything}
    parts = [
        f"<h1>{escape(student['name'])}</h1>",
        f"<p class='small'>{_school_link(student['school'])}</p>",
    ]
    if len(ctx["strip"]) > 1:            # the collapsed All Courses strip
        parts.append(_course_strip(student, ctx))
    parts.append(_stat_cards(student, ctx))
    parts.append(view_body[ctx["view"]](student, ctx))
    view_titles = {"problems": "Needs attention", "due": "Due soon",
                   "recent": "Recent grades", "everything": "Everything"}
    return _page(f"{student['name']} — {view_titles[ctx['view']]}",
                 "".join(parts), nav_students=nav_students,
                 path=f"/student/{student['agu']}")


def _alert_type_badge(alert_type: str) -> str:
    label, klass = _ALERT_TYPE_BADGE.get(
        alert_type, (alert_type.replace("_", " "), "muted"))
    return f"<span class='badge {klass}'>{escape(label)}</span>"


def _pager(page: int, last: int, href) -> str:
    """Previous | 1 … 5 6 7 … 27 | Next — real links (keyboard/SR reachable
    by default), aria-current on the active number, and the ends rendered as
    non-focusable spans when there's nowhere to go. Pages are full server
    loads, so a click lands at the top of the new page by construction —
    which is why the links carry no #fragment."""
    if last <= 1:
        return ""

    def end(kind: str, target: int, arrow: str, label: str, ok: bool) -> str:
        inner = (f"{arrow}<span class='lbl'>{label}</span>" if kind == "prev"
                 else f"<span class='lbl'>{label}</span>{arrow}")
        if not ok:
            return f"<span class='{kind}' aria-disabled='true'>{inner}</span>"
        return (f"<a class='{kind}' rel='{kind}' href='{href(target)}' "
                f"aria-label='{label} page'>{inner}</a>")

    items = []
    for n in _page_window(page, last):
        if n is None:
            items.append("<li class='gap' aria-hidden='true'>…</li>")
        elif n == page:
            items.append(f"<li><a aria-current='page' href='{href(n)}'>{n}</a></li>")
        else:
            items.append(f"<li><a href='{href(n)}' aria-label='Page {n}'>{n}</a></li>")
    return ("<nav class='pager' aria-label='Alert pages'>"
            + end("prev", page - 1, "←", "Previous", page > 1)
            + "<ol>" + "".join(items) + "</ol>"
            + end("next", page + 1, "→", "Next", page < last)
            + "</nav>")


def render_alerts(alerts, counts=(), nav_students=(),
                  page: int = 1, alert_type: str = "", total: int = 0,
                  today: date | None = None) -> str:
    if not counts:
        body = "<h1>Alerts</h1><p>No alerts yet — quiet is good.</p>"
        return _page("Alerts", body, nav_students=nav_students, path="/alerts")
    import json as _json

    today = today or date.today()
    last = alerts_last_page(total)

    def href(t: str, p: int = 1) -> str:
        q = ([f"type={quote(t)}"] if t else []) + ([f"page={p}"] if p > 1 else [])
        return "/alerts" + ("?" + "&".join(q) if q else "")

    # Type-group chips: one door per alert type present, with counts. The
    # active chip marks the filter; "all" clears it.
    all_n = sum(c["n"] for c in counts)   # every type; `total` is the filter's
    chips = [f"<a class='chip{'' if alert_type else ' active'}'"
             f"{'' if alert_type else _CURRENT} "
             f"href='/alerts'>all <b>{all_n}</b></a>"]
    chips += [
        f"<a class='chip{' active' if c['type'] == alert_type else ''}'"
        f"{_CURRENT if c['type'] == alert_type else ''} "
        f"href='{href(c['type'])}'>"
        f"{escape(c['type'].replace('_', ' '))} <b>{c['n']}</b></a>"
        for c in counts]

    rows = []
    for al in alerts:
        try:
            detail = _json.loads(al["body"]).get("detail", al["body"])
        except Exception:
            detail = al["body"]
        rows.append(
            f"<tr>"
            f"<td>{escape(detail)}</td>"
            f"<td class='small' data-label='When'>"
            f"{_when_html(al['created_at'], today)}</td>"
            f"<td data-label='Student'>{escape(al['student_name'])}</td>"
            f"<td data-label='Type'>{_alert_type_badge(al['type'])}</td></tr>")
    table = ("<table class='alerts'><tr class='head'><th scope='col'>Detail</th><th scope='col'>When</th>"
             "<th scope='col'>Student</th><th scope='col'>Type</th></tr>"
             + "".join(rows) + "</table>"
             if rows else "<p class='small'>Nothing on this page.</p>")

    # "Showing 51–100 of 1,305 alerts" sets expectations before the pager;
    # a filter names itself so the total reads as that type's count.
    first = (page - 1) * _ALERTS_PAGE + 1 if total else 0
    span = (f"{first:,}–{min(page * _ALERTS_PAGE, total):,} of {total:,}"
            if total > _ALERTS_PAGE else f"all {total:,}")
    what = f"{escape(alert_type.replace('_', ' '))} alerts" if alert_type else "alerts"
    range_line = f"<p class='pager-range'>Showing {span} {what}</p>"
    pager = _pager(page, last, lambda p: href(alert_type, p))

    heading = "Recent alerts" if page == 1 else f"Alerts — page {page}"
    # The tab title carries the active filter and page — distinguishable
    # history entries, and screen readers announce where a click landed.
    title = (f"Alerts — {alert_type.replace('_', ' ')}" if alert_type
             else "Alerts")
    if last > 1:
        title += f" — page {page} of {last}"
    return _page(title, "<h1>Alerts</h1><div class='card tablecard'>"
                 f"<h2>{heading}</h2>"
                 f"<div class='chips'>{''.join(chips)}</div>"
                 + table + range_line + pager + "</div>",
                 nav_students=nav_students, path="/alerts")


_HISTORY_PREVIEW = 8   # recent rows shown per section; the rest go behind "Show all"

# The `field` column is a raw column name; humanize the ones that read as jargon.
_FIELD_LABELS = {
    "due_date": "due date",
    "graded_at": "graded on",
    "kind": "assignment type",
    "percent": "grade %",
    "mark": "letter grade",
}


def _field_label(field: str) -> str:
    return _FIELD_LABELS.get(field, field.replace("_", " "))


def _history_transition(r) -> str:
    """A field's old → new for the history tables. Status values read as
    the badge words ("due → ungraded past due"), not the enum names."""
    words = _STATUS_LABELS if r["field"] == "status" else {}
    old = r["old_value"] if r["old_value"] is not None else "—"
    new = r["new_value"] if r["new_value"] is not None else "—"
    return f"{escape(words.get(old, (old,))[0])} → {escape(words.get(new, (new,))[0])}"


def _num(value) -> str:
    """A stored float ("10.0") as people write it ("10"); anything else verbatim."""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def _score_words(score, points) -> str:
    if score is None:
        return "—"
    return f"{_num(score)}/{_num(points)}" if points is not None else _num(score)


# Rows one poll writes about one assignment land within this many seconds
# of each other (grade_history has no poll id; seen_at is per statement).
_HISTORY_GROUP_WINDOW = timedelta(seconds=5)


def _group_history(rows) -> list[dict]:
    """One display row per event, not per audited field. The store writes a
    grade as three rows (status → graded, score, points); a reader wants
    one line saying "graded — → 8/10". Grouping is render-time only: the
    audit trail stays field-level, and the chips still count fields.

    Rows are newest first. Each item: r (the newest row of the group, for
    the name/student/course cells), seen_at, id, label, transition."""
    by_assignment: dict[str, list] = {}
    for r in rows:
        by_assignment.setdefault(r["assignment_id"], []).append(r)

    def when(r):
        try:
            return datetime.fromisoformat(r["seen_at"])
        except (TypeError, ValueError):
            return None

    out: list[dict] = []
    for arows in by_assignment.values():
        # The denominator in force at each group: the old side of the next
        # later points row, else the assignment's points today. Lets a
        # score-only change (or a ?field=score view) still read as 8/10.
        points_after = arows[0]["cur_points"]
        i = 0
        while i < len(arows):
            t0 = when(arows[i])
            j = i + 1
            while (j < len(arows) and t0 is not None and (t1 := when(arows[j]))
                   is not None and t0 - t1 <= _HISTORY_GROUP_WINDOW):
                j += 1
            group = arows[i:j]
            i = j
            out.extend(_compose_history_group(group, points_after))
            for g in group:
                if g["field"] == "points":
                    points_after = g["old_value"]
    out.sort(key=lambda d: (d["seen_at"], d["id"]), reverse=True)
    return out


def _compose_history_group(group, points_after) -> list[dict]:
    def item(r, label, transition):
        return {"r": r, "seen_at": r["seen_at"], "id": max(g["id"] for g in group),
                "label": label, "transition": transition}

    fields = {g["field"]: g for g in group}    # one row per field per poll
    score, points, status = (fields.get(k) for k in ("score", "points", "status"))
    # A graded-on date arriving with the score is the same event (its When
    # column says when); the date changing by itself stays its own row.
    folded = {"score", "points", "status"}
    if score is not None and fields.get("graded_at") is not None:
        folded.add("graded_at")
    out = [item(g, _field_label(g["field"]), _history_transition(g))
           for g in group if g["field"] not in folded]
    if score is not None:
        old_pts = points["old_value"] if points else points_after
        new_pts = points["new_value"] if points else points_after
        label = ("graded" if score["old_value"] is None and score["new_value"] is not None
                 else "score")
        out.append(item(score, label,
                        f"{escape(_score_words(score['old_value'], old_pts))} → "
                        f"{escape(_score_words(score['new_value'], new_pts))}"))
        # The status flip to graded is the same event; any other status
        # change alongside a score (say, to missing) is its own news.
        if status is not None and status["new_value"] != "graded":
            out.append(item(status, "status", _history_transition(status)))
    else:
        if points is not None:
            out.append(item(points, "points",
                            f"{escape(_num(points['old_value']) if points['old_value'] is not None else '—')} → "
                            f"{escape(_num(points['new_value']) if points['new_value'] is not None else '—')}"))
        if status is not None:
            out.append(item(status, "status", _history_transition(status)))
    return out


def render_history(rows, course_rows=(), class_counts=(), field_counts=(),
                   *, course="", field="", nav_students=(),
                   totals: tuple[int, int] | None = None,
                   show_all: bool = False) -> str:
    """``totals`` is (assignment rows, course rows) the filter covers; when a
    section's fetched rows fall short of it, the section ends with a link
    to the uncapped page (``?all=1``) after the in-page expander."""
    if not class_counts and not field_counts:
        return _page("History", "<h1>Grade history</h1><p>No changes recorded yet.</p>",
                     nav_students=nav_students, path="/history")
    today = date.today()

    def href(c: str, f: str, all_rows: bool = False) -> str:
        q = (([f"course={quote(c)}"] if c else [])
             + ([f"field={quote(f)}"] if f else [])
             + (["all=1"] if all_rows else []))
        return "/history" + ("?" + "&".join(q) if q else "")

    def class_href(r, view: str = "") -> str:
        """The student page scoped to the row's class — as deep as a link
        can safely land today (owner's call 2026-09-05: no per-row anchors)."""
        q = ([f"view={view}"] if view else []) + [f"course={quote(r['course_gu'])}"]
        return f"/student/{escape(r['student_agu'])}?" + "&".join(q)

    def chip_row(label, items, selected, href_of):
        """One filter dimension. items: (key, display, count); href_of(key)
        composes the URL preserving the other dimension (href_of('') = all)."""
        total = sum(n for _, _, n in items)
        out = [f"<a class='chip{' active' if not selected else ''}'"
               f"{'' if selected else _CURRENT} "
               f"href='{href_of('')}'>all <b>{total}</b></a>"]
        out += [
            f"<a class='chip{' active' if key == selected else ''}'"
            f"{_CURRENT if key == selected else ''} "
            f"href='{href_of(key)}'>{escape(disp)} <b>{n}</b></a>"
            for key, disp, n in items]
        return (f"<div class='filterrow'><span class='filterlabel'>{label}</span>"
                f"<div class='chips'>{''.join(out)}</div></div>")

    filters = ""
    if class_counts:
        items = [(c["course_title"], c["course_title"], c["n"]) for c in class_counts]
        filters += chip_row("Class", items, course, lambda v: href(v, field))
    if field_counts:
        items = [(c["field"], _field_label(c["field"]), c["n"]) for c in field_counts]
        filters += chip_row("Change", items, field, lambda v: href(course, v))

    def section(title, head_html, body_rows, total=None):
        """A history section, compact: the recent rows shown, the rest in a
        hidden <tbody class='overflow'> of the SAME table so expanding just
        continues the list — one header, aligned columns. The <details> below
        is the bare no-JS toggle; style.css reveals the tbody via :has().
        ``total`` above the rows fetched means the page is capped: the
        expander shows what's here and a link offers the rest."""
        n = len(body_rows)
        total = n if total is None else max(total, n)
        slug = title.lower().replace(" ", "-")
        table = (f"<table class='history' aria-label='{escape(title)}'>" + head_html
                 + "".join(body_rows[:_HISTORY_PREVIEW]))
        more = ""
        if n > _HISTORY_PREVIEW:
            table += (f"<tbody class='overflow' id='overflow-{slug}'>"
                      + "".join(body_rows[_HISTORY_PREVIEW:]) + "</tbody>")
            more = (f"<details class='more'><summary "
                    f"aria-controls='overflow-{slug}'>{_CHEVRON}"
                    f"<span class='when-closed'>Show all {n}</span>"
                    f"<span class='when-open'>Show less</span>"
                    f"<span class='vh'> — "
                    f"the remaining rows are added to the table "
                    f"above</span></summary></details>")
        table += "</table>"
        if total > n:
            more += (f"<p class='small' style='margin:0.8rem 0 0'>Newest {n:,} rows of "
                     f"{total:,} changes — <a href='{href(course, field, True)}'>"
                     f"show all {total:,}</a></p>")
        return (f"<div class='card tablecard'><h2>{escape(title)} "
                f"<span class='small'>{total}</span></h2>" + table + more + "</div>")

    t_assign, t_course = totals or (None, None)
    sections = []
    if course_rows:
        head = ("<tr class='head'><th scope='col'>Course</th><th scope='col'>When</th><th scope='col'>Student</th>"
                "<th scope='col'>Change</th><th scope='col'>From → To</th></tr>")
        body = [
            f"<tr><td><a href='{class_href(r, 'everything')}'>{escape(r['course_title'])}</a> "
            f"<span class='small'>{escape(r['term'])}</span></td>"
            f"<td class='small' data-label='When'>{_when_html(r['seen_at'], today)}</td>"
            f"<td data-label='Student'>{escape(r['student_name'])}</td>"
            f"<td data-label='Change'>{escape(_field_label(r['field']))}</td>"
            f"<td data-label='From → To'>{_history_transition(r)}</td></tr>"
            for r in course_rows]
        sections.append(section("Course grades", head, body, t_course))
    if rows:
        head = ("<tr class='head'><th scope='col'>Assignment</th><th scope='col'>When</th><th scope='col'>Student</th>"
                "<th scope='col'>Course</th><th scope='col'>Change</th><th scope='col'>From → To</th></tr>")
        grouped = _group_history(rows)
        body = [
            f"<tr><td><a href='{class_href(d['r'], 'everything')}'>"
            f"{escape(d['r']['assignment_name'])}</a></td>"
            f"<td class='small' data-label='When'>{_when_html(d['seen_at'], today)}</td>"
            f"<td data-label='Student'>{escape(d['r']['student_name'])}</td>"
            f"<td data-label='Course'>{escape(d['r']['course_title'])}</td>"
            f"<td data-label='Change'>{escape(d['label'])}</td>"
            f"<td data-label='From → To'>{d['transition']}</td></tr>"
            for d in grouped]
        # The heading counts what the reader sees. Uncapped, that is the
        # grouped rows; capped, the true total is still field rows, and
        # the "show all" line says so.
        if t_assign is not None and t_assign <= len(rows):
            t_assign = len(grouped)
        sections.append(section("Assignments", head, body, t_assign))

    body_html = f"<div class='histfilters'>{filters}</div>"
    body_html += ("".join(sections) if sections
                  else "<p class='small'>No changes match this filter.</p>")
    return _page("History", "<h1>Grade history</h1>" + body_html,
                 nav_students=nav_students, path="/history")


def _page_window(page: int, last: int, *, edge: int = 1,
                 around: int = 1) -> list:
    """Which page numbers a pager shows: the first and last ``edge`` pages,
    ``around`` on each side of the current one, and ``None`` wherever a run
    is elided — e.g. page 6 of 27 → [1, None, 5, 6, 7, None, 27]. Short
    ranges (nothing to elide) list every page."""
    keep = set(range(1, edge + 1)) | set(range(last - edge + 1, last + 1))
    keep |= set(range(page - around, page + around + 1))
    out: list = []
    for n in range(1, last + 1):
        if n in keep:
            # A gap of exactly one page is shown, not elided — "…" would be
            # longer than the number it hides.
            if out and out[-1] is not None and n - out[-1] == 2:
                out.append(n - 1)
            elif out and out[-1] is not None and n - out[-1] > 2:
                out.append(None)
            out.append(n)
    return out


