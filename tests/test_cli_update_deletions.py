"""tests/test_cli_update_deletions.py

v2.18.0 deletions.txt 方式のテスト。
A: _validate_deletion_path ユニット (13)
B: _apply_deletions 結合 (8)
C: パストラバーサル / symlink / 範囲外 攻撃 (9)
D: CLI 統合 (4)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from c3.cli_update import (
    _apply_deletions,
    _format_deletion_report,
    _load_deletions,
    _validate_deletion_path,
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
# A: _validate_deletion_path ユニットテスト (13 件)
# ---------------------------------------------------------------------------

class TestValidateDeletionPath:

    def test_validate_empty_string_rejected(self, claude_root: Path):
        """A1: 空文字列は拒否。"""
        result, warning = _validate_deletion_path("", claude_root)
        assert result is None
        assert warning is not None
        assert "empty" in warning.lower()

    def test_validate_whitespace_only_rejected(self, claude_root: Path):
        """A2: 空白のみも拒否。"""
        result, warning = _validate_deletion_path("   ", claude_root)
        assert result is None
        assert warning is not None
        assert "empty" in warning.lower()

    def test_validate_leading_slash_rejected(self, claude_root: Path):
        """A3: /etc/passwd は絶対パスとして拒否。"""
        result, warning = _validate_deletion_path("/etc/passwd", claude_root)
        assert result is None
        assert warning is not None
        assert "absolute" in warning.lower()

    def test_validate_tilde_rejected(self, claude_root: Path):
        """A4: ~/secrets は home-relative として拒否。"""
        result, warning = _validate_deletion_path("~/secrets", claude_root)
        assert result is None
        assert warning is not None
        assert "home-relative" in warning.lower()

    def test_validate_backslash_rejected(self, claude_root: Path):
        """A5: agents\\evil.md はバックスラッシュとして拒否。"""
        result, warning = _validate_deletion_path(r"agents\evil.md", claude_root)
        assert result is None
        assert warning is not None
        assert "backslash" in warning.lower()

    def test_validate_drive_letter_rejected(self, claude_root: Path):
        """A6: C:\\Windows\\... はドライブレターまたはバックスラッシュとして拒否。

        バックスラッシュチェック (step 4) がドライブレターチェック (step 5) より先に実行されるため、
        C:\\... の場合は "backslash" で弾かれる。どちらで弾かれても拒否されることが重要。
        """
        result, warning = _validate_deletion_path(r"C:\Windows\System32\evil.txt", claude_root)
        assert result is None
        assert warning is not None
        # step4（backslash）または step5（drive letter）で弾かれる
        assert "drive letter" in warning.lower() or "backslash" in warning.lower()

    def test_validate_claude_prefix_rejected(self, claude_root: Path):
        """A7: .claude/agents/x.md は .claude/ プレフィックス禁止で拒否。"""
        result, warning = _validate_deletion_path(".claude/agents/x.md", claude_root)
        assert result is None
        assert warning is not None
        assert ".claude/" in warning

    def test_validate_dotdot_rejected(self, claude_root: Path):
        """A8: ../../../etc/passwd は relative-traversal として拒否。"""
        result, warning = _validate_deletion_path("../../../etc/passwd", claude_root)
        assert result is None
        assert warning is not None
        assert "relative-traversal" in warning.lower()

    def test_validate_single_dot_rejected(self, claude_root: Path):
        """A9: ./agents/x.md は relative-traversal として拒否。"""
        result, warning = _validate_deletion_path("./agents/x.md", claude_root)
        assert result is None
        assert warning is not None
        assert "relative-traversal" in warning.lower()

    def test_validate_normal_path_accepted(self, claude_root: Path):
        """A10: agents/tdd-develop.md (ファイル実在) は正常に通過。"""
        target = claude_root / "agents" / "tdd-develop.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("dummy")
        result, warning = _validate_deletion_path("agents/tdd-develop.md", claude_root)
        assert result == target.resolve()
        assert warning is None

    def test_validate_path_escapes_via_symlink_rejected(self, tmp_path: Path, claude_root: Path):
        """A11: .claude/ 外を指す symlink は拒否。"""
        # claude_root 外のファイル
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        # .claude/agents/x.md を symlink として作成
        agents_dir = claude_root / "agents"
        agents_dir.mkdir(exist_ok=True)
        link = agents_dir / "x.md"
        try:
            link.symlink_to(outside)
        except (NotImplementedError, OSError):
            pytest.skip("symlink not supported on this platform")
        result, warning = _validate_deletion_path("agents/x.md", claude_root)
        assert result is None
        assert warning is not None
        assert "symlink" in warning.lower()

    def test_validate_directory_rejected(self, claude_root: Path):
        """A12: ディレクトリパスは拒否。"""
        d = claude_root / "skills" / "some-skill"
        d.mkdir(parents=True)
        result, warning = _validate_deletion_path("skills/some-skill", claude_root)
        assert result is None
        assert warning is not None
        assert "directory" in warning.lower()

    def test_validate_absent_returns_none_no_warning(self, claude_root: Path):
        """A13: 不在ファイルは (None, None) を返す（warning なし）。"""
        result, warning = _validate_deletion_path("agents/never-existed.md", claude_root)
        assert result is None
        assert warning is None

    def test_validate_deletions_self_rejected(self, claude_root: Path):
        """A14: deletions.txt 自身は self-referencing guard で拒否される。"""
        # 実際に deletions.txt を作成して通過しようとする
        deletions_file = claude_root / "deletions.txt"
        deletions_file.write_text("agents/foo.md\n")
        result, warning = _validate_deletion_path("deletions.txt", claude_root)
        assert result is None
        assert warning is not None
        assert "self-referencing guard" in warning

    @pytest.mark.parametrize("variant", ["Deletions.txt", "DELETIONS.TXT", "deletions.TXT"])
    def test_validate_deletions_self_rejected_case_insensitive(self, tmp_path: Path, variant: str):
        """A15: SR-M-1 回帰防止: Windows NTFS case-insensitive バリエーションも guard で拒否。"""
        claude_root = tmp_path / ".claude"
        claude_root.mkdir()
        (claude_root / "deletions.txt").write_text("# test", encoding="utf-8")
        result, warning = _validate_deletion_path(variant, claude_root)
        # Windows NTFS: variant が deletions.txt に解決される → guard で skip
        # Linux: variant がそのまま解決され、deletions.txt と等しくないため Path 不在として absent 扱い
        if os.name == "nt":
            assert result is None
            assert warning and "self-referencing" in warning.lower()


# ---------------------------------------------------------------------------
# B: _apply_deletions 結合テスト (8 件)
# ---------------------------------------------------------------------------

class TestApplyDeletions:

    def _make_file(self, claude_root: Path, rel: str) -> Path:
        p = claude_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("content")
        return p

    def test_apply_dry_run_no_filesystem_change(self, claude_root: Path):
        """B1: dry_run=True ではファイルが残る。"""
        f = self._make_file(claude_root, "agents/tdd-develop.md")
        result = _apply_deletions(
            ["agents/tdd-develop.md"], claude_root, dry_run=True, assume_yes=False
        )
        assert "agents/tdd-develop.md" in result["to_delete"]
        assert f.exists(), "dry_run 中はファイルを削除しない"
        assert result["deleted"] == []

    def test_apply_normal_mode_yes_flag_deletes(self, claude_root: Path):
        """B2: assume_yes=True でファイルが削除される。"""
        f = self._make_file(claude_root, "agents/tdd-develop.md")
        result = _apply_deletions(
            ["agents/tdd-develop.md"], claude_root, dry_run=False, assume_yes=True
        )
        assert "agents/tdd-develop.md" in result["deleted"]
        assert not f.exists()
        assert not result["cancelled"]

    def test_apply_normal_mode_prompt_y_deletes(self, claude_root: Path, monkeypatch: pytest.MonkeyPatch):
        """B3: input が 'y' ならファイルが削除される。"""
        f = self._make_file(claude_root, "skills/foo/SKILL.md")
        monkeypatch.setattr("builtins.input", lambda _: "y")
        result = _apply_deletions(
            ["skills/foo/SKILL.md"], claude_root, dry_run=False, assume_yes=False
        )
        assert "skills/foo/SKILL.md" in result["deleted"]
        assert not f.exists()
        assert not result["cancelled"]

    def test_apply_normal_mode_prompt_n_cancels(self, claude_root: Path, monkeypatch: pytest.MonkeyPatch):
        """B4: input が 'n' なら cancelled=True かつファイルが残る。"""
        f = self._make_file(claude_root, "agents/tdd-develop.md")
        monkeypatch.setattr("builtins.input", lambda _: "n")
        result = _apply_deletions(
            ["agents/tdd-develop.md"], claude_root, dry_run=False, assume_yes=False
        )
        assert result["cancelled"] is True
        assert f.exists()

    def test_apply_normal_mode_prompt_empty_cancels(self, claude_root: Path, monkeypatch: pytest.MonkeyPatch):
        """B5: input が空 Enter (空文字) なら cancel。"""
        f = self._make_file(claude_root, "agents/tdd-develop.md")
        monkeypatch.setattr("builtins.input", lambda _: "")
        result = _apply_deletions(
            ["agents/tdd-develop.md"], claude_root, dry_run=False, assume_yes=False
        )
        assert result["cancelled"] is True
        assert f.exists()

    def test_apply_normal_mode_prompt_eoferror_cancels(self, claude_root: Path, monkeypatch: pytest.MonkeyPatch):
        """B6: input が EOFError を raise すれば cancel（非対話環境）。"""
        f = self._make_file(claude_root, "agents/tdd-develop.md")

        def raise_eof(_):
            raise EOFError

        monkeypatch.setattr("builtins.input", raise_eof)
        result = _apply_deletions(
            ["agents/tdd-develop.md"], claude_root, dry_run=False, assume_yes=False
        )
        assert result["cancelled"] is True
        assert f.exists()

    def test_apply_mixed_existing_and_absent(self, claude_root: Path):
        """B7: 一部実在・一部 absent → deleted と absent に正しく振り分け。"""
        f = self._make_file(claude_root, "agents/tdd-develop.md")
        result = _apply_deletions(
            ["agents/tdd-develop.md", "agents/never-existed.md"],
            claude_root,
            dry_run=False,
            assume_yes=True,
        )
        assert "agents/tdd-develop.md" in result["deleted"]
        assert "agents/never-existed.md" in result["absent"]
        assert not f.exists()

    def test_apply_unlink_oserror_recorded(self, claude_root: Path, monkeypatch: pytest.MonkeyPatch):
        """B8: unlink 中に PermissionError → errors に記録されて他は続行。"""
        f1 = self._make_file(claude_root, "agents/a.md")
        f2 = self._make_file(claude_root, "agents/b.md")

        original_unlink = Path.unlink

        def patched_unlink(self, missing_ok=False):
            if self.name == "a.md":
                raise PermissionError("Permission denied")
            original_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", patched_unlink)

        result = _apply_deletions(
            ["agents/a.md", "agents/b.md"],
            claude_root,
            dry_run=False,
            assume_yes=True,
        )
        assert any("a.md" in e for e in result["errors"])
        assert "agents/b.md" in result["deleted"]


# ---------------------------------------------------------------------------
# C: 攻撃テスト (9 件)
# ---------------------------------------------------------------------------

class TestAttackScenarios:

    def test_load_deletions_warns_on_traversal(self, template_dir: Path):
        """C1: deletions.txt に ../../../etc/passwd → warnings に含まれ entries 0 件。"""
        (template_dir / "deletions.txt").write_text("../../../etc/passwd\n", encoding="utf-8")
        entries, warnings = _load_deletions(template_dir)
        assert len(entries) == 0
        assert any("traversal" in w.lower() or "relative-traversal" in w.lower() for w in warnings)

    def test_load_deletions_warns_on_drive_letter(self, template_dir: Path):
        """C2: C:\\Windows\\System32\\x → warnings（backslash または drive letter）。

        バックスラッシュチェックがドライブレターチェックより先のため、
        C:\\... は "backslash" で弾かれる。どちらで弾かれても entries が 0 件であればよい。
        """
        (template_dir / "deletions.txt").write_text(
            "C:\\Windows\\System32\\x\n", encoding="utf-8"
        )
        entries, warnings = _load_deletions(template_dir)
        assert len(entries) == 0
        assert any(
            "drive letter" in w.lower() or "backslash" in w.lower()
            for w in warnings
        )

    def test_load_deletions_warns_on_tilde(self, template_dir: Path):
        """C3: ~/secrets → warnings。"""
        (template_dir / "deletions.txt").write_text("~/secrets\n", encoding="utf-8")
        entries, warnings = _load_deletions(template_dir)
        assert len(entries) == 0
        assert any("home-relative" in w.lower() for w in warnings)

    def test_apply_does_not_follow_symlink_outside_claude(
        self, tmp_path: Path, claude_root: Path
    ):
        """C4: .claude/ 外を指す symlink は削除されない。"""
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        agents_dir = claude_root / "agents"
        agents_dir.mkdir(exist_ok=True)
        link = agents_dir / "x.md"
        try:
            link.symlink_to(outside)
        except (NotImplementedError, OSError):
            pytest.skip("symlink not supported on this platform")
        result = _apply_deletions(
            ["agents/x.md"], claude_root, dry_run=False, assume_yes=True
        )
        assert outside.exists(), "symlink 経由の外部ファイルは削除されていないこと"
        assert result["deleted"] == []
        assert any("symlink" in w.lower() for w in result["warnings"])

    def test_apply_does_not_delete_outside_claude_root(self, tmp_path: Path, claude_root: Path):
        """C5: resolve() 後 .claude/ 外を指す Path → warning 記録のみ。"""
        # .claude/ 配下に実在ファイルを用意するが、パス文字列を細工して範囲外へ誘導する
        # ここでは絶対パスチェックに任せる（セーフガードで弾かれることを確認）
        result = _apply_deletions(
            ["/etc/passwd"],  # 絶対パス → セーフガードで弾かれる
            claude_root,
            dry_run=False,
            assume_yes=True,
        )
        assert result["deleted"] == []
        assert len(result["warnings"]) > 0

    def test_load_deletions_skips_bom_file(self, template_dir: Path):
        """C6: UTF-8 BOM 付き deletions.txt → 全エントリ無視 + warning。"""
        content = b"\xef\xbb\xbfagents/tdd-develop.md\n"
        (template_dir / "deletions.txt").write_bytes(content)
        entries, warnings = _load_deletions(template_dir)
        assert entries == []
        assert any("bom" in w.lower() for w in warnings)

    def test_load_deletions_skips_comments_and_blanks(self, template_dir: Path):
        """C7: # 行と空行は entries に含まれない。"""
        content = (
            "# comment\n"
            "\n"
            "agents/tdd-develop.md\n"
            "  # indented comment\n"
            "\n"
            "skills/foo/SKILL.md\n"
        )
        (template_dir / "deletions.txt").write_text(content, encoding="utf-8")
        entries, warnings = _load_deletions(template_dir)
        assert entries == ["agents/tdd-develop.md", "skills/foo/SKILL.md"]
        assert warnings == []

    def test_load_deletions_missing_file_returns_empty(self, template_dir: Path):
        """C8: deletions.txt が不在 → ([], [])。"""
        entries, warnings = _load_deletions(template_dir)
        assert entries == []
        assert warnings == []

    def test_load_deletions_dedupe_preserves_order(self, template_dir: Path):
        """C9: 同一パスが 2 回出ても 1 件に collapse、順序保持。"""
        content = (
            "agents/tdd-develop.md\n"
            "skills/foo/SKILL.md\n"
            "agents/tdd-develop.md\n"  # 重複
        )
        (template_dir / "deletions.txt").write_text(content, encoding="utf-8")
        entries, warnings = _load_deletions(template_dir)
        assert entries == ["agents/tdd-develop.md", "skills/foo/SKILL.md"]
        assert warnings == []

    def test_load_deletions_warns_on_ansi_escape(self, template_dir: Path):
        """C10: ANSI エスケープシーケンスを含むパスは warnings に記録され entries に入らない。"""
        ansi_path = "agents/\x1b[31mevil\x1b[0m.md"
        content = f"{ansi_path}\nagents/normal.md\n"
        (template_dir / "deletions.txt").write_text(content, encoding="utf-8")
        entries, warnings = _load_deletions(template_dir)
        # ANSI エスケープ付きパスは entries に含まれない
        assert not any("\x1b" in e for e in entries)
        # 通常パスは entries に含まれる
        assert "agents/normal.md" in entries
        # ANSI エスケープ警告が出る
        assert any("ANSI escape" in w for w in warnings)

    def test_load_deletions_warns_on_ansi_cursor_movement(self, template_dir: Path):
        """C11: SR-L-1 回帰防止: カーソル移動系 ANSI シーケンスも検出する。"""
        payloads = [
            "agents/\x1b[1Afoo.md",      # カーソル上
            "agents/\x1b[2Bbar.md",      # カーソル下
            "agents/\x1b[6nbaz.md",      # cursor position request
        ]
        (template_dir / "deletions.txt").write_text("\n".join(payloads), encoding="utf-8")
        entries, warnings = _load_deletions(template_dir)
        assert entries == []
        assert len(warnings) >= 3
        assert all("ANSI escape" in w for w in warnings)


