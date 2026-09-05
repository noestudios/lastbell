"""Dashboard: routing + rendering against a real (temp) database."""
from __future__ import annotations

import datetime

import pytest

from lastbell import dashboard, store, watchers
from lastbell.differ import Event
from lastbell.models import (
    AlertType,
    Assignment,
    AssignmentStatus,
    Course,
    Snapshot,
    Student,
    WatcherKind,
)


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "test.db")
    store.ensure_schema(c)
    yield c
    c.close()


@pytest.fixture
def populated(conn):
    snap = Snapshot(
        student_agu="1",
        courses=[Course(edupoint_gu="709775", title="Math <Adv>", teacher="Pat Example",
                        term="MP1", mark="B+", percent="87.20%")],
        assignments=[
            Assignment(edupoint_gu="a1", course_gu="709775", name="Fractions Quiz",
                       kind="Assessment", due_date=datetime.date(2026, 9, 12),
                       score=8.0, points=10.0, status=AssignmentStatus.GRADED),
            Assignment(edupoint_gu="a2", course_gu="709775", name="Collage",
                       status=AssignmentStatus.MISSING),
        ],
    )
    store.persist_snapshot(
        conn, Student(agu="1", name="Jasper P. Hays", school="Example ES",
                      initials="J.P.H."), snap)
    return conn


def _get(conn, path):
    return dashboard._handle(conn, path)


def test_overview_empty_db(conn):
    status, html = _get(conn, "/")
    assert status == 200
    assert "No students yet" in html


def test_overview_lists_students_and_flags(populated):
    status, html = _get(populated, "/")
    assert status == 200
    assert "Jasper P. Hays" in html
    assert "Math &lt;Adv&gt;" in html          # escaped
    assert "1 missing" in html
    assert ">87.2<" in html          # one-decimal display of the raw "87.20%"


def test_student_page_defaults_to_problems_view(populated):
    """C0: the student page opens on Problems — missing + ungraded past due;
    graded work lives in the Recent/Everything views."""
    status, html = _get(populated, "/student/1")
    assert status == 200
    assert "Needs attention" in html
    assert "Collage" in html and "MISSING" in html
    assert "Fractions Quiz" not in html


def test_student_everything_view_shows_assignments(populated):
    status, html = _get(populated, "/student/1?view=everything")
    assert status == 200
    assert "Fractions Quiz" in html
    assert "80.0%" in html                    # score as a percentage…
    assert "data-tip='8/10'" in html          # …raw points in a styled tooltip
    assert "87.2% · B+" in html               # course heading, one decimal
    assert "MISSING" in html


def test_unknown_student_404s(populated):
    status, html = _get(populated, "/student/999")
    assert status == 404


# ── C0: four views, stat cards, course strip ──────────────────────────


def _persist(conn, agu, courses, assignments, term="", name="Jasper P. Hays"):
    snap = Snapshot(student_agu=agu, courses=courses,
                    assignments=assignments, term=term)
    store.persist_snapshot(
        conn, Student(agu=agu, name=name, school="Example ES"), snap)


def test_view_switcher_is_stat_cards(populated):
    _, html = _get(populated, "/student/1")
    for view in ("problems", "due", "recent", "everything"):
        assert f"href='/student/1?view={view}'" in html
    # the active view's card carries the accent-border class
    assert "<a class='stat active' aria-current='true' href='/student/1?view=problems'" in html
    _, html = _get(populated, "/student/1?view=recent")
    assert "<a class='stat active' aria-current='true' href='/student/1?view=recent'" in html


def test_unknown_view_falls_back_to_problems(populated):
    status, html = _get(populated, "/student/1?view=nonsense")
    assert status == 200
    assert "<a class='stat active' aria-current='true' href='/student/1?view=problems'" in html


def test_problems_all_clear_with_due_soon_peek(conn):
    today = datetime.date.today()
    _persist(conn, "1",
             [Course(edupoint_gu="c1", title="Math", term="MP1")],
             [Assignment(edupoint_gu="a1", course_gu="c1", name="Quiz 1",
                         score=9.0, points=10.0, status=AssignmentStatus.GRADED),
              Assignment(edupoint_gu="a2", course_gu="c1", name="Homework 5",
                         due_date=today + datetime.timedelta(days=2),
                         status=AssignmentStatus.DUE)],
             term="MP1")
    _, html = _get(conn, "/student/1")
    assert "Nothing needs attention" in html
    assert "?view=recent'>See what came in recently" in html
    assert "Homework 5" in html            # the due-soon peek below


def test_due_view_lists_open_work_soonest_first(conn):
    today = datetime.date.today()
    _persist(conn, "1",
             [Course(edupoint_gu="c1", title="Math", term="MP1")],
             [Assignment(edupoint_gu="a1", course_gu="c1", name="Later",
                         due_date=today + datetime.timedelta(days=5),
                         status=AssignmentStatus.DUE),
              Assignment(edupoint_gu="a2", course_gu="c1", name="Sooner",
                         due_date=today + datetime.timedelta(days=1),
                         status=AssignmentStatus.DUE)],
             term="MP1")
    _, html = _get(conn, "/student/1?view=due")
    assert html.index("Sooner") < html.index("Later")


def test_recent_view_groups_by_day_and_falls_back_to_history(populated):
    """graded_at comes from the seeder only — the live collector doesn't
    supply it, so the grade's date falls back to the score's first
    grade_history row."""
    snap = Snapshot(
        student_agu="1",
        courses=[Course(edupoint_gu="709775", title="Math <Adv>", term="MP1")],
        assignments=[Assignment(edupoint_gu="a1", course_gu="709775",
                                name="Fractions Quiz", score=9.0, points=10.0,
                                kind="Assessment",
                                status=AssignmentStatus.GRADED)],
    )
    store.persist_snapshot(populated, Student(agu="1", name="Jasper P. Hays"), snap)
    _, html = _get(populated, "/student/1?view=recent")
    assert "Fractions Quiz" in html
    assert ">Today</td>" in html            # regrade landed just now
    assert "90.0%" in html and "9/10" in html


