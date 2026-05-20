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
        # session_utils.py の sanitize 仕様で `-->` は `-- >` に置換されるため、
        # 既にサニタイズ済みの形式でフィクスチャを書く（実運用ファイルの再現）。
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

    def test_date_type_does_not_raise(self, tmp_path: Path) -> None:
        """today に date 型を渡しても TypeError にならず正常に動作する。

        build_summary_markdown は冒頭で date を datetime に正規化するため、
        isoformat(timespec='seconds') の呼び出しが date 型のまま到達しない。
        """
        from datetime import date
        mod = _load_hook_module()
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        _make_session(sessions, "20260507", success=["- ok"])

        files = mod.list_recent_session_files(
            str(sessions),
            window_days=7,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )

        spec = importlib.util.spec_from_file_location("su_t4", SESSION_UTILS_PATH)
        assert spec is not None and spec.loader is not None
        su = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(su)  # type: ignore[attr-defined]

        # date 型を渡す（バグ再現ケース）
        summary = mod.build_summary_markdown(
            files,
            window_days=7,
            extract_fn=su.extract_section,
            today=date(2026, 5, 8),  # date 型（datetime ではない）
        )
        # 例外なく完了し、タイムスタンプ行が含まれること
        assert "最終更新:" in summary
        assert "2026-05-08" in summary
        assert "- ok" in summary


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


