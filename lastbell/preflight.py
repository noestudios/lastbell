"""District preflight — a general, shareable go/no-go check (Phase 5).

Grown from the recon spike into a tool anyone in any Synergy/ParentVUE district
can run: does the PXP2 web portal exist? is the legacy SOAP API dead? does web
login work? does the LoadControl data path answer? and — the question that
actually decides compatibility — do *this repo's parsers* understand the
fragments the district returns?

Three properties make the output shareable:

* **Redacted by construction** — every check's ``detail`` is written without
  names, AGUs, or grades; person-identifying values live only in ``private``
  fields that are shown locally with ``--show-values`` and never exported.
* **Anonymous mode** — with no username it probes only public endpoints
  (reachability, login form, SOAP endpoint), sending no credentials at all.
* **Structured** — ``--report`` emits a Markdown block ready to paste into a
  "does my district work?" issue; ``--json`` is for scripts. Exit codes:
  0 = gate passed (or anonymous probe completed), 1 = not compatible (yet),
  2 = couldn't run (config/credentials).

Run it without anything else configured::

    lastbell preflight --district <portal-host>            # anonymous
    lastbell preflight --district <portal-host> -u USER    # full check
"""
from __future__ import annotations

import argparse
import json as jsonlib
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Callable, Optional

import requests

from . import __version__
from . import secrets as secretstore
from .client import LoginError, ParentVueClient, ParentVueError

SOAP_ENDPOINT = "/Service/PXPCommunication.asmx"
EDUPOINT_NS = "http://edupoint.com/webservices/"

PASS, FAIL, WARN, SKIP, INFO = "PASS", "FAIL", "WARN", "SKIP", "INFO"
_ICONS = {PASS: "✅", FAIL: "❌", WARN: "⚠️", SKIP: "⏭", INFO: "ℹ️"}


@dataclass
class Check:
    id: str
    title: str
    status: str
    detail: str = ""      # redacted by construction — safe to share
    private: str = ""     # local-only extra; shown with --show-values, never exported


@dataclass
class Report:
    district: str
    mode: str             # "full" | "anonymous"
    checks: list[Check] = field(default_factory=list)
    version: str = __version__
    generated: str = ""

    def add(self, *args, **kwargs) -> Check:
        c = Check(*args, **kwargs)
        self.checks.append(c)
        return c

    @property
    def verdict(self) -> str:
        by_id = {c.id: c for c in self.checks}
        if self.mode == "anonymous":
            if any(c.status == FAIL for c in self.checks):
                return "portal-not-found"
            return "anonymous-ok"
        gate = by_id.get("gate_fetch")
        parses = [by_id.get("parse_classes"), by_id.get("parse_assignments")]
        if any(c.status == FAIL for c in (by_id.get("web_login"),) if c):
            return "no-go"
        if gate is None or gate.status != PASS:
            return "no-go"
        if any(c is not None and c.status == FAIL for c in parses):
            return "partial"
        return "go"


VERDICT_TEXT = {
    "go": "Gate PASSED — the portal speaks the protocol and the parsers "
          "understand its fragments. Last Bell should work here.",
    "partial": "The data path answers, but a parser failed on this district's "
               "markup — file a report; supporting this district likely means "
               "a parser tweak, not new recon.",
    "no-go": "Not compatible yet — see the failing check above.",
    "anonymous-ok": "Public endpoints look right. Re-run with --username for "
                    "the full login + data-path check.",
    "portal-not-found": "This host doesn't look like a PXP2 ParentVUE portal.",
}


# ── probes ────────────────────────────────────────────────────────────


