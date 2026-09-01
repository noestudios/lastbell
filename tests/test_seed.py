"""Demo seeder: realistic quarter-end volume, real pipeline artifacts."""
from __future__ import annotations

import datetime

import pytest

from lastbell import seed, store

TODAY = datetime.date(2026, 9, 1)


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    conn = store.connect(tmp_path_factory.mktemp("seed") / "demo.db")
    store.ensure_schema(conn)
    seed.seed_demo(conn, seed=2026, today=TODAY)
    yield conn
    conn.close()


def test_two_students_two_terms(demo):
    assert demo.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 2
    terms = {r[0] for r in demo.execute("SELECT DISTINCT term FROM courses")}
    assert terms == {"MP1", "MP2"}
    # the rollover left MP2 current, and fired its one-shot summary alert
    assert {r[0] for r in demo.execute("SELECT current_term FROM students")} == {"MP2"}
    assert demo.execute(
        "SELECT COUNT(*) FROM alerts WHERE type='term_final'").fetchone()[0] >= 1


def test_quarter_end_density(demo):
    # the UX plan's target: ~25–40 assignments per HS class per quarter
    counts = [r[0] for r in demo.execute(
        "SELECT COUNT(a.id) FROM courses c JOIN assignments a ON a.course_id=c.id "
        "WHERE c.student_id='D1' GROUP BY c.id")]
    assert len(counts) == 14                     # 7 courses × 2 terms
    assert all(25 <= n <= 40 for n in counts)


def test_status_mix_for_signal_design(demo):
    by_status = dict(demo.execute(
        "SELECT status, COUNT(*) FROM assignments GROUP BY status"))
    assert by_status["graded"] > 300             # the backlog
    for straggler in ("missing", "ungraded_past_due", "due"):
        assert 0 < by_status[straggler] < 60, straggler


def test_history_spans_months_for_trend_charts(demo):
    lo, hi = demo.execute(
        "SELECT MIN(seen_at), MAX(seen_at) FROM course_history").fetchone()
    span = (datetime.date.fromisoformat(hi[:10])
            - datetime.date.fromisoformat(lo[:10])).days
    assert span > 100
    # per-course series are dense enough to draw a line
    per_course = [r[0] for r in demo.execute(
        "SELECT COUNT(*) FROM course_history WHERE field='percent' GROUP BY course_id")]
    assert per_course and min(per_course) >= 10


def test_alerts_are_dated_across_the_season(demo):
    lo, hi = demo.execute(
        "SELECT MIN(created_at), MAX(created_at) FROM alerts").fetchone()
    assert lo[:10] < hi[:10]                     # not all stamped "now"
    types = {r[0] for r in demo.execute("SELECT DISTINCT type FROM alerts")}
    assert {"grade_changed", "assignment_missing", "upcoming_deadline",
            "grade_drop", "term_final"} <= types


def test_watchers_ready_for_settings_and_ack(demo):
    from lastbell import watchers

    names = {w.name for w in watchers.list_watchers(demo)}
    assert names == {"Mom", "Maya"}
    assert demo.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] > 0
    # nothing was queued for delivery — the seeder records, never sends
    assert demo.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_deterministic_for_a_seed(tmp_path):
    digests = []
    for name in ("a.db", "b.db"):
        conn = store.connect(tmp_path / name)
        store.ensure_schema(conn)
        seed.seed_demo(conn, seed=7, today=TODAY)
        digests.append(conn.execute(
            "SELECT COUNT(*), SUM(LENGTH(name)) FROM assignments").fetchone())
        conn.close()
    assert digests[0] == digests[1]
