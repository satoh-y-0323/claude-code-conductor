"""Tests for src/c3/cli_tier.py (tier-routing 効果計測 CLI)."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from c3 import cli_tier
from c3 import db as c3_db


WORKTREE_ROOT = Path(__file__).parents[1]
INIT_HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "session_start.py"


def _create_c3_db(db_path: Path) -> None:
    from c3.migrate import apply_pending_migrations
    apply_pending_migrations(db_path)


def _seed_bandit(
    db_path: Path,
    *,
    complexity: str,
    tier: str,
    alpha: float,
    beta: float,
    trials: int,
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO tier_bandit "
            "(task_complexity, tier, alpha, beta, trials, last_updated) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (complexity, tier, alpha, beta, trials, ts),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_recent_outcome(
    db_path: Path,
    *,
    complexity: str,
    tier: str,
    success: int,
    ts: str | None = None,
    session_id: str | None = None,
) -> None:
    if ts is None:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO tier_recent_outcomes "
            "(task_complexity, tier, success, ts, session_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (complexity, tier, success, ts, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def _make_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        as_json=False,
        recent=10,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _run(args: argparse.Namespace, db_path: Path,
         monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: db_path)
    return cli_tier.handle_stats(args)


# ---------------------------------------------------------------------------
# テストケース
# ---------------------------------------------------------------------------


class TestTierStatsCli:

    def test_stats_empty_db_shows_collecting_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture
    ) -> None:
        """空 DB で「学習データ収集中」メッセージが表示される。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "学習データ収集中" in out
        assert "0 / 30 試行" in out
        # 9 通り（complexity 3 × tier 3）の行がある
        assert out.count("50.00%") == 9
        # outcome 履歴は記録なし
        assert "（記録なし）" in out

    def test_stats_with_bandit_data(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture
    ) -> None:
        """tier_bandit に値が入った状態で trials / 期待成功率が反映される。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        # haiku が成功多め, sonnet 平均, opus 失敗多め (complex)
        _seed_bandit(db, complexity="complex", tier="haiku",
                     alpha=10.0, beta=2.0, trials=10)
        _seed_bandit(db, complexity="complex", tier="sonnet",
                     alpha=5.0, beta=5.0, trials=8)
        _seed_bandit(db, complexity="complex", tier="opus",
                     alpha=2.0, beta=8.0, trials=8)

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        # 試行合計 26 ≥ threshold 30 ではないので uniform mode
        # 26 < 30 なので「学習データ収集中」のはず
        assert "26 / 30 試行" in out
        # 期待成功率: haiku=10/12=83.33%, sonnet=50.00%, opus=2/10=20.00%
        assert "83.33%" in out
        assert "20.00%" in out

    def test_stats_recent_outcomes_displayed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture
    ) -> None:
        """tier_recent_outcomes が時系列降順で表示される。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_recent_outcome(db, complexity="simple", tier="haiku", success=1,
                             ts="2026-05-10T10:00:00+00:00")
        _seed_recent_outcome(db, complexity="medium", tier="sonnet", success=0,
                             ts="2026-05-10T11:00:00+00:00")

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        # 最新 (11:00) が先に出る
        idx_11 = out.find("2026-05-10T11:00:00")
        idx_10 = out.find("2026-05-10T10:00:00")
        assert idx_11 != -1 and idx_10 != -1
        assert idx_11 < idx_10
        assert "success" in out
        assert "failure" in out
        # 「最新 2 件」と件数が反映される
        assert "最新 2 件" in out

    def test_stats_recent_limit_respected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture
    ) -> None:
        """--recent N で表示件数が制限される。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        for i in range(5):
            _seed_recent_outcome(
                db, complexity="simple", tier="haiku", success=1,
                ts=f"2026-05-10T0{i}:00:00+00:00",
            )

        rc = _run(_make_args(recent=2), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        # 5 件 seed したが 2 件しか表示されない
        assert "最新 2 件" in out
        assert out.count("success") == 2 or "success" in out

    def test_stats_json_output_structure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture
    ) -> None:
        """--json で構造化された JSON が出力される。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_bandit(db, complexity="medium", tier="haiku",
                     alpha=3.0, beta=1.0, trials=2)

        rc = _run(_make_args(as_json=True), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)

        # 必須トップレベルキーが揃う
        assert "learning_progress" in data
        assert "tier_bandit" in data
        assert "recent_outcomes" in data

        # learning_progress
        assert data["learning_progress"]["trials"] == 2
        assert data["learning_progress"]["threshold"] == 30
        assert data["learning_progress"]["mode"] == "uniform"

        # tier_bandit には全 9 通りが入る
        assert len(data["tier_bandit"]) == 9

        # haiku/medium のエントリを確認
        target = next(
            r for r in data["tier_bandit"]
            if r["complexity"] == "medium" and r["tier"] == "haiku"
        )
        assert target["alpha"] == 3.0
        assert target["beta"] == 1.0
        assert target["trials"] == 2
        assert target["expected_success_rate"] == 0.75

    def test_stats_db_missing_returns_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture
    ) -> None:
        """DB 不在時は exit 1 + stderr エラーメッセージ。"""
        nonexistent = tmp_path / "nonexistent.db"
        # locate_c3_db は None または存在しないパスを返す
        monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: nonexistent)

        rc = cli_tier.handle_stats(_make_args())

        assert rc == 1
        err = capsys.readouterr().err
        assert "DB が見つかりません" in err

    def test_stats_threshold_reached_switches_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture
    ) -> None:
        """合計 trials >= 30 で Thompson Sampling モード表示になる。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        # 30 試行ぴったり
        _seed_bandit(db, complexity="simple", tier="haiku",
                     alpha=10.0, beta=20.0, trials=30)

        rc = _run(_make_args(as_json=True), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["learning_progress"]["trials"] == 30
        assert data["learning_progress"]["mode"] == "thompson"

    # -------------------------------------------------------------------------
    # v2.21.0: Agent 別コスト集計（agent_cost_runs）のテスト
    # -------------------------------------------------------------------------

    def test_stats_json_contains_agent_cost_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture
    ) -> None:
        """agent_cost_runs に seed → --json 出力に agent_cost セクションが含まれ集計値が正しい。"""
        from c3.db import insert_agent_cost_run
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        # developer subagent の cost を seed
        insert_agent_cost_run(
            session_id="aaaaaaaa-0000-0000-0000-000000000001",
            agent_id="agent-deadbeef",
            agent_type="developer",
            description="test developer",
            model="claude-sonnet-4-6-20251101",
            attribution_skill=None,
            input_tokens=1000,
            output_tokens=500,
            cache_read_tokens=200,
            cache_create_tokens=100,
            total_cost_usd=0.0075,
            db_path=db,
        )

        rc = _run(_make_args(as_json=True), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)

        assert "agent_cost" in data
        assert len(data["agent_cost"]) == 1
        row = data["agent_cost"][0]
        assert row["agent_type"] == "developer"
        assert row["runs"] == 1
        assert row["input_tokens"] == 1000
        assert row["output_tokens"] == 500
        assert row["cache_read_tokens"] == 200
        assert row["cache_create_tokens"] == 100
        assert abs(row["total_cost_usd"] - 0.0075) < 1e-9

    def test_stats_human_shows_no_cost_data_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture
    ) -> None:
        """agent_cost_runs が 0 件のとき human 表示に「コストデータ未収集」文言が出る。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "コストデータ未収集" in out

    def test_stats_human_mainline_shows_note(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture
    ) -> None:
        """mainline 行に「tier 学習対象外」注記が出る。"""
        from c3.db import insert_agent_cost_run
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        insert_agent_cost_run(
            session_id="aaaaaaaa-0000-0000-0000-000000000002",
            agent_id="mainline",
            agent_type="mainline",
            description=None,
            model="claude-opus-4-7-20251101",
            attribution_skill=None,
            input_tokens=2000,
            output_tokens=1000,
            cache_read_tokens=0,
            cache_create_tokens=0,
            total_cost_usd=0.015,
            db_path=db,
        )

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "tier 学習対象外" in out