def _soap_probe(base_url: str, username: str, password: str,
                post: Callable = requests.post) -> tuple[str, str]:
    """(status, detail) for the legacy SOAP API."""
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        f'<soap:Body><ProcessWebServiceRequest xmlns="{EDUPOINT_NS}">'
        f"<userID>{escape(username)}</userID><password>{escape(password)}</password>"
        "<skipLoginLog>true</skipLoginLog><parent>true</parent>"
        "<webServiceHandleName>PXPWebServices</webServiceHandleName>"
        "<methodName>Gradebook</methodName>"
        "<paramStr>&lt;Parms&gt;&lt;ChildIntID&gt;0&lt;/ChildIntID&gt;&lt;/Parms&gt;</paramStr>"
        "</ProcessWebServiceRequest></soap:Body></soap:Envelope>"
    )
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": f"{EDUPOINT_NS}ProcessWebServiceRequest",
    }
    try:
        r = post(base_url + SOAP_ENDPOINT, data=envelope.encode(), headers=headers, timeout=30)
    except requests.RequestException as e:
        return INFO, f"unreachable ({type(e).__name__})"
    text = r.text
    if "RT_ERROR" in text or "UPD" in text or "update app" in text.lower():
        import re

        m = re.search(r'ERROR_MESSAGE="([^"]+)"', text) or re.search(r"<ERROR_MESSAGE>([^<]+)<", text)
        # The district's own deprecation code (UPD5304 / D5517 …) is the most
        # useful cross-district datum this tool collects — keep it verbatim.
        return INFO, f"rejected — {m.group(1)[:120]}" if m else "rejected (deprecated)"
    if "<html" in text[:200].lower():
        return WARN, "returned HTML (not the SOAP service)"
    return INFO, "responded (SOAP may still be enabled — the web path is still preferred)"


def _login_page_probe(base_url: str, get: Callable = requests.get) -> tuple[str, str]:
    try:
        r = get(f"{base_url}/PXP2_Login_Parent.aspx", timeout=30)
    except requests.exceptions.SSLError:
        return FAIL, "TLS handshake failed"
    except requests.RequestException as e:
        return FAIL, f"unreachable ({type(e).__name__})"
    if r.status_code != 200:
        return FAIL, f"HTTP {r.status_code} for PXP2_Login_Parent.aspx"
    text = r.text
    has_user = "MainContent$username" in text or "MainContent_username" in text
    has_pass = "MainContent$password" in text or "MainContent_password" in text
    if has_user and has_pass:
        return PASS, "PXP2 parent login form found (username + password fields)"
    return WARN, ("page loads but the expected ASP.NET form fields weren't found — "
                  "the district may use SSO or a customized login")


def _soap_endpoint_probe(base_url: str, get: Callable = requests.get) -> tuple[str, str]:
    """Anonymous SOAP check: GET the .asmx service page — no credentials sent."""
    try:
        r = get(base_url + SOAP_ENDPOINT, timeout=30)
    except requests.RequestException as e:
        return INFO, f"unreachable ({type(e).__name__})"
    if r.status_code == 404:
        return INFO, "endpoint absent (HTTP 404) — legacy API removed entirely"
    if "ProcessWebServiceRequest" in r.text:
        return INFO, "endpoint present (may still reject app logins — full check tells)"
    return INFO, f"HTTP {r.status_code}"


# ── the runs ──────────────────────────────────────────────────────────


def run_anonymous(district: str, base_url: str,
                  get: Callable = requests.get) -> Report:
    """Credential-free probe: nothing but public GETs, nothing sent."""
    report = Report(district=district, mode="anonymous",
                    generated=datetime.now().strftime("%Y-%m-%d"))
    status, detail = _login_page_probe(base_url, get)
    report.add("login_page", "PXP2 web portal", status, detail)
    status, detail = _soap_endpoint_probe(base_url, get)
    report.add("soap_endpoint", "Legacy SOAP endpoint", status, detail)
    for check_id, title in (
        ("web_login", "Web login"), ("students", "Students on credential"),
        ("gate_fetch", "LoadControl data path"), ("parse_classes", "Class-list parser"),
        ("parse_assignments", "Assignment parser"),
    ):
        report.add(check_id, title, SKIP, "needs a credential — re-run with --username")
    return report


