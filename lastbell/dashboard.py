"""Web dashboard (Phase 3).

The dashboard is for *looking things up on demand* — alerts are always pushed
out, so nobody has to open this to find out something changed. It's stdlib
``http.server`` over the same SQLite file the watch loop writes: no framework,
no build step. Pages are SELECTs; the only write paths are the
watcher/subscription forms on /settings (``POST /settings/<action>``) —
household bookkeeping only, never grade data.

It binds 127.0.0.1 by default. To share it on your LAN set
LASTBELL_DASHBOARD_HOST=0.0.0.0 — and know that unlike alert payloads it
shows full names (and, on /settings, watcher addresses), so treat the bind
address as the access control; the write paths carry no auth of their own.
"""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import schools

_STATUS_LABELS = {
    "graded": ("graded", "ok"),
    "not_due": ("not due yet", "muted"),
    "due": ("due", "info"),
    "missing": ("MISSING", "bad"),
    "ungraded_past_due": ("ungraded past due", "warn"),
}

# Phase C row signal: statuses that earn a tint + leading icon, so a mixed
# table scans by color before it's read. Escalation ladder: due soon is a
# light caution, ungraded-past-due a stronger one, missing is red. The icons
# are feather-style paths (rendered via _SVG below); each colors through the
# same token its badge uses.
_STATUS_ROWS = {
    "missing": ("st-missing", "var(--bad)",
                "<circle cx='12' cy='12' r='10'/>"
                "<line x1='12' y1='8' x2='12' y2='12'/>"
                "<line x1='12' y1='16' x2='12.01' y2='16'/>"),
    "ungraded_past_due": (
        "st-late", "var(--warn)",
        "<path d='M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0"
        " 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z'/>"
        "<line x1='12' y1='9' x2='12' y2='13'/>"
        "<line x1='12' y1='17' x2='12.01' y2='17'/>"),
    "due": ("st-due", "var(--warn)",
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
_STYLE_PATH = Path(__file__).with_name("style.css")
_APPJS_PATH = Path(__file__).with_name("app.js")


# ── data (all read-only) ──────────────────────────────────────────────


def fetch_students(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM students ORDER BY name").fetchall()


def fetch_student(conn: sqlite3.Connection, agu: str):
    return conn.execute("SELECT * FROM students WHERE agu = ?", (agu,)).fetchone()


def fetch_courses(conn: sqlite3.Connection, student_id: str,
                  term: str = "") -> list[sqlite3.Row]:
    """Courses for a student — all terms, or one term when given."""
    if term:
        return conn.execute(
            "SELECT * FROM courses WHERE student_id = ? AND term = ? ORDER BY title",
            (student_id, term)).fetchall()
    return conn.execute(
        "SELECT * FROM courses WHERE student_id = ? ORDER BY title", (student_id,)
    ).fetchall()


def fetch_open_counts(conn: sqlite3.Connection, student_id: str,
                      term: str = "") -> dict:
    """Open-issue badge counts, scoped to one term when given — a closed
    quarter's leftover 'due' rows shouldn't haunt the overview forever."""
    sql = ("SELECT SUM(a.status = 'missing') AS missing, "
           "       SUM(a.status = 'ungraded_past_due') AS past_due, "
           "       SUM(a.status = 'due') AS due "
           "FROM assignments a JOIN courses c ON c.id = a.course_id "
           "WHERE c.student_id = ?")
    params: tuple = (student_id,)
    if term:
        sql += " AND c.term = ?"
        params += (term,)
    row = conn.execute(sql, params).fetchone()
    return {k: row[k] or 0 for k in ("missing", "past_due", "due")}


# ── student page data (C0: four views, stat cards, course strip) ──────

_VIEWS = ("problems", "due", "recent", "everything")

# What the Problems view (and its trend sparkline) counts.
_PROBLEM_STATUSES = ("missing", "ungraded_past_due")


def fetch_strip_rows(conn: sqlite3.Connection, student_id: str,
                     term: str = "") -> list[sqlite3.Row]:
    """Course-strip rows: each course with its open-issue counts and the date
    a grade last landed. ``graded_at`` when the source supplied it (the demo
    seeder does), else the day a score first appeared in grade_history — the
    live collector never fills graded_at."""
    sql = ("SELECT c.*, "
           "  SUM(a.status = 'missing') AS missing, "
           "  SUM(a.status = 'ungraded_past_due') AS past_due, "
           "  SUM(a.status = 'due') AS due, "
           "  MAX(CASE WHEN a.status = 'graded' THEN COALESCE(a.graded_at, "
           "      (SELECT substr(MIN(h.seen_at), 1, 10) FROM grade_history h "
           "       WHERE h.assignment_id = a.id AND h.field = 'score')) END) "
           "  AS last_graded "
           "FROM courses c LEFT JOIN assignments a ON a.course_id = c.id "
           "WHERE c.student_id = ?")
    params: list = [student_id]
    if term:
        sql += " AND c.term = ?"
        params.append(term)
    return conn.execute(sql + " GROUP BY c.id ORDER BY c.title", params).fetchall()


def fetch_view_rows(conn: sqlite3.Connection, student_id: str,
                    term: str = "") -> list[sqlite3.Row]:
    """Assignments joined to their course, for the student-page views.
    ``graded_on`` is the display date a grade landed (same fallback rule as
    the strip's last_graded)."""
    sql = ("SELECT a.*, c.title AS course_title, c.edupoint_gu AS course_gu, "
           "  c.term AS course_term, "
           "  COALESCE(a.graded_at, (SELECT substr(MIN(h.seen_at), 1, 10) "
           "    FROM grade_history h WHERE h.assignment_id = a.id "
           "    AND h.field = 'score')) AS graded_on "
           "FROM assignments a JOIN courses c ON c.id = a.course_id "
           "WHERE c.student_id = ?")
    params: list = [student_id]
    if term:
        sql += " AND c.term = ?"
        params.append(term)
    return conn.execute(sql, params).fetchall()


def _fetch_change_rows(conn, table: str, key: str, join: str, student_id: str,
                       term: str, field: str) -> dict[str, list]:
    """id -> ascending [(day, old, new), …] from one of the history tables."""
    sql = (f"SELECT h.{key} AS k, substr(h.seen_at, 1, 10) AS d, "
           f"  h.old_value, h.new_value FROM {table} h {join} "
           f"WHERE c.student_id = ? AND h.field = ?")
    params: list = [student_id, field]
    if term:
        sql += " AND c.term = ?"
        params.append(term)
    out: dict[str, list] = {}
    for r in conn.execute(sql + " ORDER BY h.seen_at, h.id", params):
        out.setdefault(r["k"], []).append((r["d"], r["old_value"], r["new_value"]))
    return out


def fetch_percent_history(conn, student_id: str, term: str = "") -> dict[str, list]:
    """course_id -> the course's percent changes, ascending."""
    return _fetch_change_rows(
        conn, "course_history", "course_id",
        "JOIN courses c ON c.id = h.course_id", student_id, term, "percent")


def fetch_status_history(conn, student_id: str, term: str = "") -> dict[str, list]:
    """assignment_id -> the assignment's status transitions, ascending."""
    return _fetch_change_rows(
        conn, "grade_history", "assignment_id",
        "JOIN assignments a ON a.id = h.assignment_id "
        "JOIN courses c ON c.id = a.course_id", student_id, term, "status")


def _value_at(rows: list, day: str):
    """The value in effect at end of ``day``, given ascending change rows.
    Before the first recorded change, the value is that change's old_value;
    empty rows mean "no changes ever" and the caller falls back to the
    current value."""
    value = None
    for d, old, new in rows:
        if d <= day:
            value = new
        else:
            if value is None:
                value = old
            break
    return value


def _problem_series(rows, transitions: dict[str, list],
                    days: list[str]) -> list[int]:
    """How many assignments sat in a problem status on each sample day,
    reconstructed from grade_history status transitions. Assignments not yet
    assigned on a day don't count (best available proxy for existence)."""
    counts = []
    for day in days:
        n = 0
        for r in rows:
            if r["assigned"] and r["assigned"] > day:
                continue
            trans = transitions.get(r["id"], ())
            status = r["status"]
            if trans:
                status = _value_at(trans, day) or status
            n += status in _PROBLEM_STATUSES
        counts.append(n)
    return counts


def build_student_ctx(conn: sqlite3.Connection, student, view: str,
                      course_gu: str, hl: str = "",
                      today: date | None = None,
                      strip_open: bool = False) -> dict:
    """Everything render_student needs: the strip, the four stat cards' data
    stories, and the active view's rows (scoped to ?course= when given).
    ``hl`` is the ?status= highlight from an overview badge click-through;
    ``strip_open`` (?strip=open) keeps All Courses expanded on a page that
    would otherwise collapse it — the clear-filter link carries it."""
    from .models import parse_percent

    today = today or date.today()
    view = view if view in _VIEWS else "problems"
    hl = hl if hl in _STATUS_ROWS else ""
    sid, term = student["id"], student["current_term"] or ""
    strip = fetch_strip_rows(conn, sid, term)
    if course_gu and course_gu not in {
            c["edupoint_gu"] for c in fetch_courses(conn, sid)}:
        course_gu = ""

    rows = fetch_view_rows(conn, sid, term)
    problems = sorted(
        (r for r in rows if r["status"] in _PROBLEM_STATUSES),
        key=lambda r: (r["status"] != "missing", r["due_date"] or "9999",
                       r["name"]))
    due = sorted((r for r in rows if r["status"] == "due"),
                 key=lambda r: (r["due_date"] or "9999", r["name"]))
    graded = sorted(
        (r for r in rows if r["status"] == "graded" and r["graded_on"]),
        key=lambda r: r["graded_on"], reverse=True)

    # Course strip: 2-week percent deltas from course_history.
    phist = fetch_percent_history(conn, sid, term)
    cutoff = (today - timedelta(days=14)).isoformat()
    deltas: dict[str, tuple] = {}
    percents = []
    for c in strip:
        cur = parse_percent(c["percent"])
        hrows = phist.get(c["id"], [])
        base = parse_percent(_value_at(hrows, cutoff)) if hrows else cur
        deltas[c["id"]] = (cur, base if base is not None else cur)
        if cur is not None:
            percents.append(cur)
    term_avg = sum(percents) / len(percents) if percents else None

    # Problems card: 6-week open-problem trend (weekly samples).
    sample = [(today - timedelta(days=7 * i)).isoformat()
              for i in range(5, -1, -1)]
    pseries = _problem_series(rows, fetch_status_history(conn, sid, term),
                              sample)

    # Everything card: the term-average trajectory (8 weekly samples).
    tseries = []
    for day in [(today - timedelta(days=7 * i)).isoformat()
                for i in range(7, -1, -1)]:
        vals = []
        for c in strip:
            hrows = phist.get(c["id"], [])
            v = (parse_percent(_value_at(hrows, day)) if hrows
                 else parse_percent(c["percent"]))
            if v is not None:
                vals.append(v)
        if vals:
            tseries.append(sum(vals) / len(vals))

    # Recent card: the last 10 graded scores — the leading indicator.
    pcts10 = [r["score"] / r["points"] * 100
              for r in graded if r["points"] and r["score"] is not None][:10]

    def scoped(items):
        return [r for r in items
                if not course_gu or r["course_gu"] == course_gu]

    ctx = {
        "view": view, "course_gu": course_gu, "hl": hl,
        "strip_open": strip_open, "today": today, "term": term,
        "strip": strip, "deltas": deltas,
        "problems": scoped(problems), "due": scoped(due),
        "recent": scoped(graded),
        "cards": {
            "problems_count": len(problems),
            "problems_week": pseries[-1] - pseries[-2],
            "problems_series": pseries,
            "due_count": len(due), "due_next": due[:2],
            "recent_pcts": pcts10, "term_avg": term_avg,
            "term_series": tseries, "courses": len(strip),
        },
    }
    if view == "everything":
        all_rows = fetch_view_rows(conn, sid)
        by_course: dict[str, list] = {}
        for r in all_rows:
            by_course.setdefault(r["course_id"], []).append(r)
        courses = [c for c in fetch_courses(conn, sid)
                   if not course_gu or c["edupoint_gu"] == course_gu]
        terms: list[str] = []
        for c in courses:
            if c["term"] not in terms:
                terms.append(c["term"])
        ordered = ([term] if term in terms else []) + sorted(
            (t for t in terms if t != term), reverse=True)
        ctx["sections"] = [
            (t, [(c, by_course.get(c["id"], [])) for c in courses
                 if c["term"] == t])
            for t in ordered]
    return ctx


# Alerts page size ("older →" paging replaces the old silent 100-row cap).
_ALERTS_PAGE = 50


def fetch_alerts(conn: sqlite3.Connection, page: int = 1,
                 alert_type: str = "") -> tuple[list[sqlite3.Row], bool]:
    """One page of alerts, newest first, plus whether an older page exists.
    Offset paging is safe here because the sort key is stable between
    requests."""
    sql = ("SELECT al.*, st.name AS student_name "
           "FROM alerts al "
           "JOIN students st ON st.id = al.student_id ")
    params: list = []
    if alert_type:
        sql += "WHERE al.type = ? "
        params.append(alert_type)
    sql += "ORDER BY al.created_at DESC, al.rowid DESC LIMIT ? OFFSET ?"
    params += [_ALERTS_PAGE + 1, (page - 1) * _ALERTS_PAGE]
    rows = conn.execute(sql, params).fetchall()
    return rows[:_ALERTS_PAGE], len(rows) > _ALERTS_PAGE


def fetch_alert_counts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """(type, n) per alert type present — the type-group chips."""
    return conn.execute(
        "SELECT type, COUNT(*) AS n "
        "FROM alerts GROUP BY type ORDER BY n DESC, type").fetchall()


def _history_filter(course: str, field: str) -> tuple[str, list]:
    """The shared WHERE for both history tables: filter by class (course
    title) and/or change kind (the `field` column). Either may be empty."""
    where, params = [], []
    if course:
        where.append("c.title = ?")
        params.append(course)
    if field:
        where.append("h.field = ?")
        params.append(field)
    clause = ("WHERE " + " AND ".join(where) + " ") if where else ""
    return clause, params


# A high safety cap, not a display limit: history is shown a few recent rows at
# a time with the rest behind a "Show all N" expander, and the filter chips
# count the true totals — so the fetch must reach every row that a filter (or an
# expander) could surface. Comfortably covers a household's multi-year log.
_HISTORY_LIMIT = 5000


def fetch_course_history(conn: sqlite3.Connection, limit: int = _HISTORY_LIMIT, *,
                         course: str = "", field: str = "") -> list[sqlite3.Row]:
    clause, params = _history_filter(course, field)
    return conn.execute(
        "SELECT h.*, c.title AS course_title, c.term, st.name AS student_name "
        "FROM course_history h "
        "JOIN courses c ON c.id = h.course_id "
        "JOIN students st ON st.id = c.student_id " + clause
        + "ORDER BY h.seen_at DESC, h.id DESC LIMIT ?", (*params, limit)
    ).fetchall()


def fetch_history(conn: sqlite3.Connection, limit: int = _HISTORY_LIMIT, *,
                  course: str = "", field: str = "") -> list[sqlite3.Row]:
    clause, params = _history_filter(course, field)
    return conn.execute(
        "SELECT h.*, a.name AS assignment_name, c.title AS course_title, "
        "       st.name AS student_name "
        "FROM grade_history h "
        "JOIN assignments a ON a.id = h.assignment_id "
        "JOIN courses c ON c.id = a.course_id "
        "JOIN students st ON st.id = c.student_id " + clause
        + "ORDER BY h.seen_at DESC, h.id DESC LIMIT ?", (*params, limit)
    ).fetchall()


def fetch_history_class_counts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """(course_title, n) across both history tables — the class-filter chips."""
    return conn.execute(
        "SELECT course_title, COUNT(*) AS n FROM ("
        "  SELECT c.title AS course_title FROM grade_history h"
        "    JOIN assignments a ON a.id = h.assignment_id"
        "    JOIN courses c ON c.id = a.course_id"
        "  UNION ALL"
        "  SELECT c.title AS course_title FROM course_history h"
        "    JOIN courses c ON c.id = h.course_id"
        ") GROUP BY course_title ORDER BY n DESC, course_title").fetchall()


def fetch_history_field_counts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """(field, n) across both history tables — the change-kind chips."""
    return conn.execute(
        "SELECT field, COUNT(*) AS n FROM ("
        "  SELECT field FROM grade_history"
        "  UNION ALL"
        "  SELECT field FROM course_history"
        ") GROUP BY field ORDER BY n DESC, field").fetchall()


# ── rendering (pure: rows in, html out) ───────────────────────────────


# Theme toggle: cycles auto → light → dark, saved per browser. Icon-only,
# gear-sized (auto = half-filled circle, light = sun, dark = moon); the
# state rides the title/aria-label. The first statement runs before paint
# so a saved choice never flashes the wrong theme.
_THEME_SVG_OPEN = ("<svg viewBox='0 0 24 24' fill='none' stroke='currentColor'"
                   " stroke-width='2' stroke-linecap='round'"
                   " stroke-linejoin='round' aria-hidden='true'>")
_THEME_ICON_AUTO = (_THEME_SVG_OPEN + "<circle cx='12' cy='12' r='10'/>"
                    "<path d='M12 2a10 10 0 0 0 0 20z' fill='currentColor' "
                    "stroke='none'/></svg>")
_THEME_JS = """
(function(){
  var KEY='lastbell-theme', root=document.documentElement, choice=null;
  var OPEN = "%s";
  var ICON = {
    auto: OPEN + "<circle cx='12' cy='12' r='10'/>"
        + "<path d='M12 2a10 10 0 0 0 0 20z' fill='currentColor' stroke='none'/></svg>",
    light: OPEN + "<circle cx='12' cy='12' r='5'/>"
        + "<line x1='12' y1='1' x2='12' y2='3'/><line x1='12' y1='21' x2='12' y2='23'/>"
        + "<line x1='4.22' y1='4.22' x2='5.64' y2='5.64'/>"
        + "<line x1='18.36' y1='18.36' x2='19.78' y2='19.78'/>"
        + "<line x1='1' y1='12' x2='3' y2='12'/><line x1='21' y1='12' x2='23' y2='12'/>"
        + "<line x1='4.22' y1='19.78' x2='5.64' y2='18.36'/>"
        + "<line x1='18.36' y1='5.64' x2='19.78' y2='4.22'/></svg>",
    dark: OPEN + "<path d='M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z'/></svg>"
  };
  try { choice = localStorage.getItem(KEY); } catch (e) {}
  function apply(){
    if (choice==='light'||choice==='dark') root.setAttribute('data-theme', choice);
    else root.removeAttribute('data-theme');
    var b=document.getElementById('themetoggle');
    if (!b) return;
    var state = choice || 'auto';
    b.innerHTML = ICON[state];
    b.setAttribute('data-tip', 'Theme: '
                   + (choice || 'auto (follows your system)'));
    b.setAttribute('aria-label', 'Theme: ' + state);
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
""" % _THEME_SVG_OPEN.replace('"', '\\"')


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


def _nav_students(students) -> str:
    """Student links for the nav: names inline at desktop width; on narrow
    they give way to a <details> menu behind the students icon (no JS to
    open — app.js only adds outside-click close)."""
    if not students:
        return ""
    names = _nav_names(students)
    inline = "".join(
        # The tooltip earns its place only when the nav abbreviates: it reveals
        # the full name behind a first name. When the shown name already IS the
        # full name, a tooltip would just echo it, so render a plain link.
        (f"<a class='tip-b' href='/student/{escape(s['agu'])}' "
         f"data-tip='{escape(s['name'])}'>{escape(n)}</a>"
         if n != s["name"] else
         f"<a href='/student/{escape(s['agu'])}'>{escape(n)}</a>")
        for s, n in zip(students, names))
    menu = "".join(
        f"<a href='/student/{escape(s['agu'])}'>{escape(s['name'])}</a>"
        for s in students)
    return (f"<span class='navstudents'>{inline}</span>"
            f"<details class='smenu'><summary "
            f"aria-label='Students'>{_SVG.format(_STUDENTS_ICON)}</summary>"
            f"<div class='smenu-list'>{menu}</div></details>")


def _page(title: str, body: str, nav_students=()) -> str:
    links = "".join(
        f"<a href='{href}' aria-label='{label}'>{_SVG.format(icon)}"
        f"<span class='lbl'>{label}</span></a>"
        for href, label, icon in _NAV_ITEMS)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{escape(title)} · Last Bell</title>"
        "<link rel='stylesheet' href='/static/style.css'>"
        f"<script>{_THEME_JS}</script>"
        "<script src='/static/app.js' defer></script></head><body>"
        f"<nav><a class='brand' href='/'>Last Bell</a>"
        f"{_nav_students(nav_students)}{links}"
        f"<a class='gear' href='/settings' "
        f"aria-label='Settings'>{_GEAR_ICON}</a>"
        f"<button id='themetoggle' class='tip-b tip-e' "
        f"aria-label='Theme'>{_THEME_ICON_AUTO}</button></nav>"
        f"{body}</body></html>"
    )


def _badge(status: str) -> str:
    label, klass = _STATUS_LABELS.get(status, (status, "muted"))
    return f"<span class='badge {klass}'>{escape(label)}</span>"


def _tip(inner_html: str, tip: str, extra_class: str = "") -> str:
    """Wrap already-escaped html in a design-system tooltip (CSS-only: the
    bubble is a ::after reading data-tip — never the browser's native
    ``title`` speck)."""
    cls = f"tip {extra_class}".strip()
    return f"<span class='{cls}' data-tip='{escape(tip)}'>{inner_html}</span>"


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
    from .models import format_percent

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


def render_overview(students, courses_by_student, counts_by_student) -> str:
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
            f"<div class='card'><h3><a href='/student/{escape(s['agu'])}'>"
            f"{escape(s['name'])}</a></h3>"
            f"<div class='small'>{_school_link(s['school'])}</div>"
            f"<div>{' '.join(flags) or '<span class=small>all clear</span>'}</div>"
            f"<table class='courses'><tr class='head'><th>Course</th><th>Teacher</th>"
            f"<th>%</th><th>Mark</th></tr>{rows}</table></div>")
    return _page("Students", "<h1>Students</h1><div class='cards'>" + "".join(cards) + "</div>",
                 nav_students=students)


# ── student page (C0): stat cards, course strip, four views ───────────


_CHECK_BIG = ("<svg viewBox='0 0 24 24' fill='none' style='stroke:var(--accent)' "
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


def _spark_line(values, color: str, *, lo=None, hi=None) -> str:
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
    return (f"<svg class='spark' viewBox='0 0 120 34' "
            f"preserveAspectRatio='none' aria-hidden='true'>"
            f"<polyline points='{pts}' fill='none' style='stroke:{color}' "
            f"stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/>"
            f"<circle cx='116' cy='{ys[-1]:.1f}' r='3' style='fill:{color}'/>"
            f"</svg>")


def _spark_bars(pcts) -> str:
    """Up to ten bottom-aligned score micro-bars (50–100% sets the height)."""
    rects = []
    for i, p in enumerate(pcts[:10]):
        h = 4 + (min(max(p, 50.0), 100.0) - 50.0) / 50.0 * 26
        rects.append(f"<rect x='{4 + i * 12}' y='{34 - h:.1f}' width='6' "
                     f"height='{h:.1f}' rx='2' style='fill:var(--accent)'/>")
    return ("<svg class='spark' viewBox='0 0 120 34' "
            "preserveAspectRatio='none' aria-hidden='true'>"
            + "".join(rects) + "</svg>")


def _delta_html(cur, base) -> str:
    if cur is None or base is None or abs(cur - base) < 0.05:
        return "<span class='delta flat'>—</span>"
    cls = "up" if cur > base else "down"
    return (f"<span class='delta {cls}'><svg viewBox='0 0 12 12' "
            f"fill='currentColor' aria-hidden='true'><path d='{_ARROWS[cls]}'/>"
            f"</svg>{abs(cur - base):.1f}</span>")


def _stat_cards(student, ctx) -> str:
    """The view switcher: four mini stat cards, each a plain link carrying
    its own data story (active view = accent border)."""
    c = ctx["cards"]
    agu = quote(student["agu"])
    scope = f"&course={quote(ctx['course_gu'])}" if ctx["course_gu"] else ""

    def card(view, label, big, ctxline, extra="") -> str:
        cls = " active" if view == ctx["view"] else ""
        return (f"<a class='stat{cls}' href='/student/{agu}?view={view}{scope}'>"
                f"<span class='lbl'>{label}</span>"
                f"<span class='big'>{big}</span>"
                f"<span class='ctx'>{ctxline}</span>{extra}</a>")

    wk = c["problems_week"]
    if wk > 0:
        p_ctx = f"<b style='color:var(--bad)'>+{wk}</b> this week"
    elif wk < 0:
        p_ctx = f"<b style='color:var(--ok)'>−{-wk}</b> this week"
    else:
        p_ctx = "no change this week"
    p_spark = (_spark_line(c["problems_series"], "var(--bad)", lo=0)
               if max(c["problems_series"], default=0) > 0 else "")
    parts = [card("problems", "Problems", str(c["problems_count"]),
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
        extra = _spark_bars(list(reversed(pcts)))
    else:
        big, r_ctx, extra = "—", "no grades yet", ""
    parts.append(card("recent", "Recent grades", big, r_ctx, extra))

    big = (f"{c['term_avg']:.1f}<span class='unit'>%</span>"
           if c["term_avg"] is not None else "—")
    n = c["courses"]
    e_ctx = f"term average · {n} course{'s' if n != 1 else ''}"
    e_extra = (_spark_line(c["term_series"], "var(--accent)")
               if len(c["term_series"]) >= 2 else "")
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
        rows.append(
            f"<tr{' class=\'scoped\'' if active else ''}>"
            f"<td><a class='tip-s' href='{href}' data-tip='{escape(tip)}'>"
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
               f"data-tip='show all courses'>{_FILTER_ICON}"
               f"{escape(scoped_title)}{_CLEAR_ICON}</a>")
    return (
        f"<details class='allcourses' id='allcourses'"
        f"{' open' if is_open else ''}>"
        f"<summary>{_CHEVRON}<h2 class='striphead'>All Courses"
        + (f" <span class='termtag'>{label}</span>" if label else "")
        + tag + "</h2></summary><table class='strip'>"
          "<tr class='head'><th>Course</th><th>Grade</th><th>2 weeks</th>"
          "<th>Open</th><th>Last graded</th></tr>"
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
    head = ("<tr class='head'><th>Assignment</th>"
            + ("<th>Course</th>" if with_course else "")
            + "<th>Due</th><th>Status</th></tr>")
    body, hit_seen = [], False
    for r in rows:
        attrs, lead = _row_mark(r["status"], ctx["hl"], first_hit=not hit_seen)
        hit_seen = hit_seen or "id='hit'" in attrs
        body.append(
            f"<tr{attrs}><td>{lead}{escape(r['name'])}</td>"
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
            f"<tr><td>{escape(r['name'])}</td>"
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
    graded backlog collapsed to the newest five behind a no-JS expander."""
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
    upcoming = sorted((r for r in rows if r["status"] in ("due", "not_due")),
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
        return (f"<tr{attrs}><td>{lead}{escape(a['name'])}</td>"
                f"<td data-label='Type'>{escape(a['kind'] or '—')}</td>"
                f"<td data-label='Due'>{escape(a['due_date'] or '—')}</td>"
                f"<td class='num' data-label='Score'>{_score(a)}</td>"
                f"<td data-label='Status'>{_badge(a['status'])}</td></tr>")

    visible = upcoming + missing + late + graded[:5]
    table = ("<table class='assignments'>" + _ASSIGN_COLGROUP
             + "<tr class='head'><th>Assignment</th><th>Type</th><th>Due</th>"
               "<th>Score</th><th>Status</th></tr>"
             + "".join(tr(a) for a in visible) + "</table>")
    more = ""
    if len(graded) > 5:
        more = (f"<details class='more'><summary>{_CHEVRON}"
                f"<span>Show all {len(graded)} graded</span></summary>"
                f"<table class='assignments'>{_ASSIGN_COLGROUP}"
                + "".join(tr(a) for a in graded[5:]) + "</table></details>")
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
    return (f"<details class='closedterm'><summary>{_CHEVRON}"
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
    return _page(student["name"], "".join(parts), nav_students=nav_students)


def render_alerts(alerts, counts=(), nav_students=(),
                  page: int = 1, alert_type: str = "", more: bool = False,
                  today: date | None = None) -> str:
    if not counts:
        body = "<h1>Alerts</h1><p>No alerts yet — quiet is good.</p>"
        return _page("Alerts", body, nav_students=nav_students)
    import json as _json

    today = today or date.today()

    def href(t: str, p: int = 1) -> str:
        q = ([f"type={quote(t)}"] if t else []) + ([f"page={p}"] if p > 1 else [])
        return "/alerts" + ("?" + "&".join(q) if q else "")

    # Type-group chips: one door per alert type present, with counts. The
    # active chip marks the filter; "all" clears it.
    total = sum(c["n"] for c in counts)
    chips = [f"<a class='chip{'' if alert_type else ' active'}' "
             f"href='/alerts'>all <b>{total}</b></a>"]
    chips += [
        f"<a class='chip{' active' if c['type'] == alert_type else ''}' "
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
            f"<td data-label='Type'>{escape(al['type'].replace('_', ' '))}</td></tr>")
    table = ("<table class='alerts'><tr class='head'><th>Detail</th><th>When</th>"
             "<th>Student</th><th>Type</th></tr>"
             + "".join(rows) + "</table>"
             if rows else "<p class='small'>Nothing on this page.</p>")

    pager = ""
    if page > 1 or more:
        newer = (f"<a href='{href(alert_type, page - 1)}'>← newer</a>"
                 if page > 1 else "<span></span>")
        older = (f"<a href='{href(alert_type, page + 1)}'>older →</a>"
                 if more else "<span></span>")
        pager = f"<div class='pager'>{newer}{older}</div>"

    heading = "Recent alerts" if page == 1 else f"Alerts — page {page}"
    return _page("Alerts", "<h1>Alerts</h1><div class='card tablecard'>"
                 f"<h2>{heading}</h2>"
                 f"<div class='chips'>{''.join(chips)}</div>"
                 + table + pager + "</div>", nav_students=nav_students)


_HISTORY_PREVIEW = 8   # recent rows shown per section; the rest go behind "Show all"

# The `field` column is a raw column name; humanize the ones that read as jargon.
_FIELD_LABELS = {
    "due_date": "due date",
    "graded_at": "graded",
    "kind": "assignment type",
    "percent": "grade %",
    "mark": "letter grade",
}


def _field_label(field: str) -> str:
    return _FIELD_LABELS.get(field, field.replace("_", " "))


def _history_transition(r) -> str:
    old = r["old_value"] if r["old_value"] is not None else "—"
    new = r["new_value"] if r["new_value"] is not None else "—"
    return f"{escape(old)} → {escape(new)}"


def render_history(rows, course_rows=(), class_counts=(), field_counts=(),
                   *, course="", field="", nav_students=()) -> str:
    if not class_counts and not field_counts:
        return _page("History", "<h1>Grade history</h1><p>No changes recorded yet.</p>",
                     nav_students=nav_students)
    today = date.today()

    def href(c: str, f: str) -> str:
        q = ([f"course={quote(c)}"] if c else []) + ([f"field={quote(f)}"] if f else [])
        return "/history" + ("?" + "&".join(q) if q else "")

    def chip_row(label, items, selected, href_of):
        """One filter dimension. items: (key, display, count); href_of(key)
        composes the URL preserving the other dimension (href_of('') = all)."""
        total = sum(n for _, _, n in items)
        out = [f"<a class='chip{' active' if not selected else ''}' "
               f"href='{href_of('')}'>all <b>{total}</b></a>"]
        out += [
            f"<a class='chip{' active' if key == selected else ''}' "
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

    def section(title, head_html, body_rows):
        """A history section, compact: the recent rows shown, the rest in a
        hidden <tbody class='overflow'> of the SAME table so expanding just
        continues the list — one header, aligned columns. The <details> below
        is the bare no-JS toggle; style.css reveals the tbody via :has()."""
        n = len(body_rows)
        table = "<table>" + head_html + "".join(body_rows[:_HISTORY_PREVIEW])
        more = ""
        if n > _HISTORY_PREVIEW:
            table += ("<tbody class='overflow'>"
                      + "".join(body_rows[_HISTORY_PREVIEW:]) + "</tbody>")
            more = (f"<details class='more'><summary>{_CHEVRON}"
                    f"<span>Show all {n}</span></summary></details>")
        table += "</table>"
        return (f"<div class='card tablecard'><h2>{escape(title)} "
                f"<span class='small'>{n}</span></h2>" + table + more + "</div>")

    sections = []
    if course_rows:
        head = ("<tr class='head'><th>Course</th><th>When</th><th>Student</th>"
                "<th>Change</th><th>From → To</th></tr>")
        body = [
            f"<tr><td>{escape(r['course_title'])} "
            f"<span class='small'>{escape(r['term'])}</span></td>"
            f"<td class='small' data-label='When'>{_when_html(r['seen_at'], today)}</td>"
            f"<td data-label='Student'>{escape(r['student_name'])}</td>"
            f"<td data-label='Change'>{escape(_field_label(r['field']))}</td>"
            f"<td data-label='From → To'>{_history_transition(r)}</td></tr>"
            for r in course_rows]
        sections.append(section("Course grades", head, body))
    if rows:
        head = ("<tr class='head'><th>Assignment</th><th>When</th><th>Student</th>"
                "<th>Course</th><th>Change</th><th>From → To</th></tr>")
        body = [
            f"<tr><td>{escape(r['assignment_name'])}</td>"
            f"<td class='small' data-label='When'>{_when_html(r['seen_at'], today)}</td>"
            f"<td data-label='Student'>{escape(r['student_name'])}</td>"
            f"<td data-label='Course'>{escape(r['course_title'])}</td>"
            f"<td data-label='Change'>{escape(_field_label(r['field']))}</td>"
            f"<td data-label='From → To'>{_history_transition(r)}</td></tr>"
            for r in rows]
        sections.append(section("Assignments", head, body))

    body_html = f"<div class='histfilters'>{filters}</div>"
    body_html += ("".join(sections) if sections
                  else "<p class='small'>No changes match this filter.</p>")
    return _page("History", "<h1>Grade history</h1>" + body_html,
                 nav_students=nav_students)


def _type_multiselect(fid, selected) -> str:
    """The alert-types control: a checkbox dropdown (<details> popover — the
    browser handles open/close; app.js keeps the summary label fresh and
    makes 'all alerts' exclusive). ``fid`` binds row-form controls; None
    means the checkboxes sit inside their form already."""
    from .models import AlertType

    sel = set(selected)
    if not sel or "*" in sel:
        sel = {"*"}
    opts = [("*", "all alerts")] + [
        (t.value, t.value.replace("_", " ")) for t in AlertType]
    if "*" in sel:
        label = "all alerts"
    elif len(sel) == 1:
        label = next(iter(sel)).replace("_", " ")
    else:
        label = f"{len(sel)} types"
    form_attr = f" form='{escape(fid)}'" if fid else ""
    boxes = "".join(
        f"<label><input type='checkbox' name='type' value='{escape(v)}'"
        f"{form_attr}{' checked' if v in sel else ''}> {escape(lab)}</label>"
        for v, lab in opts)
    return (f"<details class='msel'><summary>{escape(label)}</summary>"
            f"<div class='msel-list'>{boxes}</div></details>")


def _options(pairs, selected="") -> str:
    """``<option>`` list from (value, label) pairs."""
    return "".join(
        f"<option value='{escape(v)}'{' selected' if v == selected else ''}>"
        f"{escape(label)}</option>"
        for v, label in pairs)


def render_settings(watcher_list, subscriptions, students=(),
                    error="", notice="") -> str:
    """The Settings page: full watcher/subscription CRUD as plain HTML forms.
    These are the dashboard's only write paths; they carry no auth of their
    own — the bind address is the access control. Env-owned config (poll
    cadence, thresholds) is deliberately absent: if it can't be changed from
    here, it isn't shown here.
    """
    from . import notify

    watcher_opts = [(w.name, w.name) for w in watcher_list]
    # The web UI offers email and text message only (sms rides the email
    # transport but is its own field). Other channels stay CLI territory.
    channel_opts = [("email", "email"), ("sms", "text message")]
    channel_label = dict(channel_opts)

    if watcher_list:
        # One row per watcher, then one row per channel under it (each
        # editable/removable in place), then an add-channel row for whatever
        # channels the watcher doesn't have yet. Row forms live in the
        # actions cell; the other cells' controls bind via form=.
        w_rows = []
        for w in watcher_list:
            w_rows.append(
                f"<tr id='row-w-{escape(w.id)}' data-w='{escape(w.id)}'>"
                f"<td><strong>{escape(w.name)}</strong> "
                f"<span class='small'>{escape(w.kind.value)}</span></td>"
                f"<td></td>"
                f"<td data-label='Actions'>"
                f"<form method='post' action='/settings/watcher-remove' "
                f"class='rowform' data-group='{escape(w.id)}'>"
                f"<input type='hidden' name='name' value='{escape(w.name)}'>"
                f"<button class='ghost'>remove</button></form>"
                f"</td></tr>")
            for cname, addr in w.channels.items():
                fid = f"ch-{escape(w.id)}-{escape(cname)}"
                hidden = (f"<input type='hidden' name='watcher' value='{escape(w.name)}'>"
                          f"<input type='hidden' name='channel' value='{escape(cname)}'>")
                if notify.ADDRESS_KEY.get(cname) is None:   # console: no address
                    addr_cell = "—"
                    buttons = "<button class='ghost'>remove</button>"
                    form = (f"<form id='{fid}' method='post' "
                            f"action='/settings/channel-remove' class='rowform'>"
                            f"{hidden}{buttons}</form>")
                else:
                    address = next(iter(addr.values()), "")
                    # name='to' (never 'address' — browsers autofill that as
                    # a street address). Email rows opt into email autofill;
                    # sms rows hold a carrier gateway, so no suggestions.
                    autofill = "email" if cname == "email" else "off"
                    addr_cell = (f"<input name='to' form='{fid}' "
                                 f"value='{escape(address)}' "
                                 f"autocomplete='{autofill}'>")
                    form = (f"<form id='{fid}' method='post' "
                            f"action='/settings/channel' class='rowform'>{hidden}"
                            f"<button class='upd'>Update</button> "
                            f"<button class='ghost' "
                            f"formaction='/settings/channel-remove'>"
                            f"remove</button></form>")
                w_rows.append(
                    f"<tr class='chrow' id='row-{fid}' data-w='{escape(w.id)}'>"
                    f"<td class='chname'>{escape(channel_label.get(cname, cname))}</td>"
                    f"<td data-label='Address'>{addr_cell}</td>"
                    f"<td data-label='Actions'>{form}</td></tr>")
            remaining = [(c, label) for c, label in channel_opts
                         if c not in w.channels]
            if remaining:
                fid = f"chadd-{escape(w.id)}"
                w_rows.append(
                    f"<tr class='chrow' id='row-{fid}' data-w='{escape(w.id)}'>"
                    f"<td class='chname'>"
                    f"<select name='channel' form='{fid}'>"
                    f"{_options(remaining)}</select></td>"
                    f"<td data-label='Address'>"
                    f"<input name='to' form='{fid}' autocomplete='off' "
                    f"placeholder='name@example.com / 5551234567@vtext.com' "
                    f"data-tip='Email address — or, for text message, your carrier&#39;s "
                    f"email-to-SMS gateway address'></td>"
                    f"<td data-label='Actions'>"
                    f"<form id='{fid}' method='post' action='/settings/channel' "
                    f"class='rowform'>"
                    f"<input type='hidden' name='watcher' value='{escape(w.name)}'>"
                    f"<button>Add channel</button></form></td></tr>")
        w_body = ("<table class='manage'><tr class='head'><th>Watcher</th>"
                  "<th>Address</th><th></th></tr>" + "".join(w_rows) + "</table>")
    else:
        w_body = "<p class='small'>None yet — add the first watcher above.</p>"

    add_form = (
        "<form method='post' action='/settings/watcher-add' class='edit'>"
        "<span class='formtitle'>Add</span>"
        "<input name='name' placeholder='Name' required autocomplete='off'>"
        f"<select name='kind'>{_options([('guardian', 'guardian'), ('student', 'student')])}</select>"
        f"<select name='channel'>{_options([('', 'no channel yet')] + channel_opts)}</select>"
        "<input name='to' autocomplete='off' "
        "placeholder='name@example.com / 5551234567@vtext.com' "
        "data-tip='Email address — or, for text message, your carrier&#39;s "
        "email-to-SMS gateway address'>"
        "<button>Add watcher</button></form>")
    w_card = ("<div class='card tablecard'><h2>Watchers</h2>"
              + add_form + w_body + "</div>")

    if subscriptions:
        # A displayed row is a GROUP of single-type subscription rows sharing
        # (watcher, student, channel, delivery, urgent); the Alerts cell is a
        # multiselect over the group's types. A <form> can't span table
        # cells, so each row's form lives in its actions cell and the cells'
        # controls point at it via form=. Remove shares the form (formaction)
        # — it only reads the hidden ids.
        groups: dict = {}
        for s in subscriptions:
            key = (s.watcher_id, s.student_id, s.channel, s.send_at, s.urgent_now)
            groups.setdefault(key, []).append(s)
        s_rows = []
        urgent_tip = ("Send urgent alerts (missing, due soon, grade drop) "
                      "immediately instead of waiting for the digest")
        for group in groups.values():
            first = group[0]
            fid = f"sub-{escape(first.id)}"
            ids = ",".join(s.id for s in group)
            s_rows.append(
                f"<tr id='row-{fid}'>"
                f"<td>{escape(first.watcher_name)} ⇒ {escape(first.student_name)}</td>"
                f"<td data-label='Alerts'>"
                f"{_type_multiselect(fid, [s.alert_type for s in group])}</td>"
                f"<td data-label='Via'><select name='channel' form='{fid}'>"
                f"{_options([('*', 'all configured')] + channel_opts, selected=first.channel)}"
                f"</select></td>"
                f"<td data-label='Delivery'><input type='time' name='at' form='{fid}' "
                f"value='{escape(first.send_at or '')}' "
                f"data-tip='Daily digest time — blank for immediate delivery'> "
                f"<label class='urgent' data-tip='{escape(urgent_tip)}'>"
                f"<input type='checkbox' name='urgent' form='{fid}'"
                f"{' checked' if first.urgent_now else ''}> urgent now</label></td>"
                f"<td data-label='Actions'>"
                f"<form id='{fid}' method='post' action='/settings/subscription-update' "
                f"class='rowform'>"
                f"<input type='hidden' name='ids' value='{escape(ids)}'>"
                f"<button class='upd'>Update</button> "
                f"<button class='ghost' formaction='/settings/unsubscribe'>"
                f"remove</button></form></td></tr>")
        s_body = ("<table class='manage'><tr class='head'><th>Watcher ⇒ Student</th>"
                  "<th>Alerts</th><th>Via</th><th>Delivery</th><th></th></tr>"
                  + "".join(s_rows) + "</table>")
    else:
        s_body = "<p class='small'>No subscriptions yet.</p>"
    if watcher_list and students:
        student_opts = [("*", "all students")] + [
            (s["agu"], s["name"]) for s in students]
        s_form = (
            "<form method='post' action='/settings/subscribe' class='edit'>"
            "<span class='formtitle'>Add</span>"
            f"<select name='watcher'>{_options(watcher_opts)}</select>"
            "<span class='small'>gets</span>"
            f"{_type_multiselect(None, ['*'])}"
            "<span class='small'>for</span>"
            f"<select name='student'>{_options(student_opts)}</select>"
            "<span class='small'>via</span>"
            f"<select name='channel'>{_options([('*', 'all channels')] + channel_opts)}</select>"
            "<input type='time' name='at' value='16:00' "
            "data-tip='Daily digest time — clear for immediate delivery'>"
            "<label class='urgent' data-tip='Send urgent alerts (missing, due soon, "
            "grade drop) immediately instead of waiting for the digest'>"
            "<input type='checkbox' name='urgent' checked> urgent now</label>"
            "<button>Subscribe</button></form>")
    else:
        s_form = ("<p class='small'>Add a watcher first.</p>" if not watcher_list
                  else "<p class='small'>Students appear after the first run.</p>")
    s_card = ("<div class='card tablecard'><h2>Subscriptions</h2>"
              + s_form + s_body + "</div>")

    banner = f"<div class='banner bad'>{escape(error)}</div>" if error else ""
    toast = (f"<div class='toast' role='status' aria-live='polite'>"
             f"{escape(notice)}</div>" if notice else "")
    # settings-main is the region app.js swaps in place after a fetch-based
    # form post — banner, cards, and toast all live inside it.
    return _page("Settings", "<h1>Settings</h1><div id='settings-main'>"
                 + banner + w_card + s_card + toast + "</div>",
                 nav_students=students)


# ── http plumbing ─────────────────────────────────────────────────────


def _handle(conn: sqlite3.Connection, path: str) -> tuple[int, str]:
    """Route one request (path may carry a query string). Returns
    (status, html) — or, for a 301, the redirect target instead of a body."""
    from . import watchers as watchermod

    parsed = urlparse(path)
    path, query = parsed.path, parse_qs(parsed.query)
    # Every page's nav carries the student links, so fetch once up front.
    students = fetch_students(conn)
    if path == "/":
        # The overview is "right now": only the current term's courses/counts.
        courses = {s["id"]: fetch_courses(conn, s["id"], term=s["current_term"])
                   for s in students}
        counts = {s["id"]: fetch_open_counts(conn, s["id"], term=s["current_term"])
                  for s in students}
        return 200, render_overview(students, courses, counts)
    if path.startswith("/student/"):
        agu = path[len("/student/"):]
        student = fetch_student(conn, agu)
        if student is None:
            return 404, _page(
                "Not found",
                "<h1>No student by that id</h1>"
                "<p>They may have been removed, or the link is stale. "
                "<a href='/'>Back to the overview</a> — every current student "
                "is listed there.</p>",
                nav_students=students)
        ctx = build_student_ctx(conn, student,
                                (query.get("view") or [""])[0],
                                (query.get("course") or [""])[0],
                                (query.get("status") or [""])[0],
                                strip_open=(query.get("strip") or [""])[0] == "open")
        return 200, render_student(student, ctx, nav_students=students)
    if path == "/alerts":
        try:
            page = max(1, int((query.get("page") or ["1"])[0]))
        except ValueError:
            page = 1
        alert_type = (query.get("type") or [""])[0]
        alert_rows, more = fetch_alerts(conn, page, alert_type)
        return 200, render_alerts(alert_rows, fetch_alert_counts(conn),
                                  nav_students=students, page=page,
                                  alert_type=alert_type, more=more)
    if path == "/history":
        h_course = (query.get("course") or [""])[0]
        h_field = (query.get("field") or [""])[0]
        return 200, render_history(
            fetch_history(conn, course=h_course, field=h_field),
            fetch_course_history(conn, course=h_course, field=h_field),
            fetch_history_class_counts(conn), fetch_history_field_counts(conn),
            course=h_course, field=h_field, nav_students=students)
    if path == "/settings":
        return 200, render_settings(watchermod.list_watchers(conn),
                                    watchermod.list_subscriptions(conn),
                                    students,
                                    error=(query.get("err") or [""])[0],
                                    notice=(query.get("ok") or [""])[0])
    if path == "/watchers":   # pre-Settings URL; keep old bookmarks working
        return 301, "/settings"
    return 404, _page(
        "Not found",
        "<h1>No such page</h1>"
        "<p>That address doesn't go anywhere. "
        "<a href='/'>Back to the overview</a>.</p>",
        nav_students=students)


def _handle_settings_post(conn: sqlite3.Connection, action: str,
                          form: dict) -> tuple[int, str]:
    """POST /settings/<action> — the Settings page's write paths. They carry
    no auth of their own: the bind address is the access control. Always
    redirects back to /settings; a validation failure carries the message in
    ?err= and renders as a banner, with the tables (and the browser's
    back-button form state) intact."""
    from . import notify
    from . import watchers as watchermod
    from .models import WatcherKind

    def val(key: str) -> str:
        return (form.get(key) or [""])[0].strip()

    def vals(key: str) -> list[str]:
        return [v.strip() for v in (form.get(key) or []) if v.strip()]

    def channel_update() -> dict:
        name = val("channel")
        if name not in notify.ADDRESS_KEY:
            raise watchermod.WatcherError(
                f"unknown channel {name!r} (valid: {', '.join(notify.CHANNEL_NAMES)})")
        key = notify.ADDRESS_KEY[name]
        if key is None:                       # console needs no address
            return {name: {}}
        address = val("to")
        if address:
            address = notify.validate_address(name, address)
        return {name: {key: address} if address else None}

    def done(message: str, new_rows: tuple = ()) -> tuple[int, str]:
        """Success redirect: ?ok= becomes the toast, ?new= names the row
        elements the client animates in."""
        target = "/settings?ok=" + quote(message)
        if new_rows:
            target += "&new=" + ",".join(new_rows)
        return 303, target

    # Toasts name who and what changed ("Removed Mom's email
    # (mom@example.com)"), not just the verb — the reader shouldn't have to
    # diff the table to learn what happened.
    chlabel = {"email": "email", "sms": "text message"}

    def _addr(update: dict, cname: str) -> str:
        return next(iter((update.get(cname) or {}).values()), "")

    try:
        if action == "watcher-add":
            name = val("name")
            if not name:
                raise watchermod.WatcherError("the watcher needs a name")
            channels = {}
            if val("channel"):
                channels = channel_update()
                if None in channels.values():
                    raise watchermod.WatcherError(
                        f"the {val('channel')} channel needs an address")
            w = watchermod.add_watcher(conn, name, WatcherKind(val("kind")), channels)
            msg = f"Added watcher {w.name}"
            if channels:
                cname = next(iter(channels))
                addr = _addr(channels, cname)
                msg += f" with {chlabel.get(cname, cname)}"
                msg += f" {addr}" if addr else ""
            return done(msg, (f"row-w-{w.id}", f"row-chadd-{w.id}"))
        if action == "watcher-remove":
            w = watchermod.get_watcher(conn, val("name"))
            watchermod.remove_watcher(conn, val("name"))
            return done(f"Removed watcher {w.name if w else val('name')}")
        if action == "channel":         # add or update; removal is its own action
            update = channel_update()
            if None in update.values():
                raise watchermod.WatcherError(
                    f"the {val('channel')} channel needs an address")
            existing = watchermod.require_watcher(conn, val("watcher"))
            cname = next(iter(update))
            watchermod.set_channels(conn, val("watcher"), update)
            label, addr = chlabel.get(cname, cname), _addr(update, cname)
            if cname in existing.channels:
                return done(f"Updated {existing.name}'s {label}"
                            + (f": {addr}" if addr else ""))
            return done(f"Added {label} for {existing.name}"
                        + (f": {addr}" if addr else ""),
                        (f"row-ch-{existing.id}-{cname}",))
        if action == "channel-remove":
            w = watchermod.require_watcher(conn, val("watcher"))
            cname = val("channel")
            old = next(iter((w.channels.get(cname) or {}).values()), "")
            watchermod.set_channels(conn, val("watcher"), {cname: None})
            return done(f"Removed {w.name}'s {chlabel.get(cname, cname)}"
                        + (f" ({old})" if old else ""))
        if action == "subscribe":
            w = watchermod.require_watcher(conn, val("watcher"))
            if val("student") == "*":     # one step: every student at once
                targets = [s["id"] for s in fetch_students(conn)]
                target_desc = "all students"
            else:
                srow = watchermod.resolve_student(conn, val("student"))
                targets = [srow["id"]]
                target_desc = srow["name"]
            sub_types, channel = vals("type"), val("channel")
            added: list[str] = []
            for student_id in targets:
                added += watchermod.subscribe(
                    conn, w, student_id,
                    None if not sub_types or "*" in sub_types else sub_types,
                    None if channel in ("", "*") else [channel],
                    val("at") or None,
                    urgent_now=bool(val("urgent")))
            if not added:
                return done(f"{w.name} is already subscribed to {target_desc}")
            n = len(added)
            return done(f"Subscribed {w.name} to {target_desc}"
                        + (f" — {n} subscriptions" if n > 1 else ""),
                        tuple(f"row-sub-{i}" for i in added))
        if action == "subscription-update":
            ids = [i for i in val("ids").split(",") if i]
            named = next((s for s in watchermod.list_subscriptions(conn)
                          if s.id in ids), None)
            watchermod.set_subscription_group(
                conn, ids, vals("type"), val("channel") or "*",
                val("at") or None, urgent_now=bool(val("urgent")))
            return done(f"Updated {named.watcher_name}'s subscription for "
                        f"{named.student_name}" if named
                        else "Subscription updated")
        if action == "unsubscribe":
            ids = [i for i in val("ids").split(",") if i]
            if not ids:
                raise watchermod.WatcherError("no subscription selected")
            named = next((s for s in watchermod.list_subscriptions(conn)
                          if s.id in ids), None)
            for sub_id in ids:
                watchermod.remove_subscription(conn, sub_id)
            return done(f"Unsubscribed {named.watcher_name} from "
                        f"{named.student_name}" if named
                        else "Subscription removed")
        return 404, _page(
            "Not found",
            "<h1>No such action</h1>"
            "<p>That settings action doesn't exist — the page may be out of "
            "date. <a href='/settings'>Back to Settings</a>.</p>",
            nav_students=fetch_students(conn))
    except (watchermod.WatcherError, ValueError) as e:
        return 303, "/settings?err=" + quote(str(e))


def serve(db_path: Path, host: str, port: int) -> None:
    from . import store

    # Apply any pending schema migrations up front — the per-request
    # connections below assume current columns.
    boot = store.connect(db_path)
    store.ensure_schema(boot)
    boot.close()

    class Handler(BaseHTTPRequestHandler):
        _STATIC = {
            "/static/style.css": (_STYLE_PATH, "text/css; charset=utf-8"),
            "/static/app.js": (_APPJS_PATH, "text/javascript; charset=utf-8"),
        }

        def do_GET(self) -> None:  # noqa: N802 (stdlib name)
            static = self._STATIC.get(urlparse(self.path).path)
            if static:
                payload = static[0].read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", static[1])
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            # A connection per request: cheap for SQLite, and thread-safe by
            # construction under ThreadingHTTPServer.
            conn = store.connect(db_path)
            try:
                status, html = _handle(conn, self.path)
            except sqlite3.OperationalError as e:
                status, html = 500, _page(
                    "Error",
                    "<h1>Something went wrong</h1>"
                    "<p>The dashboard couldn't read its database just now — "
                    "usually momentary (a poll or backup holding the file). "
                    "Refresh to try again; if it keeps happening, check that "
                    "the database file exists and is writable.</p>"
                    f"<p class='small'>Detail: {escape(str(e))}</p>")
            finally:
                conn.close()
            if status == 301:   # html is the redirect target, not a body
                self.send_response(301)
                self.send_header("Location", html)
                self.end_headers()
                return
            payload = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:  # noqa: N802 (stdlib name)
            path = urlparse(self.path).path
            if not path.startswith("/settings/"):
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode("utf-8"))
            conn = store.connect(db_path)
            try:
                status, result = _handle_settings_post(
                    conn, path[len("/settings/"):], form)
            finally:
                conn.close()
            if status == 303:
                self.send_response(303)
                self.send_header("Location", result)
                self.end_headers()
                return
            payload = result.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args) -> None:
            pass  # keep the terminal quiet; this is a background convenience

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"dashboard: http://{host}:{port}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()
