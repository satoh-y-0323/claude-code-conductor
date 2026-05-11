"""Tests for src/c3/cli_plan.py (``c3 plan validate`` / ``c3 plan waves``).

PO 廃止計画 Step 4 (v1.14.0) で旧 ``c3 po dry-run`` / ``c3 po waves`` を
置き換えた新 CLI のサブコマンド動作を確認する。
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from c3 import cli


def _make_claude_root(tmp_path: Path, agent_names: list[str]) -> Path:
    root = tmp_path / "project"
    agents_dir = root / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    for name in agent_names:
        (agents_dir / f"{name}.md").write_text(f"# {name}\n", encoding="utf-8")
    reports_dir = root / ".claude" / "reports"
    reports_dir.mkdir(parents=True)
    return root


def _make_report(root: Path, frontmatter: str) -> Path:
    path = root / ".claude" / "reports" / "plan-report-test.md"
    path.write_text(f"---\n{frontmatter}---\n# plan\n", encoding="utf-8")
    return path


def _run(monkeypatch: pytest.MonkeyPatch, root: Path, *args: str) -> int:
    """Run `c3 <args>` with cwd at `root`."""
    monkeypatch.chdir(root)
    return cli.main(list(args))


class TestCliPlanValidate:

    def test_exit_0_on_valid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        root = _make_claude_root(tmp_path, ["developer"])
        report = _make_report(
            root,
            textwrap.dedent(
                """\
                po_plan_version: "0.1"
                tasks:
                  - id: t1
                    agent: developer
                    prompt: x
                """
            ),
        )
        rc = _run(monkeypatch, root, "plan", "validate", str(report))
        assert rc == 0

    def test_exit_2_on_missing_field(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        root = _make_claude_root(tmp_path, ["developer"])
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
        rc = _run(monkeypatch, root, "plan", "validate", str(report))
        assert rc == 2
        err = capsys.readouterr().err
        assert "po_plan_version" in err

    def test_exit_2_on_missing_agent_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        root = _make_claude_root(tmp_path, ["developer"])
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
        rc = _run(monkeypatch, root, "plan", "validate", str(report))
        assert rc == 2
        err = capsys.readouterr().err
        assert "nonexistent" in err

    def test_exit_2_on_missing_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        root = _make_claude_root(tmp_path, [])
        rc = _run(monkeypatch, root, "plan", "validate", str(tmp_path / "nope.md"))
        assert rc == 2


class TestCliPlanWaves:

    def test_outputs_valid_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        root = _make_claude_root(tmp_path, ["developer"])
        report = _make_report(
            root,
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
        rc = _run(monkeypatch, root, "plan", "waves", str(report))
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert len(data["waves"]) == 2
        assert data["waves"][0]["tasks"][0]["id"] == "a"
        assert data["waves"][1]["tasks"][0]["id"] == "b"

    def test_exit_2_on_validation_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        root = _make_claude_root(tmp_path, ["developer"])
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
        rc = _run(monkeypatch, root, "plan", "waves", str(report))
        assert rc == 2
        err = capsys.readouterr().err
        assert "cycle" in err
