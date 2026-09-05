"""Fixes from the 2026-09-04 code review: the Canvas deadline race, the
settings-post origin guard, write-ahead logging, numeric config errors, the
secret setters, the capped History page, and structured alert lines."""
from __future__ import annotations

import datetime
import json
import sys

import pytest
import requests

from lastbell import canvas, dashboard, differ, outbox, store, watchers
from lastbell import config as cfg
from lastbell import secrets as secretstore
from lastbell.client import Child, ParentVueClient
from lastbell.differ import compose, event
from lastbell.models import (
    AlertType,
    Assignment,
    AssignmentStatus,
    Course,
    Snapshot,
    Student,
    WatcherKind,
)
from lastbell.notify import render
from lastbell.router import Delivery


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "test.db")
    store.ensure_schema(c)
    store.persist_snapshot(c, Student(agu="1", name="Jasper P. Hays", initials="J.P.H."),
                           Snapshot(student_agu="1"))
    yield c
    c.close()


# ── Canvas: the network half never holds the snapshot ─────────────────


class _Stub:
    host = "h"
    calls = 0

    def __init__(self, responses):
        self.responses = responses

    def get(self, path, **params):
        return json.loads(json.dumps(self.responses[path]))


def test_collect_for_returns_a_collection_and_merge_applies_it():
    layer = canvas.CanvasLayer(_Stub({
        "/api/v1/users/self/observees": [{"id": 7, "sortable_name": "Hays, Jasper"}],
        "/api/v1/courses": [{"id": 1, "name": "Hon Biology A-Yeh-S1-2027",
                             "term": {"name": "S1"},
                             "enrollments": [{"associated_user_id": 7}]}],
        "/api/v1/courses/1/assignment_groups": [],
        "/api/v1/users/7/courses/1/assignments": [
            {"id": 11, "name": "Osmosis Quiz", "published": True,
             "due_at": "2026-09-10T03:59:59Z", "points_possible": 10, "submission": {}}],
    }), [Child(agu="1", name="JASPER")])
    snap = Snapshot(student_agu="1", term="MP1",
                    courses=[Course(edupoint_gu="c1", title="Hon Biology A", term="MP1")])
    col = layer.collect_for("1")
    assert isinstance(col, canvas.CanvasCollection)
    assert snap.assignments == []            # collecting touched nothing
    assert layer.merge(snap, col)["assignments"] == 1
    assert snap.assignments[0].edupoint_gu == "canvas:11"
    assert layer.collect_for("nope") is None


def test_client_clone_copies_the_session_state_onto_a_new_session():
    pv = ParentVueClient("https://portal", "u", "p")
    pv.session.cookies.set("ASP.NET_SessionId", "abc")
    pv._logged_in = True
    twin = pv.clone()
    assert twin.session is not pv.session
    assert twin.session.cookies.get("ASP.NET_SessionId") == "abc"
    assert twin._logged_in and twin.username == "u" and twin.base_url == pv.base_url


def test_launch_pad_links_surfaces_a_portal_error():
    class Resp:
        status_code, text, url = 500, "", ""

        def raise_for_status(self):
            raise requests.HTTPError("500")

    class Session:
        headers: dict = {}

        def get(self, *a, **k):
            return Resp()

    pv = ParentVueClient("https://portal", "u", "p", session=Session())
    pv._logged_in = True
    with pytest.raises(requests.HTTPError):
        pv.launch_pad_links()


# ── dashboard: origin guard, WAL, capped history ──────────────────────


@pytest.mark.parametrize("headers, ok", [
    ({"Host": "127.0.0.1:8321"}, True),                                    # curl
    ({"Host": "127.0.0.1:8321", "Origin": "http://127.0.0.1:8321"}, True),
    ({"Host": "127.0.0.1:8321", "Referer": "http://127.0.0.1:8321/settings"}, True),
    ({"Host": "127.0.0.1:8321", "Origin": "https://evil.example"}, False),
    ({"Host": "127.0.0.1:8321", "Origin": "null"}, False),
    ({"Host": "127.0.0.1:8321", "Origin": "http://127.0.0.1:8321",
      "Sec-Fetch-Site": "cross-site"}, False),
])
def test_settings_posts_must_come_from_the_dashboards_own_pages(headers, ok):
    assert dashboard.same_origin(headers) is ok