def test_course_strip_scopes_the_active_view(conn):
    _persist(conn, "1",
             [Course(edupoint_gu="g1", title="Art", term="MP1", percent="90.00%",
                     mark="A"),
              Course(edupoint_gu="g2", title="Math", term="MP1", percent="72.00%",
                     mark="C")],
             [Assignment(edupoint_gu="a1", course_gu="g1", name="Collage",
                         status=AssignmentStatus.MISSING),
              Assignment(edupoint_gu="a2", course_gu="g2", name="Worksheet",
                         status=AssignmentStatus.MISSING)],
             term="MP1")
    _, html = _get(conn, "/student/1")
    assert "table class='strip'" in html
    assert "Collage" in html and "Worksheet" in html
    # the strip is an All Courses collapsible ABOVE the stat cards,
    # collapsed by default (owner's call 2026-09-01)
    assert "<details class='allcourses' id='allcourses'><summary>" in html
    assert "All Courses" in html
    assert html.index("allcourses") < html.index("class='stats'")
    # course names must read as filters: underline styling rides the CSS,
    # and each link carries the funnel icon
    assert html.count("class='filtericon'") == 2
    assert "points='22 3 2 3" in html               # the funnel polygon
    # scoping to Math filters the list; the strip row marks the filter,
    # and the strip holds itself open so the active scope stays visible
    _, html = _get(conn, "/student/1?course=g2")
    assert "Worksheet" in html and "Collage" not in html
    assert "class='scoped'" in html
    assert "<details class='allcourses' id='allcourses' open>" in html
    # the scoped row's icon flips to an × — click again to clear
    assert "x1='18' y1='6'" in html
    # the summary names the active filter — legible collapsed, and itself
    # the clear control (funnel · course · small ×)
    assert ("<a class='striptag' "
            "href='/student/1?view=problems&strip=open'") in html
    tag = html.split("class='striptag'")[1].split("</a>")[0]
    assert "Math" in tag
    assert "x1='18' y1='6'" in tag              # the little ×
    _, unscoped = _get(conn, "/student/1")
    assert "striptag" not in unscoped
    # the clear link carries strip=open — the JS-off fallback for "deselecting
    # doesn't collapse the bar the reader is working in"
    assert ("href='/student/1?view=problems&strip=open' "
            "data-tip='show all courses'") in html
    _, html2 = _get(conn, "/student/1?view=problems&strip=open")
    assert "<details class='allcourses' id='allcourses' open>" in html2
    # stat-card links carry the scope along
    assert "href='/student/1?view=due&course=g2'" in html


def test_all_courses_state_is_the_readers_alone(conn):
    """Only a manual toggle moves the strip: the saved per-browser choice is
    the sole authority with JS (applied inline, both directions, before
    paint); the server's open attribute is the JS-off fallback."""
    _persist(conn, "1",
             [Course(edupoint_gu="g1", title="Art", term="MP1"),
              Course(edupoint_gu="g2", title="Math", term="MP1")],
             [], term="MP1")
    _, html = _get(conn, "/student/1")
    assert "id='allcourses'" in html
    # applied synchronously right after the element, saved on user toggles
    strip_js = html.rsplit("</details>", 1)[1]     # after the strip itself
    assert "localStorage.getItem('lastbell-courses')==='open'" in strip_js
    assert "addEventListener('toggle'" in strip_js
    assert "setItem('lastbell-courses'" in strip_js


def test_single_course_student_skips_the_strip(populated):
    _, html = _get(populated, "/student/1")
    assert "table class='strip'" not in html


def test_everything_collapses_graded_backlog(conn):
    today = datetime.date.today()
    graded = [Assignment(edupoint_gu=f"a{i}", course_gu="c1", name=f"Quiz {i}",
                         due_date=today - datetime.timedelta(days=30 - i),
                         score=8.0, points=10.0,
                         status=AssignmentStatus.GRADED)
              for i in range(8)]
    _persist(conn, "1", [Course(edupoint_gu="c1", title="Math", term="MP1")],
             graded, term="MP1")
    _, html = _get(conn, "/student/1?view=everything")
    assert "details class='more'" in html
    assert "Show all 8 graded" in html
    # History's geometry: ALL rows live in the one table (the backlog in a
    # hidden tbody.overflow), and the toggle sits after it — so expanding
    # continues the list and the control ends up at the bottom, never
    # wedged mid-table.
    assert "<tbody class='overflow' id='overflow-graded-" in html
    assert html.index("Quiz 7") < html.index("Quiz 0")          # newest 5 first
    assert html.index("Quiz 0") < html.index("details class='more'")
    card = html[html.index("tablecard"):]
    card = card[:card.index("</div>")]
    assert card.count("<table") == 1                # one table, one header
    assert card.count("<tr class='head'>") == 1
    # the open state relabels the toggle "Show less" (CSS swaps the spans)
    assert "<span class='when-open'>Show less</span>" in card


def test_everything_closed_term_collapses_to_finals_line(conn):
    _persist(conn, "1",
             [Course(edupoint_gu="c1", title="1: Math A", term="MP1",
                     percent="88.00%", mark="B"),
              Course(edupoint_gu="c1", title="1: Math A", term="MP2",
                     percent="91.00%", mark="A")],
             [], term="MP2")
    _, html = _get(conn, "/student/1?view=everything")
    assert "MP2 — current" in html
    assert "details class='closedterm'" in html
    assert "finals:" in html and "Math A 88.0 B" in html


def test_overview_badges_deep_link_into_views(populated):
    """Badges deep-link into the views, carrying the ?status= highlight and
    the #hit scroll anchor (the Phase C treatment)."""
    _, html = _get(populated, "/")
    assert ("<a href='/student/1?view=problems&status=missing#hit'>"
            "<span class='badge bad'>1 missing</span></a>") in html


def test_alerts_page(populated):
    store.record_alert(populated, "1", Event(
        type=AlertType.GRADE_CHANGED, student_agu="1", course_title="Math",
        detail="Math: “Fractions Quiz” graded: 8/10"))
    status, html = _get(populated, "/alerts")
    assert status == 200
    assert "grade changed" in html
    assert "Fractions Quiz" in html


def test_history_page(populated):
    # regrade -> one history row
    snap = Snapshot(
        student_agu="1",
        courses=[Course(edupoint_gu="709775", title="Math <Adv>", term="MP1")],
        assignments=[Assignment(edupoint_gu="a1", course_gu="709775",
                                name="Fractions Quiz", score=9.0, points=10.0,
                                due_date=datetime.date(2026, 9, 12), kind="Assessment",
                                status=AssignmentStatus.GRADED)],
    )
    store.persist_snapshot(populated, Student(agu="1", name="Jasper P. Hays"), snap)
    status, html = _get(populated, "/history")
    assert status == 200
    assert "8/10 → 9/10" in html      # score and points read as one grade
    # the assignment links to the student page scoped to its class
    assert "href='/student/1?view=everything&course=709775'>Fractions Quiz</a>" in html
    assert "<table class='history' aria-label='Assignments'>" in html   # style.css underlines its links


def test_settings_page(populated):
    w = watchers.add_watcher(populated, "Mom", WatcherKind.GUARDIAN,
                             {"email": {"to": "mom@example.com"}})
    watchers.subscribe(populated, w, "1")
    status, html = _get(populated, "/settings")
    assert status == 200
    assert "Mom" in html
    assert "all configured" in html
    # a real web UI: forms for every write path, not CLI listings
    assert "lastbell watcher" not in html
    assert "lastbell subscribe" not in html
    for action in ("watcher-add", "watcher-remove", "channel", "channel-remove",
                   "subscribe", "subscription-update", "unsubscribe"):
        assert f"action='/settings/{action}'" in html
    # env-owned config is absent: if it can't be changed here, don't show it
    assert "LASTBELL_POLL_MINUTES" not in html
    assert "Polling" not in html
    # quiet hours are descoped from the web UI (CLI only)
    assert "quiet" not in html.lower()
    # subscribe form offers the one-step all-students option
    assert "all students" in html
    # add forms come above their tables
    assert (html.index("action='/settings/watcher-add'")
            < html.index("<table class='manage' aria-label='Watchers'>"))
    assert (html.index("action='/settings/subscribe'")
            < html.index("<table class='manage' aria-label='Subscriptions'>",
                        html.index("Subscriptions")))
    # the web UI offers exactly one channel: email (text message withdrawn
    # in 0.1.5 — the carrier gateways are gone)
    assert "text message" not in html
    assert "vtext" not in html
    for gone in ("ntfy", "telegram", "pushover"):
        assert f"<option value='{gone}'>" not in html
    # the quiet footer credit: © + repo + license links, and it sits outside
    # #settings-main so fetch-based form posts never repaint it
    assert "© 2026" in html
    assert "https://github.com/noestudios/lastbell'" in html
    assert "https://github.com/noestudios/lastbell/blob/main/LICENSE'" in html
    assert html.index("class='credit'") > html.index("id='settings-main'")


