"""Canvas layer: matching, normalization, merge/supersede, the API client's
paging, and the SAML hand-off — all against canned responses."""
from __future__ import annotations

import datetime
import json

import pytest

from lastbell import canvas
from lastbell.client import Child
from lastbell.models import (
    SOURCE_CANVAS,
    SOURCE_PARENTVUE,
    Assignment,
    AssignmentStatus,
    Course,
    Snapshot,
)


# ── who is who ────────────────────────────────────────────────────────


def test_match_students_by_name_tokens():
    kids = [Child(agu="0", name="JASPER", school=""),
            Child(agu="1", name="LEO", school="")]
    obs = [canvas.Observee(id=10, name="Hays, Leo D (Student)"),
           canvas.Observee(id=11, name="Hays, Jasper P (Student)")]
    got = canvas.match_students(kids, obs)
    assert {k: v.id for k, v in got.items()} == {"0": 11, "1": 10}


def test_match_students_full_portal_name_and_ambiguity():
    kids = [Child(agu="0", name="Jasper P. Hays", school=""),
            Child(agu="1", name="Sam", school="")]
    obs = [canvas.Observee(id=1, name="Hays, Jasper P"),
           canvas.Observee(id=2, name="Lee, Sam"),
           canvas.Observee(id=3, name="Park, Sam")]
    got = canvas.match_students(kids, obs)
    assert got["0"].id == 1
    assert "1" not in got                 # two Sams: don't guess


def test_lone_child_pairs_with_lone_observee_regardless_of_name():
    got = canvas.match_students([Child(agu="7", name="J", school="")],
                                [canvas.Observee(id=5, name="Anything")])
    assert got["7"].id == 5


# ── course names ──────────────────────────────────────────────────────


def test_split_course_name_strips_district_suffix():
    assert canvas.split_course_name("Hon Biology A-Yeh-S1-2027") == ("Hon Biology A", "Yeh")
    assert canvas.split_course_name("Hughes-Lloyd's class") == ("Hughes-Lloyd's class", "")
    assert canvas.split_course_name("Class of 2030") == ("Class of 2030", "")


def test_match_course_ignores_period_prefix_and_suffix():
    pv = [Course(edupoint_gu="1", title="7: Hon Biology A"),
          Course(edupoint_gu="2", title="3: Theatre HS 1A")]
    assert canvas.match_course("Hon Biology A-Yeh-S1-2027", pv).edupoint_gu == "1"
    assert canvas.match_course("Theatre HS 2A-Lazarus-S1-2027", pv) is None
    assert canvas.match_course("Library", pv) is None


# ── assignments ───────────────────────────────────────────────────────


def _api_assignment(**kw) -> dict:
    base = {"id": 11669101, "name": "Cell Lab", "due_at": "2026-09-04T03:59:59Z",
            "points_possible": 10.0, "published": True, "assignment_group_id": 5,
            "submission": {"workflow_state": "unsubmitted", "score": None,
                           "missing": False, "excused": False}}
    base.update(kw)
    return base


def test_keep_assignment_wants_a_date_or_points():
    assert canvas.keep_assignment(_api_assignment())
    assert canvas.keep_assignment(_api_assignment(due_at=None, points_possible=5))
    assert not canvas.keep_assignment(_api_assignment(due_at=None, points_possible=0))
    assert not canvas.keep_assignment(_api_assignment(published=False))
    assert not canvas.keep_assignment(_api_assignment(
        submission={"excused": True, "score": None}))


def test_to_assignment_status_ladder():
    a = canvas.to_assignment(_api_assignment(), "C1", {5: "All Tasks"})
    assert a.edupoint_gu == "canvas:11669101"
    assert a.source == SOURCE_CANVAS
    assert a.kind == "All Tasks"
    assert a.points == 10.0 and a.score is None
    assert a.status is AssignmentStatus.DUE

    sub = canvas.to_assignment(_api_assignment(
        submission={"workflow_state": "submitted", "submitted_at": "2026-09-02T14:00:00Z"}), "C1")
    assert sub.status is AssignmentStatus.SUBMITTED

    graded = canvas.to_assignment(_api_assignment(
        submission={"workflow_state": "graded", "score": 8, "graded_at": "2026-09-05T12:00:00Z"}), "C1")
    assert graded.status is AssignmentStatus.GRADED
    assert graded.score == 8.0
    assert graded.graded_at == datetime.date(2026, 9, 5)

    missing = canvas.to_assignment(_api_assignment(
        submission={"workflow_state": "unsubmitted", "missing": True}), "C1")
    assert missing.status is AssignmentStatus.MISSING


def test_due_at_is_converted_to_the_local_calendar(monkeypatch):
    # 03:59:59Z on the 4th is the evening of the 3rd anywhere in the US.
    monkeypatch.setenv("TZ", "America/New_York")
    import time
    time.tzset()
    a = canvas.to_assignment(_api_assignment(due_at="2026-09-04T03:59:59Z"), "C1")
    assert a.due_date == datetime.date(2026, 9, 3)


# ── merge ─────────────────────────────────────────────────────────────


