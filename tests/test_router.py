"""Routing: subscriptions -> deliveries, wildcards, fallback, failure isolation."""
from __future__ import annotations

import pytest

from lastbell import router, store, watchers
from lastbell.differ import Event
from lastbell.models import AlertType, Snapshot, Student, WatcherKind


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
    assert subject == "[Last Bell] 1 update for J.P.H."
    assert "quiz graded" in body


def test_subject_pluralizes():
    assert router.subject("J.P.H.", [GRADE, MISSING]).startswith("[Last Bell] 2 updates")


def test_urgent_now_beats_the_digest_hour(conn):
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                             {"email": {"to": "mom@example.com"}})
    watchers.subscribe(conn, w, "1", send_at="16:00", urgent_now=True)
    deliveries, warnings = router.plan(conn, "1", [GRADE, MISSING])
    assert warnings == []
    schedule = {e.type: d.send_at for d in deliveries for e in d.events}
    assert schedule == {AlertType.GRADE_CHANGED: "16:00",
                       AlertType.ASSIGNMENT_MISSING: None}


def test_without_urgent_flag_everything_waits_for_the_digest(conn):
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                             {"email": {"to": "mom@example.com"}})
    watchers.subscribe(conn, w, "1", send_at="16:00")
    deliveries, _ = router.plan(conn, "1", [GRADE, MISSING])
    assert {d.send_at for d in deliveries} == {"16:00"}


def test_sms_channel_rides_the_email_transport(conn):
    w = watchers.add_watcher(conn, "Mom", WatcherKind.GUARDIAN,
                             {"sms": {"to": "3015551234@vtext.com"}})
    watchers.subscribe(conn, w, "1", None, ["sms"])
    deliveries, warnings = router.plan(conn, "1", [GRADE])
    assert warnings == []
    fake = FakeChannel("sms")
    sent, send_warnings = router.dispatch(deliveries, "J.P.H.",
                                          channel_factory=lambda n: fake)
    assert sent == 1 and send_warnings == []
    assert fake.calls[0][0] == {"to": "3015551234@vtext.com"}
    # the real registry maps sms to the email transport class
    from lastbell.notify import ADDRESS_KEY
    assert ADDRESS_KEY["sms"] == "to"


# ── 0.1.3: dead carrier gateways are refused at entry ─────────────────


def test_validate_address_refuses_att_gateway():
    from lastbell import notify
    for domain in ("txt.att.net", "TXT.ATT.NET", "mms.att.net"):
        with pytest.raises(ValueError, match="AT&T shut down"):
            notify.validate_address("sms", f"3015551234@{domain}")
        with pytest.raises(ValueError, match="AT&T shut down"):
            notify.validate_address("email", f"3015551234@{domain}")
    assert notify.validate_address("sms", " 3015551234@vtext.com ") == "3015551234@vtext.com"
    with pytest.raises(ValueError) as exc:
        notify.validate_address("sms", "3015551234")
    assert "att.net" not in str(exc.value)             # no longer suggested


def test_send_test_uses_the_channel_transport(monkeypatch):
    from lastbell import notify
    calls = []

    class Fake:
        name = "email"

        def send(self, to, subject, body):
            calls.append((to, subject, body))
    monkeypatch.setattr(notify, "channel", lambda name: Fake())
    notify.send_test("email", {"to": "mom@example.com"})
    (to, subject, body), = calls
    assert to == {"to": "mom@example.com"}
    assert subject == "Last Bell test" and "test message" in body
