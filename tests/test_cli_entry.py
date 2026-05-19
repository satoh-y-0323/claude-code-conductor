"""Tests for the top-level CLI argv-rewriting logic in ``c3.cli``.

Currently the only rewrite is ``c3 recall <query>`` -> ``c3 recall search <query>``.
"""

from __future__ import annotations

from c3.cli import _rewrite_recall_shortcut


def test_passthrough_when_argv_none() -> None:
    # When ``argv`` is None the function reads sys.argv; ensure that also
    # returns a list without crashing.  We pin sys.argv to a known value.
    import sys
    from unittest.mock import patch

    with patch.object(sys, "argv", ["c3", "init"]):
        result = _rewrite_recall_shortcut(None)  # type: ignore[arg-type]
    assert isinstance(result, list)


def test_passthrough_for_non_recall_commands() -> None:
    assert _rewrite_recall_shortcut(["doctor", "--quiet"]) == ["doctor", "--quiet"]


def test_passthrough_when_explicit_subcommand_given() -> None:
    assert _rewrite_recall_shortcut(["recall", "search", "認証"]) == [
        "recall",
        "search",
        "認証",
    ]
    assert _rewrite_recall_shortcut(["recall", "rebuild"]) == ["recall", "rebuild"]
    assert _rewrite_recall_shortcut(["recall", "stats", "--json"]) == [
        "recall",
        "stats",
        "--json",
    ]


def test_passthrough_when_only_recall() -> None:
    # ``c3 recall`` with no args should fall through to argparse so it can
    # surface a helpful "subcommand required" error.
    assert _rewrite_recall_shortcut(["recall"]) == ["recall"]


def test_passthrough_when_recall_help_or_flags() -> None:
    assert _rewrite_recall_shortcut(["recall", "--help"]) == ["recall", "--help"]
    assert _rewrite_recall_shortcut(["recall", "-h"]) == ["recall", "-h"]


def test_rewrites_recall_query_shortcut() -> None:
    assert _rewrite_recall_shortcut(["recall", "認証エラー"]) == [
        "recall",
        "search",
        "認証エラー",
    ]
    assert _rewrite_recall_shortcut(["recall", "test query", "--top", "3"]) == [
        "recall",
        "search",
        "test query",
        "--top",
        "3",
    ]


def test_rewrite_does_not_swallow_query_that_looks_like_command() -> None:
    # Even if the query text starts with a regular word, only the *exact*
    # subcommand names should bypass rewriting.
    assert _rewrite_recall_shortcut(["recall", "searchable"]) == [
        "recall",
        "search",
        "searchable",
    ]