def _snapshot() -> Snapshot:
    return Snapshot(
        student_agu="1", term="MP1",
        courses=[Course(edupoint_gu="736790", title="7: Hon Biology A", term="MP1")],
        assignments=[Assignment(edupoint_gu="9001", course_gu="736790",
                                name="Cell Lab", score=8.0, points=10.0,
                                status=AssignmentStatus.GRADED)],
    )


def _collection(*courses) -> canvas.CanvasCollection:
    return canvas.CanvasCollection(observee=canvas.Observee(id=1, name="x"),
                                   courses=list(courses))


def _cc(cid, name, *names) -> canvas.CanvasCourse:
    title, teacher = canvas.split_course_name(name)
    c = canvas.CanvasCourse(id=cid, name=name, title=title, teacher=teacher, term="S1")
    c.assignments = [Assignment(edupoint_gu=f"canvas:{i}", course_gu=c.gu, name=n,
                                status=AssignmentStatus.DUE, source=SOURCE_CANVAS,
                                due_date=datetime.date(2026, 9, 10))
                     for i, n in enumerate(names, 1)]
    return c


def test_merge_attaches_to_the_matching_gradebook_course_and_supersedes_twins():
    snap = _snapshot()
    stats = canvas.merge(snap, _collection(
        _cc(1088849, "Hon Biology A-Yeh-S1-2027", "Cell Lab", "Osmosis Quiz")))
    assert stats == {"matched": 1, "own": 0, "assignments": 1, "superseded": 1, "skipped": 0}
    assert [c.edupoint_gu for c in snap.courses] == ["736790"]     # no new course row
    names = {(a.name, a.course_gu, a.source) for a in snap.assignments}
    assert ("Osmosis Quiz", "736790", SOURCE_CANVAS) in names       # re-homed
    assert ("Cell Lab", "736790", SOURCE_PARENTVUE) in names       # the record
    (twin,) = [a for a in snap.assignments
               if a.name == "Cell Lab" and a.source == SOURCE_CANVAS]
    assert twin.superseded_by == "9001"                           # kept, marked
    assert all(not a.superseded_by for a in snap.assignments if a is not twin)


def test_merge_gives_an_unmatched_course_its_own_row_only_when_it_looks_like_a_class():
    snap = _snapshot()
    shell = _cc(3, "Student Password Reset : Section 3", "Reset it")
    shell.term = "Default Term"
    other_term = _cc(4, "Yoga Camp", "Breathe")
    other_term.term = "Summer"
    stats = canvas.merge(snap, _collection(
        _cc(1, "Theatre HS 2A-Lazarus-S1-2027", "Monologue"),      # S1, like the class below
        _cc(1088849, "Hon Biology A-Yeh-S1-2027", "Osmosis Quiz"),  # matches → sets the term
        _cc(2, "Library"),                                          # no assignments
        shell, other_term))
    assert stats["own"] == 1 and stats["skipped"] == 2
    own = [c for c in snap.courses if c.source == SOURCE_CANVAS]
    assert [(c.title, c.teacher, c.term) for c in own] == [("Theatre HS 2A", "Lazarus", "MP1")]
    assert own[0].edupoint_gu == "canvas:1"
    assert sorted(a.course_gu for a in snap.assignments if a.source == SOURCE_CANVAS) == ["736790", "canvas:1"]


# ── the client ────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status, body, links=None, url=""):
        self.status_code, self.text, self.links, self.url = status, body, links or {}, url
        self.headers = {}


class _Session:
    """Scripted responses keyed by URL substring, in order."""

    def __init__(self, script):
        self.script, self.calls, self.headers = list(script), [], {}

    def get(self, url, params=None, headers=None, **kw):
        self.calls.append((url, params, headers))
        for i, (needle, resp) in enumerate(self.script):
            if needle in url:
                return self.script.pop(i)[1]
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, data=None, **kw):
        self.calls.append(("POST", url, data))
        return self.script.pop(0)[1]


def test_client_strips_prefix_follows_pages_and_sends_token():
    s = _Session([
        ("/api/v1/x", _Resp(200, 'while(1);[{"id":1}]',
                            links={"next": {"url": "https://h/api/v1/x?page=2"}})),
        ("page=2", _Resp(200, '[{"id":2}]')),
    ])
    c = canvas.CanvasClient("h", session=s, token="T", delay_s=0)
    assert c.get("/api/v1/x", per_page=100) == [{"id": 1}, {"id": 2}]
    assert c.calls == 2
    first_headers = s.calls[0][2]
    assert first_headers["Authorization"] == "Bearer T"
    assert s.calls[1][1] is None            # the next-link carries its own query


def test_client_raises_on_signed_out_or_html():
    c = canvas.CanvasClient("h", session=_Session([("/a", _Resp(401, ""))]), delay_s=0)
    with pytest.raises(canvas.CanvasError):
        c.get("/a")
    c = canvas.CanvasClient("h", session=_Session([("/b", _Resp(200, "<html>login</html>"))]),
                            delay_s=0)
    with pytest.raises(canvas.CanvasError):
        c.get("/b")


