"""Store tests: snapshot round-trip, idempotent upserts, and the change log."""
from __future__ import annotations

import datetime

import pytest

from lastbell import store
from lastbell.differ import Event
from lastbell.models import (
    AlertType,
    Assignment,
    AssignmentStatus,
    Course,
    Snapshot,
    Student,
)


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "test.db")
    store.ensure_schema(c)
    yield c
    c.close()


STUDENT = Student(agu="1", name="Jasper P. Hays", school="Example ES", initials="J.P.H.")


def _snapshot(score=8.0, status=AssignmentStatus.GRADED, percent="87.20%") -> Snapshot:
    return Snapshot(
        student_agu="1",
        courses=[Course(edupoint_gu="709775", title="Math", teacher="Pat Example",
                        term="MP1", mark="B+", percent=percent)],
        assignments=[Assignment(
            edupoint_gu="11110001", course_gu="709775", name="Fractions Quiz",
            kind="Assessment", due_date=datetime.date(2026, 9, 12),
            score=score, points=10.0, status=status,
        )],
    )


def test_unknown_student_loads_as_none(conn):
    assert store.load_snapshot(conn, "999") is None


def test_snapshot_round_trip(conn):
    store.persist_snapshot(conn, STUDENT, _snapshot())
    loaded = store.load_snapshot(conn, "1")

    assert loaded is not None
    assert [c.title for c in loaded.courses] == ["Math"]
    assert loaded.courses[0].percent == "87.20%"
    (a,) = loaded.assignments
    assert a.edupoint_gu == "11110001"
    assert a.course_gu == "709775"        # restored via the course join
    assert a.due_date == datetime.date(2026, 9, 12)
    assert a.score == 8.0
    assert a.status is AssignmentStatus.GRADED


def test_repersisting_identical_snapshot_writes_no_history(conn):
    store.persist_snapshot(conn, STUDENT, _snapshot())
    store.persist_snapshot(conn, STUDENT, _snapshot())
    assert conn.execute("SELECT COUNT(*) FROM grade_history").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 1


def test_changed_fields_are_logged_to_history(conn):
    store.persist_snapshot(conn, STUDENT, _snapshot(score=8.0))
    store.persist_snapshot(conn, STUDENT, _snapshot(score=9.0))

    rows = conn.execute("SELECT field, old_value, new_value FROM grade_history").fetchall()
    assert [(r["field"], r["old_value"], r["new_value"]) for r in rows] == [
        ("score", "8.0", "9.0"),
    ]
    # and the live row reflects the new state
    assert store.load_snapshot(conn, "1").assignments[0].score == 9.0


def test_vanished_assignment_is_kept(conn):
    store.persist_snapshot(conn, STUDENT, _snapshot())
    empty = Snapshot(student_agu="1",
                     courses=[Course(edupoint_gu="709775", title="Math", term="MP1")],
                     assignments=[])
    store.persist_snapshot(conn, STUDENT, empty)
    assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 1


def test_record_alert(conn):
    store.persist_snapshot(conn, STUDENT, _snapshot())
    store.record_alert(conn, "1", Event(
        type=AlertType.GRADE_CHANGED, student_agu="1",
        course_title="Math", detail="Math: “Fractions Quiz” graded: 8/10",
    ))
    row = conn.execute("SELECT type, body FROM alerts").fetchone()
    assert row["type"] == "grade_changed"
    assert "Fractions Quiz" in row["body"]


def test_course_grade_changes_are_logged(conn):
    store.persist_snapshot(conn, STUDENT, _snapshot(percent="87.20%"))
    store.persist_snapshot(conn, STUDENT, _snapshot(percent="91.00%"))
    rows = conn.execute(
        "SELECT field, old_value, new_value FROM course_history").fetchall()
    assert [(r["field"], r["old_value"], r["new_value"]) for r in rows] == [
        ("percent", "87.20%", "91.00%"),
    ]


def test_first_and_identical_course_persists_log_nothing(conn):
    store.persist_snapshot(conn, STUDENT, _snapshot())     # first sighting
    store.persist_snapshot(conn, STUDENT, _snapshot())     # unchanged
    assert conn.execute("SELECT COUNT(*) FROM course_history").fetchone()[0] == 0


def test_new_term_course_starts_fresh_history(conn):
    store.persist_snapshot(conn, STUDENT, _snapshot(percent="87.20%"))
    mp2 = _snapshot(percent="50.00%")
    mp2.courses[0].term = "MP2"          # new (gu, term) key -> new course row
    store.persist_snapshot(conn, STUDENT, mp2)
    assert conn.execute("SELECT COUNT(*) FROM course_history").fetchone()[0] == 0