def run_full(district: str, base_url: str, username: str, password: str,
             *, client: Optional[ParentVueClient] = None,
             dump_dir: Optional[Path] = None,
             get: Callable = requests.get,
             post: Callable = requests.post) -> Report:
    from .gradebook import ParseError, parse_class_details, parse_school_classes

    report = Report(district=district, mode="full",
                    generated=datetime.now().strftime("%Y-%m-%d"))
    client = client or ParentVueClient(base_url, username, password)

    status, detail = _login_page_probe(base_url, get)
    report.add("login_page", "PXP2 web portal", status, detail)

    status, detail = _soap_probe(base_url, username, password, post)
    report.add("soap", "Legacy SOAP API", status, detail)

    def skip_rest(reason: str) -> Report:
        for check_id, title in (
            ("students", "Students on credential"),
            ("focus_args", "Gradebook focus bootstrap"),
            ("gate_fetch", "LoadControl data path"),
            ("parse_classes", "Class-list parser"),
            ("parse_assignments", "Assignment parser"),
        ):
            if not any(c.id == check_id for c in report.checks):
                report.add(check_id, title, SKIP, reason)
        return report

    try:
        client.login()
        report.add("web_login", "Web login", PASS, "authenticated to the web portal")
    except (LoginError, requests.RequestException) as e:
        report.add("web_login", "Web login", FAIL,
                   "login rejected — bad credentials, MFA, CAPTCHA, or SSO-only"
                   if isinstance(e, LoginError) else f"request failed ({type(e).__name__})")
        return skip_rest("blocked by login")

    children = client.get_children()
    report.add("students", "Students on credential",
               PASS if children else WARN,
               f"{len(children)} student(s) found" if children else
               "none found — the child-list markup may differ on this district",
               private=", ".join(c.name for c in children))

    agu = children[0].agu if children else "0"
    focus = client.get_focus_args(agu)
    if dump_dir is not None:
        dump_dir.mkdir(parents=True, exist_ok=True)
        page = getattr(client, "last_gradebook_html", "")
        (dump_dir / "gradebook_page.html").write_text(page, encoding="utf-8")
    if not focus.args:
        report.add("focus_args", "Gradebook focus bootstrap", FAIL,
                   "PXP.GBCurrentFocus not found on the gradebook page")
        return skip_rest("no focus args to send")
    resolved = [k for k, v in (("OrgYearGU", focus.org_year_gu),
                               ("gradePeriodGU", focus.grade_period_gu),
                               ("schoolID", focus.school_id)) if v]
    report.add("focus_args", "Gradebook focus bootstrap", PASS,
               f"{len(focus.args)} FocusArgs fields; resolved: {', '.join(resolved) or 'none'}")

    def _dump(name: str, text: str) -> None:
        if dump_dir is not None and text:
            (dump_dir / name).write_text(text, encoding="utf-8")

    try:
        classes_html = client.load_control(
            "Gradebook_SchoolClasses", focus.as_parameters(), agu_header=focus.agu_header)
        report.add("gate_fetch", "LoadControl data path", PASS,
                   f"Gradebook_SchoolClasses returned a {len(classes_html) // 1024} KB fragment")
        _dump("schoolclasses_fragment.html", classes_html)
    except ParentVueError as e:
        report.add("gate_fetch", "LoadControl data path", FAIL, str(e))
        _dump("loadcontrol_error.html", e.response_text)
        return skip_rest("blocked by the data path")

    try:
        sc = parse_school_classes(classes_html)
        n_focus = sum(1 for r in sc.rows if r.focus.get("FocusArgs"))
        report.add("parse_classes", "Class-list parser", PASS if sc.rows else WARN,
                   f"{len(sc.rows)} class row(s), {len(sc.mark_periods)} mark period(s), "
                   f"{n_focus} row(s) with drill-down focus",
                   private=f"school: {sc.school_name}; term: {sc.current_term}")
    except ParseError as e:
        report.add("parse_classes", "Class-list parser", FAIL,
                   f"fragment fetched but not understood ({e}) — "
                   f"run with --dump and file a district report")

    try:
        details_html = client.load_control(
            "Gradebook_ClassDetails", focus.as_parameters(), agu_header=focus.agu_header)
        _dump("classdetails_fragment.html", details_html)
        cd = parse_class_details(details_html)
        report.add("parse_assignments", "Assignment parser",
                   PASS if cd.assignments else WARN,
                   f"{len(cd.assignments)} assignment(s) parsed from the default class"
                   + ("" if cd.assignments else " — may simply be empty this early in the term"),
                   private=f"mark: {cd.mark} {cd.percent}")
    except ParentVueError as e:
        report.add("parse_assignments", "Assignment parser", FAIL,
                   f"Gradebook_ClassDetails not returned ({e})")
        _dump("classdetails_error.html", e.response_text)
    except ParseError as e:
        report.add("parse_assignments", "Assignment parser", FAIL,
                   f"fragment fetched but not understood ({e}) — "
                   f"run with --dump and file a district report")

    return report