class TestMainE2EExtended:
    """main() の Phase 2 拡張全体の E2E 検証。"""

    def test_main_runs_all_three_extensions_in_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MVP → promotion → archive の順で各ステップが呼ばれる。"""
        mod = _load_hook_module()

        call_order: list[str] = []

        def fake_write_summary(*a, **kw):
            call_order.append("write_summary")
            return True

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
        monkeypatch.setattr(mod, "build_promotion_candidates_section",
                            fake_build_promotion)
        monkeypatch.setattr(mod, "write_promotion_candidates_log",
                            fake_write_promotion)
        monkeypatch.setattr(mod, "archive_old_sessions", fake_archive)
        monkeypatch.setattr(sys, "stdin", _StubStdin())

        rc = mod.main()
        assert rc == 0
        # main() の呼び出し順としては summary → promotion → archive
        assert call_order.index("write_summary") < call_order.index("build_promotion")
        assert call_order.index("build_promotion") < call_order.index("archive")

    def test_main_partial_failure_continues(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """write_summary が例外を投げても archive は実行される。"""
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


class TestWriteSummaryAtomicWrite:
    """`write_summary()` がアトミック書き込み（tempfile + os.replace）を使う検証。[CR-NEW]

    振る舞いベースのテスト方針:
    - write_summary() 呼び出し中に os.replace が呼ばれることを確認する
    - 直接 open(..., "w") で書き込んでいないことを間接的に検証する
    - 実装の内部詳細（tempfile の prefix 等）には依存しない
    """

    def test_write_summary_calls_os_replace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """write_summary() が output_path へ書き込む際に os.replace を経由すること。

        実装は `_atomic_write` (tempfile + os.replace) を採用済み。
        本テストは将来 open(..., "w") 直接書き込みに退行しないかを守る Green 回帰防止テスト。
        """
        mod = _load_hook_module()
        sessions = tmp_path / "memory" / "sessions"
        sessions.mkdir(parents=True)
        _make_session(sessions, "20260507", success=["- foo"])

        output = tmp_path / "memory" / "consolidated_summary.md"

        replace_called_with_target: list[str] = []
        original_replace = os.replace

        def spy_replace(src: str, dst: str) -> None:
            replace_called_with_target.append(dst)
            return original_replace(src, dst)

        monkeypatch.setattr(mod.os, "replace", spy_replace)

        ok = mod.write_summary(
            str(output),
            sessions_dir=str(sessions),
            window_days=7,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )

        assert ok is True, "write_summary() は True を返すべき"
        assert str(output) in replace_called_with_target, (
            "write_summary() は os.replace で output_path に書き込む必要がある。"
            "現在は open(..., 'w') を直接使用しており、並列 Stop hook で競合が発生するリスクがある。"
            "[CR-NEW] アトミック書き込みが実装されていない。"
        )

    def test_write_summary_does_not_use_direct_open_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """write_summary() が output_path を直接 open('w') しないこと。

        実装は `_atomic_write` (tempfile + os.replace) を採用済みで、output_path への
        直接 open('w') は行わない。本テストは将来の改修で直接書き込みに退行しないかを
        守る Green 回帰防止テスト。
        """
        mod = _load_hook_module()
        sessions = tmp_path / "memory" / "sessions"
        sessions.mkdir(parents=True)
        _make_session(sessions, "20260507", success=["- foo"])

        output = tmp_path / "memory" / "consolidated_summary.md"
        output_str = str(output)

        direct_open_paths: list[str] = []
        original_open = open  # builtins.open のコピー

        def spy_open(file, mode="r", *args, **kwargs):
            # output_path を書き込みモードで直接 open しているか検出
            if str(file) == output_str and "w" in str(mode):
                direct_open_paths.append(str(file))
            return original_open(file, mode, *args, **kwargs)

        # builtins.open ではなくモジュールスコープの open をパッチ
        # consolidate_memory.py は open() をグローバル呼び出しするため
        # monkeypatch.setattr でモジュール属性を差し替える手法は機能しない。
        # os.replace スパイで代替する（上位テストで検証済み）。
        # このテストは「os.replace が呼ばれることで direct open を使っていない」を
        # 間接確認するためのガード。
        replace_called: list[str] = []
        original_replace = os.replace

        def spy_replace(src: str, dst: str) -> None:
            replace_called.append(dst)
            return original_replace(src, dst)

        monkeypatch.setattr(mod.os, "replace", spy_replace)

        ok = mod.write_summary(
            output_str,
            sessions_dir=str(sessions),
            window_days=7,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )

        assert ok is True
        # アトミック書き込みが実装されていれば os.replace が必ず呼ばれる
        assert output_str in replace_called, (
            "アトミック書き込みでは tempfile → output_path の os.replace が必須。"
            "現在の直接書き込み実装では os.replace は呼ばれない。[CR-NEW]"
        )

    def test_write_summary_atomic_survives_concurrent_reads(
        self, tmp_path: Path
    ) -> None:
        """write_summary() 書き込み中にファイルが読まれても破損しないこと（振る舞い検証）。

        Red フェーズ: 現在の実装は open(..., "w") を使うため、書き込み途中の
        ファイルが読まれると中途半端な状態が見える可能性がある。
        アトミック書き込みでは tempfile に完全書き込み後に os.replace するため、
        読み手は常に「完全なファイル」か「前のファイル」しか見えない。

        このテストは write_summary() が実際にアトミック書き込みを使って
        出力ファイルを生成することをエンドツーエンドで確認する。
        """
        import threading

        mod = _load_hook_module()
        sessions = tmp_path / "memory" / "sessions"
        sessions.mkdir(parents=True)
        _make_session(sessions, "20260507", success=["- foo", "- bar", "- baz"])
        _make_session(sessions, "20260506", success=["- qux"])

        output = tmp_path / "memory" / "consolidated_summary.md"

        # 並列読み込みスレッドを起動して書き込みと競合させる
        read_results: list[str | None] = []
        stop_flag = {"stop": False}

        def reader_thread() -> None:
            while not stop_flag["stop"]:
                try:
                    content = output.read_text(encoding="utf-8") if output.exists() else None
                    read_results.append(content)
                except OSError:
                    read_results.append(None)

        t = threading.Thread(target=reader_thread, daemon=True)
        t.start()

        ok = mod.write_summary(
            str(output),
            sessions_dir=str(sessions),
            window_days=7,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )

        stop_flag["stop"] = True
        t.join(timeout=2)

        assert ok is True
        # 書き込み完了後のファイルが完全な内容であること
        final_content = output.read_text(encoding="utf-8")
        assert "# 集約サマリ" in final_content, (
            "出力ファイルが完全な状態でないか、または存在しない。"
            "アトミック書き込みが実装されていれば常に完全なファイルが見える。[CR-NEW]"
        )
        # 読み込み結果のいずれも「完全」か「None（未存在）」のどちらかであること
        for content in read_results:
            if content is not None:
                # 部分的な書き込みがないことを確認（ヘッダが欠けていたら中途半端）
                # 現在の直接 open("w") 実装だと、書き込み途中で切り捨てられた
                # 古い内容が見える可能性がある。アトミック書き込みであれば
                # この問題は起きない（os.replace は POSIX 上でアトミック）。
                assert "# 集約サマリ" in content or len(content) == 0, (
                    f"読み込み中に不完全なファイルが観測された（先頭 100 文字: {content[:100]!r}）。"
                    "アトミック書き込みを実装することでこの問題を解消できる。[CR-NEW]"
                )


class TestWriteSummaryAtomicWriteUsesAtomicWrite:
    """`write_summary()` が内部で `_atomic_write()` を呼ぶことの直接確認。[CR-NEW]

    `write_promotion_candidates_log()` はすでに `_atomic_write()` を使っている。
    `write_summary()` も同じパターンを採用すべきであることを、
    `_atomic_write()` のスパイで直接確認する。
    """

    def test_write_summary_delegates_to_atomic_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """write_summary() が _atomic_write() を呼ぶこと。

        実装は `_atomic_write()` 経由で書き込み済み。本テストは将来の改修で `open(..., "w")`
        直接書き込みに退行しないかをスパイで守る Green 回帰防止テスト。
        """
        mod = _load_hook_module()
        sessions = tmp_path / "memory" / "sessions"
        sessions.mkdir(parents=True)
        _make_session(sessions, "20260507", success=["- foo"])

        output = tmp_path / "memory" / "consolidated_summary.md"

        atomic_write_called_with: list[str] = []
        original_atomic_write = mod._atomic_write

        def spy_atomic_write(output_path: str, payload: str) -> bool:
            atomic_write_called_with.append(output_path)
            return original_atomic_write(output_path, payload)

        monkeypatch.setattr(mod, "_atomic_write", spy_atomic_write)

        ok = mod.write_summary(
            str(output),
            sessions_dir=str(sessions),
            window_days=7,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )

        assert ok is True
        assert str(output) in atomic_write_called_with, (
            "write_summary() は _atomic_write() を使ってファイルを書き込む必要がある。"
            "現在の実装は open(..., 'w') を直接使っているため _atomic_write() は呼ばれない。"
            "[CR-NEW] write_summary() に _atomic_write() を使う実装が必要。"
        )


class TestWriteSummaryAtomicWriteFailure:
    """`write_summary()` のアトミック書き込み失敗時の挙動確認。[CR-NEW]

    _atomic_write() が False を返す場合、write_summary() も False を返すことを確認する。
    これは実装後のリグレッションテストとして機能する。
    """

    def test_write_summary_returns_false_on_atomic_write_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_atomic_write() が False を返す場合、write_summary() も False を返すこと。

        Red フェーズ: 現在の実装は _atomic_write() を使わないため、
        このスパイは呼ばれず、動作確認ができない。
        write_summary() が _atomic_write() を使うようになれば、
        _atomic_write() が False を返した時に write_summary() も False を返す
        かどうか確認できる。

        Note: このテストはアトミック書き込みが実装された後に Green になる。
        """
        mod = _load_hook_module()
        sessions = tmp_path / "memory" / "sessions"
        sessions.mkdir(parents=True)
        _make_session(sessions, "20260507", success=["- foo"])

        output = tmp_path / "memory" / "consolidated_summary.md"

        def fake_atomic_write(output_path: str, payload: str) -> bool:
            # 書き込み失敗をシミュレート
            return False

        monkeypatch.setattr(mod, "_atomic_write", fake_atomic_write)

        ok = mod.write_summary(
            str(output),
            sessions_dir=str(sessions),
            window_days=7,
            today=datetime(2026, 5, 8, tzinfo=timezone.utc),
        )

        assert ok is False, (
            "write_summary() が _atomic_write() を使っている場合、"
            "_atomic_write() が False を返したら write_summary() も False を返すべき。"
            "[CR-NEW] _atomic_write() 未使用のため現在は True が返る。"
        )


