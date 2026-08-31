"""ParentVUE web client — the single connection implementation.

Grown from the district-recon spike. MCPS (and a growing number of districts)
disabled the legacy SOAP mobile API, so this speaks to the PXP2 *web* portal:
an ASP.NET form login, then the ``PXP2_Gradebook.aspx/LoadControl`` page method
that the gradebook UI itself calls to load data.

The login, child-list, and focus-arg code below is proven against MCPS. The
``load_control`` call's contract is read from the portal's own JavaScript but is
NOT yet verified end-to-end (see the note on the method) — that is the Phase 0
gate.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Optional

import requests

from . import __version__

USER_AGENT = f"mcpsgradewatch/{__version__}"


class ParentVueError(RuntimeError):
    """Base error for portal interactions.

    ``response_text`` carries the server's raw reply (when there was one) so
    debug tooling can dump it for inspection.
    """

    def __init__(self, message: str, response_text: str = "") -> None:
        super().__init__(message)
        self.response_text = response_text


class LoginError(ParentVueError):
    """Authentication failed (bad credentials, MFA, or CAPTCHA)."""


@dataclass
class Child:
    agu: str
    name: str
    school: str = ""


@dataclass
class FocusArgs:
    """The per-term identifiers LoadControl requires. Read from the gradebook
    page; they rotate each school year / grading period, so re-read per run."""

    org_year_gu: str
    grade_period_gu: str
    school_id: str
    raw: dict = field(default_factory=dict)

    def as_parameters(self, agu: str) -> dict:
        return {
            "schoolID": self.school_id,
            "OrgYearGU": self.org_year_gu,
            "gradePeriodGU": self.grade_period_gu,
            "AGU": agu,
            **self.raw,
        }


class _HiddenFields(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "input" and d.get("type") == "hidden" and d.get("name"):
            self.fields[d["name"]] = d.get("value", "")


class ParentVueClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self._password = password
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._logged_in = False

    # ── auth ──────────────────────────────────────────────────────────
    def login(self) -> None:
        if self._logged_in:
            return
        url = f"{self.base_url}/PXP2_Login_Parent.aspx"
        r = self.session.get(url, timeout=30)
        r.raise_for_status()

        fields = _HiddenFields()
        fields.feed(r.text)
        form = fields.fields.copy()
        form["ctl00$MainContent$username"] = self.username
        form["ctl00$MainContent$password"] = self._password
        form["ctl00$MainContent$Submit1"] = "Login"

        r2 = self.session.post(
            url + "?regenerateSessionId=true", data=form, allow_redirects=True, timeout=30
        )
        r2.raise_for_status()
        if "PXP2_Login" in r2.url:
            raise LoginError("Login failed — bad credentials, MFA, or CAPTCHA.")
        self._logged_in = True

    # ── students on this credential ───────────────────────────────────
    def get_children(self) -> list[Child]:
        self.login()
        r = self.session.get(f"{self.base_url}/Home_PXP2.aspx", timeout=30)
        r.raise_for_status()

        seen: set[str] = set()
        out: list[Child] = []
        for m in re.finditer(
            r'"agu"\s*:\s*"(\d+)".*?"name"\s*:\s*"([^"]*)".*?"school"\s*:\s*"([^"]*)"',
            r.text,
            re.DOTALL,
        ):
            agu = m.group(1)
            if agu in seen:  # dedupe students shared across credentials
                continue
            seen.add(agu)
            out.append(Child(agu=agu, name=m.group(2), school=m.group(3)))
        return out

    # ── focus args (LoadControl needs these) ──────────────────────────
    def get_focus_args(self, agu: str = "0") -> FocusArgs:
        self.login()
        r = self.session.get(f"{self.base_url}/PXP2_Gradebook.aspx?AGU={agu}", timeout=30)
        r.raise_for_status()
        html = r.text
        # Kept so debug tooling (preflight --dump) can save the raw page.
        self.last_gradebook_html = html

        def grab(key: str) -> str:
            # Values may be quoted GUIDs/strings or bare numbers, and key
            # casing varies across the page's embedded JSON blobs.
            m = re.search(rf'"{key}"\s*:\s*"([^"]*)"', html, re.IGNORECASE)
            if m is None:
                m = re.search(rf'"{key}"\s*:\s*(-?\d+)', html, re.IGNORECASE)
            return m.group(1) if m else ""

        # Optional focus fields the portal JS also passes when present.
        raw = {}
        for key in ("markPeriodGU", "GradingPeriodGroup"):
            val = grab(key)
            if val:
                raw[key] = val

        return FocusArgs(
            org_year_gu=grab("OrgYearGU"),
            grade_period_gu=grab("gradePeriodGU"),
            school_id=grab("schoolID"),
            raw=raw,
        )

    # ── the gradebook page method ─────────────────────────────────────
    def load_control(self, control: str, parameters: dict) -> str:
        """POST PXP2_Gradebook.aspx/LoadControl and return the HTML fragment.

        Controls: ``Gradebook_SchoolClasses`` (class list + overall marks),
        ``Gradebook_ClassDetails`` (assignments in a class), and
        ``Gradebook_AssignmentDetails`` (one assignment's due date / score).

        ⚠ PHASE 0 GATE: the endpoint and payload shape are read from the
        portal's own JS, but an end-to-end call returning real assignment data
        is not yet verified — the empty-parameter probe returned HTTP 500
        because the focus GUIDs were missing. Verifying this is the build's
        go/no-go. Until then, callers should expect ParentVueError.
        """
        self.login()
        url = f"{self.base_url}/PXP2_Gradebook.aspx/LoadControl"
        body = {"request": {"control": control, "parameters": parameters}}
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": f"{self.base_url}/PXP2_Gradebook.aspx",
        }
        r = self.session.post(url, data=json.dumps(body), headers=headers, timeout=30)
        if "json" not in r.headers.get("Content-Type", "").lower():
            raise ParentVueError(
                f"LoadControl({control}) did not return JSON (HTTP {r.status_code}); "
                "focus parameters are probably missing or invalid.",
                response_text=r.text,
            )
        payload = r.json()
        d = payload.get("d", payload)
        html = d.get("html") if isinstance(d, dict) else d
        if not html:
            raise ParentVueError(
                f"LoadControl({control}) returned no html fragment.",
                response_text=r.text,
            )
        return html
