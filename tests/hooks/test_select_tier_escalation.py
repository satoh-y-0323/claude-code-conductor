"""Tests for tier-routing Phase 2-B: failure rate ベースの Tier escalation。

検証対象:
  - src/c3/db.py の record_tier_recent_outcome / read_tier_failure_rate
  - .claude/hooks/select_tier.py の maybe_escalate / main 統合
  - schema.sql の tier_recent_outcomes テーブル

テストケース:
 c3_db ヘルパー:
  1. record_tier_recent_outcome: 行が追加される
  2. read_tier_failure_rate: 直近 N 件から rate を計算
  3. read_tier_failure_rate: サンプル数不足（< 5）で None
  4. read_tier_failure_rate: success/failure 混在で正しい rate

 maybe_escalate:
  5. failure_rate >= 0.5 で 1 段昇格（haiku → sonnet）
  6. failure_rate < 0.5 で昇格しない
  7. opus は昇格しない（最上位）
  8. failure_rate が None（サンプル不足）なら昇格しない

 main 統合:
  9. failure rate が高ければ tier_selection.json に escalated=True が書かれる

 v2.26.0 ESCALATION_THRESHOLD 調整可能化（C）:
 10. module.ESCALATION_THRESHOLD == db.ESCALATION_THRESHOLD_DEFAULT == 0.5（SSOT）
 11. 未設定で _resolve_escalation_threshold() → 0.5（無警告）
 12. 空文字で _resolve_escalation_threshold() → 0.5（無警告）
 13. C3_ESCALATION_THRESHOLD=0.7 のとき _resolve_escalation_threshold() → 0.7
 14. C3_ESCALATION_THRESHOLD=0.7 のとき rate=0.6 で昇格しない
 15. C3_ESCALATION_THRESHOLD=0.7 のとき rate=0.7 で昇格する
 16. 不正値（abc）→ 0.5 + stderr 警告
 17. 不正値（0）→ 0.5 + stderr 警告（x <= 0 は境界外）
 18. 不正値（-0.1）→ 0.5 + stderr 警告
 19. 不正値（1.5）→ 0.5 + stderr 警告
 20. 不正値（nan）→ 0.5 + stderr 警告
 21. maybe_escalate(threshold=None) はデフォルト動作と一致（既存シグネチャ不変）
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest
import io

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "select_tier.py"
INIT_HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "session_start.py"


def _create_c3_db(db_path: Path) -> None:
    from c3.migrate import apply_pending_migrations
    apply_pending_migrations(db_path)


def _load_select_tier() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("select_tier_e", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


# ---------------------------------------------------------------------------
# c3_db ヘルパー
#
# [v2.41.0 select-tier-hook] TestRecentOutcomesHelpers の record_tier_recent_outcome /
# read_tier_failure_rate 実 DB 蓄積前提テスト（3 件）は db-shims-and-cost タスクで
# 両関数が deprecated no-op シムに置き換わったため削除した。等価カバレッジは
# role 次元付き新 API のテストとして tests/test_db.py::TestRecordAgentOutcomeEvent /
# TestReadAgentFailureRate（O3/O4 群）が引き継ぐ。
#
# [tier-routing フェーズ2.5・T1 tester 判断（plan writes 外の追加移行）]
# read_tier_failure_rate / record_tier_recent_outcome / update_tier_params の
# 3 シムは ⑤（ADR-25-4）で db.py から完全削除される。本ファイルはこれらを
# モジュールレベルで直接呼び出しており（T1 の plan writes には含まれていな
# かったが、削除後に実 AttributeError で壊れることを grep 監査で発見した
# ため、移行リスク潰しの一環として本ファイルも合わせて更新する）:
#   - test_failure_rate_db_not_found: シムの (None, 0) 後方互換動作を検証
#     していたが、シム自体が消えるため削除する（等価カバレッジは
#     tests/test_db.py::TestReadAgentFailureRate::test_db_absent_returns_none_zero）。
#   - TestMainEscalationIntegration.test_main_writes_escalated_flag /
#     TestResolveEscalationThresholdInMain.test_threshold_07_high_rate_no_escalation:
#     record_tier_recent_outcome / update_tier_params でのデータ投入を
#     record_agent_outcome_event（role="developer", gate="D-2.5"）へ置換する
#     （導出 bandit も同じ agent_outcomes を読むため、BANDIT_GATES 対象 gate の
#     イベントを積むだけで trials 集計・failure rate 計算の両方に反映される）。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# maybe_escalate
# ---------------------------------------------------------------------------


class TestMaybeEscalate:

    def test_escalates_when_failure_rate_high(self) -> None:
        mod = _load_select_tier()
        # haiku で failure rate 0.8 → sonnet へ昇格
        tier, reason = mod.maybe_escalate(
            "simple", "haiku",
            failure_rate_fn=lambda c, t: (0.8, 10),
        )
        assert tier == "sonnet"
        assert reason is not None
        assert "haiku" in reason
        assert "sonnet" in reason

    def test_no_escalation_when_failure_rate_low(self) -> None:
        mod = _load_select_tier()
        tier, reason = mod.maybe_escalate(
            "simple", "haiku",
            failure_rate_fn=lambda c, t: (0.3, 10),
        )
        assert tier == "haiku"
        assert reason is None

    def test_opus_does_not_escalate(self) -> None:
        mod = _load_select_tier()
        tier, reason = mod.maybe_escalate(
            "complex", "opus",
            failure_rate_fn=lambda c, t: (0.9, 10),  # 高くても無視
        )
        assert tier == "opus"
        assert reason is None

    def test_no_escalation_when_samples_insufficient(self) -> None:
        mod = _load_select_tier()
        # rate=None（サンプル不足）→ 昇格しない
        tier, reason = mod.maybe_escalate(
            "simple", "haiku",
            failure_rate_fn=lambda c, t: (None, 3),
        )
        assert tier == "haiku"
        assert reason is None

    def test_threshold_boundary(self) -> None:
        """rate=0.5 ちょうどなら昇格する（>= 0.5）。"""
        mod = _load_select_tier()
        tier, _ = mod.maybe_escalate(
            "simple", "haiku",
            failure_rate_fn=lambda c, t: (0.5, 10),
        )
        assert tier == "sonnet"


# ---------------------------------------------------------------------------
# main 統合
# ---------------------------------------------------------------------------


class TestMainEscalationIntegration:

    def test_main_writes_escalated_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """failure rate が高い状態で main を呼ぶと tier_selection.json に escalated=True が出る。"""
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        # haiku 失敗を 6 件積む（BANDIT_GATES 対象 gate="D-2.5" で record_agent_outcome_event
        # 経由・シム record_tier_recent_outcome は⑤で削除されるため使わない）
        from c3 import db as c3_db
        for i in range(6):
            c3_db.record_agent_outcome_event(
                role="developer", complexity="simple", tier="haiku", success=False,
                gate="D-2.5", session_id=f"escalation-haiku-fail-{i}", db_path=db_path,
            )
        # role=developer/complexity=simple の合計 trials を 30 試行以上にして
        # thompson モードに入るようにする（uniform 期はランダムで haiku 以外が
        # 選ばれると escalation 経路を通らない）。導出 bandit は agent_outcomes の
        # BANDIT_GATES 対象イベントを role×complexity で合算するため、haiku の
        # failure rate を汚さないよう別 tier（opus）に success を積む
        # （シム update_tier_params は⑤で削除されるため使わない）。
        for i in range(30):
            c3_db.record_agent_outcome_event(
                role="developer", complexity="simple", tier="opus", success=True,
                gate="D-2.5", session_id=f"escalation-trials-{i}", db_path=db_path,
            )

        monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: db_path)

        mod = _load_select_tier()
        sel_path = tmp_path / "tier_selection.json"
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(sel_path))

        import io
        payload = {"prompt": "typo を修正してください"}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

        rc = mod.main()
        assert rc == 0

        # tier_selection.json を確認: escalated フラグがあるか
        assert sel_path.is_file()
        data = json.loads(sel_path.read_text(encoding="utf-8"))
        # haiku 失敗率が高い → sonnet に escalation されている可能性が高い
        # （Beta sampling で thompson が opus を選ぶこともあるが、その場合は escalation しない）
        if data.get("tier") == "sonnet" and data.get("escalated"):
            assert data["escalated"] is True
            assert "escalation_reason" in data
        # それ以外（thompson が opus 等を選んだ）は escalation 不要なので test 成立


# ---------------------------------------------------------------------------
# v2.26.0: ESCALATION_THRESHOLD 調整可能化（C）
# ---------------------------------------------------------------------------


class TestEscalationThresholdSSOT:
    """SSOT: module.ESCALATION_THRESHOLD == db.ESCALATION_THRESHOLD_DEFAULT == 0.5。"""

    def test_ssot_module_equals_db_default(self) -> None:
        """module 定数が db 定数と等しく、値は 0.5。"""
        from c3 import db as c3_db
        mod = _load_select_tier()
        assert mod.ESCALATION_THRESHOLD == 0.5
        assert c3_db.ESCALATION_THRESHOLD_DEFAULT == 0.5
        assert mod.ESCALATION_THRESHOLD == c3_db.ESCALATION_THRESHOLD_DEFAULT


class TestResolveEscalationThreshold:
    """_resolve_escalation_threshold() の env 解決ロジック（#11〜#20）。"""

    def test_unset_returns_default_no_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """未設定のとき 0.5 を返し stderr に何も出ない。"""
        monkeypatch.delenv("C3_ESCALATION_THRESHOLD", raising=False)
        mod = _load_select_tier()
        result = mod._resolve_escalation_threshold()
        assert result == 0.5
        assert capsys.readouterr().err == ""

    def test_empty_string_returns_default_no_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """空文字のとき 0.5 を返し stderr に何も出ない。"""
        monkeypatch.setenv("C3_ESCALATION_THRESHOLD", "")
        mod = _load_select_tier()
        result = mod._resolve_escalation_threshold()
        assert result == 0.5
        assert capsys.readouterr().err == ""

    def test_valid_value_07(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """C3_ESCALATION_THRESHOLD=0.7 のとき 0.7 を返す（無警告）。"""
        monkeypatch.setenv("C3_ESCALATION_THRESHOLD", "0.7")
        mod = _load_select_tier()
        result = mod._resolve_escalation_threshold()
        assert result == pytest.approx(0.7)
        assert capsys.readouterr().err == ""

    def test_valid_boundary_1(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """C3_ESCALATION_THRESHOLD=1.0 は妥当上限（警告なし）。"""
        monkeypatch.setenv("C3_ESCALATION_THRESHOLD", "1.0")
        mod = _load_select_tier()
        result = mod._resolve_escalation_threshold()
        assert result == pytest.approx(1.0)
        assert capsys.readouterr().err == ""

    def test_invalid_non_numeric(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """非数値 'abc' → 0.5 + stderr 警告。"""
        monkeypatch.setenv("C3_ESCALATION_THRESHOLD", "abc")
        mod = _load_select_tier()
        result = mod._resolve_escalation_threshold()
        assert result == 0.5
        err = capsys.readouterr().err
        assert "invalid" in err
        assert "C3_ESCALATION_THRESHOLD" in err

    def test_invalid_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """0 は x<=0 境界外 → 0.5 + stderr 警告。"""
        monkeypatch.setenv("C3_ESCALATION_THRESHOLD", "0")
        mod = _load_select_tier()
        result = mod._resolve_escalation_threshold()
        assert result == 0.5
        err = capsys.readouterr().err
        assert "out of range" in err

    def test_invalid_negative(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """-0.1 → 0.5 + stderr 警告。"""
        monkeypatch.setenv("C3_ESCALATION_THRESHOLD", "-0.1")
        mod = _load_select_tier()
        result = mod._resolve_escalation_threshold()
        assert result == 0.5
        err = capsys.readouterr().err
        assert "out of range" in err

    def test_invalid_too_large(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """1.5 → 0.5 + stderr 警告。"""
        monkeypatch.setenv("C3_ESCALATION_THRESHOLD", "1.5")
        mod = _load_select_tier()
        result = mod._resolve_escalation_threshold()
        assert result == 0.5
        err = capsys.readouterr().err
        assert "out of range" in err

    def test_invalid_nan(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """NaN → 0.5 + stderr 警告。"""
        monkeypatch.setenv("C3_ESCALATION_THRESHOLD", "nan")
        mod = _load_select_tier()
        result = mod._resolve_escalation_threshold()
        assert result == 0.5
        err = capsys.readouterr().err
        assert "NaN" in err


class TestMaybeEscalateThresholdKwarg:
    """maybe_escalate の threshold kwarg（#14・#15・#21）。"""

    def test_threshold_07_rate_06_no_escalation(self) -> None:
        """threshold=0.7 のとき rate=0.6 は昇格しない。"""
        mod = _load_select_tier()
        tier, reason = mod.maybe_escalate(
            "simple", "haiku",
            failure_rate_fn=lambda c, t: (0.6, 10),
            threshold=0.7,
        )
        assert tier == "haiku"
        assert reason is None

    def test_threshold_07_rate_07_escalates(self) -> None:
        """threshold=0.7 のとき rate=0.7 で昇格する（>= 閾値）。"""
        mod = _load_select_tier()
        tier, reason = mod.maybe_escalate(
            "simple", "haiku",
            failure_rate_fn=lambda c, t: (0.7, 10),
            threshold=0.7,
        )
        assert tier == "sonnet"
        assert reason is not None

    def test_threshold_none_uses_default(self) -> None:
        """threshold=None でモジュール定数 ESCALATION_THRESHOLD（0.5）が使われる（既存動作不変）。"""
        mod = _load_select_tier()
        # rate=0.5 ちょうど → threshold=None → ESCALATION_THRESHOLD=0.5 → 昇格する
        tier, _ = mod.maybe_escalate(
            "simple", "haiku",
            failure_rate_fn=lambda c, t: (0.5, 10),
            threshold=None,
        )
        assert tier == "sonnet"

    def test_existing_signature_unchanged(self) -> None:
        """threshold 引数なしの既存呼び出しが壊れていないことを確認。"""
        mod = _load_select_tier()
        # 既存シグネチャ: maybe_escalate(complexity, chosen_tier, *, failure_rate_fn=...)
        tier, reason = mod.maybe_escalate(
            "simple", "haiku",
            failure_rate_fn=lambda c, t: (0.3, 10),
        )
        assert tier == "haiku"
        assert reason is None

    def test_threshold_boundary_05_unchanged(self) -> None:
        """rate=0.5 で昇格する既存動作（test_threshold_boundary）が不変。"""
        mod = _load_select_tier()
        tier, _ = mod.maybe_escalate(
            "simple", "haiku",
            failure_rate_fn=lambda c, t: (0.5, 10),
        )
        assert tier == "sonnet"


class TestResolveEscalationThresholdInMain:
    """C3_ESCALATION_THRESHOLD env が main() 経由で実際に効く結合テスト。"""

    def test_threshold_07_high_rate_no_escalation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C3_ESCALATION_THRESHOLD=0.7 + failure rate 0.6 のとき escalation しない。"""
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        from c3 import db as c3_db
        # haiku の failure を 6/10 件積む（rate ≈ 0.6・BANDIT_GATES 対象 gate="D-2.5"。
        # シム record_tier_recent_outcome は⑤で削除されるため使わない）
        for i, s in enumerate(
            [True, True, True, True, False, False, False, False, False, False]
        ):
            c3_db.record_agent_outcome_event(
                role="developer", complexity="simple", tier="haiku", success=s,
                gate="D-2.5", session_id=f"threshold-haiku-{i}", db_path=db_path,
            )
        # thompson モード（30 試行超）に入るよう role=developer/complexity=simple の
        # 合計 trials を稼ぐ（haiku の failure rate を汚さないよう別 tier=opus に
        # success を積む。シム update_tier_params は⑤で削除されるため使わない）
        for i in range(30):
            c3_db.record_agent_outcome_event(
                role="developer", complexity="simple", tier="opus", success=True,
                gate="D-2.5", session_id=f"threshold-trials-{i}", db_path=db_path,
            )

        monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: db_path)
        monkeypatch.setenv("C3_ESCALATION_THRESHOLD", "0.7")

        mod = _load_select_tier()
        sel_path = tmp_path / "tier_selection.json"
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(sel_path))

        payload = {"prompt": "typo を修正してください"}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

        rc = mod.main()
        assert rc == 0

        assert sel_path.is_file()
        data = json.loads(sel_path.read_text(encoding="utf-8"))
        # haiku が選ばれた場合、rate=0.6 < threshold=0.7 なので escalation しないはず
        if data.get("tier") == "haiku" or (
            data.get("tier") != "sonnet"
        ):
            # haiku が選ばれて escalation なし、または opus 選択（escalation 対象外）
            assert not data.get("escalated", False)
        # sonnet が select 段階で選ばれた場合も escalation 不要（escalation は haiku→sonnet のみ）
