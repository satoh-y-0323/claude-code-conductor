"""Tests for .claude/hooks/consolidate_memory.py

F-004: MemoryConsolidation MVP の検証。

テストケース:
 list_recent_session_files:
  1. 範囲内のファイルだけ古い順で返す
  2. 範囲外（古すぎ・未来日付）は除外
  3. ".tmp" 以外 / 日付として読めないファイルは無視
  4. ディレクトリが無ければ空リスト

 build_summary_markdown:
  5. 複数ファイルからセクションをマージ（重複行除去、出現順保持）
  6. 空セクションは "_該当エントリなし_" と表示
  7. ヘッダにウィンドウ日数 / ファイル数が反映される

 write_summary:
  8. 集約成功時は output_path にファイルを書く
  9. 対象ファイル無しなら False、ファイルは作らない

 main (E2E):
 10. Stop フック相当の呼び出しで consolidated_summary.md が生成される
 11. 失敗してもセッションを止めない（exit 0）
"""

from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "consolidate_memory.py"
SESSION_UTILS_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "session_utils.py"


def _load_hook_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("consolidate_memory_t", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


def _make_session(
    sessions_dir: Path,
    date_str: str,
    *,
    success: list[str] | None = None,
    failure: list[str] | None = None,
) -> Path:
    """ダミーの session .tmp を生成する。"""
    success_lines = success or []
    failure_lines = failure or []
    success_block = "\n".join(success_lines)
    failure_block = "\n".join(failure_lines)
    text = (
        f"SESSION: {date_str}\n"
        f"AGENT: \n"
        f"DURATION: \n"
        f"\n"
        f"## うまくいったアプローチ\n"
        f"{success_block}\n"
        f"\n"
        f"## 試みたが失敗したアプローチ\n"
        f"{failure_block}\n"
        f"\n"
        f"## 残タスク\n"
        f"\n"
        f"<!-- C3:SESSION:JSON\n"
        f"{{}}\n"
        f"-- >\n"
    )
    path = sessions_dir / f"{date_str}.tmp"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# list_recent_session_files
# ---------------------------------------------------------------------------


class TestListRecentSessionFiles:

    def test_returns_files_within_window(self, tmp_path: Path) -> None:
        """window_days=7, today=2026-05-08 なら 5/02 〜 5/08 が範囲。
        5/02 と 5/05 と 5/07 は範囲内、5/01 は範囲外。
        """
        mod = _load_hook_module()
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        _make_session(sessions, "20260502")
        _make_session(sessions, "20260505")
        _make_session(sessions, "20260507")

        files = mod.list_recent_session_files(
            str(sessions),
            window_days=7,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )
        assert len(files) == 3
        # 古い順
        assert files[0].endswith("20260502.tmp")
        assert files[-1].endswith("20260507.tmp")

    def test_excludes_out_of_window(self, tmp_path: Path) -> None:
        mod = _load_hook_module()
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        _make_session(sessions, "20260101")  # 古すぎ
        _make_session(sessions, "20260507")  # 範囲内
        _make_session(sessions, "20260601")  # 未来

        files = mod.list_recent_session_files(
            str(sessions),
            window_days=7,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )
        assert len(files) == 1
        assert files[0].endswith("20260507.tmp")

    def test_ignores_non_matching_files(self, tmp_path: Path) -> None:
        mod = _load_hook_module()
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        _make_session(sessions, "20260507")
        # noise
        (sessions / "README.md").write_text("x", encoding="utf-8")
        (sessions / "notadate.tmp").write_text("x", encoding="utf-8")
        (sessions / "20260507.txt").write_text("x", encoding="utf-8")

        files = mod.list_recent_session_files(
            str(sessions),
            window_days=7,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )
        assert len(files) == 1
        assert files[0].endswith("20260507.tmp")

    def test_missing_directory_returns_empty(self, tmp_path: Path) -> None:
        mod = _load_hook_module()
        files = mod.list_recent_session_files(
            str(tmp_path / "nonexistent"),
            window_days=7,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )
        assert files == []


# ---------------------------------------------------------------------------
# build_summary_markdown
# ---------------------------------------------------------------------------


class TestBuildSummaryMarkdown:

    def test_merges_sections_dedup_preserve_order(self, tmp_path: Path) -> None:
        mod = _load_hook_module()
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        _make_session(sessions, "20260505",
                      success=["- approach A worked", "- approach B worked"])
        _make_session(sessions, "20260507",
                      success=["- approach B worked", "- approach C worked"])

        files = mod.list_recent_session_files(
            str(sessions),
            window_days=7,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )

        # session_utils を直接読み込み
        spec = importlib.util.spec_from_file_location("su_t", SESSION_UTILS_PATH)
        assert spec is not None and spec.loader is not None
        su = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(su)  # type: ignore[attr-defined]

        summary = mod.build_summary_markdown(
            files,
            window_days=7,
            extract_fn=su.extract_section,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )
        # 重複は 1 回だけ
        assert summary.count("- approach B worked") == 1
        # 出現順保持: A → B → C
        a_pos = summary.index("- approach A worked")
        b_pos = summary.index("- approach B worked")
        c_pos = summary.index("- approach C worked")
        assert a_pos < b_pos < c_pos

    def test_empty_section_shows_placeholder(self, tmp_path: Path) -> None:
        mod = _load_hook_module()
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        _make_session(sessions, "20260507")  # 全セクション空

        files = mod.list_recent_session_files(
            str(sessions),
            window_days=7,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )

        spec = importlib.util.spec_from_file_location("su_t2", SESSION_UTILS_PATH)
        assert spec is not None and spec.loader is not None
        su = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(su)  # type: ignore[attr-defined]

        summary = mod.build_summary_markdown(
            files,
            window_days=7,
            extract_fn=su.extract_section,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )
        assert "_該当エントリなし_" in summary

    def test_header_reflects_window_and_count(self, tmp_path: Path) -> None:
        mod = _load_hook_module()
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        _make_session(sessions, "20260507")
        _make_session(sessions, "20260508")

        files = mod.list_recent_session_files(
            str(sessions),
            window_days=7,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )

        spec = importlib.util.spec_from_file_location("su_t3", SESSION_UTILS_PATH)
        assert spec is not None and spec.loader is not None
        su = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(su)  # type: ignore[attr-defined]

        summary = mod.build_summary_markdown(
            files,
            window_days=7,
            extract_fn=su.extract_section,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )
        assert "直近 7 日" in summary
        assert "session ファイル 2 件" in summary


# ---------------------------------------------------------------------------
# write_summary
# ---------------------------------------------------------------------------


class TestWriteSummary:

    def test_writes_output_when_files_exist(self, tmp_path: Path) -> None:
        mod = _load_hook_module()
        sessions = tmp_path / "memory" / "sessions"
        sessions.mkdir(parents=True)
        _make_session(sessions, "20260507", success=["- foo"])

        output = tmp_path / "memory" / "consolidated_summary.md"

        ok = mod.write_summary(
            str(output),
            sessions_dir=str(sessions),
            window_days=7,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )
        assert ok is True
        assert output.is_file()
        text = output.read_text(encoding="utf-8")
        assert "# 集約サマリ" in text
        assert "- foo" in text

    def test_returns_false_when_no_sessions(self, tmp_path: Path) -> None:
        mod = _load_hook_module()
        sessions = tmp_path / "memory" / "sessions"
        sessions.mkdir(parents=True)
        # ファイル無し

        output = tmp_path / "memory" / "consolidated_summary.md"
        ok = mod.write_summary(
            str(output),
            sessions_dir=str(sessions),
            window_days=7,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )
        assert ok is False
        assert not output.exists()


# ---------------------------------------------------------------------------
# main (E2E)
# ---------------------------------------------------------------------------


class TestMainE2E:

    def test_main_returns_zero_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """write_summary が例外を投げても exit 0 でセッションを止めない。"""
        mod = _load_hook_module()

        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(mod, "write_summary", boom)
        monkeypatch.setattr(sys, "stdin", _StubStdin())

        rc = mod.main()
        assert rc == 0


class _StubStdin:
    def read(self):  # noqa: D401
        return ""
