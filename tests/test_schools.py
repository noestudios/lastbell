"""Resolver tests: ParentVUE school name -> the school's own website URL,
against a small hermetic directory fixture (not the committed full file)."""
from __future__ import annotations

import pathlib

import pytest

from lastbell import schools

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def directory(monkeypatch):
    """Point the resolver at the tiny fixture JSON and reset its cache."""
    monkeypatch.setattr(schools, "_DATA_PATH", FIXTURES / "mcps_schools.json")
    schools._index.cache_clear()
    yield
    schools._index.cache_clear()


# ── normalization / canonicalization (no data file needed) ────────────

def test_canon_folds_level_variants_to_one_key():
    assert schools._key("Winston Churchill HS") == schools._key("Winston Churchill High School")
    assert schools._key("Some Place Elementary") == schools._key("Some Place ES")
    assert schools._key("Some Place Middle School") == schools._key("Some Place MS")


def test_normalize_folds_accents_and_ampersand():
    assert schools._normalize("Café & Crème") == "cafe and creme"


def test_base_strips_trailing_level_only():
    assert schools._base(schools._key("Poolesville High School")) == "poolesville"
    assert schools._base("poolesville") == "poolesville"     # nothing to strip


# ── lookup (uses the fixture directory) ───────────────────────────────

def test_resolves_abbreviation_and_full_variants(directory):
    for variant in ("Bethesda-Chevy Chase HS", "Bethesda-Chevy Chase High School",
                    "Bethesda-Chevy Chase"):
        assert schools.school_url(variant) == "https://bcc-hs.mcpsmd.org/"


def test_cross_level_collision_resolves_when_qualified(directory):
    assert schools.school_url("Poolesville High School") == "https://poolesville-hs.mcpsmd.org/"
    assert schools.school_url("Poolesville Elementary") == "https://poolesville-es.mcpsmd.org/"


def test_bare_ambiguous_name_returns_none(directory):
    # "Poolesville" is both an ES and an HS — refuse rather than guess.
    assert schools.school_url("Poolesville") is None


def test_unknown_school_returns_none(directory):
    assert schools.school_url("Bellwood High") is None      # the seed/demo case
    assert schools.school_url("Example ES") is None


def test_empty_name_returns_none(directory):
    assert schools.school_url("") is None
    assert schools.school_url("   ") is None


def test_falls_back_to_overview_when_no_own_site(directory):
    url = schools.school_url("Blair G. Ewing Center")
    assert url == "https://ww2.montgomeryschoolsmd.org/schoolodex/schooloverview.aspx?s=55107"


def test_alias_map_applies(directory, monkeypatch):
    assert schools.school_url("BCC") is None                # not known as-is
    monkeypatch.setitem(schools._ALIASES, "bcc", "bethesda chevy chase")
    schools._index.cache_clear()
    assert schools.school_url("BCC") == "https://bcc-hs.mcpsmd.org/"


def test_missing_data_file_is_graceful(monkeypatch):
    monkeypatch.setattr(schools, "_DATA_PATH", FIXTURES / "does_not_exist.json")
    schools._index.cache_clear()
    try:
        assert schools._index() == ({}, {})
        assert schools.school_url("Bethesda-Chevy Chase HS") is None
    finally:
        schools._index.cache_clear()