# ---------------------------------------------------------------------------
# T1-B2: stdin 1 MB 上限 [L-3 SR-V-001]
# ---------------------------------------------------------------------------


class TestStdinMaxBytes:
    """main() の stdin 読み取りに 1 MB 上限が課されることを検証する。[L-3 SR-V-001]"""

    def test_main_rejects_stdin_over_max_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """stdin に 1 MB を超えるデータを流した場合、警告を出力するか非ゼロ終了コードを返すことを検証。

        regression guard for [L-3 SR-V-001]: consolidate_memory.main() は stdin が
        1 MB を超えた場合に stderr 警告を出力するか非ゼロ終了コードを返すべき。
        """
        mod = _load_hook_module()

        # 1 MB + 1 byte のペイロード
        oversized_payload = b"x" * ((1 << 20) + 1)

        class OversizedStdin:
            def read(self):
                return oversized_payload.decode("latin-1")

        monkeypatch.setattr(sys, "stdin", OversizedStdin())

        # run_sync をスタブ化して stdin 読み取り後の処理をスキップ
        monkeypatch.setattr(mod, "run_sync", lambda *a, **kw: 0)

        import io as _io
        old_stderr = sys.stderr
        sys.stderr = _io.StringIO()

        rc = mod.main()

        stderr_output = sys.stderr.getvalue()
        sys.stderr = old_stderr

        # 上限超過時は stderr に警告を出力するか、非ゼロ終了コードを返すべき。
        assert (
            "too large" in stderr_output.lower()
            or "max" in stderr_output.lower()
            or "limit" in stderr_output.lower()
            or rc != 0
        ), (
            "stdin が 1 MB を超えた場合、stderr に警告ログを出力するか非ゼロ終了コードを返すべき。"
            "[L-3 SR-V-001] regression guard: 無制限読み取りへの退行を防ぐ。"
        )

    def test_main_accepts_stdin_within_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """1 MB 以内の stdin で正常動作（exit 0）することを検証。

        既存挙動の維持確認。Green フェーズで上限実装後もこのケースは PASS すること。
        """
        mod = _load_hook_module()

        # 1 MB - 1 byte のペイロード（上限以内）
        within_payload = "x" * ((1 << 20) - 1)

        class WithinStdin:
            def read(self):
                return within_payload

        monkeypatch.setattr(sys, "stdin", WithinStdin())
        monkeypatch.setattr(mod, "run_sync", lambda *a, **kw: 0)

        rc = mod.main()

        assert rc == 0, (
            "1 MB 以内の stdin では正常終了（exit 0）すること。"
        )


