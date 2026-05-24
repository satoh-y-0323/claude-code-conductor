"""tests/test_cli_update_breaking_changes.py

v2.19.0 breaking changes 警告 + version checkpoint のテスト。
A: _load_breaking_changes パーサー (7 件)
B: _compare_versions / _bump_level / _extract_breaking_changes_between (8 件)
C: _load_version_checkpoint / _save_version_checkpoint (4 件)
D: handle() CLI 統合 (4 件)
E: Round 2 追加テスト (L-02 / L-03 / F-03 / M-03 / _semver_tuple)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from c3.cli_update import (
    BreakingChange,
    _bump_level,
    _compare_versions,
    _extract_breaking_changes_between,
    _load_breaking_changes,
    _load_version_checkpoint,
    _print_breaking_changes,
    _save_version_checkpoint,
    _semver_tuple,
    handle,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def claude_root(tmp_path: Path) -> Path:
    """tmp_path 配下に .claude/ を作り、resolve 済み絶対パスを返す。"""
    d = tmp_path / ".claude"
    d.mkdir()
    return d.resolve()


@pytest.fixture()
def template_dir(tmp_path: Path) -> Path:
    """tmp_path 配下に template/ ディレクトリを作って返す。"""
    d = tmp_path / "template"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# A: _load_breaking_changes パーサーテスト (7 件)
# ---------------------------------------------------------------------------

class TestLoadBreakingChanges:

    def test_missing_file_returns_empty(self, template_dir: Path):
        """A1: breaking-changes.txt が不在 → ([], [])。"""
        entries, warnings = _load_breaking_changes(template_dir)
        assert entries == []
        assert warnings == []

    def test_bom_detected_returns_empty_with_warning(self, template_dir: Path):
        """A2: UTF-8 BOM 付き → ファイル全体破棄 + warning に "BOM" が含まれる。"""
        content = b"\xef\xbb\xbfv2.11.0|en summary|ja summary\n"
        (template_dir / "breaking-changes.txt").write_bytes(content)
        entries, warnings = _load_breaking_changes(template_dir)
        assert entries == []
        assert any("BOM" in w or "bom" in w.lower() for w in warnings)

    def test_comment_and_blank_lines_skipped(self, template_dir: Path):
        """A3: # コメント行と空行はスキップされ、有効エントリのみ返る。"""
        content = (
            "# C3 breaking changes log\n"
            "\n"
            "v2.11.0|removed summarize-memory|summarize-memory 廃止\n"
            "  # indented comment\n"
            "\n"
        )
        (template_dir / "breaking-changes.txt").write_text(content, encoding="utf-8")
        entries, warnings = _load_breaking_changes(template_dir)
        assert len(entries) == 1
        assert entries[0].version == "2.11.0"
        assert warnings == []

    def test_pipe_insufficient_warns_and_skips(self, template_dir: Path):
        """A4: pipe が 0 個または 1 個 → warning + skip。"""
        content = (
            "v2.11.0\n"             # pipe 0個
            "v2.12.0|only-en\n"    # pipe 1個
            "v2.13.0|en|ja\n"      # 正常
        )
        (template_dir / "breaking-changes.txt").write_text(content, encoding="utf-8")
        entries, warnings = _load_breaking_changes(template_dir)
        assert len(entries) == 1
        assert entries[0].version == "2.13.0"
        assert len(warnings) == 2

    def test_semver_noncompliant_warns_and_skips(self, template_dir: Path):
        """A5: SemVer 不適合 version → warning + skip。"""
        content = (
            "v2.11.0|valid|有効\n"
            "not-a-version|en|ja\n"
            "v2.12.0-rc1|en|ja\n"
        )
        (template_dir / "breaking-changes.txt").write_text(content, encoding="utf-8")
        entries, warnings = _load_breaking_changes(template_dir)
        assert len(entries) == 1
        assert entries[0].version == "2.11.0"
        assert len(warnings) == 2

    def test_control_chars_sanitized(self, template_dir: Path):
        """A6: 制御文字 (\x00-\x08\x0b\x0c\x0e-\x1f\x7f) は除去、tab は保持。

        注意: newline はファイルの行区切りとして消費されるため、単一エントリ内に
        含めることはフォーマット上不可能。sanitize_terminal_text が newline を
        除去しない仕様はユニット層では確認済み。ここでは除去対象制御文字の確認と
        tab の保持確認のみを行う。
        """
        # \x01 (SOH), \x07 (BEL) は除去対象。\t (tab) は保持。
        en_with_ctrl = "en\x01summary\tdetail"
        ja_with_ctrl = "ja\x07summary\x04end"
        content = f"v2.11.0|{en_with_ctrl}|{ja_with_ctrl}\n"
        (template_dir / "breaking-changes.txt").write_text(content, encoding="utf-8")
        entries, warnings = _load_breaking_changes(template_dir)
        assert len(entries) == 1
        # \x01, \x07, \x04 は除去されている
        assert "\x01" not in entries[0].en
        assert "\x07" not in entries[0].ja
        assert "\x04" not in entries[0].ja
        # tab は保持
        assert "\t" in entries[0].en
        assert warnings == []

    def test_duplicate_version_first_wins_with_warning(self, template_dir: Path):
        """A7: 同一 version の重複エントリ → 先勝ち + warning。"""
        content = (
            "v2.11.0|first en|first ja\n"
            "v2.11.0|second en|second ja\n"
        )
        (template_dir / "breaking-changes.txt").write_text(content, encoding="utf-8")
        entries, warnings = _load_breaking_changes(template_dir)
        assert len(entries) == 1
        assert entries[0].en == "first en"
        assert any("duplicate" in w.lower() or "2.11.0" in w for w in warnings)


