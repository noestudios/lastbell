"""Web dashboard (Phase 3, ack added in Phase 4).

The dashboard is for *looking things up on demand* — alerts are always pushed
out, so nobody has to open this to find out something changed. It's stdlib
``http.server`` over the same SQLite file the watch loop writes: no framework,
no build step. Every page is a SELECT; the one deliberate write path is the
shared-ack button on /alerts (``POST /ack``), which only ever sets
``acked_by``/``acked_at`` on an existing alert row.

It binds 127.0.0.1 by default. To share it on your LAN set
MCPSGRADEWATCH_DASHBOARD_HOST=0.0.0.0 — and know that unlike alert payloads it
shows full names, so treat the bind address as the access control.
"""
from __future__ import annotations

import sqlite3
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_STATUS_LABELS = {
    "graded": ("graded", "ok"),
    "not_due": ("not due yet", "muted"),
    "due": ("due", "info"),
    "missing": ("MISSING", "bad"),
    "ungraded_past_due": ("ungraded past due", "warn"),
}

# The theme lives in style.css next to this module (design tokens extracted
# from the Purity UI Dashboard template) and is served at /static/style.css.
_STYLE_PATH = Path(__file__).with_name("style.css")


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


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{escape(title)} · MCPSGradeWatch</title>"
        "<link rel='stylesheet' href='/static/style.css'>"
        f"<script>{_THEME_JS}</script></head><body>"
        "<nav><a class='brand' href='/'>MCPSGradeWatch</a>"
        "<a href='/'>Students</a><a href='/alerts'>Alerts</a>"
        "<a href='/history'>History</a><a href='/watchers'>Watchers</a>"
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
            f"<td>{escape(c['teacher'])}</td>"
            f"<td class='num'>{escape(_pct(c['percent']))}</td>"
            f"<td>{escape(c['mark'] or '')}</td></tr>"
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
            f"<table class='courses'><tr><th>Course</th><th>Teacher</th>"
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
        parts.append(
            f"<h2>{head}{teacher}"
            + (f" <span class='badge muted'>{escape(overall)}</span>" if overall else "")
            + "</h2>")
        if not assignments:
            parts.append("<p class='small'>No assignments recorded.</p>")
            continue
        rows = "".join(
            f"<tr><td>{escape(a['name'])}</td><td>{escape(a['kind'] or '')}</td>"
            f"<td>{escape(a['due_date'] or '—')}</td>"
            f"<td class='num'>{_score(a)}</td><td>{_badge(a['status'])}</td></tr>"
            for a in assignments)
        parts.append("<table class='assignments'><tr><th>Assignment</th><th>Type</th>"
                     "<th>Due</th><th>Score</th><th>Status</th></tr>" + rows + "</table>")
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
            f"<tr><td class='small'>{escape(al['created_at'])}</td>"
            f"<td>{escape(al['student_name'])}</td>"
            f"<td>{escape(al['type'].replace('_', ' '))}</td>"
            f"<td>{escape(detail)}</td><td>{ack_cell}</td></tr>")
    return _page("Alerts", "<h1>Alerts</h1><p class='small'>An ack is shared: one "
                 "person marking an alert handled marks it for everyone.</p>"
                 "<table><tr><th>When (UTC)</th><th>Student</th>"
                 "<th>Type</th><th>Detail</th><th>Ack</th></tr>"
                 + "".join(rows) + "</table>")


