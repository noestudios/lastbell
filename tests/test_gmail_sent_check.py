"""After Gmail answers "451 temporarily rejected", Last Bell looks for the
message in Sent Mail before retrying: Gmail sometimes says 451 for a
message it has already delivered, and a retry would be a second copy."""
from __future__ import annotations

import imaplib
import logging
import smtplib

import pytest

from lastbell.notify import email as email_mod
from lastbell.notify.email import SmtpTransport, _sent_folder, gmail_sent_has

LIST_LINES = [
    b'(\\HasNoChildren) "/" "INBOX"',
    b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"',
    b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Gesendet"',   # localized name
]


class FakeImap:
    """Gmail's IMAP: Sent Mail holds ``sent`` Message-IDs; ``arrive_after``
    searches miss before the message shows up there."""

    instances: list = []

    def __init__(self, *a, sent=(), arrive_after=0, fail=None, **kw):
        self.sent, self.arrive_after, self.fail = set(sent), arrive_after, fail
        self.calls, self.searches, self.logged_out = [], 0, False
        FakeImap.instances.append(self)

    def login(self, user, password):
        if self.fail:
            raise self.fail
        self.calls.append(("login", user))

    def list(self):
        return "OK", LIST_LINES

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        self.searches += 1
        _, header, value = criteria
        assert header == "Message-ID"
        found = value in self.sent and self.searches > self.arrive_after
        return "OK", [b"7" if found else b""]

    def logout(self):
        self.logged_out = True


class FakeSmtp:
    """Gmail's SMTP answering the end of every message with ``reply``."""

    reply: tuple | None = (451, b"4.3.0 Mail server temporarily rejected message.")
    last = None

    def __init__(self, *a, **kw):
        FakeSmtp.last = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        self.msg = msg
        if FakeSmtp.reply:
            raise smtplib.SMTPDataError(*FakeSmtp.reply)


@pytest.fixture
def gmail(monkeypatch):
    FakeImap.instances = []
    FakeSmtp.reply = (451, b"4.3.0 Mail server temporarily rejected message.")
    state = {"sent_ids": set(), "imap_fail": None}

    def imap(*a, **kw):
        # Whatever the SMTP fake was handed counts as delivered when the
        # test says so ("in Sent Mail").
        sent = {FakeSmtp.last.msg["Message-ID"]} if state["sent_ids"] else set()
        return FakeImap(sent=sent, fail=state["imap_fail"])

    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", imap)
    monkeypatch.setattr(email_mod, "SENT_CHECK_WAIT_S", 0)
    return state


def _gmail_transport(host="smtp.gmail.com"):
    return SmtpTransport(host=host, port=587, user="parent@example.com",
                         password="app-password", sender="parent@example.com")


@pytest.fixture
def warnings_logged():
    """What the "lastbell" logger said. A handler on that logger itself:
    the CLI turns off its propagation to the root, and the caplog of the
    older pytest that Python 3.9 gets only listens at the root."""
    records: list[str] = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("lastbell")
    handler = Keep(level=logging.WARNING)
    logger.addHandler(handler)
    yield records
    logger.removeHandler(handler)


def test_a_451_for_a_message_already_in_sent_mail_is_not_resent(gmail, warnings_logged):
    gmail["sent_ids"] = {"yes"}
    _gmail_transport().deliver("parent@example.com", "digest", "body")  # no raise
    (imap,) = FakeImap.instances
    assert ("select", '"[Gmail]/Gesendet"', True) in imap.calls     # read-only
    assert imap.logged_out
    assert any("in Sent Mail" in w and "not resending" in w for w in warnings_logged)


def test_a_451_for_a_message_not_in_sent_mail_is_retried(gmail):
    with pytest.raises(smtplib.SMTPDataError):
        _gmail_transport().deliver("parent@example.com", "digest", "body")
    (imap,) = FakeImap.instances
    assert imap.searches == email_mod.SENT_CHECK_TRIES      # waited for it to arrive


def test_an_imap_problem_means_retry(gmail, warnings_logged):
    gmail["sent_ids"] = {"yes"}
    gmail["imap_fail"] = imaplib.IMAP4.error("IMAP access is disabled")
    with pytest.raises(smtplib.SMTPDataError):
        _gmail_transport().deliver("parent@example.com", "digest", "body")
    assert any("couldn't check Gmail's Sent Mail" in w for w in warnings_logged)


@pytest.mark.parametrize("host,reply", [
    ("smtp.example.com", (451, b"try later")),         # not Gmail: nothing to ask
    ("smtp.gmail.com", (550, b"5.7.1 rejected")),       # permanent: no doubt to settle
])
def test_only_a_gmail_4xx_is_checked(gmail, host, reply):
    FakeSmtp.reply = reply
    with pytest.raises(smtplib.SMTPDataError):
        _gmail_transport(host).deliver("parent@example.com", "digest", "body")
    assert FakeImap.instances == []


def test_the_sent_folder_is_found_by_its_flag_not_its_name():
    class Lister:
        def list(self):
            return "OK", LIST_LINES
    assert _sent_folder(Lister()) == '"[Gmail]/Gesendet"'


def test_a_message_that_reaches_sent_mail_late_is_still_found():
    imap = FakeImap(sent={"<m@x>"}, arrive_after=2)
    assert gmail_sent_has("u", "p", "<m@x>", tries=3, wait_s=0,
                          imap_factory=lambda: imap)
    assert imap.searches == 3


def test_a_digest_gmail_delivered_despite_a_451_is_marked_sent(gmail, tmp_path):
    """End to end through the outbox: the false rejection no longer leaves
    the digest queued for a second send a minute later."""
    from datetime import datetime, timedelta

    from lastbell import outbox, store, watchers
    from lastbell.differ import event
    from lastbell.models import AlertType, Snapshot, Student, WatcherKind
    from lastbell.notify.email import EmailChannel
    from lastbell.router import Delivery

    conn = store.connect(tmp_path / "t.db")
    store.ensure_schema(conn)
    store.persist_snapshot(conn, Student(agu="7", name="Jules P. Harlow", initials="J.P.H."),
                           Snapshot(student_agu="7", courses=[], assignments=[]))
    w = watchers.add_watcher(conn, "Parent", WatcherKind.GUARDIAN,
                             {"email": {"to": "parent@example.com"}})
    d = Delivery(watcher_name=w.name, watcher_id=w.id, channel="email",
                 to={"to": "parent@example.com"}, send_at="15:00",
                 events=[event(AlertType.GRADE_CHANGED, "7", "Art", "graded: B")])
    outbox.enqueue(conn, d, datetime.now() - timedelta(minutes=1))
    gmail["sent_ids"] = {"yes"}

    sent, warnings = outbox.flush_due(
        conn, channel_factory=lambda name: EmailChannel(transport=_gmail_transport()))
    assert (sent, warnings) == (1, [])
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL").fetchone()[0] == 0
