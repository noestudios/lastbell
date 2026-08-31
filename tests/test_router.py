"""Routing: subscriptions -> deliveries, wildcards, fallback, failure isolation."""
from __future__ import annotations

import pytest

from mcpsgradewatch import router, store, watchers
from mcpsgradewatch.differ import Event
from mcpsgradewatch.models import AlertType, Snapshot, Student, WatcherKind


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "test.db")
    store.ensure_schema(c)
    store.persist_snapshot(
        c, Student(agu="1", name="Jasper P. Hays", initials="J.P.H."),
        Snapshot(student_agu="1"))
    yield c
    c.close()


GRADE = Event(type=AlertType.GRADE_CHANGED, student_agu="1",
              course_title="Math", detail="Math: quiz graded: 8/10")
MISSING = Event(type=AlertType.ASSIGNMENT_MISSING, student_agu="1",
                course_title="Art", detail="Art: “Collage” is marked missing")


class FakeChannel:
    def __init__(self, name, fail=False):
        self.name, self.fail, self.calls = name, fail, []

    def send(self, to, subject, body):
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append((to, subject, body))


def test_no_subscriptions_means_fallback(conn):
    deliveries, warnings = router.plan(conn, "1", [GRADE])
    assert deliveries == [] and warnings == []
    assert not router.has_subscriptions(conn, "1")


def test_wildcard_subscription_hits_all_configured_channels(conn):
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                             {"email": {"to": "mom@example.com"},
                              "ntfy": {"topic": "t"}})
    watchers.subscribe(conn, w, "1")   # '*' types, '*' channels
    deliveries, warnings = router.plan(conn, "1", [GRADE, MISSING])
    assert warnings == []
    assert {(d.channel, len(d.events)) for d in deliveries} == {("email", 2), ("ntfy", 2)}
    assert all(d.watcher_name == "Mom" for d in deliveries)


def test_type_filter_and_channel_filter(conn):
    w = watchers.add_watcher(conn, "Jasper", WatcherKind.STUDENT,
                             {"ntfy": {"topic": "jasper-grades"}})
    watchers.subscribe(conn, w, "1", ["assignment_missing"], ["ntfy"])
    deliveries, _ = router.plan(conn, "1", [GRADE, MISSING])
    (d,) = deliveries
    assert d.events == [MISSING]
    assert d.to == {"topic": "jasper-grades"}


def test_unconfigured_channel_warns_instead_of_crashing(conn):
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN)   # no addresses
    watchers.subscribe(conn, w, "1", None, ["telegram"])
    deliveries, warnings = router.plan(conn, "1", [GRADE])
    assert deliveries == []
    assert len(warnings) == 1 and "set-channel" in warnings[0]


def test_overlapping_subscriptions_dedupe_into_one_delivery(conn):
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                             {"email": {"to": "mom@example.com"}})
    watchers.subscribe(conn, w, "1", ["grade_changed"], ["email"])
    watchers.subscribe(conn, w, "1", None, ["email"])   # '*' overlaps the above
    deliveries, _ = router.plan(conn, "1", [GRADE])
    (d,) = deliveries
    assert d.events == [GRADE]   # not duplicated


def test_dispatch_sends_and_isolates_failures(conn):
    channels = {"email": FakeChannel("email"), "ntfy": FakeChannel("ntfy", fail=True)}
    deliveries = [
        router.Delivery("Mom", "email", {"to": "mom@example.com"}, [GRADE]),
        router.Delivery("Jasper", "ntfy", {"topic": "t"}, [GRADE]),
    ]
    sent, warnings = router.dispatch(deliveries, "J.P.H.",
                                     channel_factory=lambda n: channels[n])
    assert sent == 1
    assert len(warnings) == 1 and "Jasper" in warnings[0]
    ((to, subject, body),) = channels["email"].calls
    assert to == {"to": "mom@example.com"}
    assert subject == "[MCPSGradeWatch] 1 update for J.P.H."
    assert "quiz graded" in body


def test_subject_pluralizes():
    assert router.subject("J.P.H.", [GRADE, MISSING]).startswith("[MCPSGradeWatch] 2 updates")
