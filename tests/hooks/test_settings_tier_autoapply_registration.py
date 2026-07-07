"""Tests for tier_autoapply.py registration in the PreToolUse "Agent" matcher.

Covers plan-report-20260707-065732.md task test-settings-sync (T2 Red):
  - `.claude/settings.json` の PreToolUse に matcher "Agent" のエントリがあり、
    その hooks 配列に check_agent_invocation.py と tier_autoapply.py の両方が
    順序どおり（check_agent_invocation が先）で含まれていた。
  - `src/c3/_template/.claude/settings.json` にも同一の登録があり、repo と
    _template が同期していた（settings.json は _template 側にも実体があるため
    直接比較可能。architecture-report-20260707-065043.md §9 T2）。
  - 新規登録エントリが既存 hook と同型
    ({"type": "command", "command": "python", "args": [...]}) だった。
  - 既存の check_agent_invocation.py 登録が不変で残存していた
    （共通受け入れ条件8・R5 exit 2 ロジックとの構造的排他を維持）。

Red 時点（本タスク作成時点）では tier_autoapply.py は settings.json の
Agent matcher にまだ登録されていないため、hooks 配列の長さ・内容チェックが
失敗する（未実装による Red。構文エラー起因ではない）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).parents[2]
REPO_SETTINGS = WORKTREE_ROOT / ".claude" / "settings.json"
TEMPLATE_SETTINGS = (
    WORKTREE_ROOT / "src" / "c3" / "_template" / ".claude" / "settings.json"
)

CHECK_AGENT_INVOCATION_ARG = (
    "${CLAUDE_PROJECT_DIR}/.claude/hooks/check_agent_invocation.py"
)
TIER_AUTOAPPLY_ARG = "${CLAUDE_PROJECT_DIR}/.claude/hooks/tier_autoapply.py"


def _load_settings(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _agent_matcher_hooks(settings: dict) -> list[dict]:
    """PreToolUse の matcher: "Agent" エントリの hooks 配列を返した。

    matcher が見つからない場合は空リストを返し、呼び出し側で assert する。
    """
    pre_tool_use = settings.get("hooks", {}).get("PreToolUse", [])
    for entry in pre_tool_use:
        if entry.get("matcher") == "Agent":
            return entry.get("hooks", [])
    return []


def _hook_script_args(hook: dict) -> list[str]:
    return hook.get("args", [])


class TestRepoSettingsAgentMatcherRegistersBothHooks:
    """`.claude/settings.json` の Agent matcher に両 hook が順序どおり登録されていた。"""

    def test_agent_matcher_exists(self) -> None:
        settings = _load_settings(REPO_SETTINGS)
        hooks = _agent_matcher_hooks(settings)
        assert hooks, "PreToolUse に matcher: 'Agent' のエントリが見つからなかった"

    def test_check_agent_invocation_still_present(self) -> None:
        """既存 check_agent_invocation.py の登録が不変で残存していた（共通受け入れ条件8）。"""
        settings = _load_settings(REPO_SETTINGS)
        hooks = _agent_matcher_hooks(settings)
        scripts = [
            arg
            for hook in hooks
            for arg in _hook_script_args(hook)
        ]
        assert CHECK_AGENT_INVOCATION_ARG in scripts, (
            "check_agent_invocation.py の既存登録が見つからなかった（不変であるべき）"
        )

    def test_tier_autoapply_registered_as_second_hook(self) -> None:
        """tier_autoapply.py が check_agent_invocation.py の次に登録されていた。"""
        settings = _load_settings(REPO_SETTINGS)
        hooks = _agent_matcher_hooks(settings)
        assert len(hooks) >= 2, (
            f"Agent matcher の hooks 配列が2本に拡張されていなかった（現状 {len(hooks)} 本）: "
            "tier_autoapply.py の登録が未実装だった"
        )
        scripts = [_hook_script_args(hook) for hook in hooks]
        assert [CHECK_AGENT_INVOCATION_ARG] == scripts[0], (
            "1本目の hook が check_agent_invocation.py ではなかった"
        )
        assert [TIER_AUTOAPPLY_ARG] == scripts[1], (
            "2本目の hook が tier_autoapply.py ではなかった（未登録または順序違反）"
        )

    def test_tier_autoapply_hook_shape_matches_existing_convention(self) -> None:
        """新規登録エントリが既存 hook と同型の command/args 構造だった。"""
        settings = _load_settings(REPO_SETTINGS)
        hooks = _agent_matcher_hooks(settings)
        tier_hooks = [
            hook
            for hook in hooks
            if TIER_AUTOAPPLY_ARG in _hook_script_args(hook)
        ]
        assert len(tier_hooks) == 1, (
            "tier_autoapply.py の登録エントリが1件見つからなかった"
        )
        hook = tier_hooks[0]
        assert hook.get("type") == "command"
        assert hook.get("command") == "python"
        assert hook.get("args") == [TIER_AUTOAPPLY_ARG]


class TestTemplateSettingsSyncedWithRepo:
    """`src/c3/_template/.claude/settings.json` が repo 側と同一登録を持っていた。

    settings.json は hatch_build.py がビルド時に .claude/ から再生成する配布実体
    だが、リポジトリ内には _template 側の実体ファイルも存在するため直接比較可能
    （T2 は本テストで repo/_template 両方の直接編集を要求する）。
    """

    def test_template_file_exists(self) -> None:
        assert TEMPLATE_SETTINGS.exists(), (
            f"テンプレート settings.json が見つからなかった: {TEMPLATE_SETTINGS}"
        )

    def test_template_agent_matcher_registers_both_hooks(self) -> None:
        if not TEMPLATE_SETTINGS.exists():
            pytest.skip("テンプレート settings.json が不在のため skip した")

        template_settings = _load_settings(TEMPLATE_SETTINGS)
        hooks = _agent_matcher_hooks(template_settings)
        assert len(hooks) >= 2, (
            f"テンプレート側 Agent matcher の hooks 配列が2本に拡張されていなかった"
            f"（現状 {len(hooks)} 本）: repo との同期が取れていなかった"
        )
        scripts = [_hook_script_args(hook) for hook in hooks]
        assert [CHECK_AGENT_INVOCATION_ARG] == scripts[0]
        assert [TIER_AUTOAPPLY_ARG] == scripts[1]

    def test_template_and_repo_agent_matcher_are_identical(self) -> None:
        """repo と _template の Agent matcher エントリが完全一致していた（3ファイル同期規律）。"""
        if not TEMPLATE_SETTINGS.exists():
            pytest.skip("テンプレート settings.json が不在のため skip した")

        repo_settings = _load_settings(REPO_SETTINGS)
        template_settings = _load_settings(TEMPLATE_SETTINGS)
        repo_hooks = _agent_matcher_hooks(repo_settings)
        template_hooks = _agent_matcher_hooks(template_settings)
        assert repo_hooks == template_hooks, (
            "repo と _template の PreToolUse Agent matcher 登録が一致していなかった\n"
            f"repo: {repo_hooks}\n"
            f"template: {template_hooks}"
        )
