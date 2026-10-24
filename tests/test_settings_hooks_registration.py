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

## 追記分（task test-onfailure）— 安全境界 hook の onFailure: "block"

plan-report-20261024-230734.md の test-onfailure /
architecture-report-20261024-230403.md（revision 4）ADR-1・ADR-3 に基づく。
測る性質は ADR-3 の性質 1〜5:

    11. 閉じた分類: PreToolUse に登録された全 hook が、本ファイルの分類表
        `_ONFAILURE_CLASSIFICATION` で「block 対象」か「非対象（理由つき）」に分類される。
        キーは (matcher, スクリプト名)。表に無い hook・キーを取り出せない形の hook
        （command が "c3" で args を持つ形でないもの）は未分類として失敗する
    12. block 対象 7 エントリは onFailure == "block"
    13. 非対象と、PreToolUse 以外の全イベントの全 hook は onFailure キーを持たない
    14. block 対象 7 エントリの type / command / args の形状（既存検査に委任しない）・
        キー集合が {type, command, args, onFailure} に一致・PreToolUse の各 matcher
        配列の hook の並び順が変更前と同じ
    15. 検知力: 判定は settings の dict を受け取る純関数で行い、手組みの dict で
        付け忘れ・誤付与・未分類・キーを取り出せない形の各ケースで失敗し、
        正しい dict でだけ成功することを示す（plan-design-guidelines ルール 19）

**分類表は本ファイルが SSOT。PreToolUse に新しい hook を追加したら、
`_ONFAILURE_CLASSIFICATION` への分類の追加（block 対象か、非対象なら理由）と
`_EXPECTED_PRETOOLUSE_ORDER` の並び順の更新が必要（追加しないと 11 / 14 が赤になる）。**

経緯: 実装前の Red 時点では `.claude/settings.json` に onFailure が 1 件も無く、
12（block 対象の onFailure）と 14 のキー集合の検査が AssertionError で失敗していた。
11・13・14 の形状と並び順・15 は実装前から緑だった（回帰ガード）。
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


# ---------------------------------------------------------------------------
# 5. 安全境界 hook の onFailure: "block"（task test-onfailure 追記分）
# ---------------------------------------------------------------------------

_ONFAILURE_BLOCK = "block"
_ONFAILURE_NON_TARGET = "non_target"

# 閉じた分類表（SSOT・ADR-1）。キー = (PreToolUse の matcher, スクリプト名)。
# 値 = (分類, 理由)。PreToolUse に hook を追加したら必ずここへ分類を追加する。
_ONFAILURE_CLASSIFICATION: dict[tuple[str, str], tuple[str, str]] = {
    ("Bash", "pre_tool.py"): (_ONFAILURE_BLOCK, "破壊的コマンド・secret の境界"),
    ("Write", "worktree_guard.py"): (_ONFAILURE_BLOCK, "worktree 外書き込みの境界"),
    ("Write", "patterns_guard.py"): (_ONFAILURE_BLOCK, "patterns.json 保護の境界"),
    ("Write", "report_path_guard.py"): (_ONFAILURE_BLOCK, "レポート書き先の封じ込め境界"),
    ("Edit", "worktree_guard.py"): (_ONFAILURE_BLOCK, "worktree 外書き込みの境界"),
    ("Edit", "patterns_guard.py"): (_ONFAILURE_BLOCK, "patterns.json 保護の境界"),
    ("Agent", "check_agent_invocation.py"): (
        _ONFAILURE_BLOCK,
        "reviewer の worktree 禁止（R5）の境界",
    ),
    ("Agent", "tier_autoapply.py"): (
        _ONFAILURE_NON_TARGET,
        "利便用・全例外を exit 0 で握る設計（失敗を block に倒す理由が無い）",
    ),
}

# PreToolUse の各 matcher 配列の hook の並び順（変更前のスナップショット・ADR-3 性質 4）。
_EXPECTED_PRETOOLUSE_ORDER: dict[str, list[str]] = {
    "Bash": ["pre_tool.py"],
    "Write": ["worktree_guard.py", "patterns_guard.py", "report_path_guard.py"],
    "Edit": ["worktree_guard.py", "patterns_guard.py"],
    "Agent": ["check_agent_invocation.py", "tier_autoapply.py"],
}

_BLOCK_ENTRY_KEYS = frozenset({"type", "command", "args", "onFailure"})

_UNEXTRACTABLE = "<キーを取り出せない形>"


def _block_keys() -> list[tuple[str, str]]:
    return [k for k, (cls, _) in _ONFAILURE_CLASSIFICATION.items() if cls == _ONFAILURE_BLOCK]


