"""Read-only SQL for the dashboard pages (plus the one derived-series
helper the student page needs). Rows in, rows out — no HTML here."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from .. import store


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
           "WHERE c.student_id = ? AND " + store.NOT_SUPERSEDED_SQL)
    params: tuple = (student_id,)
    if term:
        sql += " AND c.term = ?"
        params += (term,)
    row = conn.execute(sql, params).fetchone()
    return {k: row[k] or 0 for k in ("missing", "past_due", "due")}


# ── student page data (C0: four views, stat cards, course strip) ──────

_VIEWS = ("problems", "due", "recent", "everything")

# Statuses an overview badge can ask the student page to highlight
# (?status=). The renderer's row-signal table covers exactly these.
HIGHLIGHT_STATUSES = frozenset(("missing", "ungraded_past_due", "due"))

# What the Problems view (and its trend sparkline) counts.
_PROBLEM_STATUSES = ("missing", "ungraded_past_due")


def fetch_strip_rows(conn: sqlite3.Connection, student_id: str,
                     term: str = "") -> list[sqlite3.Row]:
    """Course-strip rows: each course with its open-issue counts and the date
    a grade last landed. ``graded_at`` when the source supplied it (the demo
    seeder does), else the local day a score first appeared in grade_history
    (seen_at is UTC; the reader's date words are local, as in _when_html) —
    the live collector never fills graded_at."""
    sql = ("SELECT c.*, "
           "  SUM(a.status = 'missing') AS missing, "
           "  SUM(a.status = 'ungraded_past_due') AS past_due, "
           "  SUM(a.status = 'due') AS due, "
           "  MAX(CASE WHEN a.status = 'graded' THEN COALESCE(a.graded_at, "
           "      (SELECT date(MIN(h.seen_at), 'localtime') FROM grade_history h "
           "       WHERE h.assignment_id = a.id AND h.field = 'score')) END) "
           "  AS last_graded "
           "FROM courses c LEFT JOIN assignments a ON a.course_id = c.id "
           "  AND " + store.NOT_SUPERSEDED_SQL + " "
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
           "  c.term AS course_term, " + store.CANVAS_TWIN_SQL + ", "
           "  COALESCE(a.graded_at, (SELECT date(MIN(h.seen_at), 'localtime') "
           "    FROM grade_history h WHERE h.assignment_id = a.id "
           "    AND h.field = 'score')) AS graded_on "
           "FROM assignments a JOIN courses c ON c.id = a.course_id "
           "WHERE c.student_id = ? AND " + store.NOT_SUPERSEDED_SQL)
    params: list = [student_id]
    if term:
        sql += " AND c.term = ?"
        params.append(term)
    return conn.execute(sql, params).fetchall()


def _fetch_change_rows(conn, table: str, key: str, join: str, student_id: str,
                       term: str, field: str) -> dict[str, list]:
    """id -> ascending [(day, old, new), …] from one of the history tables."""
    # seen_at is UTC; the sample days it is compared with are local dates.
    sql = (f"SELECT h.{key} AS k, date(h.seen_at, 'localtime') AS d, "
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


def fetch_mark_history(conn, student_id: str, term: str = "") -> dict[str, list]:
    """course_id -> the course's *mark* changes, ascending. The portal
    sometimes puts the number in the mark slot and leaves percent empty; for
    those courses this, not the percent history, is the grade trajectory."""
    return _fetch_change_rows(
        conn, "course_history", "course_id",
        "JOIN courses c ON c.id = h.course_id", student_id, term, "mark")


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


def _grade_history(course, phist: dict[str, list],
                   mhist: dict[str, list]) -> tuple[list, bool]:
    """The history rows that carry this course's grade, and which slot they
    came from. Normally the percent rows; for a course whose number sits in
    the mark slot (percent empty), the mark rows."""
    from ..models import parse_percent

    if (parse_percent(course["percent"]) is None
            and parse_percent(course["mark"]) is not None):
        return mhist.get(course["id"], []), True
    return phist.get(course["id"], []), False


def _historic_grade(raw, from_mark: bool) -> float | None:
    """One historic value read the way ``course_grade`` reads a live course:
    a bare 0 in the percent slot with no mark is the portal's "nothing graded
    yet", not a zero."""
    from ..models import course_grade

    if raw is None:
        return None
    return course_grade(raw, "")[0] if from_mark else course_grade("", raw)[0]


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
    from ..models import course_grade

    today = today or date.today()
    view = view if view in _VIEWS else "problems"
    hl = hl if hl in HIGHLIGHT_STATUSES else ""
    sid, term = student["id"], student["current_term"] or ""
    strip = fetch_strip_rows(conn, sid, term)
    if course_gu and course_gu not in {
            c["edupoint_gu"] for c in fetch_courses(conn, sid)}:
        course_gu = ""

    # One fetch serves both the current-term views and the Everything
    # archive: the term-scoped rows are a subset of the student's rows.
    all_rows = fetch_view_rows(conn, sid)
    rows = [r for r in all_rows if r["course_term"] == term] if term else all_rows
    problems = sorted(
        (r for r in rows if r["status"] in _PROBLEM_STATUSES),
        key=lambda r: (r["status"] != "missing", r["due_date"] or "9999",
                       r["name"]))
    due = sorted((r for r in rows if r["status"] == "due"),
                 key=lambda r: (r["due_date"] or "9999", r["name"]))
    graded = sorted(
        (r for r in rows if r["status"] == "graded" and r["graded_on"]),
        key=lambda r: r["graded_on"], reverse=True)

    # Course strip: 2-week percent deltas from course_history — from the
    # mark rows for a course whose number lives in the mark slot.
    phist = fetch_percent_history(conn, sid, term)
    mhist = fetch_mark_history(conn, sid, term)
    cutoff = (today - timedelta(days=14)).isoformat()
    deltas: dict[str, tuple] = {}
    percents = []
    for c in strip:
        cur = course_grade(c["mark"], c["percent"])[0]
        hrows, from_mark = _grade_history(c, phist, mhist)
        base = (_historic_grade(_value_at(hrows, cutoff), from_mark)
                if hrows else cur)
        deltas[c["id"]] = (cur, base if base is not None else cur)
        if cur is not None:
            percents.append(cur)
    term_avg = sum(percents) / len(percents) if percents else None

    def scoped(items):
        return [r for r in items
                if not course_gu or r["course_gu"] == course_gu]

    # The cards tell the SCOPED story (owner's call 2026-09-05): under a
    # course filter every number on them is that course's, so a card's
    # count is a promise about the panel it opens. The whole-student
    # figure rides along as context ("1 of 5 overall").
    scoped_course = next((c for c in strip if c["edupoint_gu"] == course_gu), None)
    card_courses = [scoped_course] if scoped_course else strip

    # Problems card: 6-week open-problem trend (weekly samples).
    sample = [(today - timedelta(days=7 * i)).isoformat()
              for i in range(5, -1, -1)]
    pseries = _problem_series(scoped(rows), fetch_status_history(conn, sid, term),
                              sample)

    # Everything card: the term-average trajectory (8 weekly samples) —
    # or, scoped, the one course's own percent trajectory.
    tseries = []
    for day in [(today - timedelta(days=7 * i)).isoformat()
                for i in range(7, -1, -1)]:
        vals = []
        for c in card_courses:
            hrows, from_mark = _grade_history(c, phist, mhist)
            v = (_historic_grade(_value_at(hrows, day), from_mark) if hrows
                 else course_grade(c["mark"], c["percent"])[0])
            if v is not None:
                vals.append(v)
        if vals:
            tseries.append(sum(vals) / len(vals))
    course_pct, course_mark = (
        course_grade(scoped_course["mark"], scoped_course["percent"])
        if scoped_course else (None, ""))

    # Recent card: the last 10 graded scores — the leading indicator.
    pcts10 = [r["score"] / r["points"] * 100
              for r in scoped(graded) if r["points"] and r["score"] is not None][:10]

    ctx = {
        "view": view, "course_gu": course_gu, "hl": hl,
        "strip_open": strip_open, "today": today, "term": term,
        "strip": strip, "deltas": deltas,
        "problems": scoped(problems), "due": scoped(due),
        "recent": scoped(graded),
        "cards": {
            "problems_count": len(scoped(problems)),
            "problems_total": len(problems),
            "problems_week": pseries[-1] - pseries[-2],
            "problems_series": pseries,
            "due_count": len(scoped(due)), "due_total": len(due),
            "due_next": scoped(due)[:2],
            "recent_pcts": pcts10, "term_avg": term_avg,
            "term_series": tseries, "courses": len(strip),
            # Scoped only: the course's grade as the Everything card's story.
            "course": scoped_course, "course_pct": course_pct,
            "course_mark": course_mark,
        },
    }
    if view == "everything":
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


# Alerts page size (numbered paging replaces the old silent 100-row cap).
_ALERTS_PAGE = 50


def fetch_alerts(conn: sqlite3.Connection, page: int = 1,
                 alert_type: str = "") -> list[sqlite3.Row]:
    """One page of alerts, newest first. Offset paging is safe here because
    the sort key is stable between requests; the total (for the page count)
    comes from fetch_alert_counts, which the chips need anyway."""
    sql = ("SELECT al.*, st.name AS student_name "
           "FROM alerts al "
           "JOIN students st ON st.id = al.student_id ")
    params: list = []
    if alert_type:
        sql += "WHERE al.type = ? "
        params.append(alert_type)
    sql += "ORDER BY al.created_at DESC, al.rowid DESC LIMIT ? OFFSET ?"
    params += [_ALERTS_PAGE, (page - 1) * _ALERTS_PAGE]
    return conn.execute(sql, params).fetchall()


def fetch_alert_counts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """(type, n) per alert type present — the type-group chips."""
    return conn.execute(
        "SELECT type, COUNT(*) AS n "
        "FROM alerts GROUP BY type ORDER BY n DESC, type").fetchall()


def alerts_total(counts, alert_type: str = "") -> int:
    """How many alerts the current filter covers, from the chip counts."""
    return sum(c["n"] for c in counts if not alert_type or c["type"] == alert_type)


def alerts_last_page(total: int) -> int:
    return max(1, -(-total // _ALERTS_PAGE))   # ceiling division


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


# History renders every fetched row into the page (the expander only hides
# them), so the fetch IS the page weight: a season of changes is a 700 KB
# page. Each section therefore fetches its newest ``_HISTORY_LIMIT`` rows
# and links to ``?all=1`` for the rest; the chips still count true totals.
_HISTORY_LIMIT = 300
_HISTORY_ALL = 20000


def fetch_course_history(conn: sqlite3.Connection, limit: int = _HISTORY_LIMIT, *,
                         course: str = "", field: str = "") -> list[sqlite3.Row]:
    clause, params = _history_filter(course, field)
    return conn.execute(
        "SELECT h.*, c.title AS course_title, c.term, c.edupoint_gu AS course_gu, "
        "       st.name AS student_name, st.agu AS student_agu "
        "FROM course_history h "
        "JOIN courses c ON c.id = h.course_id "
        "JOIN students st ON st.id = c.student_id " + clause
        + "ORDER BY h.seen_at DESC, h.id DESC LIMIT ?", (*params, limit)
    ).fetchall()


def fetch_history(conn: sqlite3.Connection, limit: int = _HISTORY_LIMIT, *,
                  course: str = "", field: str = "") -> list[sqlite3.Row]:
    clause, params = _history_filter(course, field)
    return conn.execute(
        "SELECT h.*, a.name AS assignment_name, a.points AS cur_points, "
        "       c.title AS course_title, c.edupoint_gu AS course_gu, "
        "       st.name AS student_name, st.agu AS student_agu "
        "FROM grade_history h "
        "JOIN assignments a ON a.id = h.assignment_id "
        "JOIN courses c ON c.id = a.course_id "
        "JOIN students st ON st.id = c.student_id " + clause
        + "ORDER BY h.seen_at DESC, h.id DESC LIMIT ?", (*params, limit)
    ).fetchall()


def fetch_history_totals(conn: sqlite3.Connection, *, course: str = "",
                         field: str = "") -> tuple[int, int]:
    """(assignment rows, course rows) the current filter covers — what the
    capped fetches would return uncapped."""
    clause, params = _history_filter(course, field)
    assignments = conn.execute(
        "SELECT COUNT(*) FROM grade_history h "
        "JOIN assignments a ON a.id = h.assignment_id "
        "JOIN courses c ON c.id = a.course_id " + clause, params).fetchone()[0]
    courses = conn.execute(
        "SELECT COUNT(*) FROM course_history h "
        "JOIN courses c ON c.id = h.course_id " + clause, params).fetchone()[0]
    return assignments, courses


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


