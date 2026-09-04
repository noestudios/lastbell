"""0.2.7: findings from the time-handling and SQLite-concurrency audit."""
from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from lastbell import store, summary, watchers
from lastbell.models import Assignment, AssignmentStatus, Course, Snapshot, Student, WatcherKind

TODAY = dt.date(2026, 9, 4)


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.ensure_schema(c)
    yield c
    c.close()


def _student(conn, assignments):
    snap = Snapshot(student_agu="1",
                    courses=[Course(edupoint_gu="c1", title="Math", term="MP1",
                                    mark="B", percent="85%")],
                    assignments=assignments)
    store.persist_snapshot(conn, Student(agu="1", name="Jasper P. Hays",
                                         school="Example ES", initials="J.P.H."), snap)


# ── A1: history is bucketed by local day ──────────────────────────────


def test_history_days_are_local_not_utc(conn):
    from lastbell.dashboard.queries import _fetch_change_rows

    _student(conn, [])
    # An evening change in the US lands after midnight UTC.
    utc = "2026-09-04 01:30:00"
    conn.execute("INSERT INTO course_history (course_id, field, old_value, new_value, seen_at) "
                 "SELECT id, 'percent', '80', '85', ? FROM courses WHERE student_id = '1'",
                 (utc,))
    conn.commit()
    rows = _fetch_change_rows(conn, "course_history", "course_id",
                              "JOIN courses c ON c.id = h.course_id", "1", "MP1", "percent")
    (series,) = rows.values()
    day = series[0][0]
    expected = (dt.datetime.fromisoformat(utc).replace(tzinfo=dt.timezone.utc)
                .astimezone().date().isoformat())
    assert day == expected
    # the same conversion every other date on the page uses
    assert day == conn.execute("SELECT date(?, 'localtime')", (utc,)).fetchone()[0]


# ── A3: a summary slot lost to a poll across midnight is caught up ────


class Chan:
    def __init__(self):
        self.calls = []

    def send(self, to, subject, body):
        self.calls.append((to, subject, body))


def _summary_sub(conn, at="23:55"):
    _student(conn, [])
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN, {"email": {"to": "m@x.com"}})
    watchers.subscribe(conn, w, "1", ["daily_summary"], send_at=at)
    return watchers.summary_subscriptions(conn)[0]["sub_id"]


def test_missed_summary_day_is_owed_and_sent(conn):
    sub_id = _summary_sub(conn)
    ch = Chan()
    # Sent on the 2nd. The 3rd's 23:55 slot passed while a poll ran; the
    # next tick is 00:03 on the 4th.
    watchers.mark_summary_sent(conn, sub_id, "2026-09-02")
    now = dt.datetime(2026, 9, 4, 0, 3)
    assert summary.send_due(conn, now=now, channel_factory=lambda n: ch)[0] == 1
    assert watchers.summary_subscriptions(conn)[0]["last_sent_on"] == "2026-09-04"
    # and that counts as today's: the 23:55 slot doesn't send a second one
    assert summary.send_due(conn, now=now.replace(hour=23, minute=56),
                            channel_factory=lambda n: ch)[0] == 0


def test_yesterdays_summary_that_was_sent_is_not_resent_after_midnight(conn):
    sub_id = _summary_sub(conn)
    ch = Chan()
    watchers.mark_summary_sent(conn, sub_id, "2026-09-03")
    assert summary.send_due(conn, now=dt.datetime(2026, 9, 4, 0, 3),
                            channel_factory=lambda n: ch)[0] == 0
    assert summary.send_due(conn, now=dt.datetime(2026, 9, 4, 23, 55),
                            channel_factory=lambda n: ch)[0] == 1


def test_first_ever_summary_still_waits_for_its_slot(conn):
    _summary_sub(conn, at="07:00")
    ch = Chan()
    assert summary.send_due(conn, now=dt.datetime(2026, 9, 4, 6, 59),
                            channel_factory=lambda n: ch)[0] == 0
    assert summary.send_due(conn, now=dt.datetime(2026, 9, 4, 7, 0),
                            channel_factory=lambda n: ch)[0] == 1


