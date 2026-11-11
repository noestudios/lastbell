"""Canvas scores for an observer (0.3.10). Until this release a parent's
poll received no submission from Canvas, so every Canvas item was stored as
"due". The first poll that reads scores is reading history: it records them
without alerting. Stored rows that Canvas work no longer includes and that
were never scored are removed."""
from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest

from lastbell import canvas, cli, differ, store
from lastbell.models import (
    SOURCE_CANVAS,
    AlertType,
    Assignment,
    AssignmentStatus,
    Course,
    Snapshot,
    Student,
)

TODAY = datetime.date(2026, 11, 10)
STUDENT = Student(agu="7", name="Casey Harlow", school="Example MS", initials="C.H.")


def _work(n, name, status=AssignmentStatus.DUE, score=None, due=datetime.date(2026, 11, 4),
          course="C1", source=SOURCE_CANVAS, **kw):
    return Assignment(edupoint_gu=f"canvas:{n}" if source == SOURCE_CANVAS else f"pv:{n}",
                      course_gu=course, name=name, status=status, score=score,
                      points=10.0, due_date=due, source=source, **kw)


def _snap(*assignments):
    return Snapshot(student_agu="7", term="Q2",
                    courses=[Course(edupoint_gu="C1", title="English 6", term="Q2")],
                    assignments=list(assignments))


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.ensure_schema(c)
    yield c
    c.close()


# ── the differ ────────────────────────────────────────────────────────


def _before_and_after():
    before = _snap(_work(1, "Check-In", AssignmentStatus.UNGRADED_PAST_DUE),
                   _work(2, "Response Log", AssignmentStatus.UNGRADED_PAST_DUE))
    after = _snap(_work(1, "Check-In", AssignmentStatus.GRADED, score=10.0),
                  _work(2, "Response Log", AssignmentStatus.MISSING))
    return before, after


def test_scores_and_missing_flags_alert_on_an_ordinary_poll():
    before, after = _before_and_after()
    kinds = sorted(e.type for e in differ.diff(before, after, today=TODAY))
    assert kinds == sorted([AlertType.GRADE_CHANGED, AlertType.ASSIGNMENT_MISSING])


def test_the_first_read_of_canvas_scores_is_quiet():
    before, after = _before_and_after()
    assert differ.diff(before, after, today=TODAY, quiet_canvas=True) == []


def test_a_quiet_canvas_read_still_announces_gradebook_grades():
    before = _snap(_work(1, "Essay", source="parentvue"))
    after = _snap(_work(1, "Essay", AssignmentStatus.GRADED, score=9.0, source="parentvue"))
    events = differ.diff(before, after, today=TODAY, quiet_canvas=True)
    assert [e.type for e in events] == [AlertType.GRADE_CHANGED]


def test_a_quiet_canvas_read_does_not_announce_old_disagreements():
    """A hidden Canvas twin whose score now arrives and differs from the
    gradebook's: history on the first read, never repeated afterwards."""
    pv = _work(1, "Quiz", AssignmentStatus.GRADED, score=7.0, source="parentvue")
    twin_before = _work(2, "Quiz", superseded_by="pv:1")
    twin_after = _work(2, "Quiz", AssignmentStatus.GRADED, score=9.0, superseded_by="pv:1")
    before, after = _snap(pv, twin_before), _snap(pv, twin_after)
    assert differ.diff(before, after, today=TODAY, quiet_canvas=True) == []
    assert [e.type for e in differ.diff(before, after, today=TODAY)] == [
        AlertType.SOURCE_CONFLICT]
    assert differ.diff(after, after, today=TODAY) == []     # and not on later polls


# ── stored rows Canvas no longer keeps ────────────────────────────────


def _names(conn):
    return sorted(r[0] for r in conn.execute("SELECT name FROM assignments"))


def test_untouched_canvas_rows_that_are_no_longer_kept_are_removed(conn):
    """What 0.3.9 stored for issue #10: an undated tool placeholder and a
    row with no name, both "due" for ever."""
    store.persist_snapshot(conn, STUDENT, _snap(
        _work(1, "Check-In", AssignmentStatus.UNGRADED_PAST_DUE),
        _work(2, "Tool placeholder", due=None),
        _work(3, "", due=None),
        _work(4, "Old quiz", AssignmentStatus.GRADED, score=8.0),
        _work(5, "Essay", source="parentvue")), prune_canvas=True)
    store.persist_snapshot(conn, STUDENT, _snap(
        _work(1, "Check-In", AssignmentStatus.GRADED, score=10.0)), prune_canvas=True)
    # the scored row and the gradebook row are history and stay
    assert _names(conn) == ["Check-In", "Essay", "Old quiz"]


def test_nothing_is_removed_when_canvas_was_not_read_for_the_course(conn):
    first = _snap(_work(1, "Check-In"), _work(2, "Tool placeholder", due=None))
    store.persist_snapshot(conn, STUDENT, first, prune_canvas=True)
    # Canvas failed this poll: the snapshot has gradebook work only
    store.persist_snapshot(conn, STUDENT, _snap(_work(9, "Essay", source="parentvue")),
                           prune_canvas=False)
    store.persist_snapshot(conn, STUDENT, _snap(_work(9, "Essay", source="parentvue")),
                           prune_canvas=True)     # read, but this course gave nothing
    assert _names(conn) == ["Check-In", "Essay", "Tool placeholder"]