def test_settings_channels_are_rows_under_their_watcher(populated):
    w = watchers.add_watcher(populated, "Mom", WatcherKind.GUARDIAN,
                             {"email": {"to": "mom@example.com"}})
    _, html = _get(populated, "/settings")
    # the channel row: name cell, address input preloaded, bound to a row form
    assert "<td class='chname'>email</td>" in html
    assert "value='mom@example.com'" in html
    assert f"form='ch-{w.id}-email'" in html
    # the add-channel row offers only channels the watcher doesn't have yet —
    # and email is the only channel the web UI offers at all (text message
    # was withdrawn in 0.1.5)
    assert f"chadd-{w.id}" not in html
    assert "<option value='sms'>" not in html
    assert "vtext" not in html


def test_watchers_url_redirects_to_settings(populated):
    status, target = _get(populated, "/watchers")
    assert (status, target) == (301, "/settings")


def test_unknown_path_404s(conn):
    status, _ = _get(conn, "/nope")
    assert status == 404


def test_settings_subscription_row_preselects_current_values(populated):
    conn = populated
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                             {"email": {"to": "m@x.com"}})
    watchers.subscribe(conn, w, "1", ["grade_changed"], ["email"], send_at="17:00")
    _, html = _get(conn, "/settings")
    # the row is editable: current values preloaded in its controls
    assert "value='17:00'" in html
    assert " checked> grade changed" in html   # its checkbox is preselected
    # single-type group: the multiselect summary names it (and states its
    # purpose for AT — the visible value alone doesn't)
    assert ("<summary aria-label='Alert types: grade changed'>"
            "grade changed</summary>") in html


def test_stylesheet_exists_and_is_linked(populated):
    from lastbell.dashboard import _STYLE_PATH

    css = _STYLE_PATH.read_text(encoding="utf-8")
    assert ":root" in css and "--accent" in css and "--bg" in css
    _, html = _get(populated, "/")
    assert "/static/style.css" in html


def test_theme_toggle_present_and_css_supports_override(populated):
    from lastbell.dashboard import _STYLE_PATH

    _, html = _get(populated, "/")
    assert "id='themetoggle'" in html
    assert "lastbell-theme" in html      # localStorage key in the script
    css = _STYLE_PATH.read_text(encoding="utf-8")
    assert ':root[data-theme="dark"]' in css
    assert ':root:not([data-theme="light"])' in css


def test_history_page_includes_course_grade_changes(populated):
    conn = populated
    snap = Snapshot(
        student_agu="1",
        courses=[Course(edupoint_gu="709775", title="Math <Adv>", teacher="Pat Example",
                        term="MP1", mark="A-", percent="91.00%")],
    )
    store.persist_snapshot(conn, Student(agu="1", name="Jasper P. Hays"), snap)
    status, html = _get(conn, "/history")
    assert status == 200
    assert "Course grades" in html
    assert "87.20% → 91.00%" in html
    assert "B+ → A-" in html


def _persist_change(conn, *, score_from, score_to, pct_from, pct_to,
                    mark_from, mark_to, title="Algebra"):
    """Baseline then a changed snapshot, so the differ logs history rows:
    one assignment score change + one course percent + one course mark."""
    def snap(score, pct, mark):
        return Snapshot(
            student_agu="1",
            courses=[Course(edupoint_gu="c1", title=title, term="MP1",
                            mark=mark, percent=pct)],
            assignments=[Assignment(edupoint_gu="a1", course_gu="c1", name="Quiz 1",
                                    score=score, points=10.0,
                                    status=AssignmentStatus.GRADED)])
    who = Student(agu="1", name="Kid One")
    store.persist_snapshot(conn, who, snap(score_from, pct_from, mark_from))
    store.persist_snapshot(conn, who, snap(score_to, pct_to, mark_to))


def test_history_filters_by_class_and_change(conn):
    from urllib.parse import quote
    _persist_change(conn, score_from=7.0, score_to=9.0,
                    pct_from="85.00%", pct_to="92.00%", mark_from="B", mark_to="A")

    # Both filter rows render, with chips + counts.
    _, html = _get(conn, "/history")
    assert "filterlabel'>Class" in html and "filterlabel'>Change" in html
    assert "Algebra <b>" in html          # class chip
    assert "score <b>" in html            # change chip (assignment field)
    assert "grade % <b>" in html          # change chip (course percent, humanized)

    # Filter to score changes: Assignments shows, Course grades drops out.
    _, html = _get(conn, "/history?field=score")
    assert "7/10 → 9/10" in html and "Course grades" not in html
    assert "class='chip active' aria-current='true' href='/history?field=score'" in html

    # Filter to percent (course) changes: the reverse.
    _, html = _get(conn, "/history?field=percent")
    assert "85.00% → 92.00%" in html and "Assignments" not in html
    assert "href='/student/1?view=everything&course=c1'>Algebra</a>" in html

    # Class + change compose, preserving each other in the links.
    _, html = _get(conn, "/history?course=" + quote("Algebra") + "&field=score")
    assert "7/10 → 9/10" in html

    # An active filter that matches nothing keeps the chips and says so.
    _, html = _get(conn, "/history?course=" + quote("Nonexistent"))
    assert "No changes match this filter." in html
    assert "filterlabel'>Class" in html


def test_history_groups_one_poll_into_one_row(conn):
    """A grade lands as three audit rows (status, score, points); the page
    shows one line. A status change on its own reads as badge words, and a
    due-date change stays its own row."""
    who = Student(agu="1", name="Kid")
    course = [Course(edupoint_gu="c1", title="Algebra", term="MP1")]

    def snap(**kw):
        return Snapshot(student_agu="1", courses=course, assignments=[
            Assignment(edupoint_gu="a1", course_gu="c1", name="Quiz 1", **kw)])

    store.persist_snapshot(conn, who, snap(status=AssignmentStatus.DUE,
                                           due_date=datetime.date(2026, 9, 6)))
    store.persist_snapshot(conn, who, snap(status=AssignmentStatus.UNGRADED_PAST_DUE,
                                           due_date=datetime.date(2026, 9, 8)))
    store.persist_snapshot(conn, who, snap(status=AssignmentStatus.GRADED,
                                           score=8.0, points=10.0,
                                           graded_at=datetime.date(2026, 9, 9),
                                           due_date=datetime.date(2026, 9, 8)))
    _, html = _get(conn, "/history")
    section = html[html.index("Assignments <span"):]
    assert "Assignments <span class='small'>3</span>" in html    # 6 audit rows
    assert "graded on" not in section   # the graded-on date rides the grade row
    assert section.count("<tr>") == 3
    assert "<td data-label='Change'>graded</td><td data-label='From → To'>— → 8/10</td>" in html
    assert "due → ungraded past due" in html and "ungraded_past_due" not in html
    assert "2026-09-06 → 2026-09-08" in html
    assert "<td data-label='Change'>points</td>" not in html
    assert "due → graded" not in html
    # the chips still count audit rows, and a field filter shows its rows
    # with the denominator filled in
    _, html = _get(conn, "/history?field=score")
    assert "— → 8/10" in html and "<td data-label='Change'>graded</td>" in html
    _, html = _get(conn, "/history?field=points")
    assert "<td data-label='Change'>points</td><td data-label='From → To'>— → 10</td>" in html