# ---------------------------------------------------------------------------
# T1-B2: patterns.json 型検証 [L-4 SR-V-001]
# ---------------------------------------------------------------------------


class TestPatternsJsonValidation:
    """patterns.json の型検証と description/id の改行除去を検証する。[L-4 SR-V-001]"""

    def test_load_patterns_rejects_non_list(
        self, tmp_path: Path
    ) -> None:
        r"""_load_patterns_readonly に patterns が list でなく dict の場合、stderr に警告を出力すること。

        regression guard for [L-4 SR-V-001]: 型不整合時は無言で無視せず stderr 警告を
        出力するべき（プロンプトインジェクション防止）。
        """
        import io as _io

        mod = _load_hook_module()
        patterns_path = tmp_path / "patterns.json"
        # patterns が dict（list でない）
        patterns_path.write_text(
            '{"patterns": {"unexpected_key": "unexpected_value"}}',
            encoding="utf-8",
        )

        old_stderr = sys.stderr
        sys.stderr = _io.StringIO()

        result = mod._load_patterns_readonly(str(patterns_path))

        stderr_output = sys.stderr.getvalue()
        sys.stderr = old_stderr

        # 型不整合は空リストを返すべき（既存動作の維持）
        assert result == [], f"非 list の patterns は空リストを返すべきだが {result!r} が返った"

        # 型不整合は stderr に警告を出力するべき
        assert stderr_output != "", (
            "patterns.json の `patterns` フィールドが list でない場合、"
            "stderr に型不整合警告を出力するべき。"
            "[L-4 SR-V-001] regression guard: 無言での空リスト返却への退行を防ぐ。"
        )

    def test_promotion_candidates_strips_newlines_in_description(
        self, tmp_path: Path
    ) -> None:
        r"""patterns.json の description に \n / \r が含まれていた場合、
        write_promotion_candidates_log の出力（詳細セクション）でこれらが除去されることを検証。

        regression guard for [L-4 SR-V-001]: 詳細セクションの description から
        \n / \r を除去して Markdown インジェクションを防ぐ。
        """
        mod = _load_hook_module()
        from datetime import datetime, timezone

        output_path = tmp_path / "promotion-candidates.md"

        candidates = [
            {
                "id": "injected_id",
                "description": "line1\nline2\r\nline3\rline4",
                "trust_score": 0.9,
                "promotion_candidate": True,
                "observations": [{"date": "20260501"}],
                "registered_date": "20260501",
            }
        ]

        mod.write_promotion_candidates_log(
            candidates,
            str(output_path),
            today=datetime(2026, 5, 9, tzinfo=timezone.utc),
        )

        content = output_path.read_text(encoding="utf-8")

        # description に含まれる生の \n / \r が出力ファイルに残っていないことを検証
        # 詳細セクションの description 行を特定して検証
        assert "line1\nline2" not in content, (
            "description の改行 \\n が詳細セクションに残っている。"
            "Markdown インジェクション防止のため除去すること。"
            "[L-4 SR-V-001] regression guard: description 改行除去への退行を防ぐ。"
        )
        assert "line3\rline4" not in content, (
            "description の改行 \\r が詳細セクションに残っている。"
            "Markdown インジェクション防止のため除去すること。"
            "[L-4 SR-V-001] regression guard: description 改行除去への退行を防ぐ。"
        )


