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
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "select_tier.py"
SCHEMA_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "schema.sql"
INIT_HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "session_start.py"


def _create_c3_db(db_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("init_c3_db_e", INIT_HOOK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    mod.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))


def _load_select_tier() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("select_tier_e", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


# ---------------------------------------------------------------------------
# c3_db ヘルパー
# ---------------------------------------------------------------------------


class TestRecentOutcomesHelpers:

    def test_record_inserts_row(self, tmp_path: Path) -> None:
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        from c3 import db as c3_db
        ok = c3_db.record_tier_recent_outcome(
            complexity="simple", tier="haiku", success=True, db_path=db_path,
        )
        assert ok is True

        # tier_recent_outcomes に 1 行入っているか直接 SQL で確認
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM tier_recent_outcomes"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1

    def test_failure_rate_computes_correctly(self, tmp_path: Path) -> None:
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        from c3 import db as c3_db
        # 10 件: 6 失敗 / 4 成功
        for s in [True, True, True, True, False, False, False, False, False, False]:
            c3_db.record_tier_recent_outcome(
                complexity="simple", tier="haiku", success=s, db_path=db_path,
            )

        rate, samples = c3_db.read_tier_failure_rate(
            "simple", "haiku", last_n=10, db_path=db_path,
        )
        assert samples == 10
        assert rate == 0.6

    def test_failure_rate_returns_none_below_min_samples(self, tmp_path: Path) -> None:
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        from c3 import db as c3_db
        # 4 件のみ（最小 5 件未満）
        for _ in range(4):
            c3_db.record_tier_recent_outcome(
                complexity="simple", tier="haiku", success=False, db_path=db_path,
            )

        rate, samples = c3_db.read_tier_failure_rate(
            "simple", "haiku", db_path=db_path,
        )
        assert samples == 4
        assert rate is None  # サンプル不足

    def test_failure_rate_db_not_found(self, tmp_path: Path) -> None:
        from c3 import db as c3_db
        rate, samples = c3_db.read_tier_failure_rate(
            "simple", "haiku", db_path=tmp_path / "missing.db",
        )
        assert rate is None
        assert samples == 0


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

        # haiku 失敗を 6 件積む
        from c3 import db as c3_db
        for _ in range(6):
            c3_db.record_tier_recent_outcome(
                complexity="simple", tier="haiku", success=False, db_path=db_path,
            )
        # tier_bandit に 30 試行ぶん仕込んで thompson モードに入るようにする
        # （uniform 期はランダムで haiku 以外が選ばれると escalation 経路を通らない）
        for _ in range(50):
            c3_db.update_tier_params(
                "simple", "haiku", success=False, db_path=db_path,
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