def _script_name(hook: object) -> str | None:
    """`command == "c3"` かつ `args == ["run", "<path>", ...]` の形なら basename を返す。

    それ以外（shell 形式・type: http・dict でない等）は None（例外を出さない）。
    """
    if not isinstance(hook, dict):
        return None
    if hook.get("command") != "c3":
        return None
    args = hook.get("args")
    if not isinstance(args, list) or len(args) < 2:
        return None
    if args[0] != "run" or not isinstance(args[1], str) or not args[1]:
        return None
    return args[1].replace("\\", "/").rsplit("/", 1)[-1]


def _iter_event_hooks(settings: dict, event: str):
    """(matcher, hook) を登録順に列挙する。壊れた形の hook も飛ばさず呼び出し側で判定させる。"""
    hooks_root = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks_root, dict):
        return
    entries = hooks_root.get(event)
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict):
            yield None, entry
            continue
        matcher = entry.get("matcher")
        hooks = entry.get("hooks")
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            yield matcher, hook


def _classification_violations(settings: dict) -> list[str]:
    """性質 1: PreToolUse の全 hook が閉じた分類表で分類されていること。"""
    violations: list[str] = []
    for matcher, hook in _iter_event_hooks(settings, "PreToolUse"):
        name = _script_name(hook)
        if name is None:
            violations.append(
                f"PreToolUse/{matcher!r}: 未分類（{_UNEXTRACTABLE}）: {hook!r}"
            )
            continue
        if (matcher, name) not in _ONFAILURE_CLASSIFICATION:
            violations.append(
                f"PreToolUse/{matcher!r}: 未分類の hook {name!r}"
                "（_ONFAILURE_CLASSIFICATION へ分類を追加すること）"
            )
    return violations


def _block_entries(settings: dict) -> list[tuple[tuple[str, str], dict]]:
    """PreToolUse から block 対象キーに一致する hook を登録順に取り出す。"""
    out: list[tuple[tuple[str, str], dict]] = []
    block_keys = set(_block_keys())
    for matcher, hook in _iter_event_hooks(settings, "PreToolUse"):
        name = _script_name(hook)
        if name is not None and (matcher, name) in block_keys:
            out.append(((matcher, name), hook))
    return out


def _block_violations(settings: dict) -> list[str]:
    """性質 2: block 対象 7 エントリが各 1 件存在し onFailure == "block"。"""
    found: dict[tuple[str, str], list[dict]] = {k: [] for k in _block_keys()}
    for key, hook in _block_entries(settings):
        found[key].append(hook)
    violations: list[str] = []
    for key, hooks in found.items():
        if len(hooks) != 1:
            violations.append(f"block 対象 {key} の登録が 1 件でない: {len(hooks)} 件")
            continue
        value = hooks[0].get("onFailure", "<キー無し>")
        if value != _ONFAILURE_BLOCK:
            violations.append(f"block 対象 {key} の onFailure が 'block' でない: {value!r}")
    return violations


def _no_onfailure_violations(settings: dict) -> list[str]:
    """性質 3: 非対象と、PreToolUse 以外の全イベントの全 hook は onFailure キーを持たない。"""
    violations: list[str] = []
    hooks_root = settings.get("hooks") if isinstance(settings, dict) else None
    events = list(hooks_root.keys()) if isinstance(hooks_root, dict) else []
    block_keys = set(_block_keys())
    for event in events:
        for matcher, hook in _iter_event_hooks(settings, event):
            if not isinstance(hook, dict) or "onFailure" not in hook:
                continue
            if event == "PreToolUse" and (matcher, _script_name(hook)) in block_keys:
                continue
            violations.append(
                f"{event}/{matcher!r}: 非対象の hook に onFailure がある: {hook!r}"
            )
    return violations


def _shape_violations(settings: dict) -> list[str]:
    """性質 4（形状）: block 対象の type / command / args を明示的に検査する。"""
    violations: list[str] = []
    for key, hook in _block_entries(settings):
        expected_args = ["run", _HOOK_DIR_PREFIX + key[1]]
        if hook.get("type") != "command":
            violations.append(f"{key}: type が 'command' でない: {hook.get('type')!r}")
        if hook.get("command") != "c3":
            violations.append(f"{key}: command が 'c3' でない: {hook.get('command')!r}")
        if hook.get("args") != expected_args:
            violations.append(f"{key}: args が {expected_args!r} でない: {hook.get('args')!r}")
    return violations