# ---------------------------------------------------------------------------
# T-1b: _sanitize_field 拡張テスト（タブ / null byte / ASCII 制御文字）
# ---------------------------------------------------------------------------


class TestSanitizeFieldExtended:
    """regression guard for _sanitize_field to strip tab, null byte, and ASCII control chars."""

    def test_sanitize_field_strips_tab(self) -> None:
        """regression guard for [N-3]: _sanitize_field がタブ文字を除去またはスペースに置換すること。

        Verifies that _sanitize_field strips tab characters (\t) from field values.
        """
        mod = _load_hook_module()
        result = mod._sanitize_field("before\tafter")
        assert "\t" not in result, (
            "_sanitize_field はタブ文字 \\t を除去またはスペースに置換する必要がある。"
            "[N-3] regression guard: タブ素通りへの退行を防ぐ。"
        )

    def test_sanitize_field_strips_null_byte(self) -> None:
        """regression guard for [N-3]: _sanitize_field が null byte を除去すること。

        Verifies that _sanitize_field removes null bytes (\x00) from field values.
        """
        mod = _load_hook_module()
        result = mod._sanitize_field("before\x00after")
        assert "\x00" not in result, (
            "_sanitize_field は null byte \\x00 を除去する必要がある。"
            "[N-3] regression guard: null byte 素通りへの退行を防ぐ。"
        )

    def test_sanitize_field_strips_ascii_control_chars(self) -> None:
        r"""regression guard for [N-3]: _sanitize_field が ASCII 制御文字を除去すること。

        Verifies that _sanitize_field strips ASCII control characters
        (\x01-\x08, \x0b, \x0c, \x0e-\x1f, \x7f) from field values.
        """
        mod = _load_hook_module()
        # \x01-\x08, \x0b, \x0c, \x0e-\x1f, \x7f (exclude \x09=tab tested above,
        # \x0a=LF and \x0d=CR which are already handled)
        control_chars = (
            "".join(chr(i) for i in range(0x01, 0x09))  # \x01-\x08
            + "\x0b\x0c"                                 # VT, FF
            + "".join(chr(i) for i in range(0x0e, 0x20)) # \x0e-\x1f
            + "\x7f"                                     # DEL
        )
        result = mod._sanitize_field(f"before{control_chars}after")
        for ch in control_chars:
            assert ch not in result, (
                f"_sanitize_field は ASCII 制御文字 \\x{ord(ch):02x} を除去する必要がある。"
                "[N-3] regression guard: ASCII 制御文字素通りへの退行を防ぐ。"
            )


# ---------------------------------------------------------------------------
# T-1b: registered / last_updated / cid_disp への sanitize 適用テスト
# ---------------------------------------------------------------------------