# ── A4: grace-window work stays in the summary ────────────────────────


def test_summary_lists_recently_due_ungraded_work(conn):
    _student(conn, [
        Assignment(edupoint_gu="a1", course_gu="c1", name="Lab 3",
                   due_date=TODAY - dt.timedelta(days=1), status=AssignmentStatus.DUE),
        Assignment(edupoint_gu="a2", course_gu="c1", name="Quiz",
                   due_date=TODAY + dt.timedelta(days=2), status=AssignmentStatus.DUE),
    ])
    body = summary.build(conn, "1", "J.P.H.", today=TODAY, grace_days=3)
    assert "Due recently, not yet graded (1):" in body and "Lab 3" in body
    assert "Due in the next 7 days (1):" in body and "Quiz" in body
    assert "Nothing missing" not in body
    # outside the grace window it is the time rules' business, not the summary's
    body = summary.build(conn, "1", "J.P.H.", today=TODAY + dt.timedelta(days=5),
                         grace_days=3)
    assert "Lab 3" not in body                 # past the window: the time rules' business
    assert "Due recently, not yet graded (1):" in body and "Quiz" in body


# ── B1/B3: migrations tolerate the restart race; WAL runs NORMAL ──────


def test_migration_tolerates_a_column_added_by_the_other_process(tmp_path, monkeypatch):
    conn = store.connect(tmp_path / "r.db")
    store.ensure_schema(conn)

    class Racing:
        """A connection whose table_info is stale: it says the column is
        missing after the other process already added it."""

        def __init__(self, real):
            self._real = real

        def execute(self, sql, *a):
            if sql.startswith("PRAGMA table_info"):
                return []
            return self._real.execute(sql, *a)

    monkeypatch.setattr(store, "_MIGRATIONS", [("subscriptions", "urgent_now",
                                                "INTEGER NOT NULL DEFAULT 0")])
    store._migrate(Racing(conn))                       # must not raise
    with pytest.raises(sqlite3.OperationalError):      # anything else still does
        monkeypatch.setattr(store, "_MIGRATIONS", [("no_such_table", "x", "TEXT")])
        store._migrate(Racing(conn))
    conn.close()


def test_connections_are_wal_with_normal_sync(tmp_path):
    conn = store.connect(tmp_path / "w.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1   # NORMAL
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
    conn.close()


# ── B2: a busy database answers the settings form, never drops it ─────


def test_settings_post_on_a_busy_database_redirects_with_a_message(tmp_path, monkeypatch):
    import http.client
    import threading
    from http.server import ThreadingHTTPServer

    from lastbell import dashboard
    import lastbell.dashboard.server as srvmod

    db = tmp_path / "d.db"
    c = store.connect(db)
    store.ensure_schema(c)
    c.close()

    def busy(conn, action, form):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(srvmod, "_handle_settings_post", busy)
    captured = {}

    def fake_server(addr, handler):
        captured["srv"] = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        return captured["srv"]
    monkeypatch.setattr(srvmod, "ThreadingHTTPServer", fake_server)
    threading.Thread(target=dashboard.serve, args=(db, "127.0.0.1", 0), daemon=True).start()
    for _ in range(300):
        if "srv" in captured:
            break
        threading.Event().wait(0.01)
    port = captured["srv"].server_address[1]
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/settings/watcher-add", body="name=Mom",
                     headers={"Host": f"127.0.0.1:{port}",
                              "Origin": f"http://127.0.0.1:{port}",
                              "Content-Type": "application/x-www-form-urlencoded"})
        r = conn.getresponse()
        assert r.status == 303
        assert "busy" in r.getheader("Location")
        conn.close()
    finally:
        captured["srv"].shutdown()
