"""tests/test_permission_handler_removal.py

permission_handler 一式（PermissionRequest hook 本体 / Windows トーストワーカー /
auto_allow ルール定義）の削除完了を凍結する検証テスト。

測る性質（architecture-report-20260814-123008.md §0-A / 改訂 2 §A-7 /
改訂 3 §3 / 改訂 4 §2 に対応）:

1. 削除対象 3 ファイルがリポジトリに存在しないこと（A-1 / A-2 / A-3）
2. `.claude/settings.json` に `PermissionRequest` キーが無く、
   permissions.allow に permission_handler を含む行が無いこと（A-4 / A-5）
3. `pyproject.toml` に `[project.optional-dependencies]` セクションと
   windows-toasts が無いこと（A-7）
4. `.claude/deletions.txt` に削除 3 行が記載されていること（B-12）
5. `.claude/breaking-changes.txt` の `v` で始まる非コメント行のいずれかが
   `permission_handler` を含むこと（B-16・存在断言・順序非依存・版番号非依存）

本ファイルは削除スライスの実施前は**赤であることが正常**（Red フェーズ）。
削除完了後に緑へ反転することで削除が凍結される。

なお本ファイルは不在検証の性質上、対象名 `permission_handler` /
`permission_rules` を必然的に含む（architecture 改訂 3 §0-B 補記 2 の
「§6 ヒットしてよい対象」(a) に該当）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# 削除対象ファイル（リポジトリルート相対・POSIX 表記）
REMOVED_FILES = (
    ".claude/hooks/permission_handler.py",
    ".claude/hooks/permission_handler_toast.py",
    ".claude/permission_rules.json",
)

# deletions.txt に記載されるべきパス（.claude/ 相対表記）
DELETION_ENTRIES = (
    "hooks/permission_handler.py",
    "hooks/permission_handler_toast.py",
    "permission_rules.json",
)


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


# ===========================================================================
# 1. 削除対象ファイルの不在（A-1 / A-2 / A-3）
# ===========================================================================
class TestRemovedFilesAreGone:
    @pytest.mark.parametrize("relative", REMOVED_FILES)
    def test_file_does_not_exist(self, relative: str) -> None:
        """削除対象ファイルがリポジトリに存在しないこと."""
        target = REPO_ROOT / relative
        assert not target.exists(), f"deleted file still exists: {relative}"


# ===========================================================================
# 2. settings.json の登録消滅（A-4 / A-5）
# ===========================================================================
class TestSettingsJsonHasNoPermissionRequest:
    @pytest.fixture()
    def settings(self) -> dict:
        """`.claude/settings.json` を JSON としてパースして返す（valid JSON 前提）."""
        return json.loads(_read(".claude/settings.json"))

    def test_hooks_section_has_no_permission_request_key(self, settings: dict) -> None:
        """hooks 節に `PermissionRequest` キーが存在しないこと（キーごと削除）."""
        hooks = settings.get("hooks", {})
        assert isinstance(hooks, dict), "settings.json の hooks 節が dict ではない"
        assert "PermissionRequest" not in hooks, (
            "settings.json の hooks 節に PermissionRequest キーが残っている"
        )

    def test_permissions_allow_has_no_permission_handler_entry(self, settings: dict) -> None:
        """permissions.allow に permission_handler を含むエントリが無いこと."""
        allow = settings.get("permissions", {}).get("allow", [])
        assert isinstance(allow, list), "settings.json の permissions.allow が list ではない"
        assert len(allow) > 0, "positive control: permissions.allow が空（読み取り経路が壊れている）"

        leftovers = [entry for entry in allow if "permission_handler" in str(entry)]
        assert leftovers == [], f"permissions.allow に残存エントリがある: {leftovers}"


# ===========================================================================
# 3. pyproject.toml の extra 削除（A-7）
# ===========================================================================
class TestPyprojectHasNoNotifyExtra:
    def test_optional_dependencies_section_is_absent(self) -> None:
        """`[project.optional-dependencies]` セクションが存在しないこと（空テーブルも残さない）."""
        lines = [line.strip() for line in _read("pyproject.toml").splitlines()]
        assert "[project]" in lines, "positive control: pyproject.toml の読み取り経路が壊れている"
        assert "[project.optional-dependencies]" not in lines, (
            "pyproject.toml に [project.optional-dependencies] セクションが残っている"
        )

    def test_windows_toasts_is_absent(self) -> None:
        """windows-toasts への依存記述が残っていないこと."""
        text = _read("pyproject.toml")
        assert "windows-toasts" not in text, (
            "pyproject.toml に windows-toasts への依存が残っている"
        )


# ===========================================================================
# 4. deletions.txt への追記（B-12）
# ===========================================================================
class TestDeletionsTxtListsRemovedFiles:
    @pytest.fixture()
    def entries(self) -> list[str]:
        """`.claude/deletions.txt` の実エントリ行（コメント・空行を除く）."""
        out = []
        for raw in _read(".claude/deletions.txt").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line)
        return out

    @pytest.mark.parametrize("entry", DELETION_ENTRIES)
    def test_entry_is_listed(self, entries: list[str], entry: str) -> None:
        """削除 3 行が deletions.txt に記載されていること."""
        assert len(entries) > 0, "positive control: deletions.txt に実エントリが 1 行も無い"
        assert entry in entries, f"deletions.txt に未記載: {entry}"


# ===========================================================================
# 5. breaking-changes.txt への追記（B-16・版番号非依存）
# ===========================================================================
class TestBreakingChangesTxtAnnouncesRemoval:
    def test_some_version_line_mentions_permission_handler(self) -> None:
        """`v` で始まる非コメント行のいずれかが `permission_handler` を含むこと.

        存在断言のみ。順序（末尾かどうか）にも版番号リテラルにも依存させない。
        """
        version_lines = [
            line
            for line in (raw.strip() for raw in _read(".claude/breaking-changes.txt").splitlines())
            if line.startswith("v")
        ]
        assert len(version_lines) > 0, (
            "positive control: breaking-changes.txt に v で始まる行が 1 行も無い"
        )

        hits = [line for line in version_lines if "permission_handler" in line]
        assert len(hits) >= 1, (
            "breaking-changes.txt に permission_handler の削除を告知する行が無い"
        )
