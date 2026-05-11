"""Tests for src/c3/plan_validator.py.

PO 廃止計画 Step 4 (v1.14.0) で新設されたモジュールの単体テスト。
extract_frontmatter / compute_waves / validate_plan_report / split_waves
の主要パスを確認する。
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from c3.plan_validator import (
    compute_waves,
    extract_frontmatter,
    split_waves,
    validate_plan_report,
)


def _make_report(tmp_path: Path, frontmatter: str, body: str = "# plan") -> Path:
    path = tmp_path / "plan-report-test.md"
    path.write_text(f"---\n{frontmatter}---\n{body}\n", encoding="utf-8")
    return path


def _make_claude_root_with_agents(tmp_path: Path, agent_names: list[str]) -> Path:
    root = tmp_path / "project"
    agents_dir = root / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    for name in agent_names:
        (agents_dir / f"{name}.md").write_text(f"# {name}\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# extract_frontmatter
# ---------------------------------------------------------------------------


class TestExtractFrontmatter:

    def test_returns_dict_for_valid_yaml(self, tmp_path: Path) -> None:
        report = _make_report(tmp_path, "po_plan_version: \"0.1\"\nname: test\n")
        fm = extract_frontmatter(report)
        assert fm == {"po_plan_version": "0.1", "name": "test"}

    def test_returns_none_for_no_frontmatter(self, tmp_path: Path) -> None:
        path = tmp_path / "no-fm.md"
        path.write_text("# plain markdown\n", encoding="utf-8")
        assert extract_frontmatter(path) is None

    def test_returns_none_for_malformed_yaml(self, tmp_path: Path) -> None:
        report = _make_report(tmp_path, "invalid: : :\nbad yaml\n")
        assert extract_frontmatter(report) is None

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        assert extract_frontmatter(tmp_path / "nonexistent.md") is None


# ---------------------------------------------------------------------------
# compute_waves
# ---------------------------------------------------------------------------


class TestComputeWaves:

    def test_empty_tasks_returns_empty(self) -> None:
        assert compute_waves({}) == []
        assert compute_waves({"tasks": []}) == []

    def test_single_independent_task(self) -> None:
        fm = {"tasks": [{"id": "a", "agent": "x"}]}
        waves = compute_waves(fm)
        assert len(waves) == 1
        assert waves[0][0]["id"] == "a"

    def test_two_waves_via_depends_on(self) -> None:
        fm = {
            "tasks": [
                {"id": "first", "agent": "x"},
                {"id": "second", "agent": "y", "depends_on": ["first"]},
            ]
        }
        waves = compute_waves(fm)
        assert [t["id"] for w in waves for t in w] == ["first", "second"]
        assert len(waves) == 2

    def test_parallel_in_same_wave_sorted_by_id(self) -> None:
        fm = {
            "tasks": [
                {"id": "b", "agent": "x"},
                {"id": "a", "agent": "x"},
                {"id": "c", "agent": "x"},
            ]
        }
        waves = compute_waves(fm)
        assert [t["id"] for t in waves[0]] == ["a", "b", "c"]

    def test_cycle_raises(self) -> None:
        fm = {
            "tasks": [
                {"id": "a", "agent": "x", "depends_on": ["b"]},
                {"id": "b", "agent": "x", "depends_on": ["a"]},
            ]
        }
        with pytest.raises(ValueError, match="cycle detected"):
            compute_waves(fm)

    def test_unknown_depends_on_raises(self) -> None:
        fm = {"tasks": [{"id": "a", "agent": "x", "depends_on": ["nonexistent"]}]}
        with pytest.raises(ValueError, match="depends_on unknown id"):
            compute_waves(fm)

    def test_duplicate_id_raises(self) -> None:
        fm = {
            "tasks": [
                {"id": "a", "agent": "x"},
                {"id": "a", "agent": "y"},
            ]
        }
        with pytest.raises(ValueError, match="duplicate task id"):
            compute_waves(fm)


# ---------------------------------------------------------------------------
# validate_plan_report
# ---------------------------------------------------------------------------


class TestValidatePlanReport:

    def test_valid_report_returns_empty(self, tmp_path: Path) -> None:
        root = _make_claude_root_with_agents(tmp_path, ["developer"])
        report = _make_report(
            root,
            textwrap.dedent(
                """\
                po_plan_version: "0.1"
                tasks:
                  - id: t1
                    agent: developer
                    prompt: implement login
                """
            ),
        )
        errors = validate_plan_report(report, root)
        assert errors == []

    def test_missing_po_plan_version(self, tmp_path: Path) -> None:
        root = _make_claude_root_with_agents(tmp_path, ["developer"])
        report = _make_report(
            root,
            textwrap.dedent(
                """\
                tasks:
                  - id: t1
                    agent: developer
                    prompt: x
                """
            ),
        )
        errors = validate_plan_report(report, root)
        assert any("po_plan_version" in e for e in errors)

    def test_missing_tasks(self, tmp_path: Path) -> None:
        root = _make_claude_root_with_agents(tmp_path, ["developer"])
        report = _make_report(root, "po_plan_version: \"0.1\"\n")
        errors = validate_plan_report(report, root)
        assert any("tasks" in e for e in errors)

    def test_unknown_agent_file(self, tmp_path: Path) -> None:
        root = _make_claude_root_with_agents(tmp_path, ["developer"])
        report = _make_report(
            root,
            textwrap.dedent(
                """\
                po_plan_version: "0.1"
                tasks:
                  - id: t1
                    agent: nonexistent
                    prompt: x
                """
            ),
        )
        errors = validate_plan_report(report, root)
        assert any("nonexistent" in e and "not found" in e for e in errors)

    def test_missing_prompt(self, tmp_path: Path) -> None:
        root = _make_claude_root_with_agents(tmp_path, ["developer"])
        report = _make_report(
            root,
            textwrap.dedent(
                """\
                po_plan_version: "0.1"
                tasks:
                  - id: t1
                    agent: developer
                """
            ),
        )
        errors = validate_plan_report(report, root)
        assert any("prompt" in e for e in errors)

    def test_cycle_via_compute_waves(self, tmp_path: Path) -> None:
        root = _make_claude_root_with_agents(tmp_path, ["developer"])
        report = _make_report(
            root,
            textwrap.dedent(
                """\
                po_plan_version: "0.1"
                tasks:
                  - id: a
                    agent: developer
                    prompt: x
                    depends_on: [b]
                  - id: b
                    agent: developer
                    prompt: y
                    depends_on: [a]
                """
            ),
        )
        errors = validate_plan_report(report, root)
        assert any("cycle" in e for e in errors)

    def test_no_frontmatter_returns_parse_error(self, tmp_path: Path) -> None:
        root = _make_claude_root_with_agents(tmp_path, [])
        path = tmp_path / "no-fm.md"
        path.write_text("# plain markdown\n", encoding="utf-8")
        errors = validate_plan_report(path, root)
        assert errors == ["could not parse YAML frontmatter"]


# ---------------------------------------------------------------------------
# split_waves
# ---------------------------------------------------------------------------


class TestSplitWaves:

    def test_returns_json_friendly_dict(self, tmp_path: Path) -> None:
        report = _make_report(
            tmp_path,
            textwrap.dedent(
                """\
                po_plan_version: "0.1"
                tasks:
                  - id: a
                    agent: developer
                    read_only: false
                    writes: ["src/a.py"]
                    prompt: aaa
                  - id: b
                    agent: developer
                    read_only: false
                    writes: ["src/b.py"]
                    prompt: bbb
                    depends_on: [a]
                """
            ),
        )
        output = split_waves(report)
        assert "waves" in output
        assert len(output["waves"]) == 2
        first = output["waves"][0]["tasks"][0]
        assert first == {
            "id": "a",
            "agent": "developer",
            "read_only": False,
            "writes": ["src/a.py"],
            "prompt": "aaa",
        }

    def test_missing_frontmatter_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.md"
        path.write_text("# no frontmatter\n", encoding="utf-8")
        with pytest.raises(ValueError, match="could not parse"):
            split_waves(path)