# ---------------------------------------------------------------------------
# D: CLI 統合テスト (4 件)
# ---------------------------------------------------------------------------

class TestCLIIntegration:

    def _make_args(self, tmp_path: Path, dry_run: bool = False, yes: bool = False) -> argparse.Namespace:
        return argparse.Namespace(
            target=tmp_path,
            dry_run=dry_run,
            platform="claude",
            yes=yes,
        )

    def _setup_project(self, tmp_path: Path, deletions_entries: list[str] | None = None) -> tuple[Path, Path]:
        """tmp_path に .claude/ を作り、template ディレクトリを monkeypatch 用に返す。
        deletions_entries が指定されれば template/deletions.txt に書き込む。
        """
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        template_dir = tmp_path / "fake_template"
        template_dir.mkdir()
        if deletions_entries is not None:
            content = "\n".join(deletions_entries) + "\n"
            (template_dir / "deletions.txt").write_text(content, encoding="utf-8")
        return claude_dir, template_dir

    def test_cli_update_dry_run_lists_deletions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        """D1: c3 update --dry-run で stdout に 'would be removed' が含まれる。"""
        claude_dir, template_dir = self._setup_project(
            tmp_path, deletions_entries=["agents/tdd-develop.md"]
        )
        # 削除候補ファイルを実在させる
        (claude_dir / "agents").mkdir()
        (claude_dir / "agents" / "tdd-develop.md").write_text("old")

        monkeypatch.setattr("c3.cli_update.templates_dir", lambda: template_dir)
        monkeypatch.setattr("c3.cli_update._walk_diff", lambda t, d: [])

        args = self._make_args(tmp_path, dry_run=True)
        ret = handle(args)
        assert ret == 0
        captured = capsys.readouterr()
        assert "would be removed" in captured.out

    def test_cli_update_yes_flag_deletes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        """D2: c3 update --yes で削除実行・終了コード 0。"""
        claude_dir, template_dir = self._setup_project(
            tmp_path, deletions_entries=["agents/tdd-develop.md"]
        )
        target_file = claude_dir / "agents" / "tdd-develop.md"
        target_file.parent.mkdir()
        target_file.write_text("old")

        monkeypatch.setattr("c3.cli_update.templates_dir", lambda: template_dir)
        monkeypatch.setattr("c3.cli_update._walk_diff", lambda t, d: [])

        args = self._make_args(tmp_path, dry_run=False, yes=True)
        ret = handle(args)
        assert ret == 0
        assert not target_file.exists()

    def test_cli_update_prompt_n_does_not_delete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        """D3: input を mock した c3 update で n → ファイル残存。"""
        claude_dir, template_dir = self._setup_project(
            tmp_path, deletions_entries=["agents/tdd-develop.md"]
        )
        target_file = claude_dir / "agents" / "tdd-develop.md"
        target_file.parent.mkdir()
        target_file.write_text("old")

        monkeypatch.setattr("c3.cli_update.templates_dir", lambda: template_dir)
        monkeypatch.setattr("c3.cli_update._walk_diff", lambda t, d: [])
        monkeypatch.setattr("builtins.input", lambda _: "n")

        args = self._make_args(tmp_path, dry_run=False, yes=False)
        ret = handle(args)
        assert ret == 0
        assert target_file.exists(), "n を入力したのでファイルが残っているはず"

    def test_cli_update_no_deletions_file_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        """D4: _template/.claude/deletions.txt 不在でも c3 update が正常終了。"""
        claude_dir, template_dir = self._setup_project(
            tmp_path, deletions_entries=None  # deletions.txt を作らない
        )

        monkeypatch.setattr("c3.cli_update.templates_dir", lambda: template_dir)
        monkeypatch.setattr("c3.cli_update._walk_diff", lambda t, d: [])

        args = self._make_args(tmp_path, dry_run=False, yes=False)
        ret = handle(args)
        assert ret == 0
