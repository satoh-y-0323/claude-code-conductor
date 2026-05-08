"""Tests for F-005 Phase 2-A: PO 経由サブエージェントの model 動的切替。

検証対象:
  - src/parallel_orchestra/manifest.py の Task.model_override / Defaults.model
  - src/parallel_orchestra/runner.py の _resolve_effective_model / _read_tier_selection
  - runner.py の cmd 構築（--agents JSON 化）

テストケース:
 _resolve_effective_model:
  1. task.model_override > tier_selection（manifest 優先）
  2. tier_selection（task.model_override が None なら tier_selection 採用）
  3. どちらも無ければ None（frontmatter 任せ）
  4. tier_selection が壊れた dict（suggested_model なし）→ None

 _read_tier_selection:
  5. ファイル無しで None
  6. 不正 JSON で None
  7. 正常 JSON で dict

 cmd 構築（_execute_task は subprocess を起こすので Popen を mock）:
  8. model_override=None で --agent <name> 形式
  9. model_override="haiku" で --agents JSON 形式（"--agents" の値が
     {"<agent>": {"model": "haiku"}} の JSON 文字列）

 manifest 受理:
 10. task に model: sonnet を指定 → Task.model_override="sonnet"
 11. defaults に model: opus を指定 → 全 task の model_override="opus" に継承
 12. task と defaults 両方指定 → task 側が優先
 13. 不正な model 値（claude-3-5-haiku など） → ManifestError
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from parallel_orchestra import runner
from parallel_orchestra.manifest import (
    Defaults,
    ManifestError,
    Task,
    load_manifest,
)
from parallel_orchestra.runner import (
    _read_tier_selection,
    _resolve_effective_model,
)


def _make_task(
    *, agent: str = "developer", model_override: str | None = None,
) -> Task:
    return Task(
        id="t1",
        agent=agent,
        read_only=False,
        prompt="do something",
        env={},
        model_override=model_override,
    )


# ---------------------------------------------------------------------------
# _resolve_effective_model
# ---------------------------------------------------------------------------


class TestResolveEffectiveModel:

    def test_task_override_wins(self) -> None:
        task = _make_task(model_override="haiku")
        tier_sel = {"suggested_model": "opus"}
        model, source = _resolve_effective_model(task, tier_sel)
        assert model == "haiku"
        assert source == "manifest"

    def test_tier_selection_when_no_task_override(self) -> None:
        task = _make_task(model_override=None)
        tier_sel = {"suggested_model": "sonnet"}
        model, source = _resolve_effective_model(task, tier_sel)
        assert model == "sonnet"
        assert source == "tier_selection"

    def test_neither_returns_none(self) -> None:
        task = _make_task(model_override=None)
        model, source = _resolve_effective_model(task, None)
        assert model is None
        assert source == "frontmatter"

    def test_broken_tier_selection_returns_none(self) -> None:
        """tier_selection に suggested_model が無ければ frontmatter 扱い。"""
        task = _make_task(model_override=None)
        tier_sel = {"complexity": "medium", "tier": "haiku"}  # suggested_model 無し
        model, source = _resolve_effective_model(task, tier_sel)
        assert model is None
        assert source == "frontmatter"


# ---------------------------------------------------------------------------
# _read_tier_selection
# ---------------------------------------------------------------------------


class TestReadTierSelection:

    def test_returns_none_when_file_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert _read_tier_selection() is None

    def test_returns_none_for_invalid_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sel_dir = tmp_path / ".claude" / "state"
        sel_dir.mkdir(parents=True)
        (sel_dir / "tier_selection.json").write_text("not json", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert _read_tier_selection() is None

    def test_returns_dict_when_valid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sel_dir = tmp_path / ".claude" / "state"
        sel_dir.mkdir(parents=True)
        payload = {"complexity": "medium", "tier": "sonnet", "suggested_model": "sonnet"}
        (sel_dir / "tier_selection.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        result = _read_tier_selection()
        assert result == payload


# ---------------------------------------------------------------------------
# cmd 構築（_execute_task の subprocess.Popen を mock してコマンドを検証）
# ---------------------------------------------------------------------------


class TestCmdConstruction:
    """_execute_task が subprocess.Popen に渡す cmd を検証する。"""

    def _stub_popen(self, captured: list) -> MagicMock:
        """Popen をスタブして cmd を記録するファクトリ。"""

        class _StubProc:
            def __init__(self, cmd: list, **kwargs) -> None:
                captured.append(cmd)
                self.returncode = 0
                self.stdout = self._null_stream("")
                self.stderr = self._null_stream("")

            @staticmethod
            def _null_stream(content: str):
                import io
                return io.StringIO(content)

            def wait(self) -> None:
                return None

            def kill(self) -> None:
                return None

        return _StubProc

    def test_no_model_override_uses_agent_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """model 指定がない場合は従来通り --agent <name>。"""
        captured: list = []
        monkeypatch.setattr(subprocess, "Popen", self._stub_popen(captured))
        # tier_selection 不在
        monkeypatch.chdir(tmp_path)

        task = Task(
            id="t1", agent="developer", read_only=True, prompt="x", env={},
            model_override=None,
        )
        runner._execute_task(
            task, claude_exe="claude",
            git_root=None, effective_cwd=tmp_path, dashboard=None,
        )
        assert captured, "Popen should have been called"
        cmd = captured[0]
        assert "--agent" in cmd
        agent_idx = cmd.index("--agent")
        assert cmd[agent_idx + 1] == "developer"
        assert "--agents" not in cmd

    def test_model_override_uses_agents_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """model_override 指定時は --agents JSON 形式。"""
        captured: list = []
        monkeypatch.setattr(subprocess, "Popen", self._stub_popen(captured))
        monkeypatch.chdir(tmp_path)

        task = Task(
            id="t1", agent="developer", read_only=True, prompt="x", env={},
            model_override="haiku",
        )
        runner._execute_task(
            task, claude_exe="claude",
            git_root=None, effective_cwd=tmp_path, dashboard=None,
        )
        cmd = captured[0]
        assert "--agents" in cmd
        agents_idx = cmd.index("--agents")
        agents_json = cmd[agents_idx + 1]
        # JSON として parse できること、中身が想定通り
        parsed = json.loads(agents_json)
        assert parsed == {"developer": {"model": "haiku"}}
        # 同時に --agent <name> は含まれていない
        assert "--agent" not in cmd

    def test_tier_selection_used_when_no_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """task.model_override が None で tier_selection に suggested_model があれば使う。"""
        captured: list = []
        monkeypatch.setattr(subprocess, "Popen", self._stub_popen(captured))

        sel_dir = tmp_path / ".claude" / "state"
        sel_dir.mkdir(parents=True)
        (sel_dir / "tier_selection.json").write_text(
            json.dumps({"suggested_model": "sonnet"}),
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        task = Task(
            id="t1", agent="developer", read_only=True, prompt="x", env={},
            model_override=None,
        )
        runner._execute_task(
            task, claude_exe="claude",
            git_root=None, effective_cwd=tmp_path, dashboard=None,
        )
        cmd = captured[0]
        assert "--agents" in cmd
        agents_json = cmd[cmd.index("--agents") + 1]
        parsed = json.loads(agents_json)
        assert parsed == {"developer": {"model": "sonnet"}}


# ---------------------------------------------------------------------------
# manifest 受理
# ---------------------------------------------------------------------------


def _write_manifest(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "manifest.md"
    path.write_text(content, encoding="utf-8")
    return path


class TestManifestModelField:

    def test_task_model_field(self, tmp_path: Path) -> None:
        content = (
            "---\n"
            "po_plan_version: \"0.1\"\n"
            "name: test\n"
            "cwd: \".\"\n"
            "tasks:\n"
            "  - id: t1\n"
            "    agent: developer\n"
            "    read_only: true\n"
            "    model: sonnet\n"
            "---\n"
        )
        m = load_manifest(_write_manifest(tmp_path, content))
        assert m.tasks[0].model_override == "sonnet"

    def test_defaults_model_inheritance(self, tmp_path: Path) -> None:
        content = (
            "---\n"
            "po_plan_version: \"0.1\"\n"
            "name: test\n"
            "cwd: \".\"\n"
            "defaults:\n"
            "  model: opus\n"
            "tasks:\n"
            "  - id: t1\n"
            "    agent: developer\n"
            "    read_only: true\n"
            "  - id: t2\n"
            "    agent: tester\n"
            "    read_only: true\n"
            "---\n"
        )
        m = load_manifest(_write_manifest(tmp_path, content))
        assert m.defaults is not None
        assert m.defaults.model == "opus"
        # 両 task が defaults を継承
        assert all(t.model_override == "opus" for t in m.tasks)

    def test_task_overrides_defaults(self, tmp_path: Path) -> None:
        content = (
            "---\n"
            "po_plan_version: \"0.1\"\n"
            "name: test\n"
            "cwd: \".\"\n"
            "defaults:\n"
            "  model: opus\n"
            "tasks:\n"
            "  - id: t1\n"
            "    agent: developer\n"
            "    read_only: true\n"
            "    model: haiku\n"
            "---\n"
        )
        m = load_manifest(_write_manifest(tmp_path, content))
        # task 個別指定が defaults を上書き
        assert m.tasks[0].model_override == "haiku"

    def test_invalid_model_value_raises(self, tmp_path: Path) -> None:
        content = (
            "---\n"
            "po_plan_version: \"0.1\"\n"
            "name: test\n"
            "cwd: \".\"\n"
            "tasks:\n"
            "  - id: t1\n"
            "    agent: developer\n"
            "    read_only: true\n"
            "    model: claude-3-5-haiku\n"
            "---\n"
        )
        with pytest.raises(ManifestError, match="model"):
            load_manifest(_write_manifest(tmp_path, content))