# ---------------------------------------------------------------------------
# B: バージョン比較・範囲抽出テスト (8 件)
# ---------------------------------------------------------------------------

class TestVersionComparisons:

    def test_bump_minor(self):
        """B1: minor バンプ (2.10.0 → 2.11.0) → 'minor'。"""
        assert _bump_level("2.10.0", "2.11.0") == "minor"

    def test_bump_major(self):
        """B2: major バンプ (1.5.3 → 2.0.0) → 'major'。"""
        assert _bump_level("1.5.3", "2.0.0") == "major"

    def test_bump_patch(self):
        """B3: patch バンプ (2.11.0 → 2.11.1) → 'patch'。"""
        assert _bump_level("2.11.0", "2.11.1") == "patch"

    def test_v_prefix_normalized(self):
        """B4: 'v' プレフィックス揺れ ('v2.11.0' と '2.11.0') が同一と見なされる。"""
        assert _compare_versions("v2.11.0", "2.11.0") == 0
        assert _compare_versions("2.11.0", "v2.11.0") == 0
        assert _bump_level("v2.10.0", "v2.11.0") == "minor"

    def test_initial_prev_none_returns_all(self):
        """B5: prev=None → 'initial' バンプ、_extract は全件返す。"""
        assert _bump_level(None, "2.11.0") == "initial"
        entries = [
            BreakingChange("2.10.0", "en1", "ja1"),
            BreakingChange("2.11.0", "en2", "ja2"),
        ]
        result = _extract_breaking_changes_between(None, "2.19.0", entries)
        assert len(result) == 2

    def test_boundary_curr_included(self):
        """B6: 半開区間 (prev, curr] — curr ちょうどのエントリは含まれる。"""
        entries = [
            BreakingChange("2.10.0", "en_old", "ja_old"),
            BreakingChange("2.11.0", "en_curr", "ja_curr"),
            BreakingChange("2.12.0", "en_future", "ja_future"),
        ]
        result = _extract_breaking_changes_between("2.10.0", "2.11.0", entries)
        assert len(result) == 1
        assert result[0].version == "2.11.0"

    def test_downgrade_returns_empty(self):
        """B7: prev > curr (downgrade) → [] 返却。"""
        entries = [BreakingChange("2.11.0", "en", "ja")]
        result = _extract_breaking_changes_between("2.19.0", "2.11.0", entries)
        assert result == []

    def test_same_version_returns_empty(self):
        """B8: prev == curr (same) → [] 返却。"""
        entries = [BreakingChange("2.11.0", "en", "ja")]
        result = _extract_breaking_changes_between("2.11.0", "2.11.0", entries)
        assert result == []


# ---------------------------------------------------------------------------
# C: version checkpoint テスト (4 件)
# ---------------------------------------------------------------------------