def render_history(rows, course_rows=()) -> str:
    if not rows and not course_rows:
        return _page("History", "<h1>Grade history</h1><p>No changes recorded yet.</p>")
    parts = ["<h1>Grade history</h1>"]
    if course_rows:
        c_rows = "".join(
            f"<tr><td class='small'>{escape(r['seen_at'])}</td>"
            f"<td>{escape(r['student_name'])}</td>"
            f"<td>{escape(r['course_title'])} <span class='small'>{escape(r['term'])}</span></td>"
            f"<td>{escape(r['field'])}</td>"
            f"<td>{escape(r['old_value'] if r['old_value'] is not None else '—')} → "
            f"{escape(r['new_value'] if r['new_value'] is not None else '—')}</td></tr>"
            for r in course_rows)
        parts.append("<h2>Course grades</h2><table><tr><th>When (UTC)</th>"
                     "<th>Student</th><th>Course</th><th>Field</th><th>Change</th></tr>"
                     + c_rows + "</table>")
    if rows:
        body_rows = "".join(
            f"<tr><td class='small'>{escape(r['seen_at'])}</td>"
            f"<td>{escape(r['student_name'])}</td><td>{escape(r['course_title'])}</td>"
            f"<td>{escape(r['assignment_name'])}</td><td>{escape(r['field'])}</td>"
            f"<td>{escape(r['old_value'] if r['old_value'] is not None else '—')} → "
            f"{escape(r['new_value'] if r['new_value'] is not None else '—')}</td></tr>"
            for r in rows)
        parts.append("<h2>Assignments</h2><table><tr><th>When (UTC)</th>"
                     "<th>Student</th><th>Course</th><th>Assignment</th><th>Field</th>"
                     "<th>Change</th></tr>" + body_rows + "</table>")
    return _page("History", "".join(parts))


def render_watchers(watcher_list, subscriptions) -> str:
    if not watcher_list:
        return _page("Watchers", "<h1>Watchers</h1><p>None yet. Add one with "
                     "<code>mcpsgradewatch watcher add</code>.</p>")
    def quiet(w) -> str:
        if w.quiet_hours.get("start") and w.quiet_hours.get("end"):
            return f"{w.quiet_hours['start']}–{w.quiet_hours['end']}"
        return "—"

    w_rows = "".join(
        f"<tr><td>{escape(w.name)}</td><td>{escape(w.kind.value)}</td>"
        f"<td>{escape(', '.join(w.channels) or '—')}</td>"
        f"<td>{escape(quiet(w))}</td></tr>"
        for w in watcher_list)
    s_rows = "".join(
        f"<tr><td>{escape(s.watcher_name)}</td><td>{escape(s.student_name)}</td>"
        f"<td>{escape('all' if s.alert_type == '*' else s.alert_type.replace('_', ' '))}</td>"
        f"<td>{escape('all configured' if s.channel == '*' else s.channel)}</td>"
        f"<td>{escape(f'daily at {s.send_at}' if s.send_at else 'immediate')}</td></tr>"
        for s in subscriptions)
    return _page("Watchers",
                 "<h1>Watchers</h1><table><tr><th>Name</th><th>Kind</th>"
                 "<th>Channels</th><th>Quiet hours</th></tr>" + w_rows + "</table>"
                 "<h2>Subscriptions</h2>"
                 + ("<table><tr><th>Watcher</th><th>Student</th><th>Alerts</th>"
                    "<th>Via</th><th>Delivery</th></tr>" + s_rows + "</table>"
                    if subscriptions else "<p>No subscriptions yet.</p>"))


# ── http plumbing ─────────────────────────────────────────────────────


def _handle(conn: sqlite3.Connection, path: str) -> tuple[int, str]:
    """Route one request. Returns (status, html)."""
    from . import watchers as watchermod

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
    if path == "/watchers":
        return 200, render_watchers(watchermod.list_watchers(conn),
                                    watchermod.list_subscriptions(conn))
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


def serve(db_path: Path, host: str, port: int) -> None:
    from . import store

    # Apply any pending schema migrations up front — the per-request
    # connections below assume current columns.
    boot = store.connect(db_path)
    store.ensure_schema(boot)
    boot.close()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (stdlib name)
            if urlparse(self.path).path == "/static/style.css":
                css = _STYLE_PATH.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/css; charset=utf-8")
                self.send_header("Content-Length", str(len(css)))
                self.end_headers()
                self.wfile.write(css)
                return
            # A connection per request: cheap for SQLite, and thread-safe by
            # construction under ThreadingHTTPServer.
            conn = store.connect(db_path)
            try:
                status, html = _handle(conn, urlparse(self.path).path)
            except sqlite3.OperationalError as e:
                status, html = 500, _page("Error", f"<h1>Database error</h1><p>{escape(str(e))}</p>")
            finally:
                conn.close()
            payload = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:  # noqa: N802 (stdlib name)
            if urlparse(self.path).path != "/ack":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode("utf-8"))
            conn = store.connect(db_path)
            try:
                status, result = _handle_ack(conn, form)
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
    print(f"dashboard: http://{host}:{port}/  (read-only; Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()