def test_history_caps_section_with_expander(conn):
    n = dashboard._HISTORY_PREVIEW + 4

    def assigns(score):
        return [Assignment(edupoint_gu=f"a{i}", course_gu="c1", name=f"Quiz {i}",
                           score=score, points=10.0, status=AssignmentStatus.GRADED)
                for i in range(n)]

    who = Student(agu="1", name="Kid")
    course = [Course(edupoint_gu="c1", title="Algebra", term="MP1")]
    store.persist_snapshot(conn, who, Snapshot(student_agu="1", courses=course,
                                               assignments=assigns(5.0)))
    store.persist_snapshot(conn, who, Snapshot(student_agu="1", courses=course,
                                               assignments=assigns(9.0)))

    _, html = _get(conn, "/history")
    assert f"Assignments <span class='small'>{n}</span>" in html   # count in heading
    assert "details class='more'" in html and f"Show all {n}" in html
    assert "<span class='when-open'>Show less</span>" in html
    # Expanding continues the SAME table: overflow rows in a hidden tbody,
    # not a second table with a repeated header.
    assert "<tbody class='overflow' id='overflow-assignments'>" in html
    section = html[html.index("Assignments <span"):]
    section = section[:section.index("</div>")]
    assert section.count("<table") == 1
    assert section.count("<tr class='head'>") == 1


def test_responsive_markup_hooks(populated):
    _, html = _get(populated, "/student/1")
    assert "<tr class='head'>" in html          # hideable header rows
    assert "data-label='Due'" in html           # stacked-mode cell labels
    _, home = _get(populated, "/")
    assert home.count("<svg") >= 4              # nav icons for narrow widths
    assert "class='lbl'" in home


def test_nav_links_students_by_name_on_every_page(populated):
    """Decision 2: student names are direct nav links; the redundant
    top-level Students item is gone (the brand covers the overview)."""
    for path in ("/", "/student/1", "/alerts", "/history", "/settings",
                 "/student/999"):          # even the 404 keeps the nav whole
        _, html = _get(populated, path)
        nav = html.split("</nav>")[0]
        assert "class='navstudents'" in nav, path
        # first name inline, full name on the title attribute
        assert ">Jasper</a>" in nav, path
        assert "data-tip='Jasper P. Hays'" in nav, path
        assert "href='/student/1'" in nav, path
        assert ">Students</span>" not in nav, path   # old nav item is gone


def test_nav_student_menu_for_narrow_widths(populated):
    from lastbell.dashboard import _STYLE_PATH

    _, html = _get(populated, "/")
    nav = html.split("</nav>")[0]
    assert "details class='smenu'" in nav       # icon menu markup
    assert "aria-label='Students'" in nav
    assert "Jasper P. Hays</a>" in nav          # menu carries full names
    css = _STYLE_PATH.read_text(encoding="utf-8")
    assert "nav .navstudents { display: none; }" in css     # collapse rule
    assert "nav details.smenu { display: inline-block; }" in css


def test_nav_gear_right_alignment_survives_the_cascade():
    """The gear's `margin-left: auto` (what pushes gear+theme to the nav's
    right edge) must come AFTER the hit-area rule whose `margin` shorthand
    would otherwise reset it at equal specificity — a regression that once
    left the icons squashed against the page links."""
    from lastbell.dashboard import _STYLE_PATH

    css = _STYLE_PATH.read_text(encoding="utf-8")
    push = css.index("nav a.gear { margin-left: auto; }")
    hit_area = css.index("padding: 0.35rem; margin: -0.35rem;")
    assert hit_area < push


def test_nav_first_names_fall_back_to_full_on_collision():
    from lastbell.dashboard import _nav_names

    rows = [{"name": "Jasper P. Hays"}, {"name": "Willa R. Hays"}]
    assert _nav_names(rows) == ["Jasper", "Willa"]
    rows = [{"name": "Jasper P. Hays"}, {"name": "Jasper Q. Hays"}]
    assert _nav_names(rows) == ["Jasper P. Hays", "Jasper Q. Hays"]


# ── settings write paths (POST /settings/<action>) ────────────────────


def _post(conn, action, **fields):
    """Lists become repeated form values (e.g. the type multiselect)."""
    return dashboard._handle_settings_post(
        conn, action,
        {k: (list(v) if isinstance(v, (list, tuple)) else [v])
         for k, v in fields.items()})


def _ok(result):
    """Assert a success redirect (?ok= toast message); return the target."""
    status, target = result
    assert status == 303 and target.startswith("/settings?ok=")
    return target


def test_settings_add_and_remove_watcher(populated):
    conn = populated
    target = _ok(_post(conn, "watcher-add", name="Mom", kind="guardian",
                       channel="email", to="mom@example.com"))
    w = watchers.get_watcher(conn, "Mom")
    assert w.channels == {"email": {"to": "mom@example.com"}}
    # the redirect names the new rows so the client can animate them in
    assert f"new=row-w-{w.id},row-chadd-{w.id}" in target
    _ok(_post(conn, "watcher-remove", name="Mom"))
    assert watchers.list_watchers(conn) == []


def test_settings_add_watcher_without_channel(populated):
    _ok(_post(populated, "watcher-add", name="Dad", kind="guardian",
              channel="", to=""))
    assert watchers.get_watcher(populated, "Dad").channels == {}


def test_settings_channel_add_update_and_remove(populated):
    conn = populated
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    target = _ok(_post(conn, "channel", watcher="Mom", channel="ntfy",
                       to="topic-1"))
    assert f"new=row-ch-{w.id}-ntfy" in target        # an add animates in…
    assert watchers.get_watcher(conn, "Mom").channels == {"ntfy": {"topic": "topic-1"}}
    target = _ok(_post(conn, "channel", watcher="Mom", channel="ntfy",
                       to="topic-2"))
    assert "new=" not in target                       # …an update doesn't
    assert watchers.get_watcher(conn, "Mom").channels == {"ntfy": {"topic": "topic-2"}}
    _ok(_post(conn, "channel-remove", watcher="Mom", channel="ntfy"))
    assert watchers.get_watcher(conn, "Mom").channels == {}


def test_settings_channel_without_address_is_banner(populated):
    conn = populated
    watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    status, target = _post(conn, "channel", watcher="Mom", channel="ntfy", to="")
    assert status == 303 and "err=" in target
    assert watchers.get_watcher(conn, "Mom").channels == {}


