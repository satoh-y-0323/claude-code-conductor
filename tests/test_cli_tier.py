"""Tests for src/c3/cli_tier.py (tier-routing 効果計測 CLI)。

v2.41.0 cli-tier-stats タスクでの全面書き換え。旧テーブル tier_bandit /
tier_recent_outcomes は migration 004（db-foundation, fab3ed3）で DROP 済みのため、
これらを直接 INSERT する旧テストは恒久的に再現不能。新テーブル
agent_tier_bandit / agent_outcomes（role 次元付き）ベースで全面書き直す。

新コントラクト（architecture-report-20260702-214748.md §3-7 準拠）:

- ``_collect_snapshot(db_path, recent_limit, role_filter=None)`` の返り値キー:
  ``roles`` / ``tier_bandit_by_role`` / ``recent_outcomes`` / ``agent_cost`` /
  ``tier_cost_rate`` / ``routing_params``。
  旧キー ``learning_progress`` / ``tier_bandit``（フラットリスト）/ ``tier_cost``
  は廃止（破壊的変更・互換キー併存なし）。
- ``tier_bandit_by_role[role]`` は ``{"trials", "threshold", "mode", "rows"}``。
  ``rows`` は complexity×tier の 9 行（データが無い role でも defaults で埋まる）。
  human 表示ではデータゼロ（trials==0）の role は「収集中」の 1 行のみ表示し、
  9 行テーブルは省略する。
- ``--role`` 指定時は当該 role のみを ``roles`` / ``tier_bandit_by_role`` /
  ``recent_outcomes`` に含める。未知の role は stderr メッセージ + exit 1。
- 直近 outcome は ``read_recent_agent_outcomes`` 由来で role/gate 列を含む。
- cost 表示は ``read_tier_cost_rate_summary`` のみ直読み。旧
  ``read_tier_bandit_cost``（bandit 行への cost 列埋め込み）と旧
  ``read_tier_cost_summary`` ベースの「Tier 別平均コスト（session 合計 USD）」
  セクションは cli_tier.py から削除する
  （``read_tier_cost_summary`` 自体は JOIN 元 tier_recent_outcomes が
  DROP 済みで恒久的に空リストしか返せなくなったため。tests/test_db.py の
  db-shims-and-cost タスクのコメントで「次タスク cli-tier-stats の判断まで温存」
  とされていた関数であり、本タスクで「表示に使わない」と判断する）。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from c3 import cli_tier
from c3 import db as c3_db


WORKTREE_ROOT = Path(__file__).parents[1]


# ---------------------------------------------------------------------------
# seed ヘルパー
# ---------------------------------------------------------------------------


def _create_c3_db(db_path: Path) -> None:
    from c3.migrate import apply_pending_migrations
    apply_pending_migrations(db_path)


def _seed_bandit(
    db_path: Path,
    *,
    role: str,
    complexity: str,
    tier: str,
    alpha: float,
    beta: float,
    trials: int,
) -> None:
    """agent_tier_bandit に 1 セルを直接 INSERT する（alpha/beta/trials を精密制御）。"""
    conn = sqlite3.connect(str(db_path))
    try:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO agent_tier_bandit "
            "(role, task_complexity, tier, alpha, beta, trials, last_updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (role, complexity, tier, alpha, beta, trials, ts),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_outcome(
    db_path: Path,
    *,
    role: str,
    complexity: str,
    tier: str,
    success: int,
    ts: str,
    gate: str | None = None,
    note: str | None = None,
    session_id: str | None = None,
) -> None:
    """agent_outcomes に 1 行を直接 INSERT する（ts を精密制御して順序テストの
    flaky 化を避ける。record_agent_outcome_event は ts を real-time でしか
    打てないため、順序検証テストでは本ヘルパーを使う）。
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO agent_outcomes "
            "(role, task_complexity, tier, success, gate, note, session_id, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (role, complexity, tier, success, gate, note, session_id, ts),
        )
        conn.commit()
    finally:
        conn.close()


def _make_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        as_json=False,
        recent=10,
        role=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _run(args: argparse.Namespace, db_path: Path,
         monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: db_path)
    return cli_tier.handle_stats(args)


ALL_ROLES = c3_db.AGENT_ROLES


# ---------------------------------------------------------------------------
# DB 不在（回帰なし・cli_tier.py 未改修でも Green のはず）
# ---------------------------------------------------------------------------


