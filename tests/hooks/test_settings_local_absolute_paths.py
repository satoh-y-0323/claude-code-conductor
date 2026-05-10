"""Tests for .claude/settings.local.json: hook command の絶対パス徹底。

過去 defect:
  「`settings.local.json` の古い相対パス hooks による `settings.json` 上書き」
  （`.claude/memory/llm_summary.md` 記録）。

  Claude Code は settings.local.json と settings.json をマージするが、相対パスの
  hooks コマンドがあると不整合を起こすケースがある。本リポジトリの開発者向け
  hooks（SubagentStart / SubagentStop / PreToolUse / PostToolUse）は全て
  `$CLAUDE_PROJECT_DIR` または OS 絶対パスで指定する規約とする。

`.claude/settings.local.json` 自体は `_excludes.py` / `hatch_build.py` で配布
除外されているが、本テストはリポジトリ内の運用規約として常に走らせる。

利用者環境（c3 init 展開先）には settings.local.json が無い場合があるので、
ファイル不在は skip する。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).parents[2]
SETTINGS_LOCAL = WORKTREE_ROOT / ".claude" / "settings.local.json"

pytestmark = pytest.mark.skipif(
    not SETTINGS_LOCAL.is_file(),
    reason=".claude/settings.local.json is distributor-side only",
)


def _iter_hook_commands() -> list[str]:
    cfg = json.loads(SETTINGS_LOCAL.read_text(encoding="utf-8"))
    commands: list[str] = []
    for event_hooks in cfg.get("hooks", {}).values():
        if not isinstance(event_hooks, list):
            continue
        for matcher in event_hooks:
            for h in matcher.get("hooks", []):
                cmd = h.get("command", "")
                if isinstance(cmd, str) and cmd:
                    commands.append(cmd)
    return commands


def _extract_script_path(cmd: str) -> str | None:
    """コマンド文字列からスクリプトパス（最初の .py ファイル）を抽出する。"""
    # クォート付き: python "<PATH>"
    m = re.search(r'"([^"]+\.py)"', cmd)
    if m:
        return m.group(1)
    # クォートなし: python <PATH>.py args
    m = re.search(r'\b(\S+\.py)\b', cmd)
    return m.group(1) if m else None


class TestAbsolutePathPolicy:
    """全 hook command が絶対パスまたは $CLAUDE_PROJECT_DIR 起点であること。"""

    def test_settings_local_json_is_valid(self) -> None:
        cfg = json.loads(SETTINGS_LOCAL.read_text(encoding="utf-8"))
        assert isinstance(cfg, dict)

    def test_at_least_one_hook_registered(self) -> None:
        """テストの sanity check: hooks セクションに何か登録されていること。"""
        commands = _iter_hook_commands()
        assert len(commands) > 0, "settings.local.json に hook が 1 つも登録されていない"

    def test_all_hook_commands_use_absolute_paths(self) -> None:
        """全 hook command のスクリプトパスが $CLAUDE_PROJECT_DIR か OS 絶対パスで始まる。"""
        commands = _iter_hook_commands()
        violations: list[str] = []
        for cmd in commands:
            script_path = _extract_script_path(cmd)
            if script_path is None:
                violations.append(f"スクリプトパス抽出失敗: {cmd!r}")
                continue
            if script_path.startswith("$CLAUDE_PROJECT_DIR"):
                continue
            if os.path.isabs(script_path):
                continue
            violations.append(
                f"相対パス検出（settings.json 上書き risk）: command={cmd!r}, script={script_path!r}"
            )
        assert not violations, "\n".join(violations)

    def test_no_command_starts_with_dot_slash(self) -> None:
        """スクリプトパスが ./ や .claude/ 直書きでないこと（最も典型的な相対パス defect）。"""
        commands = _iter_hook_commands()
        for cmd in commands:
            script_path = _extract_script_path(cmd) or ""
            assert not script_path.startswith("./"), \
                f"./ で始まる相対パス検出: {cmd!r}"
            assert not script_path.startswith(".claude/"), \
                f".claude/ 直書きの相対パス検出（$CLAUDE_PROJECT_DIR を使うこと）: {cmd!r}"