def test_settings_subscribe_and_unsubscribe(populated):
    conn = populated
    watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    target = _ok(_post(conn, "subscribe", watcher="Mom", student="1",
                       type="grade_changed", channel="*", at="17:00"))
    (sub,) = watchers.list_subscriptions(conn)
    assert (sub.alert_type, sub.channel, sub.send_at) == ("grade_changed", "*", "17:00")
    assert f"new=row-sub-{sub.id}" in target
    _ok(_post(conn, "unsubscribe", ids=sub.id))
    assert watchers.list_subscriptions(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM watcher_student").fetchone()[0] == 0


def test_settings_error_redirects_with_message_and_renders_banner(populated):
    conn = populated
    status, target = _post(conn, "watcher-add", name="", kind="guardian",
                           channel="", to="")
    assert status == 303 and target.startswith("/settings?err=")
    status, html = _get(conn, target)
    assert status == 200
    assert "class='banner bad'" in html and "needs a name" in html


def test_settings_duplicate_watcher_is_a_banner_not_a_crash(populated):
    conn = populated
    watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    status, target = _post(conn, "watcher-add", name="mom", kind="guardian",
                           channel="", to="")
    assert status == 303 and "err=" in target


def test_settings_unknown_action_404s(populated):
    status, _ = _post(populated, "nope")
    assert status == 404


def test_settings_subscribe_all_students(populated):
    conn = populated
    store.persist_snapshot(
        conn, Student(agu="2", name="Lilou Hays", school="Example ES",
                      initials="L.H."), Snapshot(student_agu="2"))
    watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    _ok(_post(conn, "subscribe", watcher="Mom", student="*",
              type="*", channel="*", at=""))
    subs = watchers.list_subscriptions(conn)
    assert sorted(s.student_name for s in subs) == ["Jasper P. Hays", "Lilou Hays"]
    assert all(s.alert_type == "*" and s.channel == "*" for s in subs)


def test_settings_subscription_update_in_place(populated):
    conn = populated
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    watchers.subscribe(conn, w, "1")
    (sub,) = watchers.list_subscriptions(conn)
    _ok(_post(conn, "subscription-update", ids=sub.id,
              type=["grade_changed", "grade_drop"], channel="email", at="17:00"))
    subs = watchers.list_subscriptions(conn)
    assert sorted(s.alert_type for s in subs) == ["grade_changed", "grade_drop"]
    assert all((s.channel, s.send_at) == ("email", "17:00") for s in subs)
    # …deselecting one type deletes its row; back to wildcard collapses to one
    _ok(_post(conn, "subscription-update", ids=",".join(s.id for s in subs),
              type="*", channel="*", at=""))
    (sub,) = watchers.list_subscriptions(conn)
    assert (sub.alert_type, sub.channel, sub.send_at) == ("*", "*", None)


def test_settings_subscription_update_duplicate_is_banner(populated):
    conn = populated
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    watchers.subscribe(conn, w, "1", ["grade_changed"])
    watchers.subscribe(conn, w, "1", ["grade_drop"])
    target = next(s for s in watchers.list_subscriptions(conn)
                  if s.alert_type == "grade_drop")
    status, redirect = _post(conn, "subscription-update", ids=target.id,
                             type="grade_changed", channel="*", at="")
    assert status == 303 and "err=" in redirect
    assert "identical" in redirect


def test_settings_success_renders_toast(populated):
    watchers.add_watcher(populated, "Mom", WatcherKind.GUARDIAN)
    status, html = _get(populated, "/settings?ok=Channel+removed")
    assert status == 200
    assert "class='toast'" in html and "Channel removed" in html
    # errors stay a banner, not a toast
    _, html = _get(populated, "/settings")
    assert "class='toast'" not in html


def test_settings_rows_carry_ids_and_gated_update_buttons(populated):
    conn = populated
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                             {"email": {"to": "m@x.com"}})
    watchers.subscribe(conn, w, "1")
    (sub,) = watchers.list_subscriptions(conn)
    _, html = _get(conn, "/settings")
    assert f"id='row-w-{w.id}'" in html
    assert f"id='row-ch-{w.id}-email'" in html
    assert f"id='row-sub-{sub.id}'" in html
    # watcher-remove form names its row group so channels animate out with it
    assert f"data-group='{w.id}'" in html
    # Update buttons are class 'upd' (hidden until app.js marks the form dirty)
    assert "<button class='upd'>Update</button>" in html


def test_app_js_exists_and_is_linked(populated):
    from lastbell.dashboard import _APPJS_PATH

    js = _APPJS_PATH.read_text(encoding="utf-8")
    assert "toast" in js and "dirty" in js and "prefers-reduced-motion" in js
    # fetch-and-swap (no page reload) and the removal confirmation dialog
    assert "settings-main" in js and "fetch(" in js
    assert ">Cancel<" in js and ">Remove<" in js and "last subscription" in js
    _, html = _get(populated, "/")
    assert "/static/app.js" in html
    # the student page's view cards and course filters switch in place:
    # strip, cards and panel share one region the script swaps, history
    # follows, and the strip's toggle keeps saving after a swap
    assert "student-main" in js and "pushState" in js and "popstate" in js
    assert '"toggle"' in js and "lastbell-courses" in js
    _, html = _get(populated, "/student/1?view=due")
    assert "<div id='student-main'><div class='stats'>" in html   # one course: no strip
    assert html.index("<div id='student-main'>") < html.index("class='stats'")


def test_settings_subscribe_form_defaults_to_4pm_digest_with_urgent(populated):
    watchers.add_watcher(populated, "Mom", WatcherKind.GUARDIAN)
    _, html = _get(populated, "/settings")
    form = html[html.index("action='/settings/subscribe'"):]
    form = form[:form.index("</form>")]
    assert "value='16:00'" in form
    assert "name='urgent' checked" in form


def test_settings_subscribe_and_update_carry_urgent_flag(populated):
    conn = populated
    watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    _ok(_post(conn, "subscribe", watcher="Mom", student="1",
              type="*", channel="*", at="16:00", urgent="on"))
    (sub,) = watchers.list_subscriptions(conn)
    assert sub.urgent_now and sub.send_at == "16:00"
    # unchecking the box on the row clears the flag
    _ok(_post(conn, "subscription-update", ids=sub.id, type="*",
              channel="*", at="16:00"))
    (sub,) = watchers.list_subscriptions(conn)
    assert not sub.urgent_now


def test_settings_page_has_swappable_region(populated):
    _, html = _get(populated, "/settings")
    assert "id='settings-main'" in html
    # the swap region wraps the cards so a fetch-based post can replace it
    assert html.index("id='settings-main'") < html.index("Watchers")


def test_channel_inputs_dodge_street_address_autofill(populated):
    watchers.add_watcher(populated, "Mom", WatcherKind.GUARDIAN,
                         {"email": {"to": "m@x.com"}, "sms": {"to": "5551234567@vtext.com"}})
    _, html = _get(populated, "/settings")
    # never name='address' (browsers autofill it as a street address)
    assert "name='address'" not in html
    assert "placeholder='Address'" not in html
    # email rows invite email autofill; sms rows (carrier gateways) don't
    assert "autocomplete='email'" in html
    assert "autocomplete='off'" in html


