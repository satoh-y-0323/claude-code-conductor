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


# ---------------------------------------------------------------------------
# F-004 Phase 2-A: archive_old_sessions
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
# F-004 Phase 2-B: 半自動 promotion 候補ログ
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