# ── the poll: quiet once, then never again ────────────────────────────


class _Layer:
    """Stands in for canvas.CanvasLayer: one matched student, one course."""

    def __init__(self, rows, fail=False):
        self.rows, self.fail, self.matched = rows, fail, {"7": object()}

    def collect_for(self, agu):
        if self.fail:
            return None
        cc = canvas.CanvasCourse(id=1, name="English 6-Quill-YR-2027", title="English 6",
                                 teacher="Quill", term="YR")
        cc.assignments = [canvas.to_assignment(r, cc.gu) for r in self.rows]
        return canvas.CanvasCollection(observee=canvas.Observee(id=9, name="x"), courses=[cc])

    def merge(self, snapshot, collection):
        return canvas.merge(snapshot, collection)


def _row(i, name, **sub):
    return {"id": i, "name": name, "due_at": "2026-11-04T16:00:00Z", "points_possible": 10.0,
            "published": True, "submission": {"workflow_state": "unsubmitted", "score": None,
                                              **sub}}


def _poll(conn, monkeypatch, layer, sent):
    from lastbell import collector
    pv = Course(edupoint_gu="C1", title="English 6", teacher="Robin Quill", term="Q2")
    col = collector.StudentCollection(
        student=STUDENT,
        school_classes=SimpleNamespace(current_term="Q2", mark_periods=[]),
        classes=[collector.CollectedClass(course=pv, details=SimpleNamespace(
            assignments=[], missing_text=""))])
    monkeypatch.setattr(collector, "collect_student", lambda client, child, **kw: col)
    monkeypatch.setattr(cli, "_canvas_layer", lambda client, children, conf: layer)
    client = SimpleNamespace(get_children=lambda: [SimpleNamespace(agu="7", name="Casey Harlow",
                                                                   school="")])
    conf = SimpleNamespace(canvas="auto", ungraded_grace_days=2, lookahead_days=3,
                           grade_drop_points=5.0, username="someparent")
    notifier = SimpleNamespace(send=lambda subject, body: sent.append(body))
    return cli._run_once(client, conn, notifier, conf)


def test_upgrading_reads_existing_scores_quietly_then_alerts_on_new_ones(conn, monkeypatch):
    sent: list[str] = []
    # what 0.3.9 left behind: Canvas work with no scores, flagged past due
    store.persist_snapshot(conn, STUDENT, Snapshot(
        student_agu="7", term="Q2",
        courses=[Course(edupoint_gu="C1", title="English 6", term="Q2")],
        assignments=[_work(1, "Check-In", AssignmentStatus.UNGRADED_PAST_DUE),
                     _work(2, "Paragraph Review", AssignmentStatus.UNGRADED_PAST_DUE)]))
    graded = dict(workflow_state="graded", score=10.0, graded_at="2026-11-05T12:00:00Z")

    # first poll on 0.3.10: one score was there all along — recorded, not sent
    n = _poll(conn, monkeypatch, _Layer([_row(1, "Check-In", **graded),
                                         _row(2, "Paragraph Review")]), sent)
    assert n == 0 and sent == []
    assert store.get_meta(conn, store.CANVAS_SCORES_READ_KEY) == "1"
    scores = dict(conn.execute("SELECT name, score FROM assignments"))
    assert scores == {"Check-In": 10.0, "Paragraph Review": None}

    # next poll: a score that is actually new is announced
    n = _poll(conn, monkeypatch, _Layer([_row(1, "Check-In", **graded),
                                         _row(2, "Paragraph Review", **graded)]), sent)
    alerts = [r[0] for r in conn.execute("SELECT body FROM alerts")]
    assert n == 1 and len(alerts) == 1
    assert "Paragraph Review" in alerts[0] and "Check-In" not in alerts[0]


def test_the_quiet_read_waits_for_a_poll_where_canvas_answered(conn, monkeypatch):
    sent: list[str] = []
    store.persist_snapshot(conn, STUDENT, Snapshot(
        student_agu="7", term="Q2",
        courses=[Course(edupoint_gu="C1", title="English 6", term="Q2")],
        assignments=[_work(1, "Check-In", AssignmentStatus.UNGRADED_PAST_DUE)]))
    _poll(conn, monkeypatch, _Layer([], fail=True), sent)          # Canvas down
    assert store.get_meta(conn, store.CANVAS_SCORES_READ_KEY) is None
    graded = dict(workflow_state="graded", score=10.0, graded_at="2026-11-05T12:00:00Z")
    n = _poll(conn, monkeypatch, _Layer([_row(1, "Check-In", **graded)]), sent)
    assert n == 0 and sent == []                                   # still the first read
    assert store.get_meta(conn, store.CANVAS_SCORES_READ_KEY) == "1"