class TestVersionCheckpoint:

    def test_missing_checkpoint_returns_none(self, claude_root: Path):
        """C1: checkpoint ファイル不在 → None。"""
        result = _load_version_checkpoint(claude_root)
        assert result is None

    def test_roundtrip_save_load(self, claude_root: Path):
        """C2: save → load で同じバージョン文字列が返る。"""
        _save_version_checkpoint(claude_root, "2.19.0")
        result = _load_version_checkpoint(claude_root)
        assert result == "2.19.0"

    def test_atomic_write_no_tmp_remaining(self, claude_root: Path):
        """C3: save 後に *.tmp ファイルが残らない。

        F-02 で tmp パスは c3_version.txt.{pid}.{uuid}.tmp 形式に変更済み。
        旧形式 (.txt.tmp) も新形式 (pid+uuid.tmp) もどちらも残らないことを確認する。
        """
        _save_version_checkpoint(claude_root, "2.19.0")
        state_dir = claude_root / "state"
        # 旧形式 (.txt.tmp)
        assert not (state_dir / "c3_version.txt.tmp").exists(), ".txt.tmp が残っていてはいけない"
        # 新形式 (*.tmp glob)
        remaining_tmps = list(state_dir.glob("c3_version.txt.*.tmp"))
        assert len(remaining_tmps) == 0, f"*.tmp ファイルが残っている: {remaining_tmps}"

    def test_corrupted_checkpoint_returns_none(self, claude_root: Path):
        """C4: 空ファイルまたは SemVer 不適合 → None。"""
        state_dir = claude_root / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        checkpoint = state_dir / "c3_version.txt"

        # 空ファイル
        checkpoint.write_text("", encoding="utf-8")
        assert _load_version_checkpoint(claude_root) is None

        # non-SemVer
        checkpoint.write_text("not-a-version\n", encoding="utf-8")
        assert _load_version_checkpoint(claude_root) is None


# ---------------------------------------------------------------------------
# D: handle() CLI 統合テスト (4 件)
# ---------------------------------------------------------------------------

