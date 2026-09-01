#!/usr/bin/env python3
"""Maintainer tool: regenerate ``lastbell/mcps_schools.json`` from the MCPS
public school directory. Run from a source checkout::

    python scripts/refresh_schools.py            # write the bundled JSON
    python scripts/refresh_schools.py --dry-run  # summarize, write nothing
    python scripts/refresh_schools.py --limit 5  # only first 5 overviews (debug)

This is never invoked at runtime; the committed JSON is the source of truth,
and the app resolves ParentVUE school names against it in ``lastbell/schools``.
Re-run it roughly once per school year, or when a school link breaks: review
the ``git diff`` of the JSON, run ``pytest``, and commit.

The parsing functions (``parse_index``, ``parse_overview``) are pure and are
covered by ``tests/test_refresh_schools.py``; only ``refresh`` touches the
network.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from html import unescape
from pathlib import Path

import requests

INDEX_URL = "https://ww2.montgomeryschoolsmd.org/schools/index.aspx"
OVERVIEW_URL = ("https://ww2.montgomeryschoolsmd.org/schoolodex/"
                "schooloverview.aspx?s={sid}")
OUT_PATH = Path(__file__).resolve().parent.parent / "lastbell" / "mcps_schools.json"
USER_AGENT = "lastbell schools-refresh (+https://github.com/lastbell)"

# <h2> section headers on the index page, mapped to a coarse level tag. The
# level is informational metadata; matching in lastbell/schools.py is by name.
_LEVELS = {
    "elementary schools": "elementary",
    "middle schools": "middle",
    "high schools": "high",
    "special schools": "special",
    "alternative programs": "alternative",
}

_H2_RE = re.compile(r"<h2>\s*([^<]+?)\s*</h2>", re.IGNORECASE)

# Each school is a <li> whose FIRST anchor points at schooloverview.aspx?s=ID.
# The visible name can spill past the anchor ("<a>JoAnn Leleck</a> Elementary
# School at Broad Acres"), so we keep both the anchor text and the tail. Nav
# menu <li>s link to index.aspx#panel..., so requiring schooloverview here
# skips them. Anchoring on <li>...<a> keeps a lazy match from crossing </li>.
_LI_RE = re.compile(
    r"<li\b[^>]*>\s*<a\b[^>]*schooloverview\.aspx\?s=(?P<id>\d+)[^>]*>"
    r"(?P<anchor>.*?)</a>(?P<tail>.*?)</li>",
    re.IGNORECASE | re.DOTALL,
)

# Overview page: stable ASP.NET control ids, far sturdier than label text.
_SITE_RE = re.compile(
    r'id="ContentPlaceHolder1_Schooldata1_hypWebSite"[^>]*href="(?P<url>[^"]*)"',
    re.IGNORECASE)
_NAME_RE = re.compile(
    r'id="ContentPlaceHolder1_Schooldata1_ltlSchoolName"[^>]*>(?P<v>[^<]*)<',
    re.IGNORECASE)
_ADDR_RE = re.compile(
    r'id="ContentPlaceHolder1_Schooldata1_lblAddress"[^>]*>(?P<v>[^<]*)<',
    re.IGNORECASE)
_CITY_RE = re.compile(
    r'id="ContentPlaceHolder1_Schooldata1_lblCity"[^>]*>(?P<v>[^<]*)<',
    re.IGNORECASE)
_STATE_RE = re.compile(
    r'id="ContentPlaceHolder1_Schooldata1_lblState"[^>]*>(?P<v>[^<]*)<',
    re.IGNORECASE)
_ZIP_RE = re.compile(
    r'id="ContentPlaceHolder1_Schooldata1_lblPostCode"[^>]*>(?P<v>[^<]*)<',
    re.IGNORECASE)
# Phone wraps a "<strong>Phone:</strong> 240-…" label, so grab the whole span.
_PHONE_RE = re.compile(
    r'id="ContentPlaceHolder1_Schooldata1_lblPhoneMain"[^>]*>(?P<v>.*?)</span>',
    re.IGNORECASE | re.DOTALL)
_PHONE_LABEL_RE = re.compile(r"(?i)^phone:\s*")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _text(fragment: str) -> str:
    """Strip tags and entities from an HTML fragment to clean one-line text."""
    txt = _TAG_RE.sub(" ", fragment)
    txt = unescape(txt).replace("\xa0", " ")
    return _WS_RE.sub(" ", txt).strip()


def parse_index(html: str) -> list[dict]:
    """``[{id, name, level}]`` for every school anchor, ``level`` taken from
    the nearest preceding ``<h2>``. De-duped by id (first occurrence wins)."""
    heads = [(m.start(), _LEVELS.get(m.group(1).strip().lower(), ""))
             for m in _H2_RE.finditer(html)]
    seen: set[str] = set()
    out: list[dict] = []
    for m in _LI_RE.finditer(html):
        sid = m.group("id")
        if sid in seen:
            continue
        seen.add(sid)
        level = ""
        for pos, lvl in heads:
            if pos < m.start():
                level = lvl
            else:
                break
        name = _text(m.group("anchor") + " " + m.group("tail"))
        out.append({"id": sid, "name": name, "level": level})
    return out


def parse_overview(html: str, sid: str) -> dict:
    """Own-site URL (``""`` when the school has none), the overview page's own
    short name, plus address/phone. ``overview_url`` is the fallback link."""
    site = _SITE_RE.search(html)
    website = unescape(site.group("url")).strip() if site else ""
    name = _NAME_RE.search(html)
    short_name = _text(name.group("v")) if name else ""

    addr = _ADDR_RE.search(html)
    city = _CITY_RE.search(html)
    state = _STATE_RE.search(html)
    zipc = _ZIP_RE.search(html)
    street = _text(addr.group("v")) if addr else ""
    locality = " ".join(p for p in (
        (_text(city.group("v")) if city else "") +
        ("," if city else ""),
        _text(state.group("v")) if state else "",
        _text(zipc.group("v")) if zipc else "",
    ) if p).strip()
    address = ", ".join(p for p in (street, locality) if p)

    phone = _PHONE_RE.search(html)
    phone_txt = _PHONE_LABEL_RE.sub("", _text(phone.group("v"))) if phone else ""
    return {
        "website": website,
        "short_name": short_name,
        "overview_url": OVERVIEW_URL.format(sid=sid),
        "address": address,
        "phone": phone_txt,
    }


def _get(session: requests.Session, url: str) -> str:
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def refresh(*, out_path: Path = OUT_PATH, dry_run: bool = False,
            limit: int | None = None, delay: float = 0.4,
            today: str) -> dict:
    """Fetch the directory and (unless dry-run) write the JSON. ``today`` is
    the ``generated`` stamp, passed in so this stays free of wall-clock calls."""
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    print(f"fetching index {INDEX_URL}", file=sys.stderr)
    schools = parse_index(_get(session, INDEX_URL))
    if limit is not None:
        schools = schools[:limit]
    print(f"index lists {len(schools)} schools; fetching overviews…",
          file=sys.stderr)

    records: list[dict] = []
    with_site = fallback = failed = 0
    for i, s in enumerate(schools, 1):
        url = OVERVIEW_URL.format(sid=s["id"])
        try:
            ov = parse_overview(_get(session, url), s["id"])
        except requests.RequestException as e:
            failed += 1
            print(f"  ! {s['name']} (s={s['id']}): {e}", file=sys.stderr)
            ov = {"website": "", "short_name": "",
                  "overview_url": url, "address": "", "phone": ""}
        if ov["website"]:
            with_site += 1
        else:
            fallback += 1
        records.append({
            "id": s["id"],
            "name": s["name"],
            "short_name": ov["short_name"],
            "level": s["level"],
            "website": ov["website"],
            "overview_url": ov["overview_url"],
            "address": ov["address"],
            "phone": ov["phone"],
        })
        if i % 25 == 0:
            print(f"  …{i}/{len(schools)}", file=sys.stderr)
        if delay and i < len(schools):
            time.sleep(delay)

    records.sort(key=lambda r: (r["name"].lower(), r["id"]))
    payload = {"generated": today, "source": INDEX_URL, "schools": records}

    print(f"\nfound {len(records)} schools — {with_site} with own site, "
          f"{fallback} overview-only, {failed} fetch error(s)", file=sys.stderr)
    if dry_run:
        print("dry-run: nothing written", file=sys.stderr)
        return payload
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    print(f"wrote {out_path}", file=sys.stderr)
    return payload


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and summarize, but write nothing")
    ap.add_argument("--limit", type=int, default=None,
                    help="only the first N schools (debugging)")
    ap.add_argument("--delay", type=float, default=0.4,
                    help="seconds between overview fetches (politeness)")
    args = ap.parse_args(argv)
    # Wall-clock stamp lives here, at the CLI edge, so refresh() stays testable.
    from datetime import date
    refresh(dry_run=args.dry_run, limit=args.limit, delay=args.delay,
            today=date.today().isoformat())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