# ── output ────────────────────────────────────────────────────────────


def render_text(report: Report, show_values: bool = False) -> str:
    lines = ["=" * 62,
             f"  parentvue preflight v{report.version} → {report.district}"
             f"  ({report.mode})",
             "=" * 62]
    for c in report.checks:
        lines.append(f"  {_ICONS[c.status]} {c.status:4} {c.title:28} {c.detail}")
        if show_values and c.private:
            lines.append(f"           {'':28} {c.private}")
    lines += ["-" * 62,
              f"  VERDICT: {report.verdict}",
              f"  {VERDICT_TEXT[report.verdict]}",
              "=" * 62]
    return "\n".join(lines)


def render_markdown(report: Report) -> str:
    """A paste-ready district report. Contains no names, grades, or usernames."""
    lines = [
        "### ParentVUE district compatibility report",
        "",
        f"| | |",
        f"|---|---|",
        f"| District host | `{report.district}` |",
        f"| Tool | lastbell preflight v{report.version} |",
        f"| Date | {report.generated} |",
        f"| Mode | {report.mode} |",
        f"| **Verdict** | **{report.verdict}** |",
        "",
        "| Check | Status | Detail |",
        "|---|---|---|",
    ]
    for c in report.checks:
        lines.append(f"| {c.title} | {_ICONS[c.status]} {c.status} | {c.detail} |")
    lines += ["", f"> {VERDICT_TEXT[report.verdict]}",
              "",
              "_Generated by `lastbell preflight --report`. "
              "Checks are redacted by construction: no student names, grades, "
              "or usernames appear above._"]
    return "\n".join(lines)


def render_json(report: Report) -> str:
    return jsonlib.dumps({
        "district": report.district,
        "tool": "lastbell-preflight",
        "version": report.version,
        "generated": report.generated,
        "mode": report.mode,
        "verdict": report.verdict,
        "checks": [{"id": c.id, "title": c.title, "status": c.status,
                    "detail": c.detail} for c in report.checks],
    }, indent=2)


# ── entrypoint ────────────────────────────────────────────────────────


def _base_url(district: str) -> str:
    host = district.replace("https://", "").replace("http://", "").strip("/")
    return f"https://{host}"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="lastbell preflight",
        description="ParentVUE district go/no-go check — redacted, shareable output.")
    ap.add_argument("--district", "-d",
                    help="portal hostname (default: LASTBELL_DISTRICT)")
    ap.add_argument("--username", "-u",
                    help="ParentVUE login for the full check "
                         "(default: LASTBELL_USERNAME; omit for anonymous mode)")
    ap.add_argument("--anonymous", action="store_true",
                    help="probe public endpoints only; send no credentials")
    ap.add_argument("--report", action="store_true",
                    help="emit a redacted Markdown report to paste into an issue")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--show-values", action="store_true",
                    help="reveal names/grades locally (never included in --report/--json)")
    ap.add_argument("--dump", action="store_true",
                    help="save raw portal pages to data/debug/ for parser development "
                         "(contains your students' data — stays local, git-ignored)")
    args = ap.parse_args(argv)

    district = args.district or os.environ.get("LASTBELL_DISTRICT")
    if not district:
        print("error: no district — pass --district HOST or set LASTBELL_DISTRICT",
              file=sys.stderr)
        return 2
    username = None if args.anonymous else (
        args.username or os.environ.get("LASTBELL_USERNAME"))
    if username == "your_parentvue_username":  # the .env.example placeholder
        username = None
    base = _base_url(district)

    if username is None:
        report = run_anonymous(district, base)
    else:
        backend = os.environ.get("LASTBELL_SECRET_BACKEND", "keyring")
        try:
            password = secretstore.get_password(username, backend)
        except secretstore.SecretError:
            password = secretstore.prompt_password()
        dump_dir = Path("data/debug") if args.dump else None
        report = run_full(district, base, username, password, dump_dir=dump_dir)

    if args.json:
        print(render_json(report))
    elif args.report:
        print(render_markdown(report))
    else:
        print(render_text(report, show_values=args.show_values))
        if report.mode == "full" and not args.show_values:
            print("(names/grades hidden — --show-values reveals them locally; "
                  "--report prints a shareable Markdown version)")

    return 0 if report.verdict in ("go", "anonymous-ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
