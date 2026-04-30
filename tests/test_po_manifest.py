"""Tests for c3.po.manifest.

Fixtures use the canonical example from
``.claude/docs/parallel-orchestra-manifest.md`` (lines 48-91).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from c3.po.manifest import extract_frontmatter, validate_manifest


_CANONICAL_EXAMPLE = textwrap.dedent(
    """\
    ---
    po_plan_version: "0.1"
    name: "ユーザー認証機能の並列実装"
    cwd: "../.."

    tasks:
      - id: tdd-auth-login
        agent: tdd-develop
        read_only: false
        prompt: |
          ログイン機能を TDD で実装してください。
          plan-report: .claude/reports/plan-report-20260429-120000.md
        writes:
          - src/auth/login.py
          - tests/test_login.py

      - id: tdd-auth-logout
        agent: tdd-develop
        read_only: false
        prompt: |
          ログアウト機能を TDD で実装してください。
          plan-report: .claude/reports/plan-report-20260429-120000.md
        writes:
          - src/auth/logout.py
          - tests/test_logout.py

      - id: review-auth
        agent: code-reviewer
        read_only: true
        prompt: "認証モジュール全体のコードレビューを行ってください。"
        depends_on:
          - tdd-auth-login
          - tdd-auth-logout
        concurrency_group: api-calls

    defaults:
      max_retries: 1

    concurrency_limits:
      api-calls: 2
    ---

    # plan-report 本文がここに続きます
    """
)


def _make_claude_root(root: Path) -> Path:
    """Create a fake .claude/agents/ with the agents referenced by the canonical example."""
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    for name in ("tdd-develop", "code-reviewer"):
        (agents / f"{name}.md").write_text(f"# {name}\n", encoding="utf-8")
    return root


def test_extract_frontmatter_canonical(tmp_path: Path):
    plan_report = tmp_path / "plan-report.md"
    plan_report.write_text(_CANONICAL_EXAMPLE, encoding="utf-8")
    fm = extract_frontmatter(plan_report)
    assert fm is not None
    assert fm["po_plan_version"] == "0.1"
    assert fm["name"] == "ユーザー認証機能の並列実装"
    assert fm["cwd"] == "../.."
    assert isinstance(fm["tasks"], list) and len(fm["tasks"]) == 3
    assert fm["tasks"][0]["id"] == "tdd-auth-login"
    assert fm["tasks"][0]["agent"] == "tdd-develop"
    assert fm["tasks"][0]["read_only"] is False
    assert "ログイン機能" in fm["tasks"][0]["prompt"]
    assert fm["tasks"][2]["depends_on"] == ["tdd-auth-login", "tdd-auth-logout"]
    assert fm["defaults"]["max_retries"] == 1
    assert fm["concurrency_limits"]["api-calls"] == 2


def test_extract_frontmatter_missing(tmp_path: Path):
    plan_report = tmp_path / "no-fm.md"
    plan_report.write_text("# just a heading\n", encoding="utf-8")
    assert extract_frontmatter(plan_report) is None


def test_validate_manifest_valid(tmp_path: Path):
    plan_report = tmp_path / "plan-report.md"
    plan_report.write_text(_CANONICAL_EXAMPLE, encoding="utf-8")
    _make_claude_root(tmp_path)
    errors = validate_manifest(plan_report, tmp_path)
    assert errors == []


def test_validate_manifest_missing_agent(tmp_path: Path):
    bad = _CANONICAL_EXAMPLE.replace("agent: tdd-develop", "agent: ghost-agent", 1)
    plan_report = tmp_path / "plan-report.md"
    plan_report.write_text(bad, encoding="utf-8")
    _make_claude_root(tmp_path)
    errors = validate_manifest(plan_report, tmp_path)
    assert any("ghost-agent" in e for e in errors), errors


def test_validate_manifest_duplicate_id(tmp_path: Path):
    bad = _CANONICAL_EXAMPLE.replace("id: tdd-auth-logout", "id: tdd-auth-login", 1)
    plan_report = tmp_path / "plan-report.md"
    plan_report.write_text(bad, encoding="utf-8")
    _make_claude_root(tmp_path)
    errors = validate_manifest(plan_report, tmp_path)
    assert any("duplicate task id" in e for e in errors), errors


def test_validate_manifest_wrong_version(tmp_path: Path):
    bad = _CANONICAL_EXAMPLE.replace('po_plan_version: "0.1"', 'po_plan_version: "0.2"')
    plan_report = tmp_path / "plan-report.md"
    plan_report.write_text(bad, encoding="utf-8")
    _make_claude_root(tmp_path)
    errors = validate_manifest(plan_report, tmp_path)
    assert any("po_plan_version" in e for e in errors), errors


def test_validate_manifest_missing_frontmatter(tmp_path: Path):
    plan_report = tmp_path / "plan-report.md"
    plan_report.write_text("# no frontmatter\n", encoding="utf-8")
    _make_claude_root(tmp_path)
    errors = validate_manifest(plan_report, tmp_path)
    assert errors and "frontmatter missing" in errors[0]