class TestDbMissing:

    def test_stats_db_missing_returns_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture
    ) -> None:
        """DB 不在時は exit 1 + stderr エラーメッセージ（既存動作・回帰なし）。"""
        nonexistent = tmp_path / "nonexistent.db"
        monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: nonexistent)

        rc = cli_tier.handle_stats(_make_args())

        assert rc == 1
        err = capsys.readouterr().err
        assert "DB が見つかりません" in err


# ---------------------------------------------------------------------------
# role 別グループ表示
# ---------------------------------------------------------------------------


class TestRoleGrouping:

    def test_empty_db_all_roles_shown_as_collecting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """空 DB では AGENT_ROLES の全 role が出力に含まれ、いずれも「収集中」。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        for role in ALL_ROLES:
            assert f"[{role}]" in out, f"role={role!r} の見出しが出力に無い"
        assert out.count("収集中") == len(ALL_ROLES)

    def test_role_with_data_shows_full_table_others_still_collecting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """developer にのみデータがある場合、developer は完全テーブル・他は「収集中」のまま。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_bandit(db, role="developer", complexity="complex", tier="haiku",
                     alpha=10.0, beta=2.0, trials=10)

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "83.33%" in out  # haiku 期待成功率 10/12
        # developer 以外は 収集中 のまま（AGENT_ROLES - developer 件）
        assert out.count("収集中") == len(ALL_ROLES) - 1

    def test_collect_snapshot_tier_bandit_by_role_structure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_collect_snapshot が tier_bandit_by_role キーを role ごとの
        {trials, threshold, mode, rows(9件)} 構造で返す。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: db)
        _seed_bandit(db, role="tester", complexity="medium", tier="sonnet",
                     alpha=3.0, beta=1.0, trials=2)

        snapshot = cli_tier._collect_snapshot(db, recent_limit=10)

        assert "tier_bandit_by_role" in snapshot
        by_role = snapshot["tier_bandit_by_role"]
        assert set(by_role.keys()) == set(ALL_ROLES)

        tester_group = by_role["tester"]
        assert tester_group["trials"] == 2
        assert tester_group["threshold"] == c3_db.LEARNING_THRESHOLD
        assert tester_group["mode"] == "uniform"
        assert len(tester_group["rows"]) == 9
        target = next(
            r for r in tester_group["rows"]
            if r["complexity"] == "medium" and r["tier"] == "sonnet"
        )
        assert target["alpha"] == 3.0
        assert target["beta"] == 1.0
        assert target["trials"] == 2
        assert target["expected_success_rate"] == 0.75

        # データの無い role は trials=0 の defaults 9 行
        interviewer_group = by_role["interviewer"]
        assert interviewer_group["trials"] == 0
        assert len(interviewer_group["rows"]) == 9

    def test_json_output_no_legacy_top_level_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """--json トップレベルに旧キー（learning_progress / tier_bandit フラット /
        tier_cost）が存在しない（破壊的変更・互換キー併存なし）。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(as_json=True), db, monkeypatch)

        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert "learning_progress" not in data
        assert "tier_bandit" not in data
        assert "tier_cost" not in data
        assert "tier_bandit_by_role" in data
        assert "roles" in data

    def test_json_roles_key_lists_all_agent_roles_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(as_json=True), db, monkeypatch)

        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert set(data["roles"]) == set(ALL_ROLES)

    def test_threshold_reached_switches_mode_to_thompson(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """developer × simple の trials 合計が LEARNING_THRESHOLD 以上で
        当該 role の mode が thompson になる。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_bandit(db, role="developer", complexity="simple", tier="haiku",
                     alpha=10.0, beta=20.0, trials=c3_db.LEARNING_THRESHOLD)

        rc = _run(_make_args(as_json=True), db, monkeypatch)

        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        dev_group = data["tier_bandit_by_role"]["developer"]
        assert dev_group["trials"] == c3_db.LEARNING_THRESHOLD
        assert dev_group["mode"] == "thompson"
        # 他 role は影響を受けない（uniform のまま）
        assert data["tier_bandit_by_role"]["tester"]["mode"] == "uniform"


# ---------------------------------------------------------------------------
# --role フィルタ
# ---------------------------------------------------------------------------