def test_saml_login_submits_the_idp_form_and_returns_the_canvas_host():
    idp_page = ('<form method="post" action="https://mcps.instructure.com/login/saml">'
                '<input type="hidden" name="SAMLResponse" value="abc&#43;"/>'
                '<input type="hidden" name="RelayState" value="r"/></form>')
    s = _Session([
        ("login/saml/129", _Resp(200, idp_page, url="https://sis/SamlAuth.aspx?x=1")),
        ("", _Resp(200, "<html>dashboard</html>",
                   url="https://mcps.instructure.com/?login_success=1")),
    ])
    host = canvas.saml_login(s, "https://mcps.instructure.com/login/saml/129")
    assert host == "mcps.instructure.com"
    assert s.calls[1] == ("POST", "https://mcps.instructure.com/login/saml",
                          {"SAMLResponse": "abc+", "RelayState": "r"})


def test_saml_login_that_stalls_at_the_idp_is_an_error():
    s = _Session([("login/saml/129", _Resp(200, "<html>please sign in</html>",
                                           url="https://sis/SamlAuth.aspx"))])
    with pytest.raises(canvas.CanvasError):
        canvas.saml_login(s, "https://mcps.instructure.com/login/saml/129")


def test_layer_apply_collects_and_merges(monkeypatch):
    """End-to-end over a stub client: observees → courses → assignments →
    merged snapshot, with a failing course reported rather than fatal."""
    responses = {
        "/api/v1/users/self/observees": [{"id": 199014, "sortable_name": "Hays, Jasper"}],
        "/api/v1/courses": [
            {"id": 1088849, "name": "Hon Biology A-Yeh-S1-2027",
             "term": {"name": "S1"},
             "enrollments": [{"associated_user_id": 199014}]},
            {"id": 5, "name": "Broken-X-S1-2027", "term": {"name": "S1"},
             "enrollments": [{"associated_user_id": 199014}]},
            {"id": 6, "name": "Other Kid-Y-S1-2027", "term": {"name": "S1"},
             "enrollments": [{"associated_user_id": 1}]},
        ],
        "/api/v1/courses/1088849/assignment_groups": [{"id": 5, "name": "Practice"}],
        "/api/v1/users/199014/courses/1088849/assignments": [
            _api_assignment(), _api_assignment(id=2, name="Osmosis Quiz")],
    }

    class Stub:
        calls = 0
        host = "h"

        def get(self, path, **params):
            if path.startswith("/api/v1/courses/5/"):
                raise canvas.CanvasError("boom")
            return json.loads(json.dumps(responses[path]))

    warnings = []
    layer = canvas.CanvasLayer(Stub(), [Child(agu="1", name="JASPER", school="")],
                               warn=warnings.append)
    snap = _snapshot()
    stats = layer.apply(snap)
    assert stats == {"matched": 1, "own": 0, "assignments": 1, "superseded": 1, "skipped": 0}
    assert any("Broken" in w for w in warnings)
    added = [a for a in snap.assignments if a.source == SOURCE_CANVAS and not a.superseded_by]
    assert [(a.name, a.course_gu, a.kind) for a in added] == [("Osmosis Quiz", "736790", "Practice")]
    assert layer.apply(Snapshot(student_agu="nope")) is None


def test_skip_list_and_no_matched_classes_fallback():
    snap = Snapshot(student_agu="1", term="MP1",
                    courses=[Course(edupoint_gu="e0", title="Ms. Okafor's class", term="MP1")])
    es = _cc(5, "Olney ES", "Reading log")
    es.term = "Schoolwide Courses"
    shell = _cc(6, "Student Password Reset : Section 9", "Reset it")
    shell.term = "Default Term"
    # Nothing matched, so any real-term course may stand on its own …
    assert canvas.merge(snap, _collection(es, shell))["own"] == 1
    # … unless the user names it.
    snap2 = Snapshot(student_agu="1", term="MP1")
    stats = canvas.merge(snap2, _collection(es), skip=("olney",))
    assert stats["own"] == 0 and stats["skipped"] == 1


def test_online_vs_paper_wording_for_past_due():
    from lastbell.differ import diff

    def canvas_assignment(types, status):
        return Assignment(edupoint_gu="canvas:1", course_gu="736790", name="Lab",
                          due_date=datetime.date(2026, 8, 20), status=status,
                          source=SOURCE_CANVAS, raw={"submission_types": types})

    course = Course(edupoint_gu="736790", title="Bio", term="MP1")
    for types, phrase in ((["online_upload"], "still not turned in"),
                          (["on_paper"], "still ungraded"),
                          (["external_tool"], "still ungraded")):
        prev = Snapshot(student_agu="1", courses=[course],
                        assignments=[canvas_assignment(types, AssignmentStatus.DUE)])
        curr = Snapshot(student_agu="1", courses=[course],
                        assignments=[canvas_assignment(types, AssignmentStatus.UNGRADED_PAST_DUE)])
        (ev,) = diff(prev, curr, today=datetime.date(2026, 9, 1))
        assert phrase in ev.detail and ev.detail.endswith("[Canvas]"), ev.detail
