"""Web dashboard (Phase 3, ack added in Phase 4).

The dashboard is for *looking things up on demand* — alerts are always pushed
out, so nobody has to open this to find out something changed. It's stdlib
``http.server`` over the same SQLite file the watch loop writes: no framework,
no build step. Pages are SELECTs; the write paths are the shared-ack button on
/alerts (``POST /ack``) and the watcher/subscription forms on /settings
(``POST /settings/<action>``) — household bookkeeping only, never grade data.

It binds 127.0.0.1 by default. To share it on your LAN set
MCPSGRADEWATCH_DASHBOARD_HOST=0.0.0.0 — and know that unlike alert payloads it
shows full names (and, on /settings, watcher addresses), so treat the bind
address as the access control; the write paths carry no auth of their own.
"""
from __future__ import annotations

import sqlite3
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

_STATUS_LABELS = {
    "graded": ("graded", "ok"),
    "not_due": ("not due yet", "muted"),
    "due": ("due", "info"),
    "missing": ("MISSING", "bad"),
    "ungraded_past_due": ("ungraded past due", "warn"),
}

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


def fetch_assignments(conn: sqlite3.Connection, course_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM assignments WHERE course_id = ? "
        "ORDER BY due_date IS NULL, due_date DESC, name", (course_id,)
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


def fetch_alerts(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT al.*, st.name AS student_name, w.name AS acked_by_name "
        "FROM alerts al "
        "JOIN students st ON st.id = al.student_id "
        "LEFT JOIN watchers w ON w.id = al.acked_by "
        "ORDER BY al.created_at DESC, al.rowid DESC LIMIT ?", (limit,)
    ).fetchall()


def fetch_course_history(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT h.*, c.title AS course_title, c.term, st.name AS student_name "
        "FROM course_history h "
        "JOIN courses c ON c.id = h.course_id "
        "JOIN students st ON st.id = c.student_id "
        "ORDER BY h.seen_at DESC, h.id DESC LIMIT ?", (limit,)
    ).fetchall()


def fetch_history(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT h.*, a.name AS assignment_name, c.title AS course_title, "
        "       st.name AS student_name "
        "FROM grade_history h "
        "JOIN assignments a ON a.id = h.assignment_id "
        "JOIN courses c ON c.id = a.course_id "
        "JOIN students st ON st.id = c.student_id "
        "ORDER BY h.seen_at DESC, h.id DESC LIMIT ?", (limit,)
    ).fetchall()


# ── rendering (pure: rows in, html out) ───────────────────────────────


# Theme toggle: cycles auto → light → dark, saved per browser. The first
# statement runs before paint so a saved choice never flashes the wrong theme.
_THEME_JS = """
(function(){
  var KEY='mcpsgradewatch-theme', root=document.documentElement, choice=null;
  try { choice = localStorage.getItem(KEY); } catch (e) {}
  function apply(){
    if (choice==='light'||choice==='dark') root.setAttribute('data-theme', choice);
    else root.removeAttribute('data-theme');
    var b=document.getElementById('themetoggle');
    if (b) b.textContent = choice==='light' ? '\\u2600 light'
                         : choice==='dark' ? '\\u263e dark' : '\\u25d0 auto';
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
_NAV_ITEMS = (
    ("/", "Students",
     "<path d='M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2'/><circle cx='12' cy='7' r='4'/>"),
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


def _page(title: str, body: str) -> str:
    links = "".join(
        f"<a href='{href}' title='{label}'>{_SVG.format(icon)}"
        f"<span class='lbl'>{label}</span></a>"
        for href, label, icon in _NAV_ITEMS)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{escape(title)} · MCPSGradeWatch</title>"
        "<link rel='stylesheet' href='/static/style.css'>"
        f"<script>{_THEME_JS}</script>"
        "<script src='/static/app.js' defer></script></head><body>"
        f"<nav><a class='brand' href='/'>MCPSGradeWatch</a>{links}"
        f"<a class='gear' href='/settings' title='Settings' "
        f"aria-label='Settings'>{_GEAR_ICON}</a>"
        "<button id='themetoggle' title='Theme: follows your system unless "
        "you pick one (saved in this browser)'>◐ auto</button></nav>"
        f"{body}</body></html>"
    )


def _badge(status: str) -> str:
    label, klass = _STATUS_LABELS.get(status, (status, "muted"))
    return f"<span class='badge {klass}'>{escape(label)}</span>"


def _pct(raw: str) -> str:
    """Course percent for display: one decimal place, or a dash."""
    from .models import format_percent

    formatted = format_percent(raw)
    return formatted if formatted is not None else (raw or "—")


def _score(row) -> str:
    """Assignment score as a percentage (one decimal), raw points on hover.

    No points value (or zero, e.g. extra credit) means no denominator to
    percent against — those show the raw score.
    """
    if row["score"] is None:
        return "—"
    if not row["points"]:
        return escape(f"{row['score']:g}")
    raw = f"{row['score']:g}/{row['points']:g}"
    pct = row["score"] / row["points"] * 100
    return f"<span title='{escape(raw)}'>{pct:.1f}%</span>"


def render_overview(students, courses_by_student, counts_by_student) -> str:
    if not students:
        return _page("Students",
                     "<h1>No students yet</h1><p>Run <code>mcpsgradewatch run</code> "
                     "once to establish a baseline.</p>")
    cards = []
    for s in students:
        courses = courses_by_student.get(s["id"], [])
        counts = counts_by_student.get(s["id"], {})
        rows = "".join(
            f"<tr><td><a href='/student/{escape(s['agu'])}'>{escape(c['title'])}</a></td>"
            f"<td data-label='Teacher'>{escape(c['teacher'])}</td>"
            f"<td class='num' data-label='%'>{escape(_pct(c['percent']))}</td>"
            f"<td data-label='Mark'>{escape(c['mark'] or '—')}</td></tr>"
            for c in courses)
        flags = []
        if counts.get("missing"):
            flags.append(f"<span class='badge bad'>{counts['missing']} missing</span>")
        if counts.get("past_due"):
            flags.append(f"<span class='badge warn'>{counts['past_due']} ungraded past due</span>")
        if counts.get("due"):
            flags.append(f"<span class='badge info'>{counts['due']} due soon</span>")
        cards.append(
            f"<div class='card'><h3><a href='/student/{escape(s['agu'])}'>"
            f"{escape(s['name'])}</a></h3>"
            f"<div class='small'>{escape(s['school'])}</div>"
            f"<div>{' '.join(flags) or '<span class=small>all clear</span>'}</div>"
            f"<table class='courses'><tr class='head'><th>Course</th><th>Teacher</th>"
            f"<th>%</th><th>Mark</th></tr>{rows}</table></div>")
    return _page("Students", "<h1>Students</h1><div class='cards'>" + "".join(cards) + "</div>")


def render_student(student, sections) -> str:
    """``sections`` is [(term, [(course, assignments), …]), …], current term
    first. Term headings appear only once a second term exists — a
    single-quarter database looks exactly as it did before rollover."""
    parts = [f"<h1>{escape(student['name'])}</h1>"
             f"<p class='small'>{escape(student['school'])} · AGU {escape(student['agu'])}</p>"]
    current = student["current_term"] if "current_term" in student.keys() else ""
    for term, courses_with_assignments in sections:
        if len(sections) > 1:
            label = term or "(no term)"
            if term and term == current:
                label += " — current"
            parts.append(f"<h2 class='small' style='text-transform:uppercase;"
                         f"letter-spacing:.06em'>{escape(label)}</h2>")
        parts.append(_render_term_courses(courses_with_assignments))
    return _page(student["name"], "".join(parts))


def _render_term_courses(courses_with_assignments) -> str:
    parts = []
    for course, assignments in courses_with_assignments:
        head = escape(course["title"])
        pct = _pct(course["percent"]) if course["percent"] else ""
        overall = " · ".join(x for x in (pct and f"{pct}%", course["mark"]) if x)
        teacher = f" — {escape(course['teacher'])}" if course["teacher"] else ""
        header = (
            f"<h2>{head}{teacher}"
            + (f" <span class='badge muted'>{escape(overall)}</span>" if overall else "")
            + "</h2>")
        if not assignments:
            body = "<p class='small'>No assignments recorded.</p>"
        else:
            rows = "".join(
                f"<tr><td>{escape(a['name'])}</td>"
                f"<td data-label='Type'>{escape(a['kind'] or '—')}</td>"
                f"<td data-label='Due'>{escape(a['due_date'] or '—')}</td>"
                f"<td class='num' data-label='Score'>{_score(a)}</td>"
                f"<td data-label='Status'>{_badge(a['status'])}</td></tr>"
                for a in assignments)
            body = ("<table class='assignments'><tr class='head'><th>Assignment</th><th>Type</th>"
                    "<th>Due</th><th>Score</th><th>Status</th></tr>" + rows + "</table>")
        parts.append(f"<div class='card tablecard'>{header}{body}</div>")
    return "".join(parts)


def render_alerts(alerts, watcher_list=()) -> str:
    if not alerts:
        body = "<h1>Alerts</h1><p>No alerts yet — quiet is good.</p>"
        return _page("Alerts", body)
    import json as _json

    options = "".join(f"<option>{escape(w.name)}</option>" for w in watcher_list)
    rows = []
    for al in alerts:
        try:
            detail = _json.loads(al["body"]).get("detail", al["body"])
        except Exception:
            detail = al["body"]
        if al["acked_at"]:
            ack_cell = (f"<span class='badge ok'>✓ {escape(al['acked_by_name'] or 'acked')}"
                        f"</span>")
        elif watcher_list:
            ack_cell = (f"<form method='post' action='/ack' class='ackform'>"
                        f"<input type='hidden' name='alert_id' value='{escape(al['id'])}'>"
                        f"<select name='watcher'>{options}</select> "
                        f"<button>ack</button></form>")
        else:
            ack_cell = "<span class='small'>—</span>"
        rows.append(
            f"<tr><td>{escape(detail)}</td>"
            f"<td class='small' data-label='When'>{escape(al['created_at'])}</td>"
            f"<td data-label='Student'>{escape(al['student_name'])}</td>"
            f"<td data-label='Type'>{escape(al['type'].replace('_', ' '))}</td>"
            f"<td data-label='Ack'>{ack_cell}</td></tr>")
    return _page("Alerts", "<h1>Alerts</h1><div class='card tablecard'>"
                 "<h2>Recent alerts</h2><p class='small'>An ack is shared: one "
                 "person marking an alert handled marks it for everyone.</p>"
                 "<table><tr class='head'><th>Detail</th><th>When (UTC)</th>"
                 "<th>Student</th><th>Type</th><th>Ack</th></tr>"
                 + "".join(rows) + "</table></div>")


def render_history(rows, course_rows=()) -> str:
    if not rows and not course_rows:
        return _page("History", "<h1>Grade history</h1><p>No changes recorded yet.</p>")
    parts = ["<h1>Grade history</h1>"]
    if course_rows:
        c_rows = "".join(
            f"<tr><td>{escape(r['course_title'])} "
            f"<span class='small'>{escape(r['term'])}</span></td>"
            f"<td class='small' data-label='When'>{escape(r['seen_at'])}</td>"
            f"<td data-label='Student'>{escape(r['student_name'])}</td>"
            f"<td data-label='Field'>{escape(r['field'])}</td>"
            f"<td data-label='Change'>"
            f"{escape(r['old_value'] if r['old_value'] is not None else '—')} → "
            f"{escape(r['new_value'] if r['new_value'] is not None else '—')}</td></tr>"
            for r in course_rows)
        parts.append("<div class='card tablecard'><h2>Course grades</h2>"
                     "<table><tr class='head'><th>Course</th><th>When (UTC)</th>"
                     "<th>Student</th><th>Field</th><th>Change</th></tr>"
                     + c_rows + "</table></div>")
    if rows:
        body_rows = "".join(
            f"<tr><td>{escape(r['assignment_name'])}</td>"
            f"<td class='small' data-label='When'>{escape(r['seen_at'])}</td>"
            f"<td data-label='Student'>{escape(r['student_name'])}</td>"
            f"<td data-label='Course'>{escape(r['course_title'])}</td>"
            f"<td data-label='Field'>{escape(r['field'])}</td>"
            f"<td data-label='Change'>"
            f"{escape(r['old_value'] if r['old_value'] is not None else '—')} → "
            f"{escape(r['new_value'] if r['new_value'] is not None else '—')}</td></tr>"
            for r in rows)
        parts.append("<div class='card tablecard'><h2>Assignments</h2>"
                     "<table><tr class='head'><th>Assignment</th><th>When (UTC)</th>"
                     "<th>Student</th><th>Course</th><th>Field</th>"
                     "<th>Change</th></tr>" + body_rows + "</table></div>")
    return _page("History", "".join(parts))


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
    """The Settings page: full watcher/subscription CRUD as plain HTML forms
    (same trust model as ack — the bind address is the access control).
    Env-owned config (poll cadence, thresholds) is deliberately absent: if it
    can't be changed from here, it isn't shown here.
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
                f"<button class='ghost' title='remove watcher {escape(w.name)}'>"
                f"remove</button></form>"
                f"</td></tr>")
            for cname, addr in w.channels.items():
                fid = f"ch-{escape(w.id)}-{escape(cname)}"
                hidden = (f"<input type='hidden' name='watcher' value='{escape(w.name)}'>"
                          f"<input type='hidden' name='channel' value='{escape(cname)}'>")
                if notify.ADDRESS_KEY.get(cname) is None:   # console: no address
                    addr_cell = "—"
                    buttons = ("<button class='ghost' "
                               "title='remove this channel'>remove</button>")
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
                            f"<button class='ghost' formaction='/settings/channel-remove' "
                            f"title='remove this channel'>remove</button></form>")
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
                    f"title='Email address — or, for text message, your carrier&#39;s "
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
        "title='Email address — or, for text message, your carrier&#39;s "
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
                f"title='Daily digest time — blank for immediate delivery'> "
                f"<label class='urgent' title='{escape(urgent_tip)}'>"
                f"<input type='checkbox' name='urgent' form='{fid}'"
                f"{' checked' if first.urgent_now else ''}> urgent now</label></td>"
                f"<td data-label='Actions'>"
                f"<form id='{fid}' method='post' action='/settings/subscription-update' "
                f"class='rowform'>"
                f"<input type='hidden' name='ids' value='{escape(ids)}'>"
                f"<button class='upd'>Update</button> "
                f"<button class='ghost' formaction='/settings/unsubscribe' "
                f"title='remove this subscription'>remove</button></form></td></tr>")
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
            "title='Daily digest time — clear for immediate delivery'>"
            "<label class='urgent' title='Send urgent alerts (missing, due soon, "
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
                 + banner + w_card + s_card + toast + "</div>")


