"""ParentVUE's Parent Acknowledgements page (0.3.13).

When a school posts documents a parent must acknowledge (a progress
report, a policy), the portal serves that page in place of the gradebook
until a parent signs in and acknowledges each one. Seen on MCPS
2026-11-13 → 11-17: every poll read nothing, and older versions recorded a
placeholder course and called the poll a success. Now it is an
account-level stop, named, and guardians hear after the first poll.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lastbell import collector, health, store
from lastbell.client import AcknowledgementsPending, ParentVueClient

# Invented, trimmed to what the detection reads: the page's form posts to
# its own address, and the heading counts the documents.
ACK_PAGE = """<html><body><div id="maincontent" role="main">
<form name="aspnetForm" method="post" action="PXP2_Parent_Acknowledgements.aspx?AGU=0"
 id="aspnetForm"><h1>Parent Acknowledgements</h1>
<h2 class="panel-heading"><span>Acknowledgement 1 / 5: </span></h2>
<div class="panel-body">To download this document, click here:
 Student_Progress_0000.pdf</div></form></div></body></html>"""

GRADEBOOK = ('<script>PXP.GBCurrentFocus = {"FocusArgs": {"classID": 5}}; '
             'PXP.AGU = 0;</script><form action="ToggleAccessibleMode"></form>'
             '<a href="PXP2_Parent_Acknowledgements.aspx?AGU=0">Acknowledgements</a>')


def _client(page: str, final_url: str = "") -> ParentVueClient:
    client = ParentVueClient("https://x.example", "u", "p")
    client.login = lambda: None
    client.session.get = lambda url, timeout=30: SimpleNamespace(
        text=page, url=final_url or url, raise_for_status=lambda: None)
    return client


# ── recognizing the page ──────────────────────────────────────────────


def test_the_acknowledgements_page_is_named_with_its_count():
    with pytest.raises(AcknowledgementsPending) as e:
        _client(ACK_PAGE).get_focus_args("0")
    assert e.value.count == 5
    assert "acknowledge 5 documents" in str(e.value)


def test_a_redirect_to_the_page_is_recognized_by_its_address():
    url = "https://x.example/PXP2_Parent_Acknowledgements.aspx?AGU=0"
    with pytest.raises(AcknowledgementsPending) as e:
        _client("<html>no count here</html>", final_url=url).get_focus_args("0")
    assert e.value.count is None
    assert "acknowledge documents" in str(e.value)


def test_a_gradebook_that_merely_links_to_the_page_reads_as_usual():
    focus = _client(GRADEBOOK).get_focus_args("0")
    assert focus.args == {"classID": 5}


def test_one_document_is_singular():
    assert "acknowledge 1 document " in str(AcknowledgementsPending(1))


# ── the poll stops, for every student ─────────────────────────────────


def test_collect_fails_the_poll_instead_of_skipping_the_student():
    portal = SimpleNamespace(get_focus_args=lambda agu: (_ for _ in ()).throw(
        AcknowledgementsPending(5)))
    with pytest.raises(AcknowledgementsPending):
        collector.collect_student(portal, SimpleNamespace(
            agu="0", name="Jules P. Harlow", school=""), delay_s=0)


# ── guardians hear at once, in plain words ────────────────────────────


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "t.db")
    store.ensure_schema(c)
    yield c
    c.close()


def test_the_first_poll_that_meets_the_page_tells_guardians(conn):
    exc = AcknowledgementsPending(5)
    assert health.classify(exc) == "acknowledge"
    state = health.record_failure(conn, health.classify(exc), str(exc))
    assert state.notice_due
    subject, body = health.failure_message(state, host="pi")
    assert "acknowledge" in subject
    assert "5 documents" in body
    assert "Sign in to ParentVUE" in body
    assert "can't do that for you" in body
    note = health.dashboard_note(state)
    assert "acknowledge 5 documents" in note
    assert "sign in to ParentVUE" in note


def test_the_page_never_backs_off_like_a_rejected_sign_in(conn):
    for _ in range(5):
        state = health.record_failure(conn, "acknowledge", str(AcknowledgementsPending(2)))
    assert health.next_delay_minutes(state, 180) == 180