def _keyset_violations(settings: dict) -> list[str]:
    """性質 4（キー集合）: block 対象のキー集合が {type, command, args, onFailure}。"""
    violations: list[str] = []
    for key, hook in _block_entries(settings):
        keys = set(hook.keys())
        if keys != _BLOCK_ENTRY_KEYS:
            violations.append(
                f"{key}: キー集合が一致しない（不足 {sorted(_BLOCK_ENTRY_KEYS - keys)}・"
                f"過剰 {sorted(keys - _BLOCK_ENTRY_KEYS)}）"
            )
    return violations


def _order_violations(settings: dict) -> list[str]:
    """性質 4（並び順）: PreToolUse の各 matcher 配列の並びが変更前と同じ。"""
    actual: dict[object, list[str]] = {}
    for matcher, hook in _iter_event_hooks(settings, "PreToolUse"):
        actual.setdefault(matcher, []).append(_script_name(hook) or _UNEXTRACTABLE)
    if actual != _EXPECTED_PRETOOLUSE_ORDER:
        return [f"PreToolUse の並びが変更前と異なる: {actual!r}"]
    return []


_ALL_ONFAILURE_CHECKS = (
    _classification_violations,
    _block_violations,
    _no_onfailure_violations,
    _shape_violations,
    _keyset_violations,
    _order_violations,
)


def _onfailure_violations(settings: dict) -> list[str]:
    return [v for check in _ALL_ONFAILURE_CHECKS for v in check(settings)]


class TestOnFailureBlockOnRealSettings:
    """実 `.claude/settings.json` に対する性質 1〜4。

    test_block_targets_have_onfailure_block / test_block_targets_keyset_matches は
    block 対象の onFailure が "block" であること・キー集合が一致することを測る
    （実装前の Red 時点では onFailure が 1 件も無く AssertionError で失敗していた）。
    それ以外は実装前から緑の回帰ガード。
    """

    def test_all_pretooluse_hooks_are_classified(self) -> None:
        v = _classification_violations(_load_settings())
        assert v == [], "\n".join(v)

    def test_block_targets_have_onfailure_block(self) -> None:
        v = _block_violations(_load_settings())
        assert v == [], "\n".join(v)

    def test_non_targets_and_other_events_have_no_onfailure(self) -> None:
        v = _no_onfailure_violations(_load_settings())
        assert v == [], "\n".join(v)

    def test_block_targets_shape(self) -> None:
        settings = _load_settings()
        assert len(_block_entries(settings)) == 7
        v = _shape_violations(settings)
        assert v == [], "\n".join(v)

    def test_block_targets_keyset_matches(self) -> None:
        settings = _load_settings()
        assert len(_block_entries(settings)) == 7
        v = _keyset_violations(settings)
        assert v == [], "\n".join(v)

    def test_pretooluse_order_unchanged(self) -> None:
        v = _order_violations(_load_settings())
        assert v == [], "\n".join(v)


# --- 性質 5: 検知力（手組みの dict） -------------------------------------------


def _hook(name: str, *, block: bool) -> dict:
    h: dict = {"type": "command", "command": "c3", "args": ["run", _HOOK_DIR_PREFIX + name]}
    if block:
        h["onFailure"] = "block"
    return h


def _good_settings() -> dict:
    """正しい（変更後の）settings を手で組む。実ファイルは読まない。"""
    pre = []
    for matcher, names in _EXPECTED_PRETOOLUSE_ORDER.items():
        pre.append(
            {
                "matcher": matcher,
                "hooks": [
                    _hook(n, block=_ONFAILURE_CLASSIFICATION[(matcher, n)][0] == _ONFAILURE_BLOCK)
                    for n in names
                ],
            }
        )
    return {
        "hooks": {
            "PreToolUse": pre,
            "PostToolUse": [
                {"matcher": "Write", "hooks": [_hook("post_tool.py", block=False)]},
            ],
            "Stop": [
                {"matcher": "", "hooks": [_hook("session_stop.py", block=False)]},
            ],
        }
    }


def _pre_hooks(settings: dict, matcher: str) -> list:
    for entry in settings["hooks"]["PreToolUse"]:
        if entry["matcher"] == matcher:
            return entry["hooks"]
    raise KeyError(matcher)


def _mut_forget_block(s: dict) -> None:
    del _pre_hooks(s, "Agent")[0]["onFailure"]


def _mut_wrong_value(s: dict) -> None:
    _pre_hooks(s, "Bash")[0]["onFailure"] = "continue"


def _mut_misapply_non_target(s: dict) -> None:
    _pre_hooks(s, "Agent")[1]["onFailure"] = "block"


def _mut_misapply_other_event(s: dict) -> None:
    s["hooks"]["Stop"][0]["hooks"][0]["onFailure"] = "block"