class TestRoleFilter:

    def test_role_filter_limits_human_output_to_one_role(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(role="developer"), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "[developer]" in out
        for role in ALL_ROLES:
            if role != "developer":
                assert f"[{role}]" not in out, f"--role developer なのに {role} が出力されている"

    def test_role_filter_json_roles_key_is_single_element(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(as_json=True, role="tester"), db, monkeypatch)

        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["roles"] == ["tester"]
        assert set(data["tier_bandit_by_role"].keys()) == {"tester"}

    def test_role_filter_invalid_role_returns_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """AGENT_ROLES に無い --role は stderr メッセージ + exit 1（DB アクセス前に弾く）。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(role="not-a-real-role"), db, monkeypatch)

        assert rc == 1
        err = capsys.readouterr().err
        assert "not-a-real-role" in err

    def test_role_filter_invalid_role_json_mode_also_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(as_json=True, role="bogus"), db, monkeypatch)

        assert rc == 1
        err = capsys.readouterr().err
        assert "bogus" in err, (
            "role 検証エラーメッセージに指定した role 名が含まれていない"
            "（現状は無関係な AttributeError で偶然 rc==1 になっている可能性がある）"
        )


# ---------------------------------------------------------------------------
# 直近 outcome 表（role/gate 列）
# ---------------------------------------------------------------------------


class TestRecentOutcomesTable:

    def test_recent_outcomes_human_header_includes_role_and_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_outcome(db, role="developer", complexity="medium", tier="sonnet",
                      success=1, gate="D-2.5", ts="2026-07-01T10:00:00+00:00")

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "role" in out
        assert "gate" in out
        assert "developer" in out
        assert "D-2.5" in out

    def test_recent_outcomes_ordered_by_ts_desc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_outcome(db, role="developer", complexity="simple", tier="haiku",
                      success=1, gate="D-2.5", ts="2026-07-01T10:00:00+00:00")
        _seed_outcome(db, role="tester", complexity="medium", tier="sonnet",
                      success=0, gate="D-3", ts="2026-07-01T11:00:00+00:00")

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        idx_11 = out.find("2026-07-01T11:00:00")
        idx_10 = out.find("2026-07-01T10:00:00")
        assert idx_11 != -1 and idx_10 != -1
        assert idx_11 < idx_10

    def test_recent_outcomes_role_filter_excludes_other_roles(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_outcome(db, role="developer", complexity="simple", tier="haiku",
                      success=1, gate="D-2.5", ts="2026-07-01T10:00:00+00:00")
        _seed_outcome(db, role="tester", complexity="medium", tier="sonnet",
                      success=0, gate="D-3", ts="2026-07-01T11:00:00+00:00")

        rc = _run(_make_args(role="developer"), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "D-2.5" in out
        assert "D-3" not in out

    def test_recent_outcomes_empty_shows_no_records_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "（記録なし）" in out

    def test_recent_limit_respected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        for i in range(5):
            _seed_outcome(
                db, role="developer", complexity="simple", tier="haiku",
                success=1, gate="D-2.5",
                ts=f"2026-07-01T0{i}:00:00+00:00",
            )

        rc = _run(_make_args(recent=2), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "最新 2 件" in out

    def test_json_recent_outcomes_contain_role_and_gate_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_outcome(db, role="developer", complexity="medium", tier="sonnet",
                      success=1, gate="D-2.5", ts="2026-07-01T10:00:00+00:00")

        rc = _run(_make_args(as_json=True), db, monkeypatch)

        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data["recent_outcomes"]) == 1
        row = data["recent_outcomes"][0]
        assert row["role"] == "developer"
        assert row["gate"] == "D-2.5"
        assert row["complexity"] == "medium"
        assert row["tier"] == "sonnet"
        assert row["success"] is True


# ---------------------------------------------------------------------------
# Agent 別コスト集計（agent_cost_runs、role 変更の影響を受けない・回帰確認）
# ---------------------------------------------------------------------------


class TestAgentCostSectionUnaffected:

    def test_json_contains_agent_cost_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        from c3.db import insert_agent_cost_run
        db = tmp_path / "c3.db"
        _create_c3_db(db)

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
        data = json.loads(capsys.readouterr().out)
        assert "agent_cost" in data
        assert len(data["agent_cost"]) == 1
        row = data["agent_cost"][0]
        assert row["agent_type"] == "developer"
        assert row["runs"] == 1

    def test_human_shows_no_cost_data_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "コストデータ未収集" in out


# ---------------------------------------------------------------------------
# cost 表示: read_tier_cost_rate_summary 直読みのみ（旧 tier_cost セクション廃止）
# ---------------------------------------------------------------------------


class TestCostRateDirectRead:

    def test_legacy_tier_cost_key_removed_from_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """旧 tier_cost（read_tier_cost_summary ベース・session 合計 USD 概算）
        キーが --json から消えている（read_tier_cost_summary は JOIN 元
        tier_recent_outcomes が DROP 済みで恒久的に空リストしか返せないため
        表示対象から外す判断・本タスクの設計判断）。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(as_json=True), db, monkeypatch)

        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert "tier_cost" not in data

    def test_legacy_tier_cost_section_removed_from_human_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "Tier 別平均コスト" not in out
        assert "cost 紐づけデータ未収集" not in out

    def test_rate_section_no_data_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "== Tier 別 USD/MTok レート（model 一致・tie-break が使用） ==" in out
        assert "（rate データ未収集）" in out

    def test_rate_section_with_data(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """agent_outcomes + agent_cost_runs を同一 session_id で seed すると
        read_tier_cost_rate_summary が非空になり rate セクションに表示される。"""
        from c3.db import insert_agent_cost_run
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        _seed_outcome(
            db, role="developer", complexity="simple", tier="haiku",
            success=1, ts="2026-07-01T10:00:00+00:00",
            session_id="sess-rate-0001",
        )
        insert_agent_cost_run(
            session_id="sess-rate-0001",
            agent_id="agent-rate-001",
            agent_type="developer",
            description="rate test",
            model="claude-haiku-4-7-20251101",
            attribution_skill=None,
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=0,
            cache_create_tokens=0,
            total_cost_usd=0.0075,
            db_path=db,
        )

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "（rate データ未収集）" not in out
        lines = out.splitlines()
        assert any("simple" in line and "haiku" in line for line in lines)
        # rate = 0.0075 / (150 / 1_000_000) = 50.0 USD/MTok
        assert "50.0000" in out

    def test_json_tier_cost_rate_key_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        from c3.db import insert_agent_cost_run
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        _seed_outcome(
            db, role="developer", complexity="medium", tier="sonnet",
            success=1, ts="2026-07-01T10:00:00+00:00",
            session_id="sess-rate-json-0001",
        )
        insert_agent_cost_run(
            session_id="sess-rate-json-0001",
            agent_id="agent-rate-json-001",
            agent_type="developer",
            description="rate json test",
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
        data = json.loads(capsys.readouterr().out)
        assert "tier_cost_rate" in data
        row = next(
            r for r in data["tier_cost_rate"]
            if r["complexity"] == "medium" and r["tier"] == "sonnet"
        )
        assert row["sessions"] == 1
        assert row["rate_usd_per_mtok"] > 0


# ---------------------------------------------------------------------------
# routing パラメータ（role 変更の影響を受けない・回帰確認）
# ---------------------------------------------------------------------------


class TestRoutingParamsSectionUnaffected:

    def test_json_output_contains_routing_params_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        monkeypatch.delenv("C3_TIER_COST_LAMBDA", raising=False)

        rc = _run(_make_args(as_json=True), db, monkeypatch)

        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert "routing_params" in data
        rp = data["routing_params"]
        assert "cost_lambda" in rp
        assert "epsilon" in rp
        assert "escalation_threshold" in rp

    def test_render_human_shows_routing_params_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        monkeypatch.delenv("C3_TIER_COST_LAMBDA", raising=False)

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "routing パラメータ" in out


# ---------------------------------------------------------------------------
# 学習データ記録チャネル文言: record_agent_outcome.py 参照に更新
# （旧 record_tier_outcome.py は record-script タスクで完全削除済みのため、
#  出力に古いファイル名が残っていると誤案内になる）
# ---------------------------------------------------------------------------


class TestLearningChannelText:

    def test_human_output_references_new_script_not_old(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "record_agent_outcome.py" in out
        assert "record_tier_outcome.py" not in out

    def test_human_output_reflects_actual_recording_scope_not_e_final_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """[CR-NEW] Medium: 「記録元: dev-workflow フェーズ E の最終承認時のみ」は
        再設計そのものが否定した旧仕様の記述であり、実態（A-4/B-3/C-2/C-3/D-2.5/D-3/
        D-5/E-1/E-2・parallel-agents 2-D/2-E の各フェーズ承認ゲート・並列タスク単位）
        と矛盾する。文言をその実態に即した記述へ修正すること。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "フェーズ E の最終承認時のみ" not in out
        assert "各フェーズ" in out
        assert "ゲート" in out


# ---------------------------------------------------------------------------
# SR-V-001 継承: sanitize_terminal_text 適用検証（role グループ見出し・
# recent_outcomes の role/gate 列を新たにカバー）
# ---------------------------------------------------------------------------


def _extract_section(out: str, heading: str) -> str:
    """out から heading を含む行から次の「== 」行の手前までを抽出して返す。"""
    lines = out.splitlines()
    in_section = False
    result_lines = []
    for line in lines:
        if heading in line:
            in_section = True
        if in_section:
            if result_lines and line.startswith("==") and heading not in line:
                break
            result_lines.append(line)
    return "\n".join(result_lines)


class TestSanitizeTerminalText:

    def test_bandit_section_sanitizes_control_chars_in_complexity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """SR-V-001: role グループ内テーブルの complexity に制御文字が
        混入していても出力から除去される。

        テスト修正メモ（developer 判断）: _collect_snapshot は契約
        （test-report-20260703-010910.md §3-1）通り
        ``roles × _COMPLEXITIES`` の固定ループで
        ``read_agent_tier_params(role, complexity)`` を完全一致クエリするため、
        complexity 列に制御文字を含む非正規値の行は該当 role の trials 集計に
        一切カウントされず（＝黙って無視される・クラッシュしない）、班 table 内
        に複合表示されることも無い（rows は常に正規 3 complexity × 3 tier の
        9 行のみで、DB の生値を echo しない）。そのため元のテストのように
        非正規行 1 件のみを seed すると trials==0 のままとなり「収集中」
        表示に倒れ、意図した「テーブルが描画されて simple の文字列を含む」
        検証に到達できなかった（Red 理由が誤り＝テスト側の見落とし）。
        正規値の行を追加 seed して role の trials を非ゼロにし、意図通り
        9 行テーブルが描画されることを確認した上で、制御文字を含む非正規行が
        出力を汚染しない（＝黙って無視される）ことを検証する。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_bandit(db, role="developer", complexity="\x1b[31msimple\x07",
                     tier="haiku", alpha=2.0, beta=1.0, trials=3)
        _seed_bandit(db, role="developer", complexity="simple",
                     tier="sonnet", alpha=1.0, beta=1.0, trials=1)

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        section = _extract_section(out, "== Tier 別累積")
        assert section, "role グループの累積セクションが出力に見つからない"
        assert "\x1b" not in section
        assert "\x07" not in section
        assert "simple" in section

    def test_recent_outcomes_sanitizes_control_chars_in_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """SR-V-001: 直近 outcome 表の gate 列に制御文字が混入していても
        出力から除去される。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_outcome(
            db, role="developer", complexity="medium", tier="sonnet",
            success=1, gate="\x1b[31mD-2.5\x07",
            ts="2026-07-01T10:00:00+00:00",
        )

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        section = _extract_section(out, "== 直近 outcome 履歴")
        assert section, "直近 outcome セクションが出力に見つからない"
        assert "\x1b" not in section
        assert "\x07" not in section
        assert "D-2.5" in section

    def test_agent_type_section_sanitizes_control_chars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """SR-V-001（既存機能・回帰確認）: agent_type セクションのサニタイズ維持。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        conn = sqlite3.connect(str(db))
        try:
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO agent_cost_runs "
                "(session_id, agent_id, agent_type, description, model, "
                " attribution_skill, input_tokens, output_tokens, "
                " cache_read_tokens, cache_create_tokens, total_cost_usd, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "sess-san-agent-001", "agent-san-001",
                    "\x1b[33mdeveloper\x07",
                    "sanitize agent_type test",
                    "claude-haiku-4-7-20251101",
                    None, 100, 50, 0, 0, 0.001, ts,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        agent_section = _extract_section(out, "== Agent 別コスト集計")
        assert agent_section, "agent_type セクションが出力に見つからない"
        assert "\x1b" not in agent_section
        assert "\x07" not in agent_section
        assert "developer" in agent_section
