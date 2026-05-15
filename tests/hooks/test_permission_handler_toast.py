"""Tests for .claude/hooks/permission_handler_toast.py

主に append_to_auto_allow() の atomic write を検証する。
windows-toasts のインストール状況に依存する toast 表示部分は実機/モックの
両方をサポートし、import 不能時はテストを skip する。
"""

from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).parents[2]
TOAST_SCRIPT = WORKTREE_ROOT / ".claude" / "hooks" / "permission_handler_toast.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "permission_handler_toast", TOAST_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


class TestAppendToAutoAllow:
    """append_to_auto_allow() の atomic write 検証."""

    def test_creates_new_rules_file_if_missing(self, tmp_path: Path):
        module = _load_module()
        rules_path = tmp_path / "permission_rules.json"
        assert not rules_path.exists()

        added = module.append_to_auto_allow(str(rules_path), "Bash(git *)")
        assert added is True
        assert rules_path.exists()
        data = json.loads(rules_path.read_text(encoding="utf-8"))
        assert data["auto_allow"] == ["Bash(git *)"]

    def test_appends_to_existing_auto_allow(self, tmp_path: Path):
        module = _load_module()
        rules_path = tmp_path / "permission_rules.json"
        rules_path.write_text(
            json.dumps({"auto_allow": ["Bash(git *)"], "notify_on_auto": True}),
            encoding="utf-8",
        )

        added = module.append_to_auto_allow(str(rules_path), "Bash(npm *)")
        assert added is True
        data = json.loads(rules_path.read_text(encoding="utf-8"))
        assert data["auto_allow"] == ["Bash(git *)", "Bash(npm *)"]
        # 他のキーは保持される
        assert data["notify_on_auto"] is True

    def test_returns_false_if_pattern_already_exists(self, tmp_path: Path):
        module = _load_module()
        rules_path = tmp_path / "permission_rules.json"
        rules_path.write_text(
            json.dumps({"auto_allow": ["Bash(git *)"]}), encoding="utf-8"
        )

        added = module.append_to_auto_allow(str(rules_path), "Bash(git *)")
        assert added is False
        data = json.loads(rules_path.read_text(encoding="utf-8"))
        # 重複しない
        assert data["auto_allow"] == ["Bash(git *)"]

    def test_handles_malformed_json_by_treating_as_empty(self, tmp_path: Path):
        """壊れた JSON は空オブジェクト扱いで上書きする（既存挙動を温存）."""
        module = _load_module()
        rules_path = tmp_path / "permission_rules.json"
        rules_path.write_text("not valid json {", encoding="utf-8")

        added = module.append_to_auto_allow(str(rules_path), "Bash(git *)")
        assert added is True
        data = json.loads(rules_path.read_text(encoding="utf-8"))
        assert data["auto_allow"] == ["Bash(git *)"]

    def test_handles_non_dict_root_gracefully(self, tmp_path: Path):
        """root が dict 以外の JSON でも crash せず空 dict 扱い."""
        module = _load_module()
        rules_path = tmp_path / "permission_rules.json"
        rules_path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")

        added = module.append_to_auto_allow(str(rules_path), "Bash(git *)")
        assert added is True
        data = json.loads(rules_path.read_text(encoding="utf-8"))
        assert data["auto_allow"] == ["Bash(git *)"]

    def test_unicode_pattern_is_preserved(self, tmp_path: Path):
        """日本語パターンも ensure_ascii=False で書き込まれる."""
        module = _load_module()
        rules_path = tmp_path / "permission_rules.json"
        module.append_to_auto_allow(str(rules_path), "Bash(echo こんにちは*)")
        raw = rules_path.read_text(encoding="utf-8")
        # \\u エスケープではなく直接 UTF-8 で書かれる
        assert "こんにちは" in raw

    def test_no_temp_files_remain_after_success(self, tmp_path: Path):
        """成功時に一時ファイルが残らない（os.replace で原子的に置換）."""
        module = _load_module()
        rules_path = tmp_path / "permission_rules.json"
        module.append_to_auto_allow(str(rules_path), "Bash(git *)")
        tmp_files = list(tmp_path.glob(".permission_rules.*.tmp"))
        assert tmp_files == [], f"一時ファイルが残存: {tmp_files}"

    def test_returns_false_when_at_max_size(self, tmp_path: Path, capsys):
        """上限 (_AUTO_ALLOW_MAX_SIZE) に達している場合は False を返し警告を出力する."""
        module = _load_module()
        rules_path = tmp_path / "permission_rules.json"
        max_size = module._AUTO_ALLOW_MAX_SIZE
        # 既に上限件数のパターンを設定する
        existing_patterns = [f"Bash(pattern_{i} *)" for i in range(max_size)]
        rules_path.write_text(
            json.dumps({"auto_allow": existing_patterns}), encoding="utf-8"
        )

        added = module.append_to_auto_allow(str(rules_path), "Bash(new_pattern *)")

        # 追加は失敗する
        assert added is False
        # ファイルは変更されていない
        data = json.loads(rules_path.read_text(encoding="utf-8"))
        assert len(data["auto_allow"]) == max_size
        assert "Bash(new_pattern *)" not in data["auto_allow"]
        # stderr に警告が出ている
        captured = capsys.readouterr()
        assert "上限" in captured.err

    def test_succeeds_at_one_before_max_size(self, tmp_path: Path):
        """上限より 1 件少ない状態では追加できる（境界値確認）."""
        module = _load_module()
        rules_path = tmp_path / "permission_rules.json"
        max_size = module._AUTO_ALLOW_MAX_SIZE
        # 上限より 1 件少ないパターンを設定する
        existing_patterns = [f"Bash(pattern_{i} *)" for i in range(max_size - 1)]
        rules_path.write_text(
            json.dumps({"auto_allow": existing_patterns}), encoding="utf-8"
        )

        added = module.append_to_auto_allow(str(rules_path), "Bash(new_pattern *)")

        assert added is True
        data = json.loads(rules_path.read_text(encoding="utf-8"))
        assert len(data["auto_allow"]) == max_size
        assert "Bash(new_pattern *)" in data["auto_allow"]


class TestShowToast:
    """show_toast() の挙動検証（windows-toasts が無い環境では skip）."""

    def test_silent_fail_when_windows_toasts_missing(self, tmp_path: Path, capsys):
        """windows-toasts が import できない場合は何もせず exit。"""
        module = _load_module()
        # windows_toasts をモジュール参照不能にする
        import sys as _sys

        saved_modules = {
            k: v
            for k, v in _sys.modules.items()
            if k.startswith("windows_toasts")
        }
        for k in list(saved_modules):
            del _sys.modules[k]
        # builtins.__import__ を差し替え、windows_toasts だけ ImportError
        orig_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

        def fake_import(name, *args, **kwargs):
            if name.startswith("windows_toasts"):
                raise ImportError("simulated absence")
            return orig_import(name, *args, **kwargs)

        try:
            if isinstance(__builtins__, dict):
                __builtins__["__import__"] = fake_import
            else:
                __builtins__.__import__ = fake_import  # type: ignore

            rules_path = tmp_path / "permission_rules.json"
            # show_toast はエラーを raise せず silent fail することを期待
            module.show_toast("msg", "Bash(git *)", str(rules_path))
            # rules ファイルは書き換わらない（ボタンクリックがないため）
            assert not rules_path.exists()
        finally:
            if isinstance(__builtins__, dict):
                __builtins__["__import__"] = orig_import
            else:
                __builtins__.__import__ = orig_import  # type: ignore
            # モジュールキャッシュを復元
            for k, v in saved_modules.items():
                _sys.modules[k] = v
