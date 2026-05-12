"""Tests for .claude/hooks/consolidate_memory.py

memory-consolidation: MemoryConsolidation MVP の検証。

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
import os
import sys
import time
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


# ---------------------------------------------------------------------------
# memory-consolidation Phase 2-A: archive_old_sessions
# ---------------------------------------------------------------------------


class TestArchive:
    """`archive_old_sessions()` の検証。

    21 日超 (DEFAULT_ARCHIVE_TTL_DAYS) の session.tmp を archive/ へ移動する。
    """

    def test_archives_files_older_than_ttl(self, tmp_path: Path) -> None:
        """today=2026-05-09 / ttl=21 → 04-18 以前は archive/ へ移動。

        2026-05-09 - 21 日 = 2026-04-18。04-17 (22 日前) は archive 対象、
        04-18 (21 日前) も archive 対象（>= ttl_days で判定）。
        """
        mod = _load_hook_module()
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        archive = tmp_path / "archive"

        # 範囲外（archive 対象）
        _make_session(sessions, "20260417")
        _make_session(sessions, "20260418")
        # 範囲内（残す）
        _make_session(sessions, "20260419")
        _make_session(sessions, "20260509")

        moved = mod.archive_old_sessions(
            str(sessions),
            str(archive),
            ttl_days=21,
            today=datetime(2026, 5, 9, tzinfo=timezone.utc),
        )

        # 2 件移動
        assert len(moved) == 2
        assert (archive / "20260417.tmp").is_file()
        assert (archive / "20260418.tmp").is_file()
        assert not (sessions / "20260417.tmp").exists()
        assert not (sessions / "20260418.tmp").exists()

    def test_keeps_recent_files(self, tmp_path: Path) -> None:
        """ttl 以内のファイルは sessions/ に残る（archive 対象外）。"""
        mod = _load_hook_module()
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        archive = tmp_path / "archive"

        _make_session(sessions, "20260420")  # 19 日前
        _make_session(sessions, "20260509")  # 当日

        moved = mod.archive_old_sessions(
            str(sessions),
            str(archive),
            ttl_days=21,
            today=datetime(2026, 5, 9, tzinfo=timezone.utc),
        )

        assert moved == []
        assert (sessions / "20260420.tmp").is_file()
        assert (sessions / "20260509.tmp").is_file()
        # archive ディレクトリは作られても良いが、中身は空
        if archive.exists():
            assert list(archive.glob("*.tmp")) == []

    def test_handles_filename_collision(self, tmp_path: Path) -> None:
        """archive/ に同名既存 → YYYYMMDD-1.tmp に rename。"""
        mod = _load_hook_module()
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        archive = tmp_path / "archive"
        archive.mkdir()

        # 既存 archive ファイル（衝突源）
        (archive / "20260417.tmp").write_text("existing", encoding="utf-8")

        # 移動対象（同名）
        _make_session(sessions, "20260417")

        moved = mod.archive_old_sessions(
            str(sessions),
            str(archive),
            ttl_days=21,
            today=datetime(2026, 5, 9, tzinfo=timezone.utc),
        )

        assert len(moved) == 1
        # 既存の 20260417.tmp は維持
        assert (archive / "20260417.tmp").read_text(encoding="utf-8") == "existing"
        # 新ファイルは -1 suffix で別名保存
        assert (archive / "20260417-1.tmp").is_file()

    def test_continues_on_individual_move_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """shutil.move の 1 件目が OSError でも 2 件目は処理継続。"""
        mod = _load_hook_module()
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        archive = tmp_path / "archive"

        _make_session(sessions, "20260417")
        _make_session(sessions, "20260418")

        import shutil
        original_move = shutil.move
        call_count = {"n": 0}

        def fake_move(src, dst, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise OSError("simulated failure")
            return original_move(src, dst, *args, **kwargs)

        monkeypatch.setattr(mod.shutil, "move", fake_move)

        moved = mod.archive_old_sessions(
            str(sessions),
            str(archive),
            ttl_days=21,
            today=datetime(2026, 5, 9, tzinfo=timezone.utc),
        )

        # 2 件目だけ成功
        assert len(moved) == 1
        # os.listdir の順序は OS 依存で非決定的なため、どちらが移動成功したかは
        # 特定せず件数のみ検証する。重要なのは「1 件目が失敗しても 2 件目を継続実行する」こと。
        archived_files = sorted(p.name for p in archive.glob("*.tmp"))
        assert len(archived_files) == 1

    def test_creates_archive_dir_if_missing(self, tmp_path: Path) -> None:
        """archive/ ディレクトリが無ければ自動生成する。"""
        mod = _load_hook_module()
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        archive = tmp_path / "nonexistent_archive"
        # archive ディレクトリは事前に作らない
        assert not archive.exists()

        _make_session(sessions, "20260417")

        moved = mod.archive_old_sessions(
            str(sessions),
            str(archive),
            ttl_days=21,
            today=datetime(2026, 5, 9, tzinfo=timezone.utc),
        )

        assert len(moved) == 1
        assert archive.is_dir()
        assert (archive / "20260417.tmp").is_file()


# ---------------------------------------------------------------------------
# memory-consolidation Phase 2-B: 半自動 promotion 候補ログ
# ---------------------------------------------------------------------------


import json


def _write_patterns_json(path: Path, patterns: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"patterns": patterns}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


class TestPromotionCandidates:
    """`build_promotion_candidates_section()` / `write_promotion_candidates_log()` の検証。"""

    def test_extracts_candidates_from_patterns_json(self, tmp_path: Path) -> None:
        """promotion_candidate=true & promoted=false のパターンだけ抽出される。"""
        mod = _load_hook_module()
        patterns_path = tmp_path / "patterns.json"
        _write_patterns_json(patterns_path, [
            {"id": "p1", "description": "d1", "trust_score": 0.9,
             "promotion_candidate": True, "observations": [{"date": "20260501"}],
             "registered_date": "20260501"},
            {"id": "p2", "description": "d2", "trust_score": 0.5,
             "promotion_candidate": False, "observations": [{"date": "20260501"}],
             "registered_date": "20260501"},
            {"id": "p3", "description": "d3", "trust_score": 0.95,
             "promotion_candidate": True, "observations": [{"date": "20260502"}],
             "registered_date": "20260502"},
        ])

        section, candidates = mod.build_promotion_candidates_section(
            str(patterns_path),
            today=datetime(2026, 5, 9, tzinfo=timezone.utc),
        )
        ids = [c["id"] for c in candidates]
        assert ids == ["p1", "p3"]
        assert "## 昇格候補" in section
        assert "p1" in section
        assert "p3" in section
        assert "p2" not in section

    def test_excludes_already_promoted(self, tmp_path: Path) -> None:
        """promotion_candidate=true でも promoted=true は除外。"""
        mod = _load_hook_module()
        patterns_path = tmp_path / "patterns.json"
        _write_patterns_json(patterns_path, [
            {"id": "p1", "description": "d1", "trust_score": 0.9,
             "promotion_candidate": True, "promoted": True,
             "observations": [{"date": "20260501"}],
             "registered_date": "20260501"},
            {"id": "p2", "description": "d2", "trust_score": 0.9,
             "promotion_candidate": True,
             "observations": [{"date": "20260501"}],
             "registered_date": "20260501"},
        ])

        _, candidates = mod.build_promotion_candidates_section(
            str(patterns_path),
            today=datetime(2026, 5, 9, tzinfo=timezone.utc),
        )
        ids = [c["id"] for c in candidates]
        assert ids == ["p2"]

    def test_writes_no_candidates_message_when_empty(self, tmp_path: Path) -> None:
        """候補 0 件でもファイルは出力され、「候補なし」表記が含まれる。"""
        mod = _load_hook_module()
        patterns_path = tmp_path / "patterns.json"
        _write_patterns_json(patterns_path, [
            {"id": "p1", "description": "d1", "trust_score": 0.5,
             "promotion_candidate": False,
             "observations": [{"date": "20260501"}],
             "registered_date": "20260501"},
        ])
        output_path = tmp_path / "promotion-candidates.md"

        ok = mod.write_promotion_candidates_log(
            [], str(output_path),
            today=datetime(2026, 5, 9, tzinfo=timezone.utc),
        )
        assert ok is True
        assert output_path.is_file()
        text = output_path.read_text(encoding="utf-8")
        # 「候補なし」を示す表記が含まれる
        assert "候補なし" in text or "候補数: 0" in text

    def test_consolidated_summary_includes_promotion_section(
        self, tmp_path: Path
    ) -> None:
        """consolidated_summary.md の末尾に「## 昇格候補」サマリが追加される。"""
        mod = _load_hook_module()
        sessions = tmp_path / "memory" / "sessions"
        sessions.mkdir(parents=True)
        _make_session(sessions, "20260507", success=["- foo"])

        patterns_path = tmp_path / "memory" / "patterns.json"
        _write_patterns_json(patterns_path, [
            {"id": "candidate_a", "description": "desc A", "trust_score": 0.9,
             "promotion_candidate": True,
             "observations": [{"date": "20260501"}],
             "registered_date": "20260501"},
        ])

        output = tmp_path / "memory" / "consolidated_summary.md"
        ok = mod.write_summary(
            str(output),
            sessions_dir=str(sessions),
            window_days=7,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
            patterns_path=str(patterns_path),
        )
        assert ok is True
        text = output.read_text(encoding="utf-8")
        assert "## 昇格候補" in text
        assert "candidate_a" in text

    def test_promotion_candidates_md_table_escapes_pipe_in_description(
        self, tmp_path: Path
    ) -> None:
        """description に `|` を含む場合、Markdown 表セル内でエスケープされる。"""
        mod = _load_hook_module()
        patterns_path = tmp_path / "patterns.json"
        _write_patterns_json(patterns_path, [
            {"id": "pipe_in_desc",
             "description": "before|after pipe",
             "trust_score": 0.9,
             "promotion_candidate": True,
             "observations": [{"date": "20260501"}],
             "registered_date": "20260501"},
        ])
        output_path = tmp_path / "promotion-candidates.md"

        _, candidates = mod.build_promotion_candidates_section(
            str(patterns_path),
            today=datetime(2026, 5, 9, tzinfo=timezone.utc),
        )
        ok = mod.write_promotion_candidates_log(
            candidates, str(output_path),
            today=datetime(2026, 5, 9, tzinfo=timezone.utc),
        )
        assert ok is True
        text = output_path.read_text(encoding="utf-8")
        # 表セル内では `|` を `\|` にエスケープ
        assert r"before\|after pipe" in text


