"""District preflight — the go/no-go check, grown from the recon spike.

Answers, for a configured district + credential: is the legacy SOAP API dead?
does web login work? how many students? do the focus args resolve? and — the
Phase 0 gate — does a live LoadControl fetch return data?

Config comes from the environment / .env (NO personal defaults in source). The
password is resolved from the keyring or env, or prompted; it is never printed
or stored. Grade values and names are redacted unless --show-values is passed,
so the summary is safe to share.
"""
from __future__ import annotations

import argparse
from html import escape

import requests

from . import config as cfg
from . import secrets as secretstore
from .client import LoginError, ParentVueClient, ParentVueError

SOAP_ENDPOINT = "/Service/PXPCommunication.asmx"
EDUPOINT_NS = "http://edupoint.com/webservices/"


def _redact(value: str, show: bool) -> str:
    if show or not value:
        return value
    return f"<redacted:{len(value)} chars>"


def _soap_probe(base_url: str, username: str, password: str) -> str:
    """Return a one-line status for the legacy SOAP API."""
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
        r = requests.post(base_url + SOAP_ENDPOINT, data=envelope.encode(), headers=headers, timeout=30)
    except requests.RequestException as e:
        return f"unreachable ({e})"
    text = r.text
    if "RT_ERROR" in text or "UPD" in text or "update app" in text.lower():
        # surface the district's own message (deprecation codes like UPD5304 / D5517)
        import re

        m = re.search(r'ERROR_MESSAGE="([^"]+)"', text) or re.search(r"<ERROR_MESSAGE>([^<]+)<", text)
        return f"REJECTED — {m.group(1)[:120]}" if m else "REJECTED (deprecated)"
    if "<html" in text[:200].lower():
        return "returned HTML (not the SOAP service)"
    return "responded (SOAP may still be enabled)"


def main() -> None:
    ap = argparse.ArgumentParser(description="mcpsgradewatch district preflight")
    ap.add_argument("--show-values", action="store_true", help="reveal names/grades locally (do not share)")
    args = ap.parse_args()

    conf = cfg.load()
    show = args.show_values
    try:
        password = secretstore.get_password(conf.username, conf.secret_backend)
    except secretstore.SecretError:
        password = secretstore.prompt_password()

    print("=" * 60)
    print(f"  mcpsgradewatch preflight -> {conf.district}")
    print(f"  username: {conf.username}")
    print("=" * 60)

    print("\n[1] Legacy SOAP API ...")
    print(f"    {_soap_probe(conf.base_url, conf.username, password)}")

    client = ParentVueClient(conf.base_url, conf.username, password)

    print("\n[2] Web login ...")
    try:
        client.login()
        print("    OK — authenticated to the web portal.")
    except (LoginError, requests.RequestException) as e:
        print(f"    FAIL — {e}")
        print("\nVerdict: cannot proceed until web login works (check creds / MFA).")
        return

    print("\n[3] Students on this credential ...")
    children = client.get_children()
    print(f"    found {len(children)}: " + ", ".join(_redact(c.name, show) for c in children))

    print("\n[4] Focus args (LoadControl inputs) ...")
    agu = children[0].agu if children else "0"
    focus = client.get_focus_args(agu)
    for label, val in (("OrgYearGU", focus.org_year_gu), ("gradePeriodGU", focus.grade_period_gu), ("schoolID", focus.school_id)):
        print(f"    {label:14}: {'resolved' if val else 'MISSING'}")

    print("\n[5] Gate — live LoadControl (Gradebook_SchoolClasses) ...")
    try:
        html = client.load_control("Gradebook_SchoolClasses", focus.as_parameters(agu))
        print(f"    PASS — received a {len(html)//1024} KB HTML fragment.")
        gate = all([focus.org_year_gu, focus.school_id]) and len(html) > 0
    except ParentVueError as e:
        print(f"    NOT PASSED — {e}")
        gate = False

    print("\n" + "=" * 60)
    print("  VERDICT")
    print("=" * 60)
    if gate:
        print("  Gate PASSED. Wire gradebook.py against this fragment, then Phase 1.")
    else:
        print("  Gate not passed. Capture a real LoadControl response (browser")
        print("  Network tab) to pin the exact focus params, then retry.")
    print("=" * 60)


if __name__ == "__main__":
    main()
