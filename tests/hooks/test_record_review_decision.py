"""Tests for .claude/skills/dev-workflow/scripts/record_review_decision.py

主に `_truncate` ヘルパーの単体テスト。本番引数の境界条件（None / 空文字列 /
文字数上限ちょうど / 文字数上限超過 / バイト数上限超過）を検証する [CR-T-001]。
"""
from __future__ import annotations

import importlib.util
import io
import sys
import types
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).resolve().parents[2]
HOOK_PATH = (
    WORKTREE_ROOT
    / ".claude"
    / "skills"
    / "dev-workflow"
    / "scripts"
    / "record_review_decision.py"
)


def _load_hook_module(name: str = "record_review_decision_t") -> types.ModuleType:
    """テストごとに一意な sys.modules キーでモジュールを読み込む。"""
    spec = importlib.util.spec_from_file_location(name, HOOK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


class TestTruncateNoneAndEmpty:
    """None / 空文字列はそのまま返す（切り詰めも警告も発生しない）。"""

    def test_none_returns_none(self, capsys):
        mod = _load_hook_module()
        result = mod._truncate(None, mod.MAX_FINDING_LEN, "finding")
        assert result is None
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_empty_string_returns_empty(self, capsys):
        mod = _load_hook_module()
        result = mod._truncate("", mod.MAX_FINDING_LEN, "finding")
        assert result == ""
        captured = capsys.readouterr()
        assert captured.err == ""


class TestTruncateUnderLimit:
    """文字数・バイト数とも上限以内なら値はそのまま、警告も出ない。"""

    def test_short_string_unchanged(self, capsys):
        mod = _load_hook_module()
        value = "短い指摘"
        result = mod._truncate(value, mod.MAX_FINDING_LEN, "finding")
        assert result == value
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_exactly_at_char_limit_unchanged(self, capsys):
        mod = _load_hook_module()
        value = "a" * mod.MAX_FINDING_LEN
        result = mod._truncate(value, mod.MAX_FINDING_LEN, "finding")
        assert result == value
        assert len(result) == mod.MAX_FINDING_LEN
        captured = capsys.readouterr()
        assert captured.err == ""


class TestTruncateOverCharLimit:
    """文字数上限超過時は切り詰めて警告を出す。"""

    def test_over_char_limit_truncated(self, capsys):
        mod = _load_hook_module()
        value = "a" * (mod.MAX_FINDING_LEN + 100)
        result = mod._truncate(value, mod.MAX_FINDING_LEN, "finding")
        assert len(result) == mod.MAX_FINDING_LEN
        captured = capsys.readouterr()
        assert "truncated" in captured.err
        assert "finding" in captured.err

    def test_reason_uses_correct_limit(self, capsys):
        mod = _load_hook_module()
        value = "x" * (mod.MAX_REASON_LEN + 50)
        result = mod._truncate(value, mod.MAX_REASON_LEN, "reason")
        assert len(result) == mod.MAX_REASON_LEN
        captured = capsys.readouterr()
        assert "reason" in captured.err

    def test_context_uses_correct_limit(self, capsys):
        mod = _load_hook_module()
        value = "y" * (mod.MAX_CONTEXT_LEN + 10)
        result = mod._truncate(value, mod.MAX_CONTEXT_LEN, "context")
        assert len(result) == mod.MAX_CONTEXT_LEN
        captured = capsys.readouterr()
        assert "context" in captured.err


class TestTruncateByteLimit:
    """UTF-8 バイト数で MAX_FIELD_BYTES を超えた場合の追加切り詰め検証。

    現状 MAX_FINDING_LEN=2000・MAX_FIELD_BYTES=8192 のため通常入力では
    while ループに到達しないが、文字数上限を一時的に拡張して動作を確認する。
    """

    def test_byte_limit_triggers_when_chars_exceed_byte_budget(self, monkeypatch, capsys):
        mod = _load_hook_module()
        # MAX_FINDING_LEN を一時的に拡張して、4 バイト UTF-8 文字 (U+1F600 等) で
        # バイト数上限を超えるシナリオを作る。
        monkeypatch.setattr(mod, "MAX_FINDING_LEN", 10000)
        emoji = "\U0001F600"  # 4 バイト
        # 3000 文字 × 4 バイト = 12000 バイト > MAX_FIELD_BYTES (8192)
        value = emoji * 3000
        result = mod._truncate(value, mod.MAX_FINDING_LEN, "finding")
        # 文字数は上限以内のため文字カット段階はスキップ、while でバイト切り
        assert len(result.encode("utf-8")) <= mod.MAX_FIELD_BYTES
        captured = capsys.readouterr()
        assert "truncated" in captured.err

    def test_byte_limit_preserves_valid_utf8(self, monkeypatch):
        mod = _load_hook_module()
        monkeypatch.setattr(mod, "MAX_FINDING_LEN", 10000)
        emoji = "\U0001F600"  # 4 バイト
        value = emoji * 3000
        result = mod._truncate(value, mod.MAX_FINDING_LEN, "finding")
        # 切り詰め後も有効な UTF-8（途中で切れて invalid byte にならない）
        result.encode("utf-8").decode("utf-8")


class TestChecklistIdPattern:
    """checklist-id 形式検証ロジックの単体テスト [SR-V-001]。"""

    def test_pattern_accepts_valid_cr_id(self):
        mod = _load_hook_module()
        assert mod.CHECKLIST_ID_PATTERN.match("CR-Q-001")
        assert mod.CHECKLIST_ID_PATTERN.match("CR-INJ-123")
        assert mod.CHECKLIST_ID_PATTERN.match("CR-T-9999")

    def test_pattern_accepts_valid_sr_id(self):
        mod = _load_hook_module()
        assert mod.CHECKLIST_ID_PATTERN.match("SR-K-002")
        assert mod.CHECKLIST_ID_PATTERN.match("SR-V-001")

    def test_pattern_rejects_short_number(self):
        mod = _load_hook_module()
        # 3 桁未満は不正
        assert mod.CHECKLIST_ID_PATTERN.match("CR-Q-1") is None
        assert mod.CHECKLIST_ID_PATTERN.match("CR-Q-12") is None

    def test_pattern_rejects_lowercase(self):
        mod = _load_hook_module()
        assert mod.CHECKLIST_ID_PATTERN.match("cr-q-001") is None
        assert mod.CHECKLIST_ID_PATTERN.match("CR-q-001") is None

    def test_pattern_rejects_unknown_prefix(self):
        mod = _load_hook_module()
        assert mod.CHECKLIST_ID_PATTERN.match("XX-Q-001") is None

    def test_pattern_rejects_newline_injection(self):
        mod = _load_hook_module()
        # 改行・スペース混入は不正
        assert mod.CHECKLIST_ID_PATTERN.match("CR-Q-001\n## injected") is None
        assert mod.CHECKLIST_ID_PATTERN.match("CR-Q-001 extra") is None

    def test_main_skips_invalid_checklist_id(self, capsys):
        mod = _load_hook_module()
        # 不正な checklist-id を渡すと return 0 で skip される（DB insert なし）
        rc = mod.main([
            "--checklist-id", "INVALID",
            "--finding", "test finding",
            "--decision", "accepted",
            "--reviewer", "code-reviewer",
        ])
        assert rc == 0
        captured = capsys.readouterr()
        assert "checklist-id format invalid (skipped)" in captured.err

    def test_main_accepts_cr_new_special_value(self, monkeypatch, capsys):
        mod = _load_hook_module()
        # CR-NEW は形式検証対象外（チェックリスト追加候補として記録）
        calls = []

        def fake_insert(**kwargs):
            calls.append(kwargs)
            return True

        # c3.db.insert_review_decision をモック化。
        # monkeypatch.setitem を使ってテスト終了後の自動クリーンアップを保証する。
        import sys as _sys
        import types as _types
        if "c3" not in _sys.modules:
            monkeypatch.setitem(_sys.modules, "c3", _types.ModuleType("c3"))
        if "c3.db" not in _sys.modules:
            fake_mod = _types.ModuleType("c3.db")
            fake_mod.insert_review_decision = fake_insert
            monkeypatch.setitem(_sys.modules, "c3.db", fake_mod)
        else:
            monkeypatch.setattr("c3.db.insert_review_decision", fake_insert)

        rc = mod.main([
            "--checklist-id", "CR-NEW",
            "--finding", "新規パターン",
            "--decision", "accepted",
            "--reviewer", "code-reviewer",
        ])
        assert rc == 0
        captured = capsys.readouterr()
        # 形式検証エラーは出ない
        assert "format invalid" not in captured.err


class TestTruncateConstants:
    """定数値の仕様固定テスト（仕様回帰防止）。

    MAX_*_LEN / MAX_FIELD_BYTES の値は意図的に選ばれた仕様であり、
    変更時は SKILL.md / decisions.md / CHANGELOG への波及確認が必要。
    定数が無告知で変わった場合に CI で気づけるように固定テストを置く。
    """

    def test_max_finding_len_is_2000(self):
        mod = _load_hook_module()
        assert mod.MAX_FINDING_LEN == 2000

    def test_max_reason_len_is_2000(self):
        mod = _load_hook_module()
        assert mod.MAX_REASON_LEN == 2000

    def test_max_context_len_is_1000(self):
        mod = _load_hook_module()
        assert mod.MAX_CONTEXT_LEN == 1000

    def test_max_field_bytes_is_8kb(self):
        mod = _load_hook_module()
        assert mod.MAX_FIELD_BYTES == 8 * 1024
