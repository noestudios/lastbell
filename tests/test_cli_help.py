"""`lastbell --help` and bare `lastbell`: grouped, complete, and pointing at
the dashboard key a phone needs."""
from __future__ import annotations

import pytest

from lastbell import cli


@pytest.fixture
def parser_and_sub():
    return cli.build_parser()


def test_every_command_is_in_exactly_one_group(parser_and_sub):
    parser, sub = parser_and_sub
    grouped = [name for _, names in cli.COMMAND_GROUPS for name in names]
    assert len(grouped) == len(set(grouped)), "a command is listed twice"
    assert set(grouped) == set(sub.choices), (
        "help groups and subcommands disagree: "
        f"missing {sorted(set(sub.choices) - set(grouped))}, "
        f"unknown {sorted(set(grouped) - set(sub.choices))}")


def test_help_is_grouped_and_names_the_dashboard_key(parser_and_sub):
    parser, _sub = parser_and_sub
    text = parser.format_help()
    for title, _names in cli.COMMAND_GROUPS:
        assert f"\n{title}\n" in text
    assert "usage: lastbell <command> [options]" in text
    assert "Self-hosted ParentVUE grade monitor" in text
    assert "--show-key prints the link a" in text
    assert "--version" in text
    # The descriptions come from the subparsers, so nothing is written twice.
    assert text.count("list recent alerts") == 1


def test_help_wraps_to_a_readable_width(parser_and_sub):
    parser, _sub = parser_and_sub
    body = [ln for ln in parser.format_help().splitlines()
            if not ln.startswith("`")]           # the closing pointer may run on
    assert max(len(ln) for ln in body) <= 78


def test_bare_lastbell_prints_the_help_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["lastbell"])
    assert cli.main() is None                    # no SystemExit, so exit code 0
    out = capsys.readouterr().out
    assert "usage: lastbell <command> [options]" in out
    for title, _names in cli.COMMAND_GROUPS:
        assert title in out