def _mut_unclassified_new_script(s: dict) -> None:
    _pre_hooks(s, "Bash").append(_hook("new_guard.py", block=False))


def _mut_unclassified_existing_script_other_matcher(s: dict) -> None:
    _pre_hooks(s, "Edit").append(_hook("report_path_guard.py", block=True))


def _mut_unextractable_shell_form(s: dict) -> None:
    _pre_hooks(s, "Bash").append(
        {"type": "command", "command": 'c3 run "${CLAUDE_PROJECT_DIR}/.claude/hooks/x.py"'}
    )


def _mut_unextractable_http(s: dict) -> None:
    _pre_hooks(s, "Write").append({"type": "http", "url": "http://localhost/hook"})


def _mut_unextractable_non_dict(s: dict) -> None:
    _pre_hooks(s, "Write").append("c3 run x.py")


def _mut_bad_type(s: dict) -> None:
    _pre_hooks(s, "Write")[0]["type"] = "prompt"


def _mut_bad_args_extra(s: dict) -> None:
    _pre_hooks(s, "Edit")[1]["args"] = ["run", _HOOK_DIR_PREFIX + "patterns_guard.py", "--x"]


def _mut_extra_key(s: dict) -> None:
    _pre_hooks(s, "Write")[2]["timeout"] = 5


def _mut_order_swapped(s: dict) -> None:
    hooks = _pre_hooks(s, "Write")
    hooks[0], hooks[1] = hooks[1], hooks[0]


def _mut_duplicate_block_entry(s: dict) -> None:
    _pre_hooks(s, "Bash").append(_hook("pre_tool.py", block=True))


# (ラベル, 変異, 検出すべき純関数)
_DETECTION_CASES = [
    ("付け忘れ", _mut_forget_block, _block_violations),
    ("block 以外の値", _mut_wrong_value, _block_violations),
    ("誤付与（PreToolUse 非対象）", _mut_misapply_non_target, _no_onfailure_violations),
    ("誤付与（PreToolUse 以外）", _mut_misapply_other_event, _no_onfailure_violations),
    ("未分類（新しいスクリプト）", _mut_unclassified_new_script, _classification_violations),
    (
        "未分類（既存 block スクリプトを別 matcher へ）",
        _mut_unclassified_existing_script_other_matcher,
        _classification_violations,
    ),
    ("キーを取り出せない形（shell 形式）", _mut_unextractable_shell_form, _classification_violations),
    ("キーを取り出せない形（type: http）", _mut_unextractable_http, _classification_violations),
    ("キーを取り出せない形（dict でない）", _mut_unextractable_non_dict, _classification_violations),
    ("形状（type）", _mut_bad_type, _shape_violations),
    ("形状（args）", _mut_bad_args_extra, _shape_violations),
    ("キー集合（過剰キー）", _mut_extra_key, _keyset_violations),
    ("並び順", _mut_order_swapped, _order_violations),
    ("block 対象の重複登録", _mut_duplicate_block_entry, _block_violations),
]


class TestOnFailureCheckDetectionPower:
    """性質 5: 判定純関数の検知力（plan-design-guidelines ルール 19）。

    parametrize ではなく 1 テストで表を回す（定数スタブで行単位に空回りさせないため）。
    """

    def test_good_settings_passes_all_checks(self) -> None:
        v = _onfailure_violations(_good_settings())
        assert v == [], "\n".join(v)

    def test_each_defect_is_detected_by_its_check(self) -> None:
        undetected: list[str] = []
        for label, mutate, check in _DETECTION_CASES:
            s = _good_settings()
            mutate(s)
            if not check(s):
                undetected.append(f"{label}: {check.__name__} が検出しない")
            if not _onfailure_violations(s):
                undetected.append(f"{label}: 全体判定が検出しない")
        assert undetected == [], "\n".join(undetected)

    def test_unextractable_hooks_do_not_raise(self) -> None:
        """キーを取り出せない形は例外ではなく違反として返る（全チェックで）。"""
        for mutate in (
            _mut_unextractable_shell_form,
            _mut_unextractable_http,
            _mut_unextractable_non_dict,
        ):
            s = _good_settings()
            mutate(s)
            for check in _ALL_ONFAILURE_CHECKS:
                check(s)  # 例外が出れば失敗

    def test_good_settings_has_exactly_seven_block_entries(self) -> None:
        """正しい dict 自体が ADR-1 の 7 エントリを持つ（正の対照の妥当性）。"""
        s = _good_settings()
        assert len(_block_keys()) == 7
        assert len(_block_entries(s)) == 7
        assert all(h.get("onFailure") == "block" for _, h in _block_entries(s))