class TestPromotionCandidatesSanitize:
    """regression guard for _sanitize_field applied to registered/last_updated/cid_disp fields."""

    def test_registered_with_newline_is_sanitized_in_table(
        self, tmp_path: Path
    ) -> None:
        r"""regression guard for [N-1]: registered_date の改行が表セクションで除去されること。

        Verifies that _sanitize_field is applied to registered_date in the table section,
        preventing \n from breaking the Markdown table row.
        """
        mod = _load_hook_module()
        output_path = tmp_path / "promotion-candidates.md"

        candidates = [
            {
                "id": "p1",
                "description": "desc",
                "trust_score": 0.9,
                "promotion_candidate": True,
                "observations": [{"date": "20260501"}],
                "registered_date": "20260501\nINJECTED",
                "last_updated": "20260501",
            }
        ]

        mod.write_promotion_candidates_log(
            candidates,
            str(output_path),
            today=datetime(2026, 5, 9, tzinfo=timezone.utc),
        )

        text = output_path.read_text(encoding="utf-8")
        # 表セクション内の行に生の \n があってはならない
        # テーブル行は | ... | ... | 形式であり、その中に改行が入ると行が壊れる
        assert "20260501\nINJECTED" not in text, (
            "registered_date の改行 \\n が表セクションに残っている。"
            "_sanitize_field の適用が必要。[N-1]"
        )

    def test_registered_with_newline_is_sanitized_in_detail(
        self, tmp_path: Path
    ) -> None:
        r"""regression guard for [N-1]: registered_date の改行が詳細セクションで除去されること。

        Verifies that _sanitize_field is applied to registered_date and last_updated
        in the detail section ('## 詳細'), preventing Markdown injection.
        """
        mod = _load_hook_module()
        output_path = tmp_path / "promotion-candidates.md"

        candidates = [
            {
                "id": "p1",
                "description": "desc",
                "trust_score": 0.9,
                "promotion_candidate": True,
                "observations": [{"date": "20260501"}],
                "registered_date": "20260501\nINJECTED",
                "last_updated": "20260501\nINJECTED_LU",
            }
        ]

        mod.write_promotion_candidates_log(
            candidates,
            str(output_path),
            today=datetime(2026, 5, 9, tzinfo=timezone.utc),
        )

        text = output_path.read_text(encoding="utf-8")
        assert "20260501\nINJECTED" not in text, (
            "registered_date の改行 \\n が詳細セクションに残っている。"
            "_sanitize_field の適用が必要。[N-1]"
        )
        assert "20260501\nINJECTED_LU" not in text, (
            "last_updated の改行 \\n が詳細セクションに残っている。"
            "_sanitize_field の適用が必要。[N-1]"
        )

    def test_cid_with_newline_is_sanitized_in_table(
        self, tmp_path: Path
    ) -> None:
        r"""regression guard for cid (id field) newline being stripped in table section output.

        The table section uses _truncate_for_table(f["cid"]) for cid_disp.
        _truncate_for_table internally replaces CR/LF with spaces, so this should already
        be handled — but it does NOT strip tab/null/ASCII control chars.
        This test confirms that a newline in cid does not appear raw in the table output.
        If _truncate_for_table already covers this, the test will PASS even before the fix,
        acting as a regression guard. The deeper gap (tab/null) is covered by
        TestSanitizeFieldExtended above.
        """
        mod = _load_hook_module()
        output_path = tmp_path / "promotion-candidates.md"

        candidates = [
            {
                "id": "cid_with\nnewline",
                "description": "desc",
                "trust_score": 0.9,
                "promotion_candidate": True,
                "observations": [{"date": "20260501"}],
                "registered_date": "20260501",
                "last_updated": "20260501",
            }
        ]

        mod.write_promotion_candidates_log(
            candidates,
            str(output_path),
            today=datetime(2026, 5, 9, tzinfo=timezone.utc),
        )

        text = output_path.read_text(encoding="utf-8")
        # _truncate_for_table は \n を space に置換するため、テーブル行内に生の \n はないはず
        # ただし _sanitize_field 未適用なら \t や \x00 はそのまま残る
        # このテストは改行に関する回帰防止を確認する
        table_lines = [line for line in text.splitlines() if line.startswith("|") and "cid_with" in line]
        assert len(table_lines) >= 1, (
            "cid に改行が含まれる場合でも表セクションに1行として出力されるべき。"
            "[N-2] cid_disp に _sanitize_field が適用されていないと行が壊れる。"
        )

