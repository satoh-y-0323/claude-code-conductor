"""Tests for .claude/skills/dev-workflow/scripts/review_hint_inject.py and c3_db review helpers.

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

 b4 追加テスト (TestBuildHintSection):
 12. reason に改行 + Markdown 見出し（\\n##）を含む場合、出力に \\n## が現れず空白置換される
 13. reason に backtick を含む場合、サニタイズされること
 14. 古い decided_at で [要再評価] フラグが付く（_is_old 判定が壊れないこと）

 append_hints_to_report:
 15. レポート末尾に追記
 16. 二重追記の回避

 main (E2E):
 17. 単一レポート + DB に過去判断 → ヒントセクションが追記される
 18. 2 レポート + 同一 ID 指摘 → 重複指摘フラグが両方に出る
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
HOOK_PATH = WORKTREE_ROOT / ".claude" / "skills" / "dev-workflow" / "scripts" / "review_hint_inject.py"
SCHEMA_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "schema.sql"
INIT_HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "session_start.py"



def _create_c3_db(db_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("init_c3_db_t", INIT_HOOK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    mod.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))


def _load_hook_module(name: str = "review_hint_inject") -> types.ModuleType:
    """モジュールを importlib.util.spec_from_file_location で動的読み込みする。

    NOTE: spec_from_file_location は spec.loader.exec_module() を呼ぶたびに
    fresh なモジュールを返す（sys.modules には自動登録されない）。
    引数 `name` はモジュールの `__name__` 属性として使われ、テスト間で衝突しない
    一意な名前を渡せばロガーや内部 ID の重複を避けられる。
    test_record_tier_outcome.py の同名関数とシグネチャ統一。
    """
    spec = importlib.util.spec_from_file_location(name, HOOK_PATH)
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

    # ------------------------------------------------------------------
    # b4: _sanitize_md によるサニタイズ検証（新規 Red テスト）
    # ------------------------------------------------------------------

    def _make_decision_row(
        self,
        *,
        decided_at: str,
        reason: str = "テスト理由",
        decision: str = "accepted",
    ) -> dict:
        return {
            "checklist_id": "CR-Q-001",
            "decision": decision,
            "reason": reason,
            "decided_at": decided_at,
            "reviewer": "code-reviewer",
            "finding_text": "finding",
            "context_summary": None,
        }

    def test_reason_with_newline_md_heading_is_sanitized(self) -> None:
        """reason に '\\n## 偽見出し' を含む decision row を build_hint_section に渡したとき、
        出力に '\\n## 偽見出し' が現れず、空白置換されること。

        [b4 Green 回帰防止] _sanitize_md ヘルパー実装済み。本テストは PASS を維持する回帰防止テスト。
        """
        mod = _load_hook_module()
        decided_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        row = self._make_decision_row(
            decided_at=decided_at,
            reason="正常理由\n## 偽見出し インジェクション",
        )
        decisions = {"CR-Q-001": [row]}
        section = mod.build_hint_section(decisions)

        assert "\n## 偽見出し" not in section, (
            "reason 内の '\\n## 偽見出し' が Markdown 出力にそのまま埋め込まれている。"
            "_sanitize_md で改行・# をサニタイズする必要がある。 [b4 / SR-NEW]"
        )
        # サニタイズ後の空白置換された文字列は出力に含まれる
        assert "偽見出し インジェクション" in section

    def test_reason_with_backtick_is_sanitized(self) -> None:
        """reason に backtick を含む入力でもサニタイズされること。

        [b4 Green 回帰防止] _sanitize_md ヘルパー実装済み。本テストは PASS を維持する回帰防止テスト。
        """
        mod = _load_hook_module()
        decided_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        row = self._make_decision_row(
            decided_at=decided_at,
            reason="理由 `backtick injection` 終わり",
        )
        decisions = {"CR-Q-001": [row]}
        section = mod.build_hint_section(decisions)

        assert "`" not in section, (
            "reason 内の backtick が Markdown 出力にそのまま埋め込まれている。"
            "_sanitize_md で backtick をサニタイズする必要がある。 [b4 / SR-NEW]"
        )
        # バッククォートが空白に置換された結果、前後の語は残っている
        assert "backtick injection" in section

    def test_old_decided_at_gets_reeval_flag_after_sanitize(self) -> None:
        """古い decided_at で [要再評価] フラグが付くこと。

        _is_old 判定は decided_at の生値（サニタイズ前）を使うため、
        decided_at の値が ISO 8601 として正しければフラグが付く。
        _sanitize_md が decided_at に適用されても _is_old 判定が壊れないことを確認。

        [b4 Green 回帰防止] _sanitize_md ヘルパー実装済み。本テストは「_sanitize_md 実装後も
        _is_old 判定が正しく動く」ことを保証する回帰防止テスト。
        """
        mod = _load_hook_module()
        old_iso = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat(timespec="seconds")
        row = self._make_decision_row(
            decided_at=old_iso,
            reason="古い理由\n## 見出し崩し",
        )
        decisions = {"CR-Q-001": [row]}
        section = mod.build_hint_section(decisions)

        # [要再評価] フラグが付くこと（_is_old 判定が壊れていない）
        assert "[要再評価]" in section, (
            "[要再評価] フラグが付いていない。"
            "_is_old() への入力が decided_at の生値（ISO 8601）であることを確認すること。"
        )
        # 同時に、reason のサニタイズも確認
        assert "\n## 見出し崩し" not in section

    # ------------------------------------------------------------------
    # B-1: U+2028 / U+2029 サニタイズ（Green 回帰防止テスト）
    # ------------------------------------------------------------------

    # ソースコード上に実体文字を埋め込まず、chr() 経由で参照する。
    _LS = chr(0x2028)  # LINE SEPARATOR
    _PS = chr(0x2029)  # PARAGRAPH SEPARATOR

    def test_reason_with_u2028_line_separator_is_sanitized(self) -> None:
        """reason に U+2028 (LINE SEPARATOR) を含む decision row を build_hint_section に渡したとき、
        出力に生の U+2028 が含まれず、前後の語が空白置換で残ること。

        [B-1 Green 回帰防止] _sanitize_md の正規表現に U+2028 が含まれていることを保証する回帰防止テスト。
        """
        mod = _load_hook_module()
        decided_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        reason_with_ls = f"理由before{self._LS}理由after"
        row = self._make_decision_row(
            decided_at=decided_at,
            reason=reason_with_ls,
        )
        decisions = {"CR-Q-001": [row]}
        section = mod.build_hint_section(decisions)

        assert self._LS not in section, (
            f"reason 内の U+2028 (LINE SEPARATOR) が Markdown 出力にそのまま埋め込まれている。"
            f"_sanitize_md に U+2028 を追加する必要がある。 [B-1 / SR-NEW / CR-Q-001]"
        )
        # サニタイズ後も前後の語は空白置換された形で残ること
        assert "理由before" in section
        assert "理由after" in section

    def test_reason_with_u2029_paragraph_separator_is_sanitized(self) -> None:
        """reason に U+2029 (PARAGRAPH SEPARATOR) を含む decision row を build_hint_section に渡したとき、
        出力に生の U+2029 が含まれず、前後の語が空白置換で残ること。

        [B-1 Green 回帰防止] _sanitize_md の正規表現に U+2029 が含まれていることを保証する回帰防止テスト。
        """
        mod = _load_hook_module()
        decided_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        reason_with_ps = f"前段{self._PS}後段"
        row = self._make_decision_row(
            decided_at=decided_at,
            reason=reason_with_ps,
        )
        decisions = {"CR-Q-001": [row]}
        section = mod.build_hint_section(decisions)

        assert self._PS not in section, (
            f"reason 内の U+2029 (PARAGRAPH SEPARATOR) が Markdown 出力にそのまま埋め込まれている。"
            f"_sanitize_md に U+2029 を追加する必要がある。 [B-1 / SR-NEW / CR-Q-001]"
        )
        # サニタイズ後も前後の語は空白置換された形で残ること
        assert "前段" in section
        assert "後段" in section

    # ------------------------------------------------------------------
    # B-3: U+0085 (NEL) サニタイズ（Green 回帰防止テスト）
    # ------------------------------------------------------------------

    # ソースコード上に実体文字を埋め込まず、chr() 経由で参照する。
    _NEL = chr(0x85)  # NEXT LINE (NEL)

    def test_reason_with_u0085_nel_is_sanitized(self) -> None:
        """reason に U+0085 (NEXT LINE / NEL) を含む decision row を build_hint_section に渡したとき、
        出力に生の U+0085 が含まれず、前後の語が空白置換で残ること。

        [B-3 Green 回帰防止] _sanitize_md の正規表現に U+0085 が含まれていることを保証する回帰防止テスト。
        """
        mod = _load_hook_module()
        decided_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        reason_with_nel = f"before{self._NEL}after"
        row = self._make_decision_row(
            decided_at=decided_at,
            reason=reason_with_nel,
        )
        decisions = {"CR-Q-001": [row]}
        section = mod.build_hint_section(decisions)

        assert self._NEL not in section, (
            f"reason 内の U+0085 (NEXT LINE / NEL) が Markdown 出力にそのまま埋め込まれている。"
            f"_sanitize_md に U+0085 (\\x85) を追加する必要がある。 [B-3 / SR-NEW]"
        )
        # サニタイズ後も前後の語は空白置換された形で残ること
        assert "before" in section
        assert "after" in section


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
        # B-2 実装後: ALLOWED_REPORT_DIR を tmp_path に差し替えてパスガードを回避 [SR-V-002]
        monkeypatch.setattr(mod, "ALLOWED_REPORT_DIR", tmp_path)

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
        # B-2 実装後: ALLOWED_REPORT_DIR を tmp_path に差し替えてパスガードを回避 [SR-V-002]
        monkeypatch.setattr(mod, "ALLOWED_REPORT_DIR", tmp_path)

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


# ---------------------------------------------------------------------------
# B-2: .claude/reports/ 配下限定ガード（Green 回帰防止テスト）
# ---------------------------------------------------------------------------


class TestMainReportsPathGuard:
    """.claude/reports/ 配下以外のパスを main() に渡したとき、ファイルが書き換えられないことを確認する。

    [B-2 Green 回帰防止] .claude/reports/ 配下限定ガード（ALLOWED_REPORT_DIR）が実装済み。
    本クラスは範囲外パスが skip される振る舞いを保証する回帰防止テスト。
    """

    def test_main_skips_path_outside_reports_dir(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """tmp_path 配下（.claude/reports/ 外）のファイルを main() に渡したとき、
        ファイルが書き換えられず、stderr に skip メッセージが出ること。

        [B-2 Green 回帰防止] tmp_path 配下（.claude/reports/ 外）のファイルを main() に渡したとき、
        ファイルが書き換えられず、stderr に skip メッセージが出ることを保証する。
        """
        mod = _load_hook_module()

        # .claude/reports/ 配下ではないテンポラリパス
        outside_report = tmp_path / "outside.md"
        original_content = "# Outside Report\n\n[CR-Q-001] some finding\n"
        outside_report.write_text(original_content, encoding="utf-8")

        rc = mod.main([str(outside_report)])

        # セッションを止めない方針: 戻り値は 0
        assert rc == 0, f"main() は範囲外パスに対して 0 を返すべきだが {rc} が返された"

        # ファイルが書き換えられていないこと
        actual_content = outside_report.read_text(encoding="utf-8")
        assert actual_content == original_content, (
            ".claude/reports/ 外のファイルが書き換えられた。"
            "範囲外パスは処理スキップする必要がある。 [B-2 / SR-V-002]"
        )

        # stderr に skip ログが出ること
        captured = capsys.readouterr()
        assert "path outside reports/" in captured.err or "skipped" in captured.err, (
            "範囲外パスに対して stderr に skip メッセージが出力されていない。 [B-2 / SR-V-002]"
        )

    def test_main_processes_path_inside_reports_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """正常パス（ALLOWED_REPORT_DIR 配下のファイル）では従来通り動作すること。

        monkeypatch で ALLOWED_REPORT_DIR を tmp_path に差し替え、
        実際の .claude/reports/ ディレクトリへの書き込みを発生させない。
        本テストは Green 回帰防止テスト。
        """
        mod = _load_hook_module()
        # ALLOWED_REPORT_DIR を tmp_path に差し替え（実 .claude/reports/ を汚さない）
        monkeypatch.setattr(mod, "ALLOWED_REPORT_DIR", tmp_path.resolve())

        tmp_report_path = tmp_path / "code-review-report.md"
        original_content = "# Code Review Report\n\n[CR-Q-001] some finding\n"
        tmp_report_path.write_text(original_content, encoding="utf-8")

        rc = mod.main([str(tmp_report_path)])
        assert rc == 0, (
            "main() は ALLOWED_REPORT_DIR 配下の正常パスに対して 0 を返すべきだが "
            f"{rc} が返された"
        )
        # 正常パスが処理対象に含まれる（ファイルの存在確認を通過する）ことを確認。
        # DB が空のため hint は追記されないが、ファイル自体は読み込まれる。
        assert tmp_report_path.exists(), "ALLOWED_REPORT_DIR 配下のファイルが削除された"