def test_database_uses_write_ahead_logging(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_history_is_capped_with_a_link_to_the_full_list(conn, monkeypatch):
    course = [Course(edupoint_gu="c1", title="Algebra", term="MP1")]

    def assigns(score):
        return [Assignment(edupoint_gu=f"a{i}", course_gu="c1", name=f"Quiz {i}",
                           score=score, points=10.0, status=AssignmentStatus.GRADED)
                for i in range(12)]
    who = Student(agu="1", name="Jasper P. Hays")
    for score in (5.0, 9.0):
        store.persist_snapshot(conn, who, Snapshot(student_agu="1", courses=course,
                                                   assignments=assigns(score)))
    monkeypatch.setattr(dashboard.server, "_HISTORY_LIMIT", 5)

    _, html = dashboard._handle(conn, "/history")
    assert "Assignments <span class='small'>12</span>" in html   # the true total
    assert "Newest 5 rows of 12 changes" in html
    assert "href='/history?all=1'>show all 12</a>" in html
    _, html = dashboard._handle(conn, "/history?all=1")
    assert "Newest" not in html and html.count("Quiz ") == 12


# ── config + secrets ──────────────────────────────────────────────────


def test_a_non_numeric_setting_is_a_one_line_error(monkeypatch):
    monkeypatch.setenv("LASTBELL_DISTRICT", "x.example.com")
    monkeypatch.setenv("LASTBELL_USERNAME", "someone")
    monkeypatch.setenv("LASTBELL_POLL_MINUTES", "soon")
    with pytest.raises(cfg.ConfigError, match="LASTBELL_POLL_MINUTES"):
        cfg.load()


def test_smtp_password_goes_to_the_settings_file_on_the_env_backend(tmp_path, monkeypatch):
    from lastbell import paths
    from lastbell.setup_wizard import read_env

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LASTBELL_HOME", str(tmp_path))
    monkeypatch.setenv("LASTBELL_SECRET_BACKEND", "env")
    monkeypatch.delenv("LASTBELL_PASSWORD_SMTP", raising=False)

    class Boom:
        def __getattr__(self, name):
            raise AssertionError("keyring must not be touched on the env backend")
    monkeypatch.setitem(sys.modules, "keyring", Boom())

    where = secretstore.set_smtp_password("s3cret")
    assert "settings file" in where
    assert read_env(paths.default_env_file())["LASTBELL_PASSWORD_SMTP"] == "s3cret"
    assert secretstore.get_smtp_password() == "s3cret"


def test_smtp_password_keyring_failure_is_a_plain_error(monkeypatch):
    import keyring

    monkeypatch.setenv("LASTBELL_SECRET_BACKEND", "keyring")

    def boom(service, username, password):
        raise RuntimeError("no backend")
    monkeypatch.setattr(keyring, "set_password", boom)
    with pytest.raises(secretstore.SecretError, match="SMTP password"):
        secretstore.set_smtp_password("x")


# ── structured alert lines ────────────────────────────────────────────


def test_event_carries_its_parts_and_composes_the_sentence():
    e = event(AlertType.ASSIGNMENT_MISSING, "1", "Algebra", differ.MISSING_PHRASE,
              item="Unit 3", via="Canvas")
    assert e.detail == "Algebra: “Unit 3” is marked missing [Canvas]"
    assert e.as_dict() == {"course": "Algebra", "item": "Unit 3",
                           "what": "is marked missing", "via": "Canvas", "detail": e.detail}
    assert compose("Algebra", "overall 90% → 88%") == "Algebra: overall 90% → 88%"
    assert compose("", "MP1 closed — final grades: Math 90%") == "MP1 closed — final grades: Math 90%"
    assert compose("Algebra", "", "Quiz") == "Algebra: “Quiz”"


def test_diff_events_carry_parts():
    course = [Course(edupoint_gu="c1", title="Art", term="MP1")]
    before = Snapshot(student_agu="1", term="MP1", courses=course, assignments=[
        Assignment(edupoint_gu="a1", course_gu="c1", name="Collage",
                   status=AssignmentStatus.DUE)])
    after = Snapshot(student_agu="1", term="MP1", courses=course, assignments=[
        Assignment(edupoint_gu="a1", course_gu="c1", name="Collage",
                   status=AssignmentStatus.MISSING)])
    (e,) = differ.diff(before, after)
    assert (e.type, e.course_title, e.item, e.what, e.via) == (
        AlertType.ASSIGNMENT_MISSING, "Art", "Collage", "is marked missing", "")
    assert e.detail == "Art: “Collage” is marked missing"


def test_item_html_lays_out_parts_and_still_parses_old_sentences():
    parts = {"course": "Algebra", "item": "Unit 3 <b>", "what": "is marked missing",
             "via": "Canvas", "detail": "Algebra: “Unit 3 <b>” is marked missing [Canvas]"}
    h = render.item_html(parts)
    assert "<strong>Unit 3 &lt;b&gt;</strong> is marked missing" in h
    assert ">Algebra</div>" in h and ">Canvas</span>" in h
    legacy = {"course": "Algebra", "detail": "Algebra: “Quiz” graded: 9/10"}  # pre-0.2.4 row
    assert "<strong>Quiz</strong> graded: 9/10" in render.item_html(legacy)
    text = render.alerts([("J.", [("assignment_missing", parts)])])
    assert "• Algebra: “Unit 3 <b>” is marked missing [Canvas]" in str(text)


def test_outbox_and_alert_log_keep_the_parts(conn):
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                             {"email": {"to": "mom@example.com"}})
    e = event(AlertType.ASSIGNMENT_MISSING, "1", "Art", differ.MISSING_PHRASE, item="Collage")
    store.record_alert(conn, "1", e)
    assert json.loads(conn.execute("SELECT body FROM alerts").fetchone()["body"])["item"] == "Collage"

    d = Delivery(watcher_name="Mom", channel="email", to={"to": "mom@example.com"},
                 events=[e], watcher_id=w.id)
    outbox.enqueue(conn, d, datetime.datetime(2026, 9, 1, 8, 0))
    row = conn.execute("SELECT body, detail FROM outbox").fetchone()
    assert json.loads(row["body"])["what"] == "is marked missing" and row["detail"] == e.detail

    sent = []

    class Channel:
        def send(self, to, subject, body):
            sent.append(body)
    later = datetime.datetime(2026, 9, 1, 9, 0)
    outbox.flush_due(conn, now=later, channel_factory=lambda name: Channel())
    assert "<strong>Collage</strong> is marked missing" in sent[0].html
    assert "Art: “Collage” is marked missing" in str(sent[0])

    # A row queued by an older version carries only the sentence.
    conn.execute(
        "INSERT INTO outbox (id, watcher_id, channel, student_id, alert_type, detail, "
        "send_after) VALUES ('old', ?, 'email', '1', 'grade_changed', "
        "'Art: “Collage” graded: 9/10', '2026-09-01 08:00:00')", (w.id,))
    conn.commit()
    sent.clear()
    outbox.flush_due(conn, now=later, channel_factory=lambda name: Channel())
    assert "<strong>Collage</strong> graded: 9/10" in sent[0].html