# ---------------------------------------------------------------------------
# v2.22.0: Tier 別コスト表示（tier_cost）のテスト
# ---------------------------------------------------------------------------


class TestTierCostSection:
    """T5: _collect_snapshot の tier_cost キーと _render_human の Tier 別コストセクション。"""

    def test_collect_snapshot_contains_tier_cost_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_collect_snapshot の snapshot に tier_cost キーが含まれる。"""
        from c3 import db as c3_db
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: db)

        snapshot = cli_tier._collect_snapshot(db, recent_limit=10)

        assert "tier_cost" in snapshot

    def test_render_human_tier_cost_empty_shows_no_data_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """tier_cost が空のとき「（cost 紐づけデータ未収集）」文言が出る。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "cost 紐づけデータ未収集" in out

    def test_render_human_tier_cost_section_heading(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """human 出力に Tier 別コストセクション見出しが含まれる。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "Tier 別平均コスト" in out
        assert "粗い概算" in out
        assert "v2.24.0" in out

    def test_render_human_tier_cost_with_data(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """read_tier_cost_summary がデータを返すとき sessions / avg_cost_usd が表示される。"""
        from c3.db import insert_agent_cost_run
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        # 同一 session_id で outcome + agent_cost_runs を seed
        _seed_recent_outcome(
            db, complexity="simple", tier="haiku", success=1,
            session_id="sess-t5-0001",
        )
        insert_agent_cost_run(
            session_id="sess-t5-0001",
            agent_id="agent-001",
            agent_type="developer",
            description="test",
            model="claude-haiku-4-7-20251101",
            attribution_skill=None,
            input_tokens=500,
            output_tokens=200,
            cache_read_tokens=0,
            cache_create_tokens=0,
            total_cost_usd=0.0020,
            db_path=db,
        )

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        # セクション見出し
        assert "Tier 別平均コスト" in out
        # 「cost 紐づけデータ未収集」は出ない
        assert "cost 紐づけデータ未収集" not in out
        # ヘッダ行確認: sessions / avg_usd 列が出る
        assert "sessions" in out
        assert "avg_usd" in out
        # データ行確認: simple と haiku が同一行に現れる（Tier 別コストセクションのデータ行）
        lines = out.splitlines()
        assert any("simple" in line and "haiku" in line for line in lines), \
            "simple × haiku のデータ行が見つからない"

    def test_render_human_tier_cost_with_data_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """--json 出力の tier_cost キーに complexity/tier/sessions/avg_cost_usd が含まれる。"""
        from c3.db import insert_agent_cost_run
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        _seed_recent_outcome(
            db, complexity="medium", tier="sonnet", success=1,
            session_id="sess-t5-0002",
        )
        insert_agent_cost_run(
            session_id="sess-t5-0002",
            agent_id="agent-002",
            agent_type="developer",
            description="test",
            model="claude-sonnet-4-6-20251101",
            attribution_skill=None,
            input_tokens=1000,
            output_tokens=400,
            cache_read_tokens=0,
            cache_create_tokens=0,
            total_cost_usd=0.0050,
            db_path=db,
        )

        rc = _run(_make_args(as_json=True), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)

        assert "tier_cost" in data
        assert len(data["tier_cost"]) >= 1
        row = next(
            r for r in data["tier_cost"]
            if r["complexity"] == "medium" and r["tier"] == "sonnet"
        )
        assert row["sessions"] == 1
        assert abs(row["avg_cost_usd"] - 0.0050) < 1e-9
        assert abs(row["total_cost_usd"] - 0.0050) < 1e-9

    def test_existing_sections_not_regressed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """既存セクション（tier_bandit / recent_outcomes / 学習データ記録チャネル / agent_cost）が回帰なし。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "== Tier 別累積（tier_bandit） ==" in out
        assert "== 直近 outcome 履歴" in out
        assert "== 学習データ記録チャネル ==" in out
        assert "== Agent 別コスト集計（agent_cost_runs） ==" in out
