"""Tests for .claude/hooks/review_hint_inject.py and c3_db review helpers.

review-hint: レビュー判断ヒント機能の検証。

テストケース:
 c3_db ヘルパー:
  1. insert + fetch（基本動作）
  2. fetch は decided_at DESC でソートされる
  3. fetch の limit が効く
  4. aggregate_decisions が fixed/accepted/deferred を集計

 extract_checklist_ids:
  5. CR-XX-NNN / SR-XX-NNN を抽出
  6. 重複は除去、出現順保持
  7. 関係ない括弧文字列は無視

 build_hint_section:
  8. 過去判断ありで Markdown を組み立て
  9. 過去判断なし + 重複指摘なしで空文字列
 10. 6 ヶ月超の判断に [要再評価] が付く
 11. 重複指摘フラグセクションが付与される

 append_hints_to_report:
 12. レポート末尾に追記
 13. 二重追記の回避

 main (E2E):
 14. 単一レポート + DB に過去判断 → ヒントセクションが追記される
 15. 2 レポート + 同一 ID 指摘 → 重複指摘フラグが両方に出る
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "review_hint_inject.py"
SCHEMA_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "schema.sql"
INIT_HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "session_start.py"


def _create_c3_db(db_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("init_c3_db_t", INIT_HOOK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    mod.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))


def _load_hook_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("review_hint_inject", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


# ---------------------------------------------------------------------------
# c3_db ヘルパー
# ---------------------------------------------------------------------------


class TestC3DbReviewHelpers:

    def test_insert_then_fetch(self, tmp_path: Path) -> None:
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        from c3 import db as c3_db

        ok = c3_db.insert_review_decision(
            checklist_id="CR-Q-001",
            finding_text="関数が長い",
            decision="accepted",
            reason="既存設計との整合のため",
            reviewer="code-reviewer",
            db_path=db_path,
        )
        assert ok is True

        rows = c3_db.fetch_review_decisions("CR-Q-001", db_path=db_path)
        assert len(rows) == 1
        assert rows[0]["checklist_id"] == "CR-Q-001"
        assert rows[0]["decision"] == "accepted"
        assert rows[0]["reason"] == "既存設計との整合のため"
        assert rows[0]["reviewer"] == "code-reviewer"

    def test_fetch_orders_desc_by_decided_at(self, tmp_path: Path) -> None:
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        from c3 import db as c3_db

        old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        new = datetime(2026, 5, 1, tzinfo=timezone.utc)
        c3_db.insert_review_decision(
            checklist_id="CR-Q-002", finding_text="x", decision="fixed",
            reviewer="code-reviewer", decided_at=old, db_path=db_path,
        )
        c3_db.insert_review_decision(
            checklist_id="CR-Q-002", finding_text="y", decision="accepted",
            reason="r", reviewer="code-reviewer", decided_at=new, db_path=db_path,
        )

        rows = c3_db.fetch_review_decisions("CR-Q-002", db_path=db_path)
        assert len(rows) == 2
        assert rows[0]["decision"] == "accepted"  # 直近
        assert rows[1]["decision"] == "fixed"

    def test_fetch_limit(self, tmp_path: Path) -> None:
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        from c3 import db as c3_db

        for i in range(5):
            c3_db.insert_review_decision(
                checklist_id="CR-T-001",
                finding_text=f"f{i}",
                decision="fixed",
                reviewer="code-reviewer",
                decided_at=datetime(2026, 5, i + 1, tzinfo=timezone.utc),
                db_path=db_path,
            )
        rows = c3_db.fetch_review_decisions("CR-T-001", db_path=db_path, limit=2)
        assert len(rows) == 2

    def test_aggregate_decisions(self) -> None:
        from c3 import db as c3_db

        rows = [
            {"decision": "fixed"},
            {"decision": "accepted"},
            {"decision": "accepted"},
            {"decision": "deferred"},
        ]
        s = c3_db.aggregate_decisions(rows)
        assert s == {"total": 4, "fixed": 1, "accepted": 2, "deferred": 1}


# ---------------------------------------------------------------------------
# extract_checklist_ids
# ---------------------------------------------------------------------------


class TestExtractChecklistIds:

    def test_extracts_cr_and_sr_ids(self) -> None:
        mod = _load_hook_module()
        text = "## High [CR-Q-001] foo\n## Low [SR-K-002] bar\n## Note [CR-Q-001] dup"
        ids = mod.extract_checklist_ids(text)
        assert ids == ["CR-Q-001", "SR-K-002"]  # 重複除去 + 順序保持

    def test_ignores_non_matching_brackets(self) -> None:
        mod = _load_hook_module()
        text = "[NOT-AN-ID] [CR-Q-1] (CR-Q-001) [CR-Q-001]"
        ids = mod.extract_checklist_ids(text)
        assert ids == ["CR-Q-001"]


# ---------------------------------------------------------------------------
# build_hint_section
# ---------------------------------------------------------------------------


class TestBuildHintSection:

    def test_with_decisions_builds_markdown(self) -> None:
        mod = _load_hook_module()
        decisions = {
            "CR-Q-001": [
                {
                    "checklist_id": "CR-Q-001",
                    "decision": "accepted",
                    "reason": "テストの可読性のため",
                    "decided_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "reviewer": "code-reviewer",
                    "finding_text": "f",
                    "context_summary": None,
                },
            ],
        }
        section = mod.build_hint_section(decisions)
        assert mod.HINT_HEADING in section
        assert "[CR-Q-001]" in section
        assert "テストの可読性のため" in section

    def test_empty_returns_empty_string(self) -> None:
        mod = _load_hook_module()
        section = mod.build_hint_section({})
        assert section == ""

    def test_old_decision_gets_reeval_flag(self) -> None:
        mod = _load_hook_module()
        old_iso = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat(timespec="seconds")
        decisions = {
            "CR-Q-001": [
                {
                    "checklist_id": "CR-Q-001",
                    "decision": "accepted",
                    "reason": "x",
                    "decided_at": old_iso,
                    "reviewer": "code-reviewer",
                    "finding_text": "f",
                    "context_summary": None,
                },
            ],
        }
        section = mod.build_hint_section(decisions)
        assert "[要再評価]" in section

    def test_duplicate_flag_section(self) -> None:
        mod = _load_hook_module()
        section = mod.build_hint_section({}, duplicate_ids={"CR-Q-001", "SR-K-002"})
        assert "重複指摘フラグ" in section
        assert "CR-Q-001" in section
        assert "SR-K-002" in section


# ---------------------------------------------------------------------------
# append_hints_to_report
# ---------------------------------------------------------------------------


class TestAppendHintsToReport:

    def test_appends_to_end(self, tmp_path: Path) -> None:
        mod = _load_hook_module()
        report = tmp_path / "code-review-report.md"
        report.write_text("# Code Review\n\nfoo\n", encoding="utf-8")

        ok = mod.append_hints_to_report(
            report,
            mod.HINT_HEADING + "\n\n- 過去 1 件: 許容 1\n",
        )
        assert ok is True
        text = report.read_text(encoding="utf-8")
        assert text.startswith("# Code Review")
        assert mod.HINT_HEADING in text

    def test_does_not_append_twice(self, tmp_path: Path) -> None:
        mod = _load_hook_module()
        report = tmp_path / "report.md"
        report.write_text(
            "# Code Review\n\n" + mod.HINT_HEADING + "\n\n- already here\n",
            encoding="utf-8",
        )
        ok = mod.append_hints_to_report(
            report,
            mod.HINT_HEADING + "\n\n- new hint\n",
        )
        assert ok is False  # 二重追記回避
        text = report.read_text(encoding="utf-8")
        # 元の内容が変わっていない
        assert text.count(mod.HINT_HEADING) == 1


# ---------------------------------------------------------------------------
# main (E2E)
# ---------------------------------------------------------------------------


class TestMainE2E:

    def test_single_report_with_past_decision(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """単一レポート + DB に過去判断 → ヒントセクションが追記される。"""
        # DB セットアップ
        db_path = tmp_path / ".claude" / "state" / "c3.db"
        db_path.parent.mkdir(parents=True)
        _create_c3_db(db_path)

        from c3 import db as c3_db
        c3_db.insert_review_decision(
            checklist_id="CR-Q-001",
            finding_text="関数が長すぎる",
            decision="accepted",
            reason="既存スタイルを尊重",
            reviewer="code-reviewer",
            db_path=db_path,
        )

        # locate_c3_db を tmp_path 配下を見るように差し替え
        mod = _load_hook_module()
        monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: db_path)

        # レポート作成
        report = tmp_path / "code-review-report.md"
        report.write_text(
            "# Code Review Report\n\n"
            "## High\n\n"
            "1. [CR-Q-001] 関数が長すぎる\n",
            encoding="utf-8",
        )

        rc = mod.main([str(report)])
        assert rc == 0

        text = report.read_text(encoding="utf-8")
        assert mod.HINT_HEADING in text
        assert "既存スタイルを尊重" in text
        # 元本文は壊れていない
        assert "1. [CR-Q-001] 関数が長すぎる" in text

    def test_two_reports_with_duplicate_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """両レポートで同じ checklist_id が指摘されたら重複指摘フラグが両方に付く。

        ただし、本実装では「2 つ以上のレポートで現れる ID」を重複と判定している。
        SR が CR と同じ ID を出すことは実運用では稀（プレフィックスが違うため）。
        ここではプレフィックスが異なる ID の交差が無いことだけを確認し、
        重複検出ロジック自体は build_hint_section 単体テストで保証する。
        """
        # DB セットアップ
        db_path = tmp_path / ".claude" / "state" / "c3.db"
        db_path.parent.mkdir(parents=True)
        _create_c3_db(db_path)

        from c3 import db as c3_db
        monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: db_path)

        mod = _load_hook_module()

        cr_report = tmp_path / "code-review-report.md"
        sr_report = tmp_path / "security-review-report.md"
        cr_report.write_text("# CR\n\n[CR-Q-001] foo\n[SR-K-002] bar\n", encoding="utf-8")
        sr_report.write_text("# SR\n\n[SR-K-002] bar\n", encoding="utf-8")

        rc = mod.main([str(cr_report), str(sr_report)])
        assert rc == 0

        # SR-K-002 は両レポートで言及 → 重複フラグが付く
        cr_text = cr_report.read_text(encoding="utf-8")
        sr_text = sr_report.read_text(encoding="utf-8")
        assert "重複指摘フラグ" in cr_text
        assert "SR-K-002" in cr_text
        assert "重複指摘フラグ" in sr_text

    def test_no_args_returns_zero(self) -> None:
        """引数無しでも crash せず 0 を返す（usage を stderr に出すのみ）。"""
        mod = _load_hook_module()
        rc = mod.main([])
        assert rc == 0

    def test_nonexistent_path_is_skipped(self, tmp_path: Path) -> None:
        """存在しないパスを指定されてもエラーにならない。"""
        mod = _load_hook_module()
        rc = mod.main([str(tmp_path / "ghost.md")])
        assert rc == 0
