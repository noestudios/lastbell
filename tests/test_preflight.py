"""Preflight: verdict logic, redaction guarantees, and the full run offline
against fake portal responses (the real fixtures the parsers were wired on)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lastbell import preflight
from lastbell.client import LoginError, ParentVueError
from lastbell.preflight import (
    FAIL,
    PASS,
    SKIP,
    WARN,
    Report,
    render_json,
    render_markdown,
    render_text,
    run_anonymous,
    run_full,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ── fakes ─────────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, text: str = "", status_code: int = 200):
        self.text, self.status_code = text, status_code


LOGIN_PAGE = "<form><input name='ctl00$MainContent$username'><input name='ctl00$MainContent$password'></form>"
SOAP_DEAD = '<root ERROR_MESSAGE="The client version is not supported. UPD5304-00" RT_ERROR="TRUE"/>'


def fake_get(url, **kw):
    if "PXP2_Login_Parent" in url:
        return FakeResponse(LOGIN_PAGE)
    if "PXPCommunication.asmx" in url:
        return FakeResponse("ProcessWebServiceRequest")
    return FakeResponse("", 404)


def fake_post(url, **kw):
    return FakeResponse(SOAP_DEAD)


class FakeFocus:
    args = {"OrgYearGU": "x", "gradePeriodGU": "y", "schoolID": 123, "classID": 9}
    org_year_gu, grade_period_gu, school_id, agu_header = "x", "y", "123", "5"

    def as_parameters(self):
        return dict(self.args)


class FakeChild:
    def __init__(self, agu, name):
        self.agu, self.name, self.school = agu, name, "Example ES"


class FakeClient:
    """Serves the same captured fragments the parsers were wired against."""

    def __init__(self, *, login_ok=True, gate_ok=True, classes_html=None,
                 details_html=None):
        self.login_ok, self.gate_ok = login_ok, gate_ok
        self.classes_html = classes_html if classes_html is not None else \
            (FIXTURES / "schoolclasses_secondary.html").read_text(encoding="utf-8")
        self.details_html = details_html if details_html is not None else \
            (FIXTURES / "classdetails_sample.html").read_text(encoding="utf-8")
        self.last_gradebook_html = "<html/>"

    def login(self):
        if not self.login_ok:
            raise LoginError("Login failed — bad credentials, MFA, or CAPTCHA.")

    def get_children(self):
        return [FakeChild("42", "Jasper P. Hays")]

    def get_focus_args(self, agu):
        return FakeFocus()

    def load_control(self, control, parameters, agu_header="0"):
        if not self.gate_ok:
            raise ParentVueError("LoadControl(x) server error: nope",
                                 response_text="err")
        return self.classes_html if control == "Gradebook_SchoolClasses" \
            else self.details_html


def _run(client) -> Report:
    return run_full("example.edupoint.com", "https://example.edupoint.com",
                    "someparent", "pw", client=client,
                    get=fake_get, post=fake_post)


# ── verdicts ──────────────────────────────────────────────────────────


def test_all_green_is_go():
    report = _run(FakeClient())
    assert report.verdict == "go"
    by_id = {c.id: c for c in report.checks}
    assert by_id["web_login"].status == PASS
    assert by_id["gate_fetch"].status == PASS
    assert by_id["parse_classes"].status == PASS
    assert "UPD5304" in by_id["soap"].detail   # deprecation code kept verbatim


def test_login_failure_is_no_go_and_skips_the_rest():
    report = _run(FakeClient(login_ok=False))
    assert report.verdict == "no-go"
    by_id = {c.id: c for c in report.checks}
    assert by_id["web_login"].status == FAIL
    assert by_id["gate_fetch"].status == SKIP
    assert by_id["parse_assignments"].status == SKIP


def test_gate_failure_is_no_go():
    report = _run(FakeClient(gate_ok=False))
    assert report.verdict == "no-go"


def test_unparseable_fragment_is_partial():
    report = _run(FakeClient(classes_html="<div>some other district's markup</div>"))
    by_id = {c.id: c for c in report.checks}
    assert by_id["gate_fetch"].status == PASS
    assert by_id["parse_classes"].status == FAIL
    assert "district report" in by_id["parse_classes"].detail
    assert report.verdict == "partial"


def test_anonymous_mode_sends_no_credentials():
    calls = []

    def spying_get(url, **kw):
        calls.append(url)
        return fake_get(url, **kw)

    report = run_anonymous("example.edupoint.com", "https://example.edupoint.com",
                           get=spying_get)
    assert report.verdict == "anonymous-ok"
    assert all("password" not in u for u in calls)
    by_id = {c.id: c for c in report.checks}
    assert by_id["login_page"].status == PASS
    assert by_id["web_login"].status == SKIP


def test_anonymous_unreachable_portal_is_not_found():
    def dead_get(url, **kw):
        import requests

        raise requests.ConnectionError("boom")

    report = run_anonymous("nope.example.com", "https://nope.example.com", get=dead_get)
    assert report.verdict == "portal-not-found"


# ── redaction ─────────────────────────────────────────────────────────


def test_shareable_outputs_contain_no_pii():
    report = _run(FakeClient())
    md, js = render_markdown(report), render_json(report)
    for payload in (md, js):
        assert "Jasper" not in payload        # student name
        assert "someparent" not in payload    # username
        assert "example.edupoint.com" in payload   # the district IS the point
    # names live only in private fields, surfaced by --show-values locally
    assert "Jasper" in render_text(report, show_values=True)
    assert "Jasper" not in render_text(report, show_values=False)


def test_json_is_parseable_and_structured():
    report = _run(FakeClient())
    data = json.loads(render_json(report))
    assert data["verdict"] == "go"
    assert {c["id"] for c in data["checks"]} >= {"web_login", "gate_fetch",
                                                 "parse_classes", "parse_assignments"}
    assert "private" not in json.dumps(data)


def test_markdown_report_is_paste_ready():
    md = render_markdown(_run(FakeClient()))
    assert md.startswith("### ParentVUE district compatibility report")
    assert "| District host | `example.edupoint.com` |" in md
    assert "**go**" in md


# ── entrypoint ────────────────────────────────────────────────────────


def test_main_requires_a_district(monkeypatch, capsys):
    monkeypatch.delenv("LASTBELL_DISTRICT", raising=False)
    assert preflight.main([]) == 2
    assert "--district" in capsys.readouterr().err


def test_main_anonymous_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(preflight, "run_anonymous",
                        lambda d, b, **kw: Report(district=d, mode="anonymous"))
    assert preflight.main(["--district", "x.edupoint.com", "--anonymous"]) == 0
    out = capsys.readouterr().out
    assert "anonymous-ok" in out