class TestHandleBreakingChangesIntegration:
    """handle() に breaking changes ブロックが正しく統合されていることを確認する。"""

    # D1/D3/D4 で prev_version=2.18.0 の区間 (2.18.0, 2.19.0] に含まれるエントリ
    _BC_ENTRY = "v2.19.0|some-feature removed|some-feature 廃止\n"
    # D2 では prev=1.0.0, curr=2.19.0 の major bump テスト用（どのバージョンでも区間に入る）
    _BC_ENTRY_MAJOR = "v2.11.0|summarize-memory removed|summarize-memory 廃止\n"

    def _make_args(
        self,
        tmp_path: Path,
        dry_run: bool = False,
        yes: bool = False,
        platform: str = "claude",
    ) -> argparse.Namespace:
        return argparse.Namespace(
            target=tmp_path,
            dry_run=dry_run,
            platform=platform,
            yes=yes,
        )

    def _setup_project(
        self,
        tmp_path: Path,
        prev_version: str | None = None,
        curr_version: str = "2.19.0",
        bc_content: str | None = None,
    ) -> tuple[Path, Path]:
        """tmp_path に .claude/ を作り、fake_template を設定する。

        prev_version が指定されれば state/c3_version.txt に書き込む（checkpoint 設定）。
        bc_content が指定されれば template の breaking-changes.txt に書き込む。
        curr_version は c3.__version__ を monkeypatch するために返す。
        """
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        template_dir = tmp_path / "fake_template"
        template_dir.mkdir()

        if prev_version is not None:
            state_dir = claude_dir / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "c3_version.txt").write_text(f"{prev_version}\n", encoding="utf-8")

        if bc_content is not None:
            (template_dir / "breaking-changes.txt").write_text(bc_content, encoding="utf-8")

        return claude_dir, template_dir

    def test_d1_minor_bump_displays_and_saves_checkpoint(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        """D1: minor bump で表示あり + checkpoint 更新 + prompt 非発火。"""
        # prev=2.18.0, curr=2.19.0 → minor bump
        claude_dir, template_dir = self._setup_project(
            tmp_path,
            prev_version="2.18.0",
            bc_content=self._BC_ENTRY,
        )

        monkeypatch.setattr("c3.cli_update.templates_dir", lambda: template_dir)
        monkeypatch.setattr("c3.cli_update._walk_diff", lambda t, d: [])
        monkeypatch.setattr("c3.__version__", "2.19.0")
        # input が呼ばれた場合はテスト失敗とする（prompt 非発火確認）
        monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(AssertionError("prompt must not fire")))

        args = self._make_args(tmp_path)
        ret = handle(args)

        assert ret == 0
        captured = capsys.readouterr()
        # minor bump なのでヘッダが出ている
        assert "breaking changes" in captured.out or "2.18.0" in captured.out
        # checkpoint が 2.19.0 で保存されている
        checkpoint = claude_dir / "state" / "c3_version.txt"
        assert checkpoint.exists()
        assert checkpoint.read_text(encoding="utf-8").strip() == "2.19.0"

    def test_d2_major_bump_n_cancels_and_no_checkpoint(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        """D2: major bump で input mock が 'n' → return 0 + checkpoint 未更新 + add/update / deletions も走らない。"""
        # prev=1.0.0, curr=2.19.0 → major bump (v2.11.0 は (1.0.0, 2.19.0] 区間内)
        claude_dir, template_dir = self._setup_project(
            tmp_path,
            prev_version="1.0.0",
            bc_content=self._BC_ENTRY_MAJOR,
        )

        monkeypatch.setattr("c3.cli_update.templates_dir", lambda: template_dir)
        monkeypatch.setattr("c3.cli_update._walk_diff", lambda t, d: [])
        monkeypatch.setattr("c3.__version__", "2.19.0")
        monkeypatch.setattr("builtins.input", lambda _: "n")

        args = self._make_args(tmp_path, yes=False)
        ret = handle(args)

        assert ret == 0
        # checkpoint は 1.0.0 のまま更新されていない
        checkpoint = claude_dir / "state" / "c3_version.txt"
        assert checkpoint.read_text(encoding="utf-8").strip() == "1.0.0"

    def test_d3_major_bump_yes_skips_prompt_and_saves_checkpoint(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        """D3: major bump + --yes → prompt skip + 通常実行 + checkpoint 更新。"""
        # prev=1.0.0, curr=2.19.0 → major bump (v2.11.0 は (1.0.0, 2.19.0] 区間内)
        claude_dir, template_dir = self._setup_project(
            tmp_path,
            prev_version="1.0.0",
            bc_content=self._BC_ENTRY_MAJOR,
        )

        monkeypatch.setattr("c3.cli_update.templates_dir", lambda: template_dir)
        monkeypatch.setattr("c3.cli_update._walk_diff", lambda t, d: [])
        monkeypatch.setattr("c3.__version__", "2.19.0")
        # input が呼ばれた場合はテスト失敗（--yes なのでスキップのはず）
        monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(AssertionError("prompt must not fire with --yes")))

        args = self._make_args(tmp_path, yes=True)
        ret = handle(args)

        assert ret == 0
        captured = capsys.readouterr()
        assert "--yes" in captured.out or "skipping prompt" in captured.out
        # checkpoint が 2.19.0 に更新されている
        checkpoint = claude_dir / "state" / "c3_version.txt"
        assert checkpoint.exists()
        assert checkpoint.read_text(encoding="utf-8").strip() == "2.19.0"

    def test_d4_dry_run_no_checkpoint_written(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ):
        """D4: --dry-run で checkpoint 未書き込み。MAJOR でも prompt skip。"""
        # prev=1.0.0, curr=2.19.0 → major bump (v2.11.0 は (1.0.0, 2.19.0] 区間内)
        claude_dir, template_dir = self._setup_project(
            tmp_path,
            prev_version="1.0.0",
            bc_content=self._BC_ENTRY_MAJOR,
        )

        monkeypatch.setattr("c3.cli_update.templates_dir", lambda: template_dir)
        monkeypatch.setattr("c3.cli_update._walk_diff", lambda t, d: [])
        monkeypatch.setattr("c3.__version__", "2.19.0")
        # dry-run では prompt が出ないはずなので input が呼ばれたら失敗
        monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(AssertionError("prompt must not fire in dry-run")))

        args = self._make_args(tmp_path, dry_run=True)
        ret = handle(args)

        assert ret == 0
        captured = capsys.readouterr()
        assert "dry-run" in captured.out
        # dry-run では checkpoint が更新されない（1.0.0 のまま）
        checkpoint = claude_dir / "state" / "c3_version.txt"
        assert checkpoint.exists()
        assert checkpoint.read_text(encoding="utf-8").strip() == "1.0.0"