# ---------------------------------------------------------------------------
# memory-consolidation Phase 2-C: claude --headless LLM 要約
# ---------------------------------------------------------------------------


class TestLLMSummary:
    """`build_llm_summary_section()` の検証。subprocess.run / shutil.which を mock 化。"""

    def _make_files_for_summary(self, tmp_path: Path) -> list[str]:
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        _make_session(sessions, "20260507", success=["- foo"], failure=["- bar"])
        mod = _load_hook_module()
        return mod.list_recent_session_files(
            str(sessions),
            window_days=7,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )

    def test_skipped_when_cli_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """shutil.which が None を返す → build_llm_summary_section は None。"""
        mod = _load_hook_module()
        files = self._make_files_for_summary(tmp_path)

        monkeypatch.setattr(mod.shutil, "which", lambda _: None)

        result = mod.build_llm_summary_section(
            files,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )
        assert result is None

    def test_skipped_on_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """subprocess.run が TimeoutExpired を投げる → None。"""
        import subprocess as sp
        mod = _load_hook_module()
        files = self._make_files_for_summary(tmp_path)

        monkeypatch.setattr(mod.shutil, "which", lambda _: "/fake/claude")

        def fake_run(*args, **kwargs):
            raise sp.TimeoutExpired(cmd=args[0], timeout=60)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        result = mod.build_llm_summary_section(
            files,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )
        assert result is None

    def test_skipped_on_nonzero_returncode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """returncode != 0 → None。"""
        mod = _load_hook_module()
        files = self._make_files_for_summary(tmp_path)

        monkeypatch.setattr(mod.shutil, "which", lambda _: "/fake/claude")

        class _FakeResult:
            returncode = 1
            stdout = "Error: something"
            stderr = "boom"

        def fake_run(*args, **kwargs):
            return _FakeResult()

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        result = mod.build_llm_summary_section(
            files,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )
        assert result is None

    def test_truncates_oversized_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """stdout が出力上限を超えた場合は切り詰めマーカーが付与される。"""
        mod = _load_hook_module()
        files = self._make_files_for_summary(tmp_path)

        monkeypatch.setattr(mod.shutil, "which", lambda _: "/fake/claude")

        oversized = "- " + ("x" * 5000)  # 5000 文字超

        class _FakeResult:
            returncode = 0
            stdout = oversized
            stderr = ""

        monkeypatch.setattr(mod.subprocess, "run",
                            lambda *a, **k: _FakeResult())

        result = mod.build_llm_summary_section(
            files,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )
        assert result is not None
        # 「## LLM 要約」見出しと切り詰めマーカーが含まれる
        assert "## LLM 要約" in result
        assert "切り詰め" in result or "truncated" in result
        # 全体が制御サイズ内に収まっている
        assert len(result) < 5500  # 4000 + ヘッダ + マーカー余裕

    def test_recursive_depth_guard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """env に C3_CONSOLIDATE_LLM_DEPTH=1 → subprocess.run 未呼び出しで None。"""
        mod = _load_hook_module()
        files = self._make_files_for_summary(tmp_path)

        monkeypatch.setenv("C3_CONSOLIDATE_LLM_DEPTH", "1")
        monkeypatch.setattr(mod.shutil, "which", lambda _: "/fake/claude")

        called = {"n": 0}

        def fake_run(*args, **kwargs):
            called["n"] += 1
            raise AssertionError("subprocess.run should NOT be called when depth>=1")

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        result = mod.build_llm_summary_section(
            files,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )
        assert result is None
        assert called["n"] == 0

    def test_claude_subprocess_uses_create_no_window_on_windows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """build_llm_summary_section が Windows で claude.exe を呼ぶ際に CREATE_NO_WINDOW を指定する。

        DETACHED 系で起動された python から console アプリ (claude.exe) を呼ぶと
        Windows が新コンソールを割り当てて可視化する問題への対策。
        """
        mod = _load_hook_module()
        files = self._make_files_for_summary(tmp_path)

        monkeypatch.setattr(mod.shutil, "which", lambda _: "C:/fake/claude.exe")
        # 再帰防止フラグはクリア
        monkeypatch.delenv("C3_CONSOLIDATE_LLM_DEPTH", raising=False)
        # platform を win32 とみなして実行（実環境が win 以外でも検証可能にする）
        monkeypatch.setattr(mod.sys, "platform", "win32")

        recorded: dict = {}

        class _FakeResult:
            returncode = 0
            stdout = "## summary body"
            stderr = ""

        def fake_run(args, **kwargs):
            recorded["kwargs"] = kwargs
            return _FakeResult()

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        mod.build_llm_summary_section(
            files,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )

        CREATE_NO_WINDOW = 0x08000000
        flags = recorded["kwargs"].get("creationflags", 0)
        assert flags & CREATE_NO_WINDOW, (
            "Windows では claude subprocess に CREATE_NO_WINDOW を指定する必要がある"
        )

    def test_claude_subprocess_no_creationflags_on_unix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unix 系では claude subprocess に creationflags を指定しない。"""
        mod = _load_hook_module()
        files = self._make_files_for_summary(tmp_path)

        monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/local/bin/claude")
        monkeypatch.delenv("C3_CONSOLIDATE_LLM_DEPTH", raising=False)
        monkeypatch.setattr(mod.sys, "platform", "linux")

        recorded: dict = {}

        class _FakeResult:
            returncode = 0
            stdout = "## summary body"
            stderr = ""

        def fake_run(args, **kwargs):
            recorded["kwargs"] = kwargs
            return _FakeResult()

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        mod.build_llm_summary_section(
            files,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )

        assert "creationflags" not in recorded["kwargs"], (
            "Unix では creationflags を指定してはならない（Windows 専用フラグ）"
        )


class TestMainE2EExtended:
    """main() の Phase 2 拡張全体の E2E 検証。"""

    def test_main_runs_all_three_extensions_in_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MVP → LLM → promotion → archive の順で各ステップが呼ばれる。"""
        mod = _load_hook_module()

        call_order: list[str] = []

        def fake_write_summary(*a, **kw):
            call_order.append("write_summary")
            return True

        def fake_build_llm(*a, **kw):
            call_order.append("build_llm")
            return None  # スキップ扱い (副作用なし)

        def fake_build_promotion(*a, **kw):
            call_order.append("build_promotion")
            return ("", [])

        def fake_write_promotion(*a, **kw):
            call_order.append("write_promotion")
            return True

        def fake_archive(*a, **kw):
            call_order.append("archive")
            return []

        monkeypatch.setattr(mod, "write_summary", fake_write_summary)
        monkeypatch.setattr(mod, "build_llm_summary_section", fake_build_llm)
        monkeypatch.setattr(mod, "build_promotion_candidates_section",
                            fake_build_promotion)
        monkeypatch.setattr(mod, "write_promotion_candidates_log",
                            fake_write_promotion)
        monkeypatch.setattr(mod, "archive_old_sessions", fake_archive)
        monkeypatch.setattr(sys, "stdin", _StubStdin())

        rc = mod.main()
        assert rc == 0
        # write_summary は LLM 要約セクションも呼ぶ可能性があるが、
        # main() の直接呼び出し順としては summary → promotion → archive
        assert call_order.index("write_summary") < call_order.index("build_promotion")
        assert call_order.index("build_promotion") < call_order.index("archive")

    def test_main_partial_failure_continues(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LLM ステップ (write_summary 内) が例外を投げても archive は実行される。"""
        mod = _load_hook_module()

        archive_called = {"n": 0}

        def boom_summary(*a, **kw):
            raise RuntimeError("summary boom")

        def fake_archive(*a, **kw):
            archive_called["n"] += 1
            return []

        monkeypatch.setattr(mod, "write_summary", boom_summary)
        monkeypatch.setattr(mod, "archive_old_sessions", fake_archive)
        monkeypatch.setattr(sys, "stdin", _StubStdin())

        rc = mod.main()
        assert rc == 0
        assert archive_called["n"] == 1


# ---------------------------------------------------------------------------
# Stop hook 軽量化（LLM 要約のバックグラウンド化）
# ---------------------------------------------------------------------------


class TestFullSyncMain:
    """`_full_sync_main()` が同期処理のみで完了し、LLM をデタッチ起動する検証。"""

    def test_full_sync_main_uses_enable_llm_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """同期フェーズで write_summary は enable_llm=False で呼ばれる。"""
        mod = _load_hook_module()

        recorded: dict = {}

        def fake_write_summary(*a, **kw):
            recorded["enable_llm"] = kw.get("enable_llm")
            return True

        def fake_promotion(*a, **kw):
            return ("", [])

        monkeypatch.setattr(mod, "write_summary", fake_write_summary)
        monkeypatch.setattr(mod, "build_promotion_candidates_section", fake_promotion)
        monkeypatch.setattr(mod, "write_promotion_candidates_log",
                            lambda *a, **kw: True)
        monkeypatch.setattr(mod, "archive_old_sessions", lambda *a, **kw: [])
        monkeypatch.setattr(mod, "_spawn_detached_llm", lambda *a, **kw: None)
        monkeypatch.setattr(sys, "stdin", _StubStdin())

        rc = mod._full_sync_main()
        assert rc == 0
        assert recorded.get("enable_llm") is False, (
            "_full_sync_main は LLM を呼び出さないため enable_llm=False で write_summary を呼ぶこと"
        )

    def test_full_sync_main_spawns_detached_llm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_full_sync_main の最後に _spawn_detached_llm が呼ばれる。"""
        mod = _load_hook_module()

        spawned: dict = {"called": False, "today_iso": None}

        def fake_spawn(today_iso: str) -> None:
            spawned["called"] = True
            spawned["today_iso"] = today_iso

        monkeypatch.setattr(mod, "write_summary", lambda *a, **kw: True)
        monkeypatch.setattr(mod, "build_promotion_candidates_section",
                            lambda *a, **kw: ("", []))
        monkeypatch.setattr(mod, "write_promotion_candidates_log",
                            lambda *a, **kw: True)
        monkeypatch.setattr(mod, "archive_old_sessions", lambda *a, **kw: [])
        monkeypatch.setattr(mod, "_spawn_detached_llm", fake_spawn)
        monkeypatch.setattr(sys, "stdin", _StubStdin())

        rc = mod._full_sync_main()
        assert rc == 0
        assert spawned["called"] is True, "_spawn_detached_llm が呼ばれていません"
        # ISO 形式の日時が渡されること
        assert spawned["today_iso"] is not None
        # parse できることを確認（fromisoformat が成功すれば OK）
        datetime.fromisoformat(spawned["today_iso"])

    def test_full_sync_main_does_not_wait_for_llm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_spawn_detached_llm が Popen を呼んでも親は wait しない（即返却）。"""
        mod = _load_hook_module()

        popen_kwargs_recorded: dict = {}

        class FakePopen:
            def __init__(self, args, **kwargs):
                popen_kwargs_recorded["args"] = args
                popen_kwargs_recorded["kwargs"] = kwargs
                self.waited = False

            def wait(self, *a, **kw):
                self.waited = True

        monkeypatch.setattr(mod, "write_summary", lambda *a, **kw: True)
        monkeypatch.setattr(mod, "build_promotion_candidates_section",
                            lambda *a, **kw: ("", []))
        monkeypatch.setattr(mod, "write_promotion_candidates_log",
                            lambda *a, **kw: True)
        monkeypatch.setattr(mod, "archive_old_sessions", lambda *a, **kw: [])
        monkeypatch.setattr(mod.subprocess, "Popen", FakePopen)
        monkeypatch.setattr(sys, "stdin", _StubStdin())

        rc = mod._full_sync_main()
        assert rc == 0
        # Popen が呼ばれていること
        assert popen_kwargs_recorded.get("args") is not None
        # 引数に --llm-only が含まれていること
        assert "--llm-only" in popen_kwargs_recorded["args"]


class TestSpawnDetachedLLM:
    """`_spawn_detached_llm()` のプラットフォーム別 kwargs 検証。"""

    def test_spawn_uses_devnull_stdio(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """子プロセスの stdin/stdout/stderr が DEVNULL になる。"""
        mod = _load_hook_module()
        recorded: dict = {}

        def fake_popen(args, **kwargs):
            recorded["args"] = args
            recorded["kwargs"] = kwargs

            class _Stub:
                pass
            return _Stub()

        monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)

        mod._spawn_detached_llm("2026-05-10T12:00:00+00:00")

        kwargs = recorded["kwargs"]
        import subprocess as sp
        assert kwargs["stdin"] == sp.DEVNULL
        assert kwargs["stdout"] == sp.DEVNULL
        assert kwargs["stderr"] == sp.DEVNULL
        assert kwargs.get("close_fds") is True

    def test_spawn_uses_detach_flags_per_platform(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows: creationflags が DETACHED_PROCESS を含む。Unix: start_new_session=True。"""
        mod = _load_hook_module()
        recorded: dict = {}

        def fake_popen(args, **kwargs):
            recorded["kwargs"] = kwargs

            class _Stub:
                pass
            return _Stub()

        monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)

        mod._spawn_detached_llm("2026-05-10T12:00:00+00:00")
        kwargs = recorded["kwargs"]

        if sys.platform == "win32":
            # CREATE_NO_WINDOW を使う（DETACHED_PROCESS だと子が更に呼ぶ
            # console アプリで新ウィンドウが可視化されるため）
            CREATE_NO_WINDOW = 0x08000000
            DETACHED_PROCESS = 0x00000008
            assert "creationflags" in kwargs
            assert kwargs["creationflags"] & CREATE_NO_WINDOW, (
                "Windows では CREATE_NO_WINDOW フラグが必要"
            )
            assert not (kwargs["creationflags"] & DETACHED_PROCESS), (
                "DETACHED_PROCESS は CREATE_NO_WINDOW と排他なので指定してはならない"
            )
        else:
            assert kwargs.get("start_new_session") is True, (
                "Unix では start_new_session=True が必要"
            )

    def test_spawn_propagates_today_iso(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """argv に渡された today_iso が子プロセス引数に含まれる。"""
        mod = _load_hook_module()
        recorded: dict = {}

        def fake_popen(args, **kwargs):
            recorded["args"] = args

            class _Stub:
                pass
            return _Stub()

        monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)

        today_iso = "2026-05-10T12:34:56+00:00"
        mod._spawn_detached_llm(today_iso)

        assert today_iso in recorded["args"], (
            f"args に today_iso が含まれていません: {recorded['args']}"
        )

    def test_spawn_swallows_oserror(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Popen が OSError を投げても親はクラッシュしない（exit 0 維持）。"""
        mod = _load_hook_module()

        def boom(*a, **kw):
            raise OSError("simulated spawn failure")

        monkeypatch.setattr(mod.subprocess, "Popen", boom)

        # 例外を投げずに完了すること
        mod._spawn_detached_llm("2026-05-10T12:00:00+00:00")
        captured = capsys.readouterr()
        assert "detach spawn failed" in captured.err


class TestLLMLock:
    """LLM 子プロセスのロック機構検証。"""

    def test_acquire_creates_lock_file(self, tmp_path: Path) -> None:
        """ロック取得時にロックファイルが作成される。"""
        mod = _load_hook_module()
        lock_path = str(tmp_path / "test.lock")

        assert mod._acquire_llm_lock(lock_path) is True
        assert Path(lock_path).is_file()

    def test_acquire_fails_when_fresh_lock_exists(
        self, tmp_path: Path
    ) -> None:
        """新鮮なロックがある場合 False を返す。"""
        mod = _load_hook_module()
        lock_path = tmp_path / "test.lock"
        # 新鮮なロックを作る
        lock_path.write_text("123\n0.0", encoding="utf-8")
        # mtime を現在に更新
        os.utime(lock_path, None)

        assert mod._acquire_llm_lock(str(lock_path)) is False

    def test_acquire_breaks_stale_lock(self, tmp_path: Path) -> None:
        """stale なロック（LOCK_STALE_SEC 超過）は破棄して取得できる。"""
        mod = _load_hook_module()
        lock_path = tmp_path / "test.lock"
        lock_path.write_text("123\n0.0", encoding="utf-8")
        # mtime を LOCK_STALE_SEC + 10 秒前に設定
        old_time = time.time() - (mod.LOCK_STALE_SEC + 10)
        os.utime(lock_path, (old_time, old_time))

        assert mod._acquire_llm_lock(str(lock_path)) is True

    def test_release_removes_lock_file(self, tmp_path: Path) -> None:
        """ロック解放時にロックファイルが削除される。"""
        mod = _load_hook_module()
        lock_path = str(tmp_path / "test.lock")
        mod._acquire_llm_lock(lock_path)
        assert Path(lock_path).is_file()

        mod._release_llm_lock(lock_path)
        assert not Path(lock_path).is_file()

    def test_release_handles_missing_file(self, tmp_path: Path) -> None:
        """ロックファイルが存在しなくても release はエラーを投げない。"""
        mod = _load_hook_module()
        lock_path = str(tmp_path / "nonexistent.lock")
        # 例外なく完了すること
        mod._release_llm_lock(lock_path)


class TestLLMOnlyMain:
    """`--llm-only` モードの検証。"""

    def test_llm_only_main_calls_write_summary_with_llm_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_llm_only_main は enable_llm=True で write_summary を呼ぶ。"""
        mod = _load_hook_module()
        lock_path = str(tmp_path / "test.lock")
        monkeypatch.setattr(mod, "LOCK_PATH", lock_path)

        recorded: dict = {}

        def fake_write_summary(*a, **kw):
            recorded["enable_llm"] = kw.get("enable_llm")
            return True

        monkeypatch.setattr(mod, "write_summary", fake_write_summary)
        monkeypatch.setattr(sys, "argv",
                            ["prog", "--llm-only", "2026-05-10T12:00:00+00:00"])

        rc = mod._llm_only_main()
        assert rc == 0
        assert recorded.get("enable_llm") is True, (
            "_llm_only_main は LLM を呼ぶため enable_llm=True で write_summary を呼ぶこと"
        )
        # ロックは解放されていること
        assert not Path(lock_path).is_file()

    def test_llm_only_main_skips_when_lock_held(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """新鮮なロックがある場合 write_summary は呼ばれない。"""
        mod = _load_hook_module()
        lock_path = tmp_path / "test.lock"
        # 既存の新鮮なロックを設置
        lock_path.write_text("999\n0.0", encoding="utf-8")
        os.utime(lock_path, None)

        monkeypatch.setattr(mod, "LOCK_PATH", str(lock_path))

        called = {"n": 0}

        def fake_write_summary(*a, **kw):
            called["n"] += 1
            return True

        monkeypatch.setattr(mod, "write_summary", fake_write_summary)
        monkeypatch.setattr(sys, "argv",
                            ["prog", "--llm-only", "2026-05-10T12:00:00+00:00"])

        rc = mod._llm_only_main()
        assert rc == 0
        assert called["n"] == 0, "ロック保持中は write_summary が呼ばれないこと"
        # 既存ロックは保護される（解放されない）
        assert lock_path.is_file()

    def test_llm_only_main_releases_lock_on_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """write_summary が例外を投げても finally でロックが解放される。"""
        mod = _load_hook_module()
        lock_path = str(tmp_path / "test.lock")
        monkeypatch.setattr(mod, "LOCK_PATH", lock_path)

        def boom(*a, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(mod, "write_summary", boom)
        monkeypatch.setattr(sys, "argv",
                            ["prog", "--llm-only", "2026-05-10T12:00:00+00:00"])

        rc = mod._llm_only_main()
        assert rc == 0  # 例外を握りつぶして 0 を返す
        # ロックは解放されている
        assert not Path(lock_path).is_file()


class TestParseTodayArg:
    """`_parse_today_arg()` の検証。"""

    def test_parses_iso_with_tz(self) -> None:
        mod = _load_hook_module()
        argv = ["prog", "--llm-only", "2026-05-10T12:34:56+00:00"]
        result = mod._parse_today_arg(argv)
        assert result == datetime(2026, 5, 10, 12, 34, 56, tzinfo=timezone.utc)

    def test_parses_iso_without_tz_assumes_utc(self) -> None:
        mod = _load_hook_module()
        argv = ["prog", "--llm-only", "2026-05-10T12:34:56"]
        result = mod._parse_today_arg(argv)
        assert result.tzinfo is not None
        assert result.tzinfo.utcoffset(None) == timedelta(0)

    def test_falls_back_to_now_on_invalid(self) -> None:
        mod = _load_hook_module()
        argv = ["prog", "--llm-only", "garbage"]
        result = mod._parse_today_arg(argv)
        # now に近い値が返ることを確認
        assert isinstance(result, datetime)
        assert (datetime.now(timezone.utc) - result).total_seconds() < 5

    def test_falls_back_to_now_when_flag_missing(self) -> None:
        mod = _load_hook_module()
        argv = ["prog"]
        result = mod._parse_today_arg(argv)
        assert isinstance(result, datetime)


class TestMainDispatch:
    """`main()` が argv に応じて適切なエントリへ分岐する検証。"""

    def test_main_dispatches_to_llm_only_when_flag_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _load_hook_module()
        called = {"llm_only": False, "full_sync": False}

        monkeypatch.setattr(mod, "_llm_only_main",
                            lambda: called.update(llm_only=True) or 0)
        monkeypatch.setattr(mod, "_full_sync_main",
                            lambda: called.update(full_sync=True) or 0)
        monkeypatch.setattr(sys, "argv",
                            ["prog", "--llm-only", "2026-05-10T12:00:00+00:00"])

        rc = mod.main()
        assert rc == 0
        assert called["llm_only"] is True
        assert called["full_sync"] is False

    def test_main_dispatches_to_full_sync_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _load_hook_module()
        called = {"llm_only": False, "full_sync": False}

        monkeypatch.setattr(mod, "_llm_only_main",
                            lambda: called.update(llm_only=True) or 0)
        monkeypatch.setattr(mod, "_full_sync_main",
                            lambda: called.update(full_sync=True) or 0)
        monkeypatch.setattr(sys, "argv", ["prog"])

        rc = mod.main()
        assert rc == 0
        assert called["full_sync"] is True
        assert called["llm_only"] is False


# ---------------------------------------------------------------------------
# memory-consolidation 消費側: LLM 要約抽出 + プレースホルダ
# ---------------------------------------------------------------------------


def _make_summary_with_all_sections() -> str:
    """MVP / LLM 要約 / 昇格候補 の 3 セクションを含むダミー summary を返す。"""
    return (
        "# 集約サマリ\n"
        "\n"
        "_直近 7 日のマージ_\n"
        "\n"
        "## うまくいったアプローチ\n"
        "\n"
        "- 成功 1\n"
        "- 成功 2\n"
        "\n"
        "## 試みたが失敗したアプローチ\n"
        "\n"
        "- 失敗 1\n"
        "\n"
        "## LLM 要約\n"
        "\n"
        "_生成: 2026-05-10T12:00:00+00:00 / model: claude (CLI default)_\n"
        "\n"
        "### 主要な傾向\n"
        "- バックグラウンド化が体感に効いた\n"
        "- Windows console 問題は CREATE_NO_WINDOW で解決\n"
        "\n"
        "## 昇格候補\n"
        "\n"
        "| ID | trust |\n"
        "|---|---|\n"
        "| foo | 0.5 |\n"
    )


class TestLLMSummaryExtract:
    """`_write_llm_summary_extract()` の検証。"""

    def test_extracts_llm_section_only(self, tmp_path: Path) -> None:
        """consolidated_summary.md から ## LLM 要約 セクションだけを切り出す。"""
        mod = _load_hook_module()
        source = tmp_path / "consolidated_summary.md"
        target = tmp_path / "llm_summary.md"
        source.write_text(_make_summary_with_all_sections(), encoding="utf-8")

        result = mod._write_llm_summary_extract(str(source), str(target))
        assert result is True
        assert target.is_file()

        content = target.read_text(encoding="utf-8")
        # LLM 要約 セクション本体は含まれる
        assert content.startswith("## LLM 要約\n")
        assert "バックグラウンド化が体感に効いた" in content
        assert "Windows console" in content
        # MVP / 昇格候補 は含まれない
        assert "うまくいったアプローチ" not in content
        assert "成功 1" not in content
        assert "昇格候補" not in content
        # サイズが小さい（4KB 以下）
        assert len(content) < 4096

    def test_skips_when_source_missing(self, tmp_path: Path) -> None:
        """source ファイル不在時は False を返し target を作らない。"""
        mod = _load_hook_module()
        source = tmp_path / "missing.md"
        target = tmp_path / "llm_summary.md"

        result = mod._write_llm_summary_extract(str(source), str(target))
        assert result is False
        assert not target.exists()

    def test_skips_when_llm_section_missing(self, tmp_path: Path) -> None:
        """source に ## LLM 要約 がなければ False、既存 target を上書きしない。"""
        mod = _load_hook_module()
        source = tmp_path / "consolidated_summary.md"
        target = tmp_path / "llm_summary.md"
        # LLM 要約セクションを含まない summary
        source.write_text(
            "# 集約サマリ\n\n## うまくいったアプローチ\n- 成功\n",
            encoding="utf-8",
        )
        # 既存 target を作っておく
        target.write_text("既存内容", encoding="utf-8")

        result = mod._write_llm_summary_extract(str(source), str(target))
        assert result is False
        # 既存 target は上書きされない
        assert target.read_text(encoding="utf-8") == "既存内容"

    def test_atomic_write_replaces_existing(self, tmp_path: Path) -> None:
        """既存の target は新しい内容で上書きされる。"""
        mod = _load_hook_module()
        source = tmp_path / "consolidated_summary.md"
        target = tmp_path / "llm_summary.md"
        source.write_text(_make_summary_with_all_sections(), encoding="utf-8")
        target.write_text("古い内容", encoding="utf-8")

        result = mod._write_llm_summary_extract(str(source), str(target))
        assert result is True
        content = target.read_text(encoding="utf-8")
        assert "古い内容" not in content
        assert content.startswith("## LLM 要約\n")


class TestLLMSummaryPlaceholder:
    """`_ensure_llm_summary_placeholder()` の検証。"""

    def test_creates_placeholder_when_missing(self, tmp_path: Path) -> None:
        """target ファイル不在時にプレースホルダを書き出す。"""
        mod = _load_hook_module()
        target = tmp_path / "llm_summary.md"
        assert not target.exists()

        mod._ensure_llm_summary_placeholder(str(target))

        assert target.is_file()
        content = target.read_text(encoding="utf-8")
        assert content.startswith("## LLM 要約\n")
        assert "未生成" in content

    def test_no_overwrite_when_exists(self, tmp_path: Path) -> None:
        """既存 target は上書きしない。"""
        mod = _load_hook_module()
        target = tmp_path / "llm_summary.md"
        target.write_text("既存の重要な内容", encoding="utf-8")

        mod._ensure_llm_summary_placeholder(str(target))

        assert target.read_text(encoding="utf-8") == "既存の重要な内容"


class TestFullSyncMainEnsuresPlaceholder:
    """`_full_sync_main()` がプレースホルダ確保を呼ぶ検証。"""

    def test_full_sync_main_calls_placeholder_helper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _load_hook_module()
        called = {"placeholder": False}

        def fake_placeholder(*a, **kw):
            called["placeholder"] = True

        monkeypatch.setattr(mod, "write_summary", lambda *a, **kw: True)
        monkeypatch.setattr(mod, "build_promotion_candidates_section",
                            lambda *a, **kw: ("", []))
        monkeypatch.setattr(mod, "write_promotion_candidates_log",
                            lambda *a, **kw: True)
        monkeypatch.setattr(mod, "archive_old_sessions", lambda *a, **kw: [])
        monkeypatch.setattr(mod, "_spawn_detached_llm", lambda *a, **kw: None)
        monkeypatch.setattr(mod, "_ensure_llm_summary_placeholder",
                            fake_placeholder)
        monkeypatch.setattr(sys, "stdin", _StubStdin())

        rc = mod._full_sync_main()
        assert rc == 0
        assert called["placeholder"] is True, (
            "_full_sync_main は _ensure_llm_summary_placeholder を呼ぶ必要がある"
        )


class TestLLMOnlyMainExtractsLLMSummary:
    """`_llm_only_main()` が LLM 要約抽出を呼ぶ検証。"""

    def test_llm_only_main_calls_extract_helper(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _load_hook_module()
        lock_path = str(tmp_path / "test.lock")
        monkeypatch.setattr(mod, "LOCK_PATH", lock_path)

        called = {"extract": False}

        def fake_extract(*a, **kw):
            called["extract"] = True
            return True

        monkeypatch.setattr(mod, "write_summary", lambda *a, **kw: True)
        monkeypatch.setattr(mod, "_write_llm_summary_extract", fake_extract)
        monkeypatch.setattr(sys, "argv",
                            ["prog", "--llm-only", "2026-05-10T12:00:00+00:00"])

        rc = mod._llm_only_main()
        assert rc == 0
        assert called["extract"] is True, (
            "_llm_only_main は _write_llm_summary_extract を呼ぶ必要がある"
        )
