"""tests/test_extract_breaking_changes.py

scripts/extract_breaking_changes.py のユニットテスト。
E1: _load_recorded_versions SemVer 不適合 skip (M-05)
E2: _load_recorded_versions 正常エントリ集合化
E3: _load_recorded_versions コメント行・空行 skip
E4: _sanitize_input ESC 除去確認 (F-04)
E5: _sanitize_input 制御文字除去 / newline・tab 保持
E6: _contains_pipe 単体テスト (F-01)
E7: _contains_pipe pipe なし → False
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ を sys.path に追加して import できるようにする
_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import os
import uuid as _uuid_module
from unittest.mock import patch

from extract_breaking_changes import (  # noqa: E402
    _SEMVER_RE,
    _append_entry,
    _contains_pipe,
    _load_recorded_versions,
    _sanitize_input,
)


# ---------------------------------------------------------------------------
# M-05: _load_recorded_versions の SemVer バリデーション
# ---------------------------------------------------------------------------

class TestLoadRecordedVersions:

    def test_e1_semver_noncompliant_is_skipped(self, tmp_path: Path):
        """E1: SemVer 不適合行は黙って skip される（M-05）。"""
        content = (
            "not-a-version|en|ja\n"
            "v2.11.0-rc1|en|ja\n"   # pre-release はスキップ
            "2.11.0.1|en|ja\n"      # 4 パートはスキップ
            "v2.12.0|en|ja\n"       # 正常
        )
        bc_path = tmp_path / "breaking-changes.txt"
        bc_path.write_text(content, encoding="utf-8")
        seen = _load_recorded_versions(bc_path)
        # 正常な 2.12.0 のみ登録
        assert seen == {"2.12.0"}

    def test_e2_valid_entries_collected(self, tmp_path: Path):
        """E2: 正常な SemVer エントリが集合に追加される。"""
        content = (
            "v2.11.0|en1|ja1\n"
            "v2.12.0|en2|ja2\n"
            "3.0.0|en3|ja3\n"
        )
        bc_path = tmp_path / "breaking-changes.txt"
        bc_path.write_text(content, encoding="utf-8")
        seen = _load_recorded_versions(bc_path)
        assert seen == {"2.11.0", "2.12.0", "3.0.0"}

    def test_e3_comments_and_blank_lines_skipped(self, tmp_path: Path):
        """E3: コメント行・空行はスキップされる。"""
        content = (
            "# C3 breaking changes log\n"
            "\n"
            "  # indented comment\n"
            "v2.11.0|en|ja\n"
            "\n"
        )
        bc_path = tmp_path / "breaking-changes.txt"
        bc_path.write_text(content, encoding="utf-8")
        seen = _load_recorded_versions(bc_path)
        assert seen == {"2.11.0"}

    def test_e3b_missing_file_returns_empty(self, tmp_path: Path):
        """E3b: ファイル不在 → 空集合を返す。"""
        bc_path = tmp_path / "nonexistent.txt"
        seen = _load_recorded_versions(bc_path)
        assert seen == set()

    def test_e3c_semver_re_accepts_only_pure_xyz(self):
        """E3c: _SEMVER_RE が X.Y.Z 純粋形式のみを許可する（cli_update との整合）。
        R2-L-01: .fullmatch() で完全一致を強制することで ^...$ アンカー冗長を解消。
        """
        assert _SEMVER_RE.fullmatch("2.11.0") is not None
        assert _SEMVER_RE.fullmatch("10.0.0") is not None
        # pre-release / build metadata は不適合
        assert _SEMVER_RE.fullmatch("2.11.0-rc1") is None
        assert _SEMVER_RE.fullmatch("2.11.0+build") is None
        assert _SEMVER_RE.fullmatch("2.11") is None       # 2 パート
        assert _SEMVER_RE.fullmatch("2.11.0.1") is None   # 4 パート


# ---------------------------------------------------------------------------
# F-04: _sanitize_input の ESC 除去確認
# ---------------------------------------------------------------------------

class TestSanitizeInput:

    def test_e4_esc_is_removed(self):
        """E4: ESC 文字 (\x1b) は _DISALLOWED_CTRL_RE (\x0e-\x1f) 範囲内で除去される（F-04）。"""
        result = _sanitize_input("hello\x1bworld")
        assert "\x1b" not in result
        assert "helloworld" == result

    def test_e5_control_chars_removed_newline_tab_preserved(self):
        """E5: \x00-\x08\x0b\x0c\x0e-\x1f\x7f を除去し、newline / tab / CR は保持。"""
        # 除去対象
        assert _sanitize_input("a\x00b") == "ab"
        assert _sanitize_input("a\x07b") == "ab"   # BEL
        assert _sanitize_input("a\x7fb") == "ab"   # DEL
        assert _sanitize_input("a\x0eb") == "ab"   # SO (0x0E)
        assert _sanitize_input("a\x1fb") == "ab"   # US (0x1F)
        # 保持対象
        assert "\t" in _sanitize_input("a\tb")     # tab (0x09)
        assert "\n" in _sanitize_input("a\nb")     # newline (0x0A)
        assert "\r" in _sanitize_input("a\rb")     # CR (0x0D)
        # 0x0B (VT) は除去対象
        assert _sanitize_input("a\x0bb") == "ab"
        # 0x0C (FF) は除去対象
        assert _sanitize_input("a\x0cb") == "ab"


# ---------------------------------------------------------------------------
# F-01: _contains_pipe 単体テスト
# ---------------------------------------------------------------------------

class TestContainsPipe:

    def test_e6_contains_pipe_detects_pipe(self):
        """E6: pipe 文字を含む文字列 → True（F-01）。"""
        assert _contains_pipe("test|injection") is True
        assert _contains_pipe("|") is True
        assert _contains_pipe("a|b|c") is True

    def test_e7_no_pipe_returns_false(self):
        """E7: pipe を含まない文字列 → False（F-01 非回帰）。"""
        assert _contains_pipe("normal summary") is False
        assert _contains_pipe("") is False
        assert _contains_pipe("summary: removed feature") is False


# ---------------------------------------------------------------------------
# R2-M-01 / SR N-01: _append_entry の tmp パス PID+uuid 付与確認
# ---------------------------------------------------------------------------

class TestAppendEntryTmpPath:

    def test_n1_append_entry_tmp_name_includes_pid_and_uuid(self, tmp_path: Path):
        """N1: _append_entry の tmp パスに PID と uuid4.hex が含まれること（R2-M-01 / SR N-01）。

        uuid.uuid4 を固定値に差し替え、_append_entry を実際に実行して破壊的変更エントリを
        書き込んだ後、生成後の bc_path の内容を確認しつつ、tmp 命名規則を
        with_name() の引数から直接検証する。
        実装上 tmp 名は bc_path.with_name(f"breaking-changes.txt.{pid}.{uuid4.hex}.tmp") のため、
        fixed_hex で uuid4 をモックすると期待通りの名前が生成される。
        cli_update._save_version_checkpoint の F-02 修正と同パターン。
        """
        fixed_hex = "abcdef1234567890abcdef1234567890"
        mock_uuid = _uuid_module.UUID(hex=fixed_hex)

        bc_path = tmp_path / "breaking-changes.txt"
        pid_str = str(os.getpid())
        expected_tmp_name = f"breaking-changes.txt.{pid_str}.{fixed_hex}.tmp"

        with patch("extract_breaking_changes.uuid.uuid4", return_value=mock_uuid):
            _append_entry(bc_path, "1.0.0", "test en summary", "テスト日本語サマリ")

        # 正常に書き込まれ、bc_path が存在すること（atomic write が完了している）
        assert bc_path.exists(), "bc_path が atomic write 後に存在すること"

        # tmp ファイルは os.replace で bc_path に rename されて消えていること
        expected_tmp_path = tmp_path / expected_tmp_name
        assert not expected_tmp_path.exists(), "atomic write 後 tmp ファイルは残存しないこと"

        # PID が expected_tmp_name に含まれること（命名パターン確認）
        assert pid_str in expected_tmp_name, f"tmp 名 {expected_tmp_name!r} に PID {pid_str} が含まれること"
        # 固定 uuid4.hex が expected_tmp_name に含まれること
        assert fixed_hex in expected_tmp_name, f"tmp 名 {expected_tmp_name!r} に uuid hex {fixed_hex} が含まれること"
        # 書き込まれた内容が正しいこと（副次確認）
        content = bc_path.read_text(encoding="utf-8")
        assert "v1.0.0|test en summary|" in content, "書き込み内容にエントリが含まれること"