# ---------------------------------------------------------------------------
# E: Round 2 追加テスト (L-01 / L-02 / L-03 / F-03 / M-03)
# ---------------------------------------------------------------------------

class TestRound2Additions:
    """v2.19.0 Round 2 で追加した修正の検証テスト。"""

    def test_e1_semver_tuple_converts_correctly(self):
        """E1: _semver_tuple が (major, minor, patch) タプルを返す（L-01 共通ヘルパー確認）。"""
        assert _semver_tuple("2.11.0") == (2, 11, 0)
        assert _semver_tuple("v1.5.3") == (1, 5, 3)
        assert _semver_tuple("10.0.0") == (10, 0, 0)

    def test_e2_load_breaking_changes_strips_whitespace(self, template_dir: Path):
        """E2: en_raw / ja_raw に前後空白があっても strip 済みで BreakingChange に格納される（L-02）。"""
        content = "v2.11.0|  en summary  |  ja summary  \n"
        (template_dir / "breaking-changes.txt").write_text(content, encoding="utf-8")
        entries, warnings = _load_breaking_changes(template_dir)
        assert len(entries) == 1
        assert entries[0].en == "en summary"
        assert entries[0].ja == "ja summary"
        assert warnings == []

    def test_e3_initial_no_entries_header_message(self, capsys: pytest.CaptureFixture):
        """E3: bump=initial + relevant=[] のとき「エントリなし」ヘッダが stdout に出る（L-03）。"""
        _print_breaking_changes(
            relevant=[],
            bump="initial",
            prev=None,
            curr="0.1.0",
            parse_warnings=[],
        )
        captured = capsys.readouterr()
        assert "breaking-changes.txt にエントリなし" in captured.out

    def test_e4_initial_with_entries_shows_full_header(self, capsys: pytest.CaptureFixture):
        """E4: bump=initial + relevant あり のとき「全件表示」ヘッダが stdout に出る（L-03 非回帰）。"""
        entries = [BreakingChange("0.1.0", "en", "ja")]
        _print_breaking_changes(
            relevant=entries,
            bump="initial",
            prev=None,
            curr="0.1.0",
            parse_warnings=[],
        )
        captured = capsys.readouterr()
        assert "breaking changes を全件表示します" in captured.out

    def test_e5_prev_disp_esc_sanitized(self, capsys: pytest.CaptureFixture):
        """E5: prev に ESC 入り文字列を渡すと sanitize されて stdout に出ない（F-03）。"""
        _print_breaking_changes(
            relevant=[],
            bump="downgrade",
            prev="2.0.0\x1b[31m",
            curr="1.0.0",
            parse_warnings=[],
        )
        captured = capsys.readouterr()
        # ESC シーケンスが除去されていること
        assert "\x1b[31m" not in captured.err
        # sanitize 後のバージョン文字列は含まれていること
        assert "2.0.0" in captured.err

    def test_e6_parse_warnings_goes_to_stderr_minor_count_zero(
        self,
        capsys: pytest.CaptureFixture,
    ):
        """E6: minor bump + count==0 のとき parse_warnings が stderr に出力される（M-03）。"""
        _print_breaking_changes(
            relevant=[],
            bump="minor",
            prev="2.10.0",
            curr="2.11.0",
            parse_warnings=["test warning message"],
        )
        captured = capsys.readouterr()
        # stdout には出ない
        assert "test warning message" not in captured.out
        # stderr に出る
        assert "test warning message" in captured.err

    def test_e7_parse_warnings_goes_to_stderr_after_entries(
        self,
        capsys: pytest.CaptureFixture,
    ):
        """E7: initial/major の後続 parse_warnings が stderr に出力される（M-03）。"""
        entries = [BreakingChange("2.11.0", "en", "ja")]
        _print_breaking_changes(
            relevant=entries,
            bump="major",
            prev="1.0.0",
            curr="2.11.0",
            parse_warnings=["warn1", "warn2"],
        )
        captured = capsys.readouterr()
        # ヘッダ + エントリは stdout
        assert "MAJOR" in captured.out
        # parse_warnings は stderr
        assert "warn1" in captured.err
        assert "warn2" in captured.err
        # stdout には警告が含まれない
        assert "warn1" not in captured.out
