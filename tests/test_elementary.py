"""The elementary gradebook since 2026-09-15: one class per subject, opened
from a data row under each subject header, and (since the 2026-10 portal
upgrade) a standards view unless the assignment view is asked for by name.
Fixtures are synthetic, shaped like the pages MCPS served on 2026-10-16."""
from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest

from lastbell import health, store
from lastbell.client import ParentVueClient
from lastbell.collector import StudentCollection, collect_student
from lastbell.gradebook import (
    ParseError,
    SchoolClasses,
    parse_class_details,
    parse_school_classes,
)
from lastbell.models import Student

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ── parsing ───────────────────────────────────────────────────────────


def test_subject_rows_take_their_name_and_teacher_from_the_header():
    sc = parse_school_classes(_fixture("schoolclasses_elementary.html"))
    classes = [r for r in sc.rows if r.focus.get("FocusArgs")]
    assert [(r.title, r.teacher, r.teacher_id) for r in classes] == [
        ("Art", "Robin Quill", "9001"),
        ("Mathematics", "Dana Ellery", "9002"),
    ]
    assert [r.focus["FocusArgs"]["classID"] for r in classes] == [500101, 500102]


def test_a_class_resources_link_is_not_a_class():
    """The headers' links open the homeroom's resources (subject -1, every
    subject's work at once); following them would double every assignment."""
    sc = parse_school_classes(_fixture("schoolclasses_elementary.html"))
    headers = [r for r in sc.rows if r.title in ("Art", "Mathematics") and not r.focus]
    assert [r.teacher for r in headers] == ["Robin Quill", "Dana Ellery"]


def test_secondary_rich_content_links_still_open_the_class():
    html = _fixture("schoolclasses_secondary.html").replace(
        "Gradebook_ClassDetails", "Gradebook_RichContentClassDetails")
    sc = parse_school_classes(html)
    assert sum(1 for r in sc.rows if r.focus.get("FocusArgs")) == 2


def test_elementary_assignment_view_has_no_class_grade():
    cd = parse_class_details(_fixture("classdetails_elementary_assignments.html"),
                             course_gu="500102")
    assert (cd.mark, cd.percent) == ("", "")
    assert [a.name for a in cd.assignments] == ["Place Value Check", "Measurement Project"]
    assert cd.assignments[0].score == 3.0 and cd.assignments[0].points == 4.0


def test_the_standards_view_is_not_mistaken_for_assignments():
    with pytest.raises(ParseError):
        parse_class_details(_fixture("classdetails_standards.html"))


# ── fetching ──────────────────────────────────────────────────────────


def test_class_details_asks_for_the_assignment_view(monkeypatch):
    client = ParentVueClient("https://x.example", "u", "p")
    sent = []
    monkeypatch.setattr(client, "load_control",
                        lambda control, params, agu_header="0": sent.append((control, params)))
    client.class_details({"classID": 1, "viewName": None})
    client.class_details({"classID": 2, "viewName": "standard"})
    assert sent == [
        ("Gradebook_ClassDetails", {"classID": 1, "viewName": "assignment"}),
        # a view the portal itself asked for is left alone
        ("Gradebook_ClassDetails", {"classID": 2, "viewName": "standard"}),
    ]


class _ElementaryClient:
    def __init__(self):
        self.views = []

    def get_focus_args(self, agu):
        return SimpleNamespace(args={"classID": 500100}, agu_header="0",
                               as_parameters=lambda: {"AGU": "0"})

    def load_control(self, control, params, agu_header="0"):
        assert control == "Gradebook_SchoolClasses"
        return _fixture("schoolclasses_elementary.html")

    def class_details(self, params, agu_header="0"):
        self.views.append(params["classID"])
        return _fixture("classdetails_elementary_assignments.html")


def test_collect_reads_each_subject_once_and_skips_the_homeroom():
    client = _ElementaryClient()
    col = collect_student(client, SimpleNamespace(agu="7", name="Jules P. Harlow",
                                                  school="Example ES"), delay_s=0)
    assert col.errors == []
    assert [c.course.title for c in col.classes] == ["Art", "Mathematics"]
    assert [c.course.teacher for c in col.classes] == ["Robin Quill", "Dana Ellery"]
    assert client.views == [500101, 500102]   # never 500100, the homeroom


# ── never fail silently ───────────────────────────────────────────────


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.ensure_schema(c)
    yield c
    c.close()


def _collection(agu, initials, *, classes, errors):
    sc = SchoolClasses(school_name="Example", school_id="1", org_year_gu="o",
                       current_term="MP1")
    return StudentCollection(
        student=Student(agu=agu, name=f"Student {agu}", school="Example",
                        initials=initials),
        school_classes=sc, classes=classes, errors=errors)


def test_a_student_with_every_class_unread_fails_the_poll_after_the_rest_is_saved(
        conn, monkeypatch):
    from lastbell import cli, collector

    readable = SimpleNamespace(agu="1", name="A", school="")
    unreadable = SimpleNamespace(agu="2", name="B", school="")
    by_agu = {
        "1": _collection("1", "A.H.", classes=[], errors=[]),
        "2": _collection("2", "J.H.", classes=[], errors=["MP1: not understood"]),
    }
    monkeypatch.setattr(collector, "collect_student", lambda client, child: by_agu[child.agu])
    client = SimpleNamespace(get_children=lambda: [readable, unreadable])
    conf = SimpleNamespace(canvas="off", ungraded_grace_days=2, lookahead_days=3,
                           grade_drop_points=5.0, username="someparent")

    with pytest.raises(health.ClassesUnreadable) as exc:
        cli._run_once(client, conn, notifier=None, conf=conf)
    assert exc.value.initials == ["J.H."]
    # both students were still persisted before the poll was failed
    assert {r[0] for r in conn.execute("SELECT id FROM students")} == {"1", "2"}
    assert health.classify(exc.value) == "unread"


def test_unread_notice_names_the_student_by_initials_and_says_what_to_do(conn):
    for _ in range(health.NOTIFY_AFTER["unread"]):
        s = health.record_failure(conn, "unread", str(health.ClassesUnreadable(["J.H."])))
    assert s.notice_due
    subject, body = health.failure_message(s, host="pi")
    assert "J.H." in subject and "action needed" in subject
    assert "lastbell upgrade" in body and "other students are still being checked" in body
    assert "<code>lastbell upgrade</code>" in body.html
    assert "J.H.'s classes couldn't be read" in health.dashboard_note(s)

    subject, body = health.recovery_message(s)
    assert subject == "[Last Bell] reading J.H.'s classes again"
    assert "taken in quietly" in body
