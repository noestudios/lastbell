"""Dashboard: routing + rendering against a real (temp) database."""
from __future__ import annotations

import datetime

import pytest

from mcpsgradewatch import dashboard, store, watchers
from mcpsgradewatch.differ import Event
from mcpsgradewatch.models import (
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


def test_student_page_shows_assignments(populated):
    status, html = _get(populated, "/student/1")
    assert status == 200
    assert "Fractions Quiz" in html
    assert "80.0%" in html                    # score as a percentage…
    assert "title='8/10'" in html             # …raw points on hover
    assert "87.2% · B+" in html               # course heading, one decimal
    assert "MISSING" in html


def test_unknown_student_404s(populated):
    status, html = _get(populated, "/student/999")
    assert status == 404


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
    assert "8.0 → 9.0" in html


def test_watchers_page(populated):
    w = watchers.add_watcher(populated, "Mom", WatcherKind.GUARDIAN,
                             {"email": {"to": "mom@example.com"}})
    watchers.subscribe(populated, w, "1")
    status, html = _get(populated, "/watchers")
    assert status == 200
    assert "Mom" in html
    assert "all configured" in html


def test_unknown_path_404s(conn):
    status, _ = _get(conn, "/nope")
    assert status == 404


def test_alerts_page_offers_ack_form_and_shows_ack_state(populated):
    conn = populated
    watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)
    store.record_alert(conn, "1", Event(
        type=AlertType.GRADE_CHANGED, student_agu="1", course_title="Math",
        detail="Math: “Fractions Quiz” graded: 8/10"))
    status, html = _get(conn, "/alerts")
    assert "action='/ack'" in html and "<option>Mom</option>" in html

    alert_id = conn.execute("SELECT id FROM alerts").fetchone()["id"]
    status, target = dashboard._handle_ack(
        conn, {"alert_id": [alert_id], "watcher": ["Mom"]})
    assert (status, target) == (303, "/alerts")

    _, html = _get(conn, "/alerts")
    assert "✓ Mom" in html and "action='/ack'" not in html


def test_bad_ack_is_rejected(populated):
    status, html = dashboard._handle_ack(populated, {"alert_id": ["x"], "watcher": ["Nobody"]})
    assert status == 400


def test_watchers_page_shows_quiet_hours_and_schedule(populated):
    conn = populated
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                             {"email": {"to": "m@x.com"}})
    watchers.set_quiet_hours(conn, "Mom", "21:00", "07:00")
    watchers.subscribe(conn, w, "1", ["grade_changed"], ["email"], send_at="17:00")
    _, html = _get(conn, "/watchers")
    assert "21:00–07:00" in html
    assert "daily at 17:00" in html


def test_stylesheet_exists_and_is_linked(populated):
    from mcpsgradewatch.dashboard import _STYLE_PATH

    css = _STYLE_PATH.read_text(encoding="utf-8")
    assert ":root" in css and "--accent" in css and "--bg" in css
    _, html = _get(populated, "/")
    assert "/static/style.css" in html


def test_theme_toggle_present_and_css_supports_override(populated):
    from mcpsgradewatch.dashboard import _STYLE_PATH

    _, html = _get(populated, "/")
    assert "id='themetoggle'" in html
    assert "mcpsgradewatch-theme" in html      # localStorage key in the script
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