def test_gateway_addresses_are_refused_but_legacy_sms_rows_still_work(populated):
    """Text message was withdrawn in 0.1.5: a carrier gateway is refused on
    any channel with the reason, while a pre-0.1.5 sms row can still be
    updated to a real address (it delivers over SMTP) or removed."""
    from urllib.parse import unquote
    conn = populated
    watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                         {"sms": {"to": "3015551234@vtext.com"}})
    status, target = _post(conn, "channel", watcher="Mom", channel="email",
                           to="3015551234@vtext.com")
    assert status == 303 and "Verizon is retiring" in unquote(target)
    status, target = _post(conn, "channel", watcher="Mom", channel="sms",
                           to="3015551234@tmomail.net")
    assert status == 303 and "T-Mobile shut down" in unquote(target)
    # the legacy row still renders (labelled) and can be repointed or removed
    _, html = _get(conn, "/settings")
    assert "<td class='chname'>text message</td>" in html
    _ok(_post(conn, "channel", watcher="Mom", channel="sms", to="mom@example.com"))
    assert watchers.get_watcher(conn, "Mom").channels == {"sms": {"to": "mom@example.com"}}
    _ok(_post(conn, "channel-remove", watcher="Mom", channel="sms"))
    assert watchers.get_watcher(conn, "Mom").channels == {}
def test_email_address_must_look_like_email(populated):
    conn = populated
    status, target = _post(conn, "watcher-add", name="Dad", kind="guardian",
                           channel="email", to="not-an-address")
    assert status == 303 and "err=" in target
    assert watchers.get_watcher(conn, "Dad") is None


# ── Phase C: status signal (tints, icons, cutoff, highlight) ──────────


def test_status_rows_carry_tint_class_and_icon(conn):
    today = datetime.date.today()
    _persist(conn, "1",
             [Course(edupoint_gu="c1", title="Math", term="MP1")],
             [Assignment(edupoint_gu="a1", course_gu="c1", name="Lost",
                         status=AssignmentStatus.MISSING),
              Assignment(edupoint_gu="a2", course_gu="c1", name="Late",
                         due_date=today - datetime.timedelta(days=5),
                         status=AssignmentStatus.UNGRADED_PAST_DUE),
              Assignment(edupoint_gu="a3", course_gu="c1", name="Soon",
                         due_date=today + datetime.timedelta(days=1),
                         status=AssignmentStatus.DUE)],
             term="MP1")
    _, html = _get(conn, "/student/1?view=everything")
    for cls in ("st-missing", "st-late", "st-due"):
        assert f"class='{cls}'" in html
    assert "class='rowicon'" in html
    # graded rows earn no tint class
    _, html = _get(conn, "/student/1?view=problems")
    assert "st-missing" in html and "st-late" in html
    assert "st-due" not in html          # due rows live in their own view


def test_low_score_tints_bad_below_the_global_cutoff(populated, monkeypatch):
    # populated's quiz is 8/10 = 80% — above the default cutoff of 70
    _, html = _get(populated, "/student/1?view=everything")
    assert "tip low" not in html
    monkeypatch.setenv("LASTBELL_SCORE_CUTOFF", "85")
    _, html = _get(populated, "/student/1?view=everything")
    assert "class='tip low' tabindex='0' data-tip='8/10'" in html
    monkeypatch.setenv("LASTBELL_SCORE_CUTOFF", "0")   # 0 disables the tint
    _, html = _get(populated, "/student/1?view=everything")
    assert "tip low" not in html


def test_recent_view_low_score_cell(populated, monkeypatch):
    # a re-persist gives the grade a history row, landing it in Recent
    snap = Snapshot(
        student_agu="1",
        courses=[Course(edupoint_gu="709775", title="Math <Adv>", term="MP1")],
        assignments=[Assignment(edupoint_gu="a1", course_gu="709775",
                                name="Fractions Quiz", score=6.0, points=10.0,
                                kind="Assessment",
                                status=AssignmentStatus.GRADED)],
    )
    store.persist_snapshot(populated, Student(agu="1", name="Jasper P. Hays"), snap)
    _, html = _get(populated, "/student/1?view=recent")
    assert "class='num low'" in html          # 60% < the default 70 cutoff


def test_status_param_highlights_and_anchors_matching_rows(populated):
    _, html = _get(populated, "/student/1?view=problems&status=missing")
    assert "class='st-missing hit' id='hit'" in html
    # without the param — and with a nonsense value — nothing highlights
    _, html = _get(populated, "/student/1?view=problems")
    assert "id='hit'" not in html
    _, html = _get(populated, "/student/1?view=problems&status=nonsense")
    assert "id='hit'" not in html


def test_tooltips_are_styled_not_native(populated):
    from lastbell.dashboard import _STYLE_PATH

    css = _STYLE_PATH.read_text(encoding="utf-8")
    assert "attr(data-tip)" in css
    _, html = _get(populated, "/student/1?view=everything")
    # the score hover is the design-system tooltip, not a title bubble
    assert "data-tip='8/10'" in html and "title='8/10'" not in html


# ── Phase C ripple: the alerts page ───────────────────────────────────


def _alert(conn, detail, type_=AlertType.GRADE_CHANGED):
    store.record_alert(conn, "1", Event(
        type=type_, student_agu="1", course_title="Math", detail=detail))


def test_alerts_when_is_local_date_words_with_tooltip(populated):
    _alert(populated, "Math: quiz graded")
    _, html = _get(populated, "/alerts")
    assert "When (UTC)" not in html
    assert ">today<span class='vh'>" in html  # visible word + SR timestamp suffix            # date words in the cell…
    assert "data-tip='20" in html             # …full local timestamp on hover


def test_alerts_type_chips_group_and_filter(populated):
    _alert(populated, "quiz graded")
    _alert(populated, "quiz graded again")
    _alert(populated, "Math slipped", AlertType.GRADE_DROP)
    _, html = _get(populated, "/alerts")
    assert ">all <b>3</b></a>" in html
    assert "grade changed <b>2</b>" in html
    assert "grade drop <b>1</b>" in html
    # filtering keeps only that type's rows and marks the chip active
    _, html = _get(populated, "/alerts?type=grade_drop")
    assert "Math slipped" in html and "quiz graded" not in html
    assert "class='chip active' aria-current='true' href='/alerts?type=grade_drop'" in html


def test_alerts_are_newest_first(populated):
    conn = populated
    _alert(conn, "older alert")
    _alert(conn, "newer alert")
    _, html = _get(conn, "/alerts")
    assert html.index("newer alert") < html.index("older alert")


def test_alerts_page_numbered_paging_replaces_the_cap(populated):
    for i in range(55):
        _alert(populated, f"alert number {i}")
    _, html = _get(populated, "/alerts")
    assert html.count("data-label='Type'") == 50
    assert "Showing 1–50 of 55 alerts" in html
    # page 1: Previous is a dead (non-focusable) span, Next is a live link,
    # and the current page is marked for both eyes and screen readers
    assert "<span class='prev' aria-disabled='true'>" in html
    assert "<a class='next' rel='next' href='/alerts?page=2'" in html
    assert "<a aria-current='page' href='/alerts'>1</a>" in html
    assert "<title>Alerts — page 1 of 2 · Last Bell</title>" in html
    _, html = _get(populated, "/alerts?page=2")
    assert html.count("data-label='Type'") == 5
    assert "Showing 51–55 of 55 alerts" in html
    assert "<a class='prev' rel='prev' href='/alerts'" in html
    assert "<span class='next' aria-disabled='true'>" in html
    assert "<a aria-current='page' href='/alerts?page=2'>2</a>" in html