# ── http plumbing ─────────────────────────────────────────────────────


def _handle(conn: sqlite3.Connection, path: str) -> tuple[int, str]:
    """Route one request (path may carry a query string). Returns
    (status, html) — or, for a 301, the redirect target instead of a body."""
    from . import watchers as watchermod

    parsed = urlparse(path)
    path, query = parsed.path, parse_qs(parsed.query)
    if path == "/":
        students = fetch_students(conn)
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
            return 404, _page("Not found", "<h1>Unknown student</h1>")
        all_courses = fetch_courses(conn, student["id"])
        current = student["current_term"] or ""
        terms: list[str] = []
        for c in all_courses:
            if c["term"] not in terms:
                terms.append(c["term"])
        ordered = ([current] if current in terms else []) \
            + sorted((t for t in terms if t != current), reverse=True)
        sections = [
            (t, [(c, fetch_assignments(conn, c["id"]))
                 for c in all_courses if c["term"] == t])
            for t in ordered]
        return 200, render_student(student, sections)
    if path == "/alerts":
        return 200, render_alerts(fetch_alerts(conn),
                                  watchermod.list_watchers(conn))
    if path == "/history":
        return 200, render_history(fetch_history(conn), fetch_course_history(conn))
    if path == "/settings":
        return 200, render_settings(watchermod.list_watchers(conn),
                                    watchermod.list_subscriptions(conn),
                                    fetch_students(conn),
                                    error=(query.get("err") or [""])[0],
                                    notice=(query.get("ok") or [""])[0])
    if path == "/watchers":   # pre-Settings URL; keep old bookmarks working
        return 301, "/settings"
    return 404, _page("Not found", "<h1>404</h1><p>No such page.</p>")


