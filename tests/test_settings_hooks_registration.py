"""Tests for .claude/settings.json registration of P1/P2/P3 hooks (未実装 — Red フェーズ)

plan-report-20260725-180252.md T1(4) / architecture-report-20260725-175915.md §2 D-4 の
仕様に基づく。

## Red の理由

`.claude/settings.json` にはまだ patterns_guard.py / report_contract_check.py /
session_mode_watch.py の登録が無い（T2 developer 実装前）。本ファイルのテストは
JSON の内容検査のみで完結する（hook スクリプト自体の実在は問わない）ため、
`.claude/settings.json` 自体は実在するファイルを対象にした通常のアサーション失敗
（AssertionError）として Red になる。構文エラー・タイポによる失敗ではない。

## 検証対象

リポジトリ実体の `.claude/settings.json`（読み取りのみ）。plan T1(4) の明記どおり
`src/c3/_template/` 側は対象外（ビルド委譲）とし、本ファイルでは比較しない。

## ケース仕様（plan T1(4) 準拠）

    1. PreToolUse "Write"・"Edit" に patterns_guard.py が登録されている
    2. PostToolUse "Write" に report_contract_check.py が登録されている
    3. PostToolUse "Edit" に session_mode_watch.py が登録されている
    4. 相互の取り違え・過剰登録が無いこと
       （patterns_guard は PostToolUse に出現しない・report_contract_check は
       PreToolUse/PostToolUse Edit に出現しない・session_mode_watch は
       PreToolUse/PostToolUse Write に出現しない）
    5. 形式は `command: "c3"` + `args: ["run", "${CLAUDE_PROJECT_DIR}/.claude/hooks/<name>.py"]`
    6. 既存登録の不変確認（PreToolUse Bash/Write/Edit/Agent・PostToolUse Write/Edit の
       既存 hook 列挙）・各 matcher 配列の末尾に追記されていること
    7. json.load 可能（壊れていない）

## 追記分（task test-reg・Red）— P4: report_path_guard.py

新規 PreToolUse hook `.claude/hooks/report_path_guard.py`（未実装）の settings 登録。
検査方式は上記と同一（実 `.claude/settings.json` の JSON 内容検査のみ）。

    8. PreToolUse "Write" に report_path_guard.py が 1 件のみ登録されている
       （形式は `command: "c3"` + `args: ["run", "<hooks>/report_path_guard.py"]`）
       — 未登録の現状では AssertionError で Red
    9. 既存 hook（worktree_guard.py / patterns_guard.py）より後ろに追記されている
       — 未登録の現状では Red
    10. 負側（回帰ガード群・現状でも緑）: PreToolUse "Edit" および
        PostToolUse "Write" / "Edit" の各 matcher には report_path_guard.py が
        出現しない（P4 は PreToolUse Write 専用）
"""

from __future__ import annotations

import json
from pathlib import Path

WORKTREE_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = WORKTREE_ROOT / ".claude" / "settings.json"

_HOOK_DIR_PREFIX = "${CLAUDE_PROJECT_DIR}/.claude/hooks/"

PATTERNS_GUARD_ARG = _HOOK_DIR_PREFIX + "patterns_guard.py"
REPORT_CONTRACT_CHECK_ARG = _HOOK_DIR_PREFIX + "report_contract_check.py"
SESSION_MODE_WATCH_ARG = _HOOK_DIR_PREFIX + "session_mode_watch.py"
REPORT_PATH_GUARD_ARG = _HOOK_DIR_PREFIX + "report_path_guard.py"

PRE_TOOL_ARG = _HOOK_DIR_PREFIX + "pre_tool.py"
WORKTREE_GUARD_ARG = _HOOK_DIR_PREFIX + "worktree_guard.py"
CHECK_AGENT_INVOCATION_ARG = _HOOK_DIR_PREFIX + "check_agent_invocation.py"
TIER_AUTOAPPLY_ARG = _HOOK_DIR_PREFIX + "tier_autoapply.py"
POST_TOOL_ARG = _HOOK_DIR_PREFIX + "post_tool.py"
PLANNER_CHECK_ARG = _HOOK_DIR_PREFIX + "planner_check.py"


def _load_settings() -> dict:
    return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))


def _matcher_hooks(settings: dict, event: str, matcher: str) -> list[dict]:
    entries = settings.get("hooks", {}).get(event, [])
    for entry in entries:
        if entry.get("matcher") == matcher:
            return entry.get("hooks", [])
    return []


def _all_args(hooks: list[dict]) -> list[str]:
    return [arg for h in hooks for arg in h.get("args", [])]