def test_alerts_pager_preserves_filter_and_counts_that_type(populated):
    for i in range(52):
        _alert(populated, f"drop {i}", AlertType.GRADE_DROP)
    _alert(populated, "a change")
    _, html = _get(populated, "/alerts?type=grade_drop&page=2")
    assert "Showing 51–52 of 52 grade drop alerts" in html
    pager = html.split("<nav class='pager'")[1].split("</nav>")[0]
    assert "href='/alerts?type=grade_drop'" in pager        # page 1 keeps the filter
    assert "href='/alerts?page=" not in pager               # never drops it
    assert "<nav class='pager' aria-label='Alert pages'>" in html


def test_alerts_single_page_has_range_but_no_pager(populated):
    _alert(populated, "only one")
    _, html = _get(populated, "/alerts")
    assert "Showing all 1 alerts" in html
    assert "class='pager'" not in html
    assert "<title>Alerts · Last Bell</title>" in html


def test_alerts_past_the_end_redirects_to_last_page(populated):
    for i in range(55):
        _alert(populated, f"alert number {i}")
    assert _get(populated, "/alerts?page=999") == (301, "/alerts?page=2")
    _alert(populated, "one drop", AlertType.GRADE_DROP)
    assert _get(populated, "/alerts?type=grade_drop&page=7") == (
        301, "/alerts?type=grade_drop")


def test_alerts_type_cell_is_a_severity_badge(populated):
    _alert(populated, "slipped", AlertType.GRADE_DROP)
    _alert(populated, "gone", AlertType.ASSIGNMENT_MISSING)
    _alert(populated, "late", AlertType.UNGRADED_PAST_DUE)
    _alert(populated, "soon", AlertType.UPCOMING_DEADLINE)
    _alert(populated, "moved", AlertType.GRADE_CHANGED)
    _, html = _get(populated, "/alerts")
    assert "<span class='badge bad'>grade drop</span>" in html
    assert "<span class='badge bad'>assignment missing</span>" in html
    assert "<span class='badge warn'>ungraded past due</span>" in html
    assert "<span class='badge info'>upcoming deadline</span>" in html
    assert "<span class='badge muted'>grade changed</span>" in html


def test_page_window_elides_long_runs_only():
    from lastbell.dashboard import _page_window as w

    assert w(1, 1) == [1]
    assert w(3, 5) == [1, 2, 3, 4, 5]                    # nothing to elide
    assert w(1, 27) == [1, 2, None, 27]
    assert w(6, 27) == [1, None, 5, 6, 7, None, 27]
    assert w(27, 27) == [1, None, 26, 27]
    assert w(3, 27) == [1, 2, 3, 4, None, 27]             # a 1-page gap is shown, not "…"


def test_overview_course_names_deep_link_scoped(populated):
    _, html = _get(populated, "/")
    assert "<a href='/student/1?course=709775'>Math &lt;Adv&gt;</a>" in html


def test_favicon_and_brand_mark(populated):
    """The tab icon is a packaged PNG (installed copies must have it too),
    and the brand link leads with the bell mark drawn in currentColor so it
    survives both themes."""
    from lastbell.dashboard import _FAVICON_PATH

    assert _FAVICON_PATH.is_file() and _FAVICON_PATH.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    _, html = _get(populated, "/")
    assert "<link rel='icon' type='image/png' href='/static/favicon.png'>" in html
    brand = html.split("class='brand'")[1].split("</a>")[0]
    assert "<svg class='mark'" in brand and "stroke='currentColor'" in brand
    assert brand.index("<svg") < brand.index("Last Bell")       # mark before the text
    assert "#EEEBE5" not in brand                                # no hard-coded cream


def test_theme_toggle_is_icon_only(populated):
    """All three state icons ship in the button (CSS shows the one matching
    <html data-theme>, set before first paint — no post-load icon swap),
    and nothing in it is visible text."""
    import re

    _, html = _get(populated, "/")
    btn = html.split("id='themetoggle'")[1].split("</button>")[0]
    assert btn.count("<svg") == 3
    for kind in ("auto", "light", "dark"):
        assert f"<svg class='ic-{kind}'" in btn
    assert re.sub(r"<[^>]+>", "", btn.split(">", 1)[1]).strip() == ""   # icons, not text
    assert "aria-label='Theme: auto'" in btn
    from lastbell.dashboard import _STYLE_PATH
    css = _STYLE_PATH.read_text(encoding="utf-8")
    assert ":root[data-theme='dark'] #themetoggle .ic-dark { display: inline-block; }" in css


def test_no_native_title_hovers_anywhere(populated):
    """Every hover is a design-system data-tip bubble; the browser's native
    title speck appears nowhere in a page body."""
    watchers.add_watcher(populated, "Mom", WatcherKind.GUARDIAN,
                         {"email": {"to": "m@x.com"}})
    for path in ("/", "/student/1", "/alerts", "/history", "/settings"):
        _, html = _get(populated, path)
        assert "title='" not in html.split("</head>")[1], path


def test_settings_toasts_name_who_and_what(populated):
    from urllib.parse import unquote

    conn = populated
    watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                         {"email": {"to": "mom@x.com"}})
    _, target = _post(conn, "channel-remove", watcher="Mom", channel="email")
    assert "Removed Mom's email (mom@x.com)" in unquote(target)
    target = _ok(_post(conn, "subscribe", watcher="Mom", student="1",
                       type="*", channel="*", at=""))
    assert "Subscribed Mom to Jasper P. Hays" in unquote(target)
    (sub,) = watchers.list_subscriptions(conn)
    target = _ok(_post(conn, "unsubscribe", ids=sub.id))
    assert "Unsubscribed Mom from Jasper P. Hays" in unquote(target)
    target = _ok(_post(conn, "watcher-remove", name="Mom"))
    assert "Removed watcher Mom" in unquote(target)


def test_school_name_links_when_resolvable(populated, monkeypatch):
    """When the MCPS directory resolves the school, its name becomes a new-tab
    link (with the 'Visit school website' tip) on both the overview and the
    student page."""
    monkeypatch.setattr(dashboard.schools, "school_url",
                        lambda name: "https://bcc-hs.mcpsmd.org/")
    for path in ("/", "/student/1"):
        _, html = _get(populated, path)
        assert "class='schoollink'" in html
        assert "href='https://bcc-hs.mcpsmd.org/'" in html
        assert "target='_blank'" in html and "rel='noopener noreferrer'" in html
        assert "Visit school website" in html
        assert "Example ES</a>" in html          # the name itself is the link


def test_school_name_plain_when_unresolvable(populated, monkeypatch):
    """An unknown school stays plain muted text — no anchor, no outbound link."""
    monkeypatch.setattr(dashboard.schools, "school_url", lambda name: None)
    for path in ("/", "/student/1"):
        _, html = _get(populated, path)
        assert "Example ES" in html
        assert "schoollink" not in html
        assert "target='_blank'" not in html


def test_nav_omits_tooltip_when_name_not_abbreviated():
    """A single-word full name is shown whole in the nav, so no reveal tooltip
    (it would only echo the visible text)."""
    html = dashboard._nav_students([{"agu": "1", "name": "Jasper"}])
    assert ">Jasper</a>" in html
    assert "tip-b" not in html
    assert "data-tip" not in html


