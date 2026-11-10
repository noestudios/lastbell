"""A student the portal has no gradebook for (0.3.8): a second child on the
account whose class list comes back empty. Before, that one student's empty
list failed the whole poll, every poll (issue #10). Now the student is
skipped, the others are checked, and the skip is stated by `lastbell status`
and the dashboard instead of being left in a log line."""
from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest

from lastbell import cli, health, preflight, store
from lastbell.client import LoginError, ParentVueError
from lastbell.collector import collect_student
from lastbell.dashboard.render import render_overview

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
EMPTY = "<div class='gb-panel'><div class='msg'>No data available</div></div>"

CASEY = SimpleNamespace(agu="1", name="Casey Harlow", school="Example HS")
VAL = SimpleNamespace(agu="2", name="Val Harlow", school="")
CONF = SimpleNamespace(canvas="off", ungraded_grace_days=2, lookahead_days=3,
                       grade_drop_points=5.0, username="someparent")


class Portal:
    """Serves the secondary fixture to every student except those in
    ``empty`` (an empty class list) or ``raises`` (an exception)."""

    def __init__(self, children, *, empty=(), raises=None):
        self.children, self.empty, self.raises = children, set(empty), raises or {}
        self.last_gradebook_html = "<html/>"

    def login(self):
        pass

    def get_children(self):
        return self.children

    def get_focus_args(self, agu):
        self._agu = agu
        return SimpleNamespace(args={"classID": 1, "OrgYearGU": "x"}, agu_header="0",
                               org_year_gu="x", grade_period_gu="", school_id="",
                               as_parameters=lambda: {"classID": 1})

    def load_control(self, control, params, agu_header="0"):
        if self._agu in self.raises:
            raise self.raises[self._agu]
        if self._agu in self.empty:
            return EMPTY
        name = ("schoolclasses_secondary.html" if control == "Gradebook_SchoolClasses"
                else "classdetails_sample.html")
        return (FIXTURES / name).read_text(encoding="utf-8")

    def class_details(self, params, agu_header="0"):
        return self.load_control("Gradebook_ClassDetails", params, agu_header)


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.ensure_schema(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def no_pauses(monkeypatch):
    monkeypatch.setattr("lastbell.collector.CLASS_FETCH_DELAY_S", 0)
    monkeypatch.setattr("lastbell.collector.time.sleep", lambda s: None)


def _saved(conn):
    return [r[0] for r in conn.execute("SELECT initials FROM students ORDER BY id")]


# ── the poll ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("children", [[CASEY, VAL], [VAL, CASEY]],
                         ids=["empty student last", "empty student first"])
def test_an_empty_student_is_skipped_and_the_rest_are_checked(conn, children):
    cli._run_once(Portal(children, empty={"2"}), conn, notifier=None, conf=CONF)
    assert _saved(conn) == ["C.H."]
    assert store.last_poll(conn)                       # the poll counted
    assert store.no_gradebook(conn) == ["V.H."]


def test_the_skip_is_forgotten_once_the_student_has_a_gradebook(conn):
    cli._run_once(Portal([CASEY, VAL], empty={"2"}), conn, notifier=None, conf=CONF)
    cli._run_once(Portal([CASEY, VAL]), conn, notifier=None, conf=CONF)
    assert store.no_gradebook(conn) == []
    assert _saved(conn) == ["C.H.", "V.H."]


def test_every_student_empty_is_a_failed_poll_not_a_quiet_one(conn):
    """One empty student among several is a child without grades. All of
    them empty is far more likely a portal that changed: guardians hear."""
    with pytest.raises(health.ClassesUnreadable) as exc:
        cli._run_once(Portal([CASEY, VAL], empty={"1", "2"}), conn,
                      notifier=None, conf=CONF)
    assert exc.value.initials == ["C.H.", "V.H."]
    assert health.classify(exc.value) == "unread"
    assert store.no_gradebook(conn) == []              # not "skipped": failed


def test_a_portal_error_for_one_student_fails_the_poll_after_the_rest_is_saved(conn):
    """An error that isn't "no gradebook" is not a skip: the other student
    is saved, then the poll fails so the guardians are told."""
    boom = ParentVueError("LoadControl(Gradebook_SchoolClasses) did not return JSON (HTTP 500).")
    with pytest.raises(health.ClassesUnreadable) as exc:
        cli._run_once(Portal([VAL, CASEY], raises={"2": boom}), conn,
                      notifier=None, conf=CONF)
    assert exc.value.initials == ["V.H."]
    assert _saved(conn) == ["C.H."]
    assert store.no_gradebook(conn) == []


def test_a_rejected_sign_in_still_fails_the_poll_at_once(conn):
    with pytest.raises(LoginError):
        cli._run_once(Portal([CASEY, VAL], raises={"1": LoginError("rejected")}),
                      conn, notifier=None, conf=CONF)


def test_collect_reports_the_empty_student_instead_of_raising():
    col = collect_student(Portal([VAL], empty={"2"}), VAL, delay_s=0)
    assert col.empty_gradebook and col.classes == []
    assert "neither terms nor rows" in col.errors[0]


# ── never silent: the dashboard says who is skipped (status: test_status) ──


def test_the_dashboard_names_the_skipped_student():
    student = {"id": "1", "agu": "1", "name": "Casey Harlow", "initials": "C.H.",
               "school": "Example HS", "current_term": "MP1"}
    html = render_overview([student], {}, {}, "2026-11-09 20:00:00", not_watched=["V.H."])
    assert "Not watched: V.H. — the portal shows no gradebook for this student." in html
    assert "Not watched" not in render_overview([student], {}, {}, "2026-11-09 20:00:00")


# ── preflight reads every student, and --dump saves each class list ───


def _preflight(portal, dump_dir=None):
    def get(url, **kw):
        return SimpleNamespace(status_code=404, text="", headers={})

    def post(url, **kw):
        return SimpleNamespace(status_code=404, text="", headers={})
    return preflight.run_full("x.example", "https://x.example", "u", "p",
                              client=portal, dump_dir=dump_dir, get=get, post=post)


def test_preflight_flags_an_empty_second_student_without_naming_them(tmp_path):
    report = _preflight(Portal([CASEY, VAL], empty={"2"}), dump_dir=tmp_path)
    check = next(c for c in report.checks if c.id == "other_students")
    assert check.status == preflight.WARN
    assert "student 2: nothing in the class list" in check.detail
    assert report.verdict == "go"                       # the first student reads
    assert (tmp_path / "schoolclasses_fragment_student2.html").read_text() == EMPTY
    md = preflight.render_markdown(report)
    assert "Val" not in md and "Harlow" not in md


def test_preflight_passes_a_second_student_with_classes():
    report = _preflight(Portal([CASEY, VAL]))
    check = next(c for c in report.checks if c.id == "other_students")
    assert check.status == preflight.PASS and "student 2: " in check.detail


def test_preflight_with_one_student_has_no_extra_check():
    report = _preflight(Portal([CASEY]))
    assert not any(c.id == "other_students" for c in report.checks)