def _index_containing_arg(hooks: list[dict], arg: str) -> int | None:
    for i, h in enumerate(hooks):
        if arg in h.get("args", []):
            return i
    return None


# ---------------------------------------------------------------------------
# 0. settings.json が実在し json.load 可能
# ---------------------------------------------------------------------------


class TestSettingsJsonLoadable:
    def test_settings_file_exists(self) -> None:
        assert SETTINGS_PATH.is_file(), f"{SETTINGS_PATH} が見つからない"

    def test_settings_json_is_valid_json(self) -> None:
        settings = _load_settings()
        assert isinstance(settings, dict)


# ---------------------------------------------------------------------------
# 1. patterns_guard.py: PreToolUse Write / Edit
# ---------------------------------------------------------------------------


class TestPatternsGuardRegistration:
    def test_registered_in_pretooluse_write(self) -> None:
        settings = _load_settings()
        hooks = _matcher_hooks(settings, "PreToolUse", "Write")
        assert PATTERNS_GUARD_ARG in _all_args(hooks), (
            "PreToolUse/Write に patterns_guard.py が登録されていない"
        )

    def test_registered_in_pretooluse_edit(self) -> None:
        settings = _load_settings()
        hooks = _matcher_hooks(settings, "PreToolUse", "Edit")
        assert PATTERNS_GUARD_ARG in _all_args(hooks), (
            "PreToolUse/Edit に patterns_guard.py が登録されていない"
        )

    def test_entry_shape_matches_convention(self) -> None:
        settings = _load_settings()
        hooks = _matcher_hooks(settings, "PreToolUse", "Write")
        matches = [h for h in hooks if PATTERNS_GUARD_ARG in h.get("args", [])]
        assert len(matches) == 1, (
            f"patterns_guard.py の登録エントリが1件でない: {matches}"
        )
        entry = matches[0]
        assert entry.get("type") == "command"
        assert entry.get("command") == "c3"
        assert entry.get("args") == ["run", PATTERNS_GUARD_ARG]

    def test_appended_after_worktree_guard_in_write_matcher(self) -> None:
        """D-4: 「worktree_guard の次に追記」= 既存より後ろに位置する。"""
        settings = _load_settings()
        hooks = _matcher_hooks(settings, "PreToolUse", "Write")
        idx_worktree = _index_containing_arg(hooks, WORKTREE_GUARD_ARG)
        idx_patterns = _index_containing_arg(hooks, PATTERNS_GUARD_ARG)
        assert idx_worktree is not None, "worktree_guard.py の既存登録が見当たらない"
        assert idx_patterns is not None, "patterns_guard.py が未登録"
        assert idx_patterns > idx_worktree, (
            "patterns_guard.py が worktree_guard.py より前に追記されている"
            "（末尾追記の規約違反）"
        )

    def test_not_registered_in_posttooluse(self) -> None:
        """P1 は PreToolUse 専用。PostToolUse への過剰登録・取り違えが無いこと。"""
        settings = _load_settings()
        for matcher in ("Write", "Edit"):
            hooks = _matcher_hooks(settings, "PostToolUse", matcher)
            assert PATTERNS_GUARD_ARG not in _all_args(hooks), (
                f"patterns_guard.py が PostToolUse/{matcher} に誤登録されている"
            )


# ---------------------------------------------------------------------------
# 2. report_contract_check.py: PostToolUse Write のみ
# ---------------------------------------------------------------------------


class TestReportContractCheckRegistration:
    def test_registered_in_posttooluse_write(self) -> None:
        settings = _load_settings()
        hooks = _matcher_hooks(settings, "PostToolUse", "Write")
        assert REPORT_CONTRACT_CHECK_ARG in _all_args(hooks), (
            "PostToolUse/Write に report_contract_check.py が登録されていない"
        )

    def test_entry_shape_matches_convention(self) -> None:
        settings = _load_settings()
        hooks = _matcher_hooks(settings, "PostToolUse", "Write")
        matches = [h for h in hooks if REPORT_CONTRACT_CHECK_ARG in h.get("args", [])]
        assert len(matches) == 1
        entry = matches[0]
        assert entry.get("type") == "command"
        assert entry.get("command") == "c3"
        assert entry.get("args") == ["run", REPORT_CONTRACT_CHECK_ARG]

    def test_appended_after_existing_posttooluse_write_hooks(self) -> None:
        settings = _load_settings()
        hooks = _matcher_hooks(settings, "PostToolUse", "Write")
        idx_post_tool = _index_containing_arg(hooks, POST_TOOL_ARG)
        idx_planner_check = _index_containing_arg(hooks, PLANNER_CHECK_ARG)
        idx_report_contract = _index_containing_arg(hooks, REPORT_CONTRACT_CHECK_ARG)
        assert idx_post_tool is not None
        assert idx_planner_check is not None
        assert idx_report_contract is not None, "report_contract_check.py が未登録"
        assert idx_report_contract > idx_post_tool
        assert idx_report_contract > idx_planner_check

    def test_not_registered_in_pretooluse(self) -> None:
        settings = _load_settings()
        for matcher in ("Write", "Edit"):
            hooks = _matcher_hooks(settings, "PreToolUse", matcher)
            assert REPORT_CONTRACT_CHECK_ARG not in _all_args(hooks), (
                f"report_contract_check.py が PreToolUse/{matcher} に誤登録されている"
            )

    def test_not_registered_in_posttooluse_edit(self) -> None:
        """D-4: report_contract_check.py は Write 専用（新規命名は Write のみで発生）。"""
        settings = _load_settings()
        hooks = _matcher_hooks(settings, "PostToolUse", "Edit")
        assert REPORT_CONTRACT_CHECK_ARG not in _all_args(hooks), (
            "report_contract_check.py が PostToolUse/Edit に誤登録されている"
        )