def test_nav_reveals_full_name_when_abbreviated():
    """When the nav shows a first name, the link reveals the full name on hover."""
    html = dashboard._nav_students([{"agu": "1", "name": "Jasper P. Hays"}])
    assert "class='tip-b'" in html
    assert "data-tip='Jasper P. Hays'" in html


def test_history_when_is_local_date_words(populated):
    snap = Snapshot(
        student_agu="1",
        courses=[Course(edupoint_gu="709775", title="Math <Adv>", term="MP1")],
        assignments=[Assignment(edupoint_gu="a1", course_gu="709775",
                                name="Fractions Quiz", score=9.0, points=10.0,
                                kind="Assessment",
                                status=AssignmentStatus.GRADED)],
    )
    store.persist_snapshot(populated, Student(agu="1", name="Jasper P. Hays"), snap)
    _, html = _get(populated, "/history")
    assert "When (UTC)" not in html
    assert ">today<span class='vh'>" in html  # visible word + SR timestamp suffix


# ── 0.1.3: the per-row test button ────────────────────────────────────


def test_settings_channel_rows_offer_a_test_button(populated):
    watchers.add_watcher(populated, "Mom", WatcherKind.GUARDIAN,
                         {"email": {"to": "mom@example.com"}})
    _, html = _get(populated, "/settings")
    assert "formaction='/settings/channel-test'" in html
    assert "Send a test email to mom@example.com" in html


def test_settings_channel_test_sends_the_saved_address(populated, monkeypatch):
    from lastbell import notify
    sent = []
    monkeypatch.setattr(notify, "send_test", lambda c, a: sent.append((c, a)))
    conn = populated
    watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                         {"email": {"to": "mom@example.com"}})
    target = _ok(_post(conn, "channel-test", watcher="Mom", channel="email",
                       to="edited-but-not-saved@example.com"))
    assert sent == [("email", {"to": "mom@example.com"})]   # saved, not the field
    from urllib.parse import unquote
    assert "Sent a test email to mom@example.com" in unquote(target)
    assert "check that it arrived" in unquote(target)

    # a transport failure is a banner with the reason, not a 500
    def boom(c, a):
        raise ValueError("LASTBELL_SMTP_HOST is not set")
    monkeypatch.setattr(notify, "send_test", boom)
    status, target = _post(conn, "channel-test", watcher="Mom", channel="email")
    assert status == 303 and target.startswith("/settings?err=")
    assert "LASTBELL_SMTP_HOST" in target

    # asking for a channel the watcher doesn't have
    status, target = _post(conn, "channel-test", watcher="Mom", channel="sms")
    assert status == 303 and "no text message channel" in unquote(target)


# ── 0.1.4: the footer knows its version; Check for updates is on-click ─


def test_settings_footer_has_version_links_and_update_check(populated):
    from lastbell import __version__
    _, html = _get(populated, "/settings")
    foot = html[html.index("<footer class='credit'>"):html.index("</footer>")]
    assert f">Last Bell {__version__}</a>" in foot
    assert f"/releases/tag/v{__version__}" in foot
    assert "/releases'" in foot and ">What's new</a>" in foot
    assert "/issues'" in foot and ">Report a problem</a>" in foot
    assert "action='/settings/check-updates'" in foot   # a POST: never on load
    assert "MIT license" in foot and "Chris Hays" in foot
    # nothing on the students page fetches anything: the footer is Settings-only
    _, students = _get(populated, "/")
    assert "check-updates" not in students


def test_settings_check_updates_reports_and_never_runs_on_get(populated, monkeypatch):
    from urllib.parse import unquote

    from lastbell import updates
    calls = []

    def fake_check():
        calls.append(1)
        return "newer", "9.9.9"
    monkeypatch.setattr(updates, "check", fake_check)
    _get(populated, "/settings")
    assert calls == []                                    # rendering never checks
    target = _ok(_post(populated, "check-updates"))
    assert calls == [1]
    assert "9.9.9 is available" in unquote(target)
    assert "pipx upgrade lastbell" in unquote(target)

    def down():
        raise updates.UpdateCheckError("couldn't reach PyPI (ConnectionError)")
    monkeypatch.setattr(updates, "check", down)
    status, target = _post(populated, "check-updates")
    assert status == 303 and "Update check failed" in unquote(target)
    assert "ConnectionError" in unquote(target)


def test_canvas_rows_are_marked_and_twins_hidden(populated):
    from lastbell.models import SOURCE_CANVAS

    snap = Snapshot(
        student_agu="1", term="MP1",
        courses=[Course(edupoint_gu="709775", title="Math <Adv>", teacher="Pat Example",
                        term="MP1", mark="B+", percent="87.20%")],
        assignments=[
            Assignment(edupoint_gu="canvas:1", course_gu="709775", name="Collage",
                       status=AssignmentStatus.MISSING, source=SOURCE_CANVAS),
            Assignment(edupoint_gu="canvas:2", course_gu="709775", name="Osmosis Quiz",
                       due_date=datetime.date(2026, 9, 20),
                       status=AssignmentStatus.DUE, source=SOURCE_CANVAS),
            Assignment(edupoint_gu="canvas:3", course_gu="709775", name="Essay",
                       status=AssignmentStatus.SUBMITTED, source=SOURCE_CANVAS),
        ],
    )
    store.persist_snapshot(
        populated, Student(agu="1", name="Jasper P. Hays", school="Example ES",
                           initials="J.P.H."), snap)
    status, html = _get(populated, "/")
    assert "1 missing" in html                       # the Canvas twin isn't double-counted
    status, html = _get(populated, "/student/1?view=due")
    assert "Osmosis Quiz" in html and "class='src'>Canvas<" in html
    status, html = _get(populated, "/student/1?view=everything")
    assert "Essay" in html and "turned in" in html
    assert html.count("Collage") == 1               # the gradebook row only


def test_home_page_says_when_it_last_checked(populated):
    status, html = _get(populated, "/")
    assert "Not checked yet" in html
    store.record_poll(populated)
    status, html = _get(populated, "/")
    assert "Last checked today at" in html and "stale" not in html
    store.record_poll(populated, "2026-01-01 12:00:00")
    status, html = _get(populated, "/")
    assert "freshness stale" in html and "isn't running" in html


def test_alerts_type_filter_is_never_reflected_as_html(conn):
    """?type= is echoed into the 'Showing … alerts' line; an unknown value
    is dropped and a known one is escaped."""
    from lastbell.models import AlertType, Snapshot, Student

    student = Student(agu="7", name="Jasper P. Hays", school="Example ES", initials="J.P.H.")
    store.persist_snapshot(conn, student, Snapshot(student_agu="7"))
    store.record_alert(conn, "7", Event(AlertType.ASSIGNMENT_MISSING, "7", "Algebra",
                                        "Essay is marked missing", item="Essay",
                                        what="is marked missing"))
    payload = "<img src=x onerror=alert(1)>"
    status, html = _get(conn, "/alerts?type=" + payload.replace(" ", "%20"))
    assert status == 200 and payload not in html and "&lt;img" not in html
    status, html = _get(conn, "/alerts?type=assignment_missing")
    assert status == 200 and "assignment missing alerts" in html