def _handle_ack(conn: sqlite3.Connection, form: dict) -> tuple[int, str]:
    """POST /ack — the dashboard's single write path (shared ack)."""
    from . import store
    from . import watchers as watchermod

    alert_id = (form.get("alert_id") or [""])[0]
    watcher_name = (form.get("watcher") or [""])[0]
    w = watchermod.get_watcher(conn, watcher_name) if watcher_name else None
    if not alert_id or w is None:
        return 400, _page("Bad request", "<h1>Bad ack</h1><p>Missing alert or watcher.</p>")
    try:
        store.ack_alert(conn, alert_id, w.id)
    except store.AckError as e:
        return 400, _page("Bad request", f"<h1>Bad ack</h1><p>{escape(str(e))}</p>")
    return 303, "/alerts"   # redirect target, not a body


def _handle_settings_post(conn: sqlite3.Connection, action: str,
                          form: dict) -> tuple[int, str]:
    """POST /settings/<action> — the Settings page's write paths. Same trust
    model as ack: the bind address is the access control. Always redirects
    back to /settings; a validation failure carries the message in ?err= and
    renders as a banner, with the tables (and the browser's back-button form
    state) intact."""
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
            return done(f"Added watcher {w.name}",
                        (f"row-w-{w.id}", f"row-chadd-{w.id}"))
        if action == "watcher-remove":
            watchermod.remove_watcher(conn, val("name"))
            return done("Watcher removed")
        if action == "channel":         # add or update; removal is its own action
            update = channel_update()
            if None in update.values():
                raise watchermod.WatcherError(
                    f"the {val('channel')} channel needs an address")
            existing = watchermod.require_watcher(conn, val("watcher"))
            cname = next(iter(update))
            watchermod.set_channels(conn, val("watcher"), update)
            if cname in existing.channels:
                return done(f"{cname} channel updated")
            return done(f"{cname} channel added",
                        (f"row-ch-{existing.id}-{cname}",))
        if action == "channel-remove":
            watchermod.set_channels(conn, val("watcher"), {val("channel"): None})
            return done("Channel removed")
        if action == "subscribe":
            w = watchermod.require_watcher(conn, val("watcher"))
            if val("student") == "*":     # one step: every student at once
                targets = [s["id"] for s in fetch_students(conn)]
            else:
                targets = [watchermod.resolve_student(conn, val("student"))["id"]]
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
                return done("Already subscribed")
            return done(f"Added {len(added)} subscription{'s' if len(added) != 1 else ''}",
                        tuple(f"row-sub-{i}" for i in added))
        if action == "subscription-update":
            watchermod.set_subscription_group(
                conn, [i for i in val("ids").split(",") if i],
                vals("type"), val("channel") or "*",
                val("at") or None, urgent_now=bool(val("urgent")))
            return done("Subscription updated")
        if action == "unsubscribe":
            ids = [i for i in val("ids").split(",") if i]
            if not ids:
                raise watchermod.WatcherError("no subscription selected")
            for sub_id in ids:
                watchermod.remove_subscription(conn, sub_id)
            return done("Subscription removed")
        return 404, _page("Not found", "<h1>404</h1><p>No such action.</p>")
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
                status, html = 500, _page("Error", f"<h1>Database error</h1><p>{escape(str(e))}</p>")
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
            if path != "/ack" and not path.startswith("/settings/"):
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode("utf-8"))
            conn = store.connect(db_path)
            try:
                if path == "/ack":
                    status, result = _handle_ack(conn, form)
                else:
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