# ---------------------------------------------------------------------------
# 3. session_mode_watch.py: PostToolUse Edit のみ
# ---------------------------------------------------------------------------


class TestSessionModeWatchRegistration:
    def test_registered_in_posttooluse_edit(self) -> None:
        settings = _load_settings()
        hooks = _matcher_hooks(settings, "PostToolUse", "Edit")
        assert SESSION_MODE_WATCH_ARG in _all_args(hooks), (
            "PostToolUse/Edit に session_mode_watch.py が登録されていない"
        )

    def test_entry_shape_matches_convention(self) -> None:
        settings = _load_settings()
        hooks = _matcher_hooks(settings, "PostToolUse", "Edit")
        matches = [h for h in hooks if SESSION_MODE_WATCH_ARG in h.get("args", [])]
        assert len(matches) == 1
        entry = matches[0]
        assert entry.get("type") == "command"
        assert entry.get("command") == "c3"
        assert entry.get("args") == ["run", SESSION_MODE_WATCH_ARG]

    def test_appended_after_existing_posttooluse_edit_hooks(self) -> None:
        settings = _load_settings()
        hooks = _matcher_hooks(settings, "PostToolUse", "Edit")
        idx_post_tool = _index_containing_arg(hooks, POST_TOOL_ARG)
        idx_planner_check = _index_containing_arg(hooks, PLANNER_CHECK_ARG)
        idx_session_watch = _index_containing_arg(hooks, SESSION_MODE_WATCH_ARG)
        assert idx_post_tool is not None
        assert idx_planner_check is not None
        assert idx_session_watch is not None, "session_mode_watch.py が未登録"
        assert idx_session_watch > idx_post_tool
        assert idx_session_watch > idx_planner_check

    def test_not_registered_in_pretooluse(self) -> None:
        settings = _load_settings()
        for matcher in ("Write", "Edit"):
            hooks = _matcher_hooks(settings, "PreToolUse", matcher)
            assert SESSION_MODE_WATCH_ARG not in _all_args(hooks), (
                f"session_mode_watch.py が PreToolUse/{matcher} に誤登録されている"
            )

    def test_not_registered_in_posttooluse_write(self) -> None:
        """D-4 改訂: P3 は Write 対象外のため登録も Edit のみ。"""
        settings = _load_settings()
        hooks = _matcher_hooks(settings, "PostToolUse", "Write")
        assert SESSION_MODE_WATCH_ARG not in _all_args(hooks), (
            "session_mode_watch.py が PostToolUse/Write に誤登録されている"
        )


# ---------------------------------------------------------------------------
# 3-b. report_path_guard.py: PreToolUse Write のみ（task test-reg 追記分）
# ---------------------------------------------------------------------------


