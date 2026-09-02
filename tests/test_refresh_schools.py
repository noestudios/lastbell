"""Offline tests for the MCPS directory scraper's pure parsers. No network is
touched — only ``parse_index`` / ``parse_overview`` run, against synthetic
fixtures that mirror the real markup (captured 2026-09-01)."""
from __future__ import annotations

import pathlib
import sys

# scripts/ is a maintainer tool dir, not an installed package.
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))
import refresh_schools as R  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _index():
    return R.parse_index((FIXTURES / "schools_index.html").read_text(encoding="utf-8"))


def test_parse_index_extracts_id_name_level():
    by_id = {s["id"]: s for s in _index()}
    assert by_id["02790"] == {"id": "02790", "name": "Arcola", "level": "elementary"}
    assert by_id["04406"]["name"] == "Bethesda-Chevy Chase"
    assert by_id["04406"]["level"] == "high"


def test_parse_index_level_from_nearest_heading():
    # The two Poolesville entries sit under different <h2> headings.
    levels = {s["id"]: s["level"] for s in _index()}
    assert levels["02900"] == "elementary"
    assert levels["04900"] == "high"


def test_parse_index_captures_name_spilling_past_anchor():
    names = {s["id"]: s["name"] for s in _index()}
    # "<a>JoAnn Leleck</a> Elementary School at Broad Acres"
    assert names["02304"] == "JoAnn Leleck Elementary School at Broad Acres"


def test_parse_index_decodes_entities_and_nbsp():
    names = {s["id"]: s["name"] for s in _index()}
    assert names["02500"] == "Rachel Carson & Friends"   # &amp; decoded
    assert names["02790"] == "Arcola"                    # trailing &nbsp; gone


def test_parse_index_dedupes_by_id_and_skips_nav():
    schools = _index()
    ids = [s["id"] for s in schools]
    assert ids.count("02790") == 1                       # duplicate collapsed
    assert len(schools) == 6                             # 6 unique; nav links ignored


def test_parse_overview_extracts_own_site_and_nothing_else():
    ov = R.parse_overview(
        (FIXTURES / "schooloverview_bcc.html").read_text(encoding="utf-8"), "04406")
    assert ov["website"] == "https://bcc-hs.mcpsmd.org/"
    assert ov["short_name"] == "Bethesda-Chevy Chase HS"
    assert ov["overview_url"].endswith("schooloverview.aspx?s=04406")
    # a link table, not a directory copy: address/phone are on the page
    # but deliberately not collected
    assert set(ov) == {"website", "short_name", "overview_url"}


def test_bundled_json_is_a_link_table_only():
    import json
    data = json.loads((R.OUT_PATH).read_text(encoding="utf-8"))
    keys = {k for s in data["schools"] for k in s}
    assert keys == {"id", "name", "short_name", "level", "website", "overview_url"}


def test_parse_overview_without_site_falls_back():
    ov = R.parse_overview(
        (FIXTURES / "schooloverview_nosite.html").read_text(encoding="utf-8"), "55107")
    assert ov["website"] == ""                           # no hypWebSite anchor
    assert ov["short_name"] == "Blair G. Ewing Center"
    assert ov["overview_url"].endswith("s=55107")