class TestReportPathGuardRegistration:
    """P4 (report_path_guard.py) の settings 登録。

    Red 群: test_registered_in_pretooluse_write /
            test_entry_shape_matches_convention /
            test_appended_after_existing_pretooluse_write_hooks
        → `.claude/settings.json` に未登録のため AssertionError で失敗する
          （構文エラー・タイポによる失敗ではない）。

    回帰ガード群: test_not_registered_in_pretooluse_edit /
                  test_not_registered_in_posttooluse
        → 現状でも緑。P4 追加時に Edit / PostToolUse へ取り違え登録されることを防ぐ。
    """

    def test_registered_in_pretooluse_write(self) -> None:
        settings = _load_settings()
        hooks = _matcher_hooks(settings, "PreToolUse", "Write")
        assert REPORT_PATH_GUARD_ARG in _all_args(hooks), (
            "PreToolUse/Write に report_path_guard.py が登録されていない"
        )

    def test_entry_shape_matches_convention(self) -> None:
        """1 件のみ・`c3 run <hook>` 形式であること（重複登録の禁止も兼ねる）。"""
        settings = _load_settings()
        hooks = _matcher_hooks(settings, "PreToolUse", "Write")
        matches = [h for h in hooks if REPORT_PATH_GUARD_ARG in h.get("args", [])]
        assert len(matches) == 1, (
            f"report_path_guard.py の登録エントリが1件でない: {matches}"
        )
        entry = matches[0]
        assert entry.get("type") == "command"
        assert entry.get("command") == "c3"
        assert entry.get("args") == ["run", REPORT_PATH_GUARD_ARG]

    def test_appended_after_existing_pretooluse_write_hooks(self) -> None:
        """既存 hook（worktree_guard / patterns_guard）より後ろに追記されている。"""
        settings = _load_settings()
        hooks = _matcher_hooks(settings, "PreToolUse", "Write")
        idx_worktree = _index_containing_arg(hooks, WORKTREE_GUARD_ARG)
        idx_patterns = _index_containing_arg(hooks, PATTERNS_GUARD_ARG)
        idx_report_path = _index_containing_arg(hooks, REPORT_PATH_GUARD_ARG)
        assert idx_worktree is not None, "worktree_guard.py の既存登録が見当たらない"
        assert idx_patterns is not None, "patterns_guard.py の既存登録が見当たらない"
        assert idx_report_path is not None, "report_path_guard.py が未登録"
        assert idx_report_path > idx_worktree, (
            "report_path_guard.py が worktree_guard.py より前に追記されている"
            "（末尾追記の規約違反）"
        )
        assert idx_report_path > idx_patterns, (
            "report_path_guard.py が patterns_guard.py より前に追記されている"
            "（末尾追記の規約違反）"
        )

    def test_not_registered_in_pretooluse_edit(self) -> None:
        """P4 は Write 専用。PreToolUse/Edit への取り違え登録が無いこと。"""
        settings = _load_settings()
        hooks = _matcher_hooks(settings, "PreToolUse", "Edit")
        assert REPORT_PATH_GUARD_ARG not in _all_args(hooks), (
            "report_path_guard.py が PreToolUse/Edit に誤登録されている"
        )

    def test_not_registered_in_posttooluse(self) -> None:
        """P4 は PreToolUse 専用。PostToolUse への過剰登録が無いこと。"""
        settings = _load_settings()
        for matcher in ("Write", "Edit"):
            hooks = _matcher_hooks(settings, "PostToolUse", matcher)
            assert REPORT_PATH_GUARD_ARG not in _all_args(hooks), (
                f"report_path_guard.py が PostToolUse/{matcher} に誤登録されている"
            )


# ---------------------------------------------------------------------------
# 4. 既存登録の不変確認
# ---------------------------------------------------------------------------


class TestExistingRegistrationsUnchanged:
    def test_pretooluse_bash_pre_tool_still_present(self) -> None:
        settings = _load_settings()
        hooks = _matcher_hooks(settings, "PreToolUse", "Bash")
        assert PRE_TOOL_ARG in _all_args(hooks)

    def test_pretooluse_write_worktree_guard_still_present(self) -> None:
        settings = _load_settings()
        hooks = _matcher_hooks(settings, "PreToolUse", "Write")
        assert WORKTREE_GUARD_ARG in _all_args(hooks)

    def test_pretooluse_edit_worktree_guard_still_present(self) -> None:
        settings = _load_settings()
        hooks = _matcher_hooks(settings, "PreToolUse", "Edit")
        assert WORKTREE_GUARD_ARG in _all_args(hooks)

    def test_pretooluse_agent_hooks_still_present(self) -> None:
        settings = _load_settings()
        hooks = _matcher_hooks(settings, "PreToolUse", "Agent")
        args = _all_args(hooks)
        assert CHECK_AGENT_INVOCATION_ARG in args
        assert TIER_AUTOAPPLY_ARG in args

    def test_posttooluse_write_existing_hooks_still_present(self) -> None:
        settings = _load_settings()
        hooks = _matcher_hooks(settings, "PostToolUse", "Write")
        args = _all_args(hooks)
        assert POST_TOOL_ARG in args
        assert PLANNER_CHECK_ARG in args

    def test_posttooluse_edit_existing_hooks_still_present(self) -> None:
        settings = _load_settings()
        hooks = _matcher_hooks(settings, "PostToolUse", "Edit")
        args = _all_args(hooks)
        assert POST_TOOL_ARG in args
        assert PLANNER_CHECK_ARG in args
