"""Tests for src/c3/db.py:locate_c3_db() + v2.21.0 usage-ingester ヘルパー

`locate_c3_db()` の 3 経路を契約として固定する回帰テスト:
  1. C3_DB_PATH env (worktree 内子プロセス用、v2.0.0+)
  2. C3_PO_DB_PATH env (legacy, v1.x 互換)
  3. CWD からの親遡り fallback

G 群 (6 件): insert_agent_cost_run / read_agent_cost_summary /
             get_ingest_offset / set_ingest_offset のヘルパーテスト
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

import pytest

from c3.db import (
    _compute_tier_cost_rate_summary,
    get_ingest_offset,
    insert_agent_cost_run,
    locate_c3_db,
    read_agent_cost_summary,
    read_tier_cost_rate_for_complexity,
    read_tier_cost_rate_summary,
    record_agent_outcome_event,
    set_ingest_offset,
)

# NOTE(tier-routing フェーズ2.5・C-3 DC-GP-002 対応): 旧 deprecated シム 5 関数
# （read_tier_params / update_tier_params / record_tier_recent_outcome /
# read_tier_failure_rate / sync_tier_bandit_cost）はトップレベル import に
# 残していると ⑤のシム削除で ImportError となりファイル全体の collection が
# 落ちるため、本ファイルからは意図的に import しない（TestDeprecatedFunctionsRemoved
# が not hasattr で削除を確認する）。


def _make_fake_db(base: Path) -> Path:
    """`base/.claude/state/c3.db` を作って返す。

    locate_c3_db は is_file() 判定のみ行い中身を読まないため、
    空ファイルで十分。
    """
    db_path = base / ".claude" / "state" / "c3.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.touch()
    return db_path


def _assert_fallback_warning(caplog: pytest.LogCaptureFixture) -> None:
    """env が無効値・ディレクトリ等で fallback した時の警告内容を契約として固定する。

    NOTE: src/c3/db.py の現在の警告フォーマット
      "%s set but file not found: %s (falling back to traversal)"
    を現状スナップショットとして検証する。将来ログメッセージを精緻化する場合は
    本ヘルパーを意図的に更新すること（caplog の片側 OR では検出できないため）。
    """
    assert "C3_DB_PATH" in caplog.text, "Warning must mention env var name C3_DB_PATH."
    assert "file not found" in caplog.text, (
        "Warning must mention 'file not found' (current db.py message). "
        "If db.py message is intentionally refined, update this assertion."
    )
    assert "falling back to traversal" in caplog.text, (
        "Warning must mention 'falling back to traversal' (current db.py message). "
        "If db.py message is intentionally refined, update this assertion."
    )


def _iso_days_ago(days: float) -> str:
    """UTC 秒精度 ISO 文字列で `days` 日前の時刻を返す（時間窓テスト用）。"""
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _now_iso() -> str:
    return _iso_days_ago(0)


def test_locate_c3_db_env_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """C3_DB_PATH が設定されていれば、CWD 親遡りより優先される."""
    env_db = _make_fake_db(tmp_path / "env_root")
    cwd_db = _make_fake_db(tmp_path / "cwd_root")
    cwd_subdir = tmp_path / "cwd_root" / "sub" / "deeper"
    cwd_subdir.mkdir(parents=True)

    monkeypatch.setenv("C3_DB_PATH", str(env_db))
    monkeypatch.delenv("C3_PO_DB_PATH", raising=False)

    result = locate_c3_db(start=cwd_subdir)

    assert result == env_db.resolve(), (
        f"C3_DB_PATH should win over parent traversal. "
        f"Expected {env_db.resolve()}, got {result}"
    )
    assert result != cwd_db.resolve(), (
        "Parent traversal should NOT have been triggered while C3_DB_PATH is valid."
    )


def test_locate_c3_db_legacy_env_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """C3_DB_PATH 未設定で C3_PO_DB_PATH が設定されていれば legacy 経路で解決。
    またこのとき deprecation 警告が WARNING で発火する。"""
    legacy_db = _make_fake_db(tmp_path / "legacy_root")

    monkeypatch.delenv("C3_DB_PATH", raising=False)
    monkeypatch.setenv("C3_PO_DB_PATH", str(legacy_db))

    with caplog.at_level(logging.WARNING, logger="c3.db"):
        result = locate_c3_db(start=tmp_path)

    assert result == legacy_db.resolve(), (
        f"C3_PO_DB_PATH should resolve when C3_DB_PATH is unset. "
        f"Expected {legacy_db.resolve()}, got {result}"
    )
    assert "C3_PO_DB_PATH is deprecated" in caplog.text, (
        "Legacy env path should emit a deprecation warning."
    )


def test_locate_c3_db_parent_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """env 未設定なら start から親遡りで .claude/state/c3.db を見つける."""
    root_db = _make_fake_db(tmp_path)
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)

    monkeypatch.delenv("C3_DB_PATH", raising=False)
    monkeypatch.delenv("C3_PO_DB_PATH", raising=False)

    result = locate_c3_db(start=deep)

    assert result == root_db.resolve(), (
        f"Parent traversal should find .claude/state/c3.db. "
        f"Expected {root_db.resolve()}, got {result}"
    )


def test_locate_c3_db_invalid_env_falls_back_to_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """env が設定されていても指すパスが無効ならば親遡り fallback に進む."""
    valid_db = _make_fake_db(tmp_path)

    monkeypatch.setenv("C3_DB_PATH", str(tmp_path / "nonexistent.db"))
    monkeypatch.delenv("C3_PO_DB_PATH", raising=False)

    with caplog.at_level(logging.WARNING, logger="c3.db"):
        result = locate_c3_db(start=tmp_path)

    assert result == valid_db.resolve(), (
        "Invalid env path should fall through to parent traversal."
    )
    _assert_fallback_warning(caplog)


def test_locate_c3_db_env_pointing_to_directory_falls_back_to_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """ディレクトリ指定のテスト（SR L-3 追加）。

    `test_locate_c3_db_invalid_env_falls_back_to_traversal` は env が「存在しない
    ファイルパス」を指すケースをカバーするが、本テストは env が「実在するディレクトリ」
    を指すケースをカバーする（is_file() False の別経路）。setup は env に
    `tmp_path/some_dir` (mkdir 済) を設定し、親遡り fallback 用に
    `_make_fake_db(tmp_path)` で `tmp_path/.claude/state/c3.db` を別途用意する。

    SR L-3 指摘: ディレクトリ指定でも "file not found" のログが出る（診断メッセージの
    誤解可能性）。この挙動をテストとして固定し、将来の修正時に意図的な変更として検出する。
    """
    some_dir = tmp_path / "some_dir"
    some_dir.mkdir()
    valid_db = _make_fake_db(tmp_path)

    monkeypatch.setenv("C3_DB_PATH", str(some_dir))
    monkeypatch.delenv("C3_PO_DB_PATH", raising=False)

    with caplog.at_level(logging.WARNING, logger="c3.db"):
        result = locate_c3_db(start=tmp_path)

    assert result == valid_db.resolve(), (
        "Directory env path should fall through to parent traversal."
    )
    _assert_fallback_warning(caplog)


# ---------------------------------------------------------------------------
# G 群: insert_agent_cost_run / read_agent_cost_summary / offset ヘルパー (6 件)
# ---------------------------------------------------------------------------


def _make_c3_db(tmp_path: Path) -> Path:
    """tmp_path に c3.db を作成して 001+002 migration を適用する。"""
    from c3.migrate import apply_pending_migrations  # noqa: PLC0415
    db_path = tmp_path / "c3.db"
    apply_pending_migrations(db_path)
    return db_path


class TestAgentCostHelpers:
    """G 群: v2.21.0 で追加した 4 ヘルパーのテスト。"""

    def test_insert_and_summary(self, tmp_path: Path):
        """G1: insert_agent_cost_run → read_agent_cost_summary で集計が返る。"""
        db = _make_c3_db(tmp_path)

        ok = insert_agent_cost_run(
            session_id="aaaabbbb-cccc-dddd-eeee-000000000001",
            agent_id="agent-abc",
            agent_type="developer",
            description="Test agent",
            model="claude-sonnet-4-6-20260101",
            attribution_skill=None,
            input_tokens=1000,
            output_tokens=500,
            cache_read_tokens=200,
            cache_create_tokens=100,
            total_cost_usd=0.005,
            db_path=db,
        )
        assert ok is True

        summary = read_agent_cost_summary(db_path=db)
        assert len(summary) == 1
        row = summary[0]
        assert row["agent_type"] == "developer"
        assert row["runs"] == 1
        assert abs(row["total_cost_usd"] - 0.005) < 1e-9
        assert row["input_tokens"] == 1000
        assert row["output_tokens"] == 500

    def test_upsert_overwrites_not_appends(self, tmp_path: Path):
        """G2: 同一 PK で upsert すると上書きされ行が増えない。"""
        db = _make_c3_db(tmp_path)

        kwargs = dict(
            session_id="aaaabbbb-cccc-dddd-eeee-000000000002",
            agent_id="agent-abc",
            agent_type="developer",
            description=None,
            model="claude-sonnet-4-6-20260101",
            attribution_skill=None,
            db_path=db,
        )
        insert_agent_cost_run(
            **kwargs,
            input_tokens=100, output_tokens=50,
            cache_read_tokens=0, cache_create_tokens=0,
            total_cost_usd=0.001,
        )
        insert_agent_cost_run(
            **kwargs,
            input_tokens=200, output_tokens=100,
            cache_read_tokens=0, cache_create_tokens=0,
            total_cost_usd=0.002,
        )

        summary = read_agent_cost_summary(db_path=db)
        assert len(summary) == 1
        assert summary[0]["runs"] == 1, "upsert で行が増えてはいけない"
        assert summary[0]["input_tokens"] == 200, "最新値に上書きされるはず"

    def test_different_model_creates_separate_row(self, tmp_path: Path):
        """G3: 別 model は別行になる（PK に model が含まれる）。"""
        db = _make_c3_db(tmp_path)

        base = dict(
            session_id="aaaabbbb-cccc-dddd-eeee-000000000003",
            agent_id="agent-abc",
            agent_type="developer",
            description=None,
            attribution_skill=None,
            input_tokens=100, output_tokens=50,
            cache_read_tokens=0, cache_create_tokens=0,
            total_cost_usd=0.001,
            db_path=db,
        )
        insert_agent_cost_run(**base, model="claude-sonnet-4-6-20260101")
        insert_agent_cost_run(**base, model="claude-haiku-4-5-20260101")

        summary = read_agent_cost_summary(db_path=db)
        total_runs = sum(r["runs"] for r in summary)
        assert total_runs == 2, f"別 model が別行にならなかった: {summary}"

    def test_get_ingest_offset_unset_returns_zero(self, tmp_path: Path):
        """G4: get_ingest_offset — 未設定キーは 0 を返す。"""
        db = _make_c3_db(tmp_path)
        offset = get_ingest_offset("nonexistent-key", db_path=db)
        assert offset == 0

    def test_set_get_ingest_offset_round_trip(self, tmp_path: Path):
        """G5: set_ingest_offset → get_ingest_offset round-trip。"""
        db = _make_c3_db(tmp_path)

        ok = set_ingest_offset("session-abc:mainline", 42, db_path=db)
        assert ok is True

        offset = get_ingest_offset("session-abc:mainline", db_path=db)
        assert offset == 42

        # 更新も確認
        set_ingest_offset("session-abc:mainline", 99, db_path=db)
        assert get_ingest_offset("session-abc:mainline", db_path=db) == 99

    def test_db_absent_returns_silent_failures(self, tmp_path: Path):
        """G6: DB 不在パスで insert/summary/get/set が False/[]/0/False（静かに失敗）。"""
        absent_db = tmp_path / "nonexistent.db"

        insert_ok = insert_agent_cost_run(
            session_id="aaaabbbb-cccc-dddd-eeee-000000000006",
            agent_id="mainline",
            agent_type="mainline",
            description=None,
            model="claude-opus-4-7-20260101",
            attribution_skill=None,
            input_tokens=10,
            output_tokens=5,
            cache_read_tokens=0,
            cache_create_tokens=0,
            total_cost_usd=0.0,
            db_path=absent_db,
        )
        assert insert_ok is False

        summary = read_agent_cost_summary(db_path=absent_db)
        assert summary == []

        offset = get_ingest_offset("some-key", db_path=absent_db)
        assert offset == 0

        set_ok = set_ingest_offset("some-key", 10, db_path=absent_db)
        assert set_ok is False


# ---------------------------------------------------------------------------
# H 群: record_tier_recent_outcome session_id 拡張 + read_tier_cost_summary (v2.22.0)
# ---------------------------------------------------------------------------


def _make_c3_db_v003(tmp_path: Path) -> Path:
    """tmp_path に c3.db を作成して 003 migration まで適用する。"""
    from c3.migrate import apply_pending_migrations  # noqa: PLC0415
    db_path = tmp_path / "c3.db"
    apply_pending_migrations(db_path)
    return db_path


# 設計判断（db-shims-and-cost タスク）: H 群（TestTierCostHelpers）は
# record_tier_recent_outcome（旧 tier_recent_outcomes への INSERT）を seed 手段として
# read_tier_cost_summary の JOIN 結果を検証していたが、migration 004（db-foundation, fab3ed3）で
# tier_recent_outcomes が DROP 済みのため、record_tier_recent_outcome は本タスクで
# DB 非接続の no-op シムに置換した（ADR-5）。結果として read_tier_cost_summary は
# JOIN 元テーブルが永続的に存在しないため常に [] を返す関数になった
# （read_tier_cost_summary 自体は次タスク cli-tier-stats の判断まで温存・
#  test-report §3-4 参照）。この関数の非空 JOIN 結果を前提にした H 群テストは
# 恒久的に再現不能になったため、テストクラスごと削除する（_seed_cost_run も
# H 群専用ヘルパーのため合わせて削除）。読み出し専用の空リスト回帰は J 群
# （TestReadTierCostRateSummary 等・agent_outcomes ベース）が引き継ぐ。


# 設計判断（db-shims-and-cost タスク）: 旧 read_tier_cost_for_complexity（avg_cost_usd 版・
# v2.23.0）は db.py から削除済み（architecture-report §3-3 削除対象）。当該関数専用の
# I 群テスト（TestReadTierCostForComplexity）は関数不在のため丸ごと削除する。


# ---------------------------------------------------------------------------
# J 群: _compute_tier_cost_rate_summary (純関数) + read_tier_cost_rate_summary
# ---------------------------------------------------------------------------


def _seed_cost_run_with_tokens(
    db: "Path",
    *,
    session_id: str,
    agent_id: str,
    agent_type: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    total_cost_usd: float,
) -> None:
    """agent_cost_runs に token 列付き seed を挿入するヘルパー（J 群専用）。"""
    insert_agent_cost_run(
        session_id=session_id,
        agent_id=agent_id,
        agent_type=agent_type,
        description=None,
        model=model,
        attribution_skill=None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=0,
        cache_create_tokens=0,
        total_cost_usd=total_cost_usd,
        db_path=db,
    )


class TestComputeTierCostRateSummary:
    """J1 群: _compute_tier_cost_rate_summary 純関数の単体テスト（DB 不要）。"""

    def test_rate_formula_hand_calculation(self):
        """J1-1: AC-2 rate 式手計算検証。

        input=100 / output=50 / total_cost_usd=0.0075
        → billable=150 → rate = 0.0075 / (150 / 1_000_000) = 50.0 USD/MTok
        """
        cost_rows = [
            ("sess-j1", "claude-sonnet-4-6-20260101", 0.0075, 100, 50),
        ]
        outcome_rows = [
            ("sess-j1", "medium", "sonnet"),
        ]
        result = _compute_tier_cost_rate_summary(cost_rows, outcome_rows)
        assert len(result) == 1
        row = result[0]
        assert row["complexity"] == "medium"
        assert row["tier"] == "sonnet"
        assert row["sessions"] == 1
        assert abs(row["total_cost_usd"] - 0.0075) < 1e-12
        assert row["billable_tokens"] == 150
        assert abs(row["rate_usd_per_mtok"] - 50.0) < 1e-9

    def test_model_match_haiku_not_in_opus(self):
        """J1-2: AC-1 model 一致のみ集計 — haiku モデル行が opus 集計に混ざらない。

        H5 テストが「session 全体の non-mainline 合算」を固定しているのに対し、
        新関数は resolve_tier(model) で tier を振り分けるため、
        haiku モデル行は haiku バケットにのみ集計される。
        """
        cost_rows = [
            # opus 行
            ("sess-j2", "claude-opus-4-7-20260101", 0.03, 100, 50),
            # 同 session 内の haiku 行（別 agent_id）
            ("sess-j2", "claude-haiku-4-5-20260101", 0.001, 100, 50),
        ]
        outcome_rows = [
            ("sess-j2", "complex", "opus"),
        ]
        result = _compute_tier_cost_rate_summary(cost_rows, outcome_rows)

        # opus outcome に haiku コストは混ざらない
        assert len(result) == 1
        row = result[0]
        assert row["tier"] == "opus"
        # opus モデル行のコストのみ: 0.03 USD / (150/1e6) = 200.0 USD/MTok
        assert abs(row["total_cost_usd"] - 0.03) < 1e-12
        assert row["billable_tokens"] == 150
        assert abs(row["rate_usd_per_mtok"] - 200.0) < 1e-9

    def test_unknown_model_skipped(self):
        """J1-3: AC-3 未知 model（resolve_tier が None）はスキップされる。"""
        cost_rows = [
            ("sess-j3a", "claude-sonnet-4-6-20260101", 0.01, 100, 50),
            ("sess-j3b", "unknown-model-xyz", 0.99, 200, 100),  # unknown
        ]
        outcome_rows = [
            ("sess-j3a", "medium", "sonnet"),
            ("sess-j3b", "medium", "sonnet"),  # cost バケットが存在しないため除外される
        ]
        result = _compute_tier_cost_rate_summary(cost_rows, outcome_rows)

        # sonnet セッションのみ集計される（unknown は bucket に入らず outcome も除外）
        assert len(result) == 1
        row = result[0]
        assert row["tier"] == "sonnet"
        assert row["sessions"] == 1

    def test_mainline_agent_type_excluded(self):
        """J1-4: AC-3 mainline は SQL で除外される前提（純関数は agent_type を受け取らない）。

        純関数は SQL フィルタ後のデータを受け取るため、
        mainline 行が渡ってきた場合でも resolve_tier で tier が振り分けられてしまう。
        実際には read_tier_cost_rate_summary の SQL が WHERE agent_type <> 'mainline' で除外するため、
        純関数にはmainline 行が渡らないことを DB テストで確認する（J2-2 参照）。
        本テストは pure function の境界確認のみ: agent_type 情報なしに model で振り分けること。
        """
        # mainline でも model が sonnet なら sonnet バケットに集計される（SQL フィルタ前提）
        cost_rows = [
            ("sess-j4", "claude-sonnet-4-6-20260101", 0.01, 100, 50),
        ]
        outcome_rows = [
            ("sess-j4", "simple", "sonnet"),
        ]
        result = _compute_tier_cost_rate_summary(cost_rows, outcome_rows)
        assert len(result) == 1

    def test_session_tier_deduplication(self):
        """J1-5: AC-3 (session, tier) 重複排除 — 同一 session で複数 agent_id 行の cost が 1 回集約される。

        PK=(session_id, agent_id, model) のため行は一意だが、
        同一 (session_id, tier) の複数行は cost_sum に加算集約される。
        """
        cost_rows = [
            # 同一 session、同 tier（sonnet）、別 agent_id
            ("sess-j5", "claude-sonnet-4-6-20260101", 0.01, 100, 50),
            ("sess-j5", "claude-sonnet-4-6-20260202", 0.02, 200, 100),
        ]
        outcome_rows = [
            ("sess-j5", "medium", "sonnet"),
        ]
        result = _compute_tier_cost_rate_summary(cost_rows, outcome_rows)

        # sessions=1（同一 session）、cost は両行の合計 0.03
        assert len(result) == 1
        row = result[0]
        assert row["sessions"] == 1
        assert abs(row["total_cost_usd"] - 0.03) < 1e-12
        assert row["billable_tokens"] == 450  # 150 + 300
        expected_rate = 0.03 / (450 / 1_000_000)
        assert abs(row["rate_usd_per_mtok"] - expected_rate) < 1e-9

    def test_billable_tokens_zero_excluded(self):
        """J1-6: AC-4 billable_tokens == 0 の (complexity, tier) は除外される。"""
        cost_rows = [
            ("sess-j6", "claude-haiku-4-5-20260101", 0.001, 0, 0),  # billable=0
        ]
        outcome_rows = [
            ("sess-j6", "simple", "haiku"),
        ]
        result = _compute_tier_cost_rate_summary(cost_rows, outcome_rows)
        assert result == [], "billable_tokens=0 の行は除外されるべき"

    def test_empty_inputs_returns_empty(self):
        """J1-7: 空の入力で空リストを返す。"""
        assert _compute_tier_cost_rate_summary([], []) == []
        assert _compute_tier_cost_rate_summary([], [("s", "c", "t")]) == []
        assert _compute_tier_cost_rate_summary([("s", "m", 0.1, 100, 50)], []) == []

    def test_multiple_complexity_tiers(self):
        """J1-8: 複数 (complexity, tier) が存在する場合、それぞれ独立して集計される。"""
        cost_rows = [
            ("sess-j8a", "claude-haiku-4-5-20260101", 0.001, 100, 50),
            ("sess-j8b", "claude-opus-4-7-20260101", 0.10, 1000, 500),
        ]
        outcome_rows = [
            ("sess-j8a", "simple", "haiku"),
            ("sess-j8b", "complex", "opus"),
        ]
        result = _compute_tier_cost_rate_summary(cost_rows, outcome_rows)
        assert len(result) == 2
        result_by_tier = {r["tier"]: r for r in result}
        assert "haiku" in result_by_tier
        assert "opus" in result_by_tier
        # haiku: 0.001 / (150/1e6) ≈ 6.667 USD/MTok
        assert abs(result_by_tier["haiku"]["rate_usd_per_mtok"] - 0.001 / (150 / 1_000_000)) < 1e-9
        # opus: 0.10 / (1500/1e6) ≈ 66.667 USD/MTok
        assert abs(result_by_tier["opus"]["rate_usd_per_mtok"] - 0.10 / (1500 / 1_000_000)) < 1e-9

    def test_result_sorted_by_rate_descending(self):
        """J1-9: CR-Q-004 返却順が rate_usd_per_mtok 降順であること。

        既存 read_tier_cost_summary の ORDER BY total_cost_usd DESC と対称。
        """
        cost_rows = [
            # haiku: rate ≈ 6.667 USD/MTok (低)
            ("sess-j9a", "claude-haiku-4-5-20260101", 0.001, 100, 50),
            # opus: rate ≈ 66.667 USD/MTok (高)
            ("sess-j9b", "claude-opus-4-7-20260101", 0.10, 1000, 500),
        ]
        outcome_rows = [
            ("sess-j9a", "simple", "haiku"),
            ("sess-j9b", "complex", "opus"),
        ]
        result = _compute_tier_cost_rate_summary(cost_rows, outcome_rows)
        assert len(result) == 2
        # 先頭が最大 rate（opus ≈ 66.667）、末尾が最小 rate（haiku ≈ 6.667）
        assert result[0]["rate_usd_per_mtok"] >= result[1]["rate_usd_per_mtok"]
        assert result[0]["tier"] == "opus"
        assert result[1]["tier"] == "haiku"


class TestReadTierCostRateSummary:
    """J2 群: read_tier_cost_rate_summary の DB 統合テスト。"""

    def test_rate_formula_via_db(self, tmp_path):
        """J2-1: AC-2 DB 経由での rate 式手計算検証。

        input=100, output=50, total_cost_usd=0.0075 → rate = 50.0 USD/MTok
        """
        db = _make_c3_db_v003(tmp_path)
        sess = "j2-1-rate"

        record_agent_outcome_event(
            role="developer",
            complexity="medium", tier="sonnet", success=True,
            session_id=sess, db_path=db,
        )
        _seed_cost_run_with_tokens(
            db,
            session_id=sess,
            agent_id="agent-j21",
            agent_type="developer",
            model="claude-sonnet-4-6-20260101",
            input_tokens=100,
            output_tokens=50,
            total_cost_usd=0.0075,
        )

        result = read_tier_cost_rate_summary(db_path=db)
        assert len(result) == 1
        row = result[0]
        assert row["complexity"] == "medium"
        assert row["tier"] == "sonnet"
        assert row["sessions"] == 1
        assert abs(row["total_cost_usd"] - 0.0075) < 1e-12
        assert row["billable_tokens"] == 150
        assert abs(row["rate_usd_per_mtok"] - 50.0) < 1e-9

    def test_mainline_excluded(self, tmp_path):
        """J2-2: AC-3 mainline agent_type の行は集計から除外される。"""
        db = _make_c3_db_v003(tmp_path)
        sess = "j2-2-mainline"

        record_agent_outcome_event(
            role="developer",
            complexity="simple", tier="haiku", success=True,
            session_id=sess, db_path=db,
        )
        # mainline のみ seed（SQL フィルタで除外される）
        _seed_cost_run_with_tokens(
            db,
            session_id=sess,
            agent_id="mainline",
            agent_type="mainline",
            model="claude-haiku-4-5-20260101",
            input_tokens=100,
            output_tokens=50,
            total_cost_usd=0.05,
        )

        result = read_tier_cost_rate_summary(db_path=db)
        assert result == [], "mainline のみの session は除外されるべき"

    def test_model_match_only(self, tmp_path):
        """J2-3: AC-1 model 一致のみ集計 — haiku モデル行が opus 集計に入らない。

        H5 テスト（既存）では session 全体の non-mainline コストを合算するため
        haiku モデル行も opus 集計に入る。本テストでは新関数がそれを排除することを確認する。
        """
        db = _make_c3_db_v003(tmp_path)
        sess = "j2-3-model-match"

        record_agent_outcome_event(
            role="developer",
            complexity="complex", tier="opus", success=True,
            session_id=sess, db_path=db,
        )
        # opus モデル行
        _seed_cost_run_with_tokens(
            db,
            session_id=sess,
            agent_id="agent-opus",
            agent_type="developer",
            model="claude-opus-4-7-20260101",
            input_tokens=100,
            output_tokens=50,
            total_cost_usd=0.03,
        )
        # haiku モデル行（同 session）— opus 集計に混ざってはいけない
        _seed_cost_run_with_tokens(
            db,
            session_id=sess,
            agent_id="agent-haiku",
            agent_type="developer",
            model="claude-haiku-4-5-20260101",
            input_tokens=100,
            output_tokens=50,
            total_cost_usd=0.001,
        )

        result = read_tier_cost_rate_summary(db_path=db)
        # opus outcome → opus バケットのみ
        opus_rows = [r for r in result if r["tier"] == "opus"]
        assert len(opus_rows) == 1
        # opus コストのみ: 0.03 USD（haiku の 0.001 は含まれない）
        assert abs(opus_rows[0]["total_cost_usd"] - 0.03) < 1e-12
        assert opus_rows[0]["billable_tokens"] == 150

    def test_unknown_model_skipped(self, tmp_path):
        """J2-4: AC-3 未知モデルの行はスキップされる。"""
        db = _make_c3_db_v003(tmp_path)
        sess = "j2-4-unknown"

        record_agent_outcome_event(
            role="developer",
            complexity="medium", tier="sonnet", success=True,
            session_id=sess, db_path=db,
        )
        # 未知モデル行のみ seed
        _seed_cost_run_with_tokens(
            db,
            session_id=sess,
            agent_id="agent-unknown",
            agent_type="developer",
            model="unknown-model-xyz",
            input_tokens=100,
            output_tokens=50,
            total_cost_usd=0.99,
        )

        result = read_tier_cost_rate_summary(db_path=db)
        assert result == [], "未知モデルのみの session は除外されるべき"

    def test_billable_tokens_zero_excluded(self, tmp_path):
        """J2-5: AC-4 billable_tokens == 0 の (complexity, tier) は除外される。"""
        db = _make_c3_db_v003(tmp_path)
        sess = "j2-5-billable-zero"

        record_agent_outcome_event(
            role="developer",
            complexity="simple", tier="haiku", success=True,
            session_id=sess, db_path=db,
        )
        _seed_cost_run_with_tokens(
            db,
            session_id=sess,
            agent_id="agent-j25",
            agent_type="developer",
            model="claude-haiku-4-5-20260101",
            input_tokens=0,
            output_tokens=0,
            total_cost_usd=0.001,
        )

        result = read_tier_cost_rate_summary(db_path=db)
        assert result == [], "billable_tokens=0 の行は除外されるべき"

    def test_db_absent_returns_empty_list(self, tmp_path):
        """J2-6: DB 不在で [] を返す。"""
        absent_db = tmp_path / "no_such_j26.db"
        result = read_tier_cost_rate_summary(db_path=absent_db)
        assert result == []

    def test_table_absent_returns_empty_list(self, tmp_path):
        """J2-7: テーブル不在（DB ファイルはあるが migration 未適用）で [] を返す。"""
        import sqlite3 as _sqlite3  # noqa: PLC0415
        db = tmp_path / "empty_j27.db"
        # 空の DB ファイルだけ作成（migration なし = テーブルなし）
        conn = _sqlite3.connect(str(db))
        conn.close()

        result = read_tier_cost_rate_summary(db_path=db)
        assert result == []

    def test_busy_timeout_applied(self, tmp_path, monkeypatch):
        """J2-8: read 規約 — busy_timeout が設定されること。

        _apply_busy_timeout が呼ばれることを monkeypatch で確認する。
        """
        import c3.db as c3_db  # noqa: PLC0415
        db = _make_c3_db_v003(tmp_path)

        timeout_called = []
        original = c3_db._apply_busy_timeout

        def recording_apply_busy_timeout(conn):
            timeout_called.append(True)
            original(conn)

        monkeypatch.setattr(c3_db, "_apply_busy_timeout", recording_apply_busy_timeout)

        read_tier_cost_rate_summary(db_path=db)
        assert timeout_called, "_apply_busy_timeout が呼ばれるべき"

    def test_return_keys_structure(self, tmp_path):
        """J2-9: 戻り値 dict のキーが仕様通りであることを確認する。"""
        db = _make_c3_db_v003(tmp_path)
        sess = "j2-9-keys"

        record_agent_outcome_event(
            role="developer",
            complexity="medium", tier="sonnet", success=True,
            session_id=sess, db_path=db,
        )
        _seed_cost_run_with_tokens(
            db,
            session_id=sess,
            agent_id="agent-j29",
            agent_type="developer",
            model="claude-sonnet-4-6-20260101",
            input_tokens=100,
            output_tokens=50,
            total_cost_usd=0.01,
        )

        result = read_tier_cost_rate_summary(db_path=db)
        assert len(result) == 1
        row = result[0]
        expected_keys = {
            "complexity", "tier", "sessions",
            "total_cost_usd", "billable_tokens", "rate_usd_per_mtok",
        }
        assert set(row.keys()) == expected_keys

    def test_empty_tables_returns_empty_list(self, tmp_path):
        """J2-10: テーブル空（seed なし）で [] を返す。"""
        db = _make_c3_db_v003(tmp_path)
        result = read_tier_cost_rate_summary(db_path=db)
        assert result == []


# ---------------------------------------------------------------------------
# K 群: read_tier_cost_rate_for_complexity (v2.24.0 T2)
# ---------------------------------------------------------------------------


class TestReadTierCostRateForComplexity:
    """K 群: read_tier_cost_rate_for_complexity のテスト。

    read_tier_cost_rate_summary の薄いラッパーであるため、DB セットアップは
    J 群の _make_c3_db_v003 / _seed_cost_run_with_tokens /
    record_tier_recent_outcome を流用する。
    I 群（read_tier_cost_for_complexity）と対称な構造で記述する。
    """

    def _seed_session_with_tokens(
        self,
        db: "Path",
        *,
        complexity: str,
        tier_for_outcome: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        total_cost_usd: float,
        session_id: str,
    ) -> None:
        """outcome + cost_run (token 付き) を 1 セッション分まとめて seed するヘルパー。"""
        record_agent_outcome_event(
            role="developer",
            complexity=complexity,
            tier=tier_for_outcome,
            success=True,
            session_id=session_id,
            db_path=db,
        )
        _seed_cost_run_with_tokens(
            db,
            session_id=session_id,
            agent_id=f"agent-k-{session_id}",
            agent_type="developer",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_cost_usd=total_cost_usd,
        )

    def test_complexity_filter_returns_matching_rows_only(self, tmp_path: "Path"):
        """K1: complexity が一致する行のみ {tier: rate_usd_per_mtok} で返す。

        medium を指定したら medium 行の {tier: rate} のみを返し、
        他の complexity (simple/complex) は含まない。
        """
        db = _make_c3_db_v003(tmp_path)

        # medium/sonnet: billable=150 → rate = 0.0075 / (150/1e6) = 50.0 USD/MTok
        self._seed_session_with_tokens(
            db,
            complexity="medium",
            tier_for_outcome="sonnet",
            model="claude-sonnet-4-6-20260101",
            input_tokens=100,
            output_tokens=50,
            total_cost_usd=0.0075,
            session_id="k1-medium-sonnet",
        )
        # simple/haiku: 別 complexity
        self._seed_session_with_tokens(
            db,
            complexity="simple",
            tier_for_outcome="haiku",
            model="claude-haiku-4-5",
            input_tokens=200,
            output_tokens=100,
            total_cost_usd=0.0009,
            session_id="k1-simple-haiku",
        )
        # complex/opus: 別 complexity
        self._seed_session_with_tokens(
            db,
            complexity="complex",
            tier_for_outcome="opus",
            model="claude-opus-4-7-20250514",
            input_tokens=300,
            output_tokens=150,
            total_cost_usd=0.0135,
            session_id="k1-complex-opus",
        )

        result = read_tier_cost_rate_for_complexity("medium", db_path=db)

        assert isinstance(result, dict)
        assert set(result.keys()) == {"sonnet"}
        assert abs(result["sonnet"] - 50.0) < 1e-6
        # simple / complex は含まない
        assert "haiku" not in result
        assert "opus" not in result

    def test_rate_zero_or_negative_excluded(self, tmp_path: "Path"):
        """K2: rate_usd_per_mtok <= 0 の行は除外される。

        read_tier_cost_rate_summary をモックして rate=0 のデータを注入し、
        フィルタ動作を独立して検証する（I2 の rate 版）。
        """
        from unittest.mock import patch  # noqa: PLC0415

        fake_rows = [
            {
                "complexity": "medium",
                "tier": "haiku",
                "sessions": 1,
                "total_cost_usd": 0.0,
                "billable_tokens": 0,
                "rate_usd_per_mtok": 0.0,
            },
            {
                "complexity": "medium",
                "tier": "sonnet",
                "sessions": 1,
                "total_cost_usd": 0.01,
                "billable_tokens": 200,
                "rate_usd_per_mtok": 50.0,
            },
        ]
        with patch("c3.db.read_tier_cost_rate_summary", return_value=fake_rows):
            result = read_tier_cost_rate_for_complexity("medium")

        # rate_usd_per_mtok=0 の haiku は除外、sonnet のみ返る
        assert "haiku" not in result
        assert "sonnet" in result
        assert abs(result["sonnet"] - 50.0) < 1e-9

    def test_no_matching_complexity_returns_empty_dict(self, tmp_path: "Path"):
        """K3: 該当 complexity のデータが無い場合は {} を返す。"""
        db = _make_c3_db_v003(tmp_path)

        # simple のみ seed
        self._seed_session_with_tokens(
            db,
            complexity="simple",
            tier_for_outcome="haiku",
            model="claude-haiku-4-5",
            input_tokens=100,
            output_tokens=50,
            total_cost_usd=0.0009,
            session_id="k3-simple",
        )

        result = read_tier_cost_rate_for_complexity("complex", db_path=db)
        assert result == {}

    def test_db_absent_returns_empty_dict(self, tmp_path: "Path"):
        """K4: DB 不在（存在しないパスを db_path に渡す）で {} を返す。"""
        absent_db = tmp_path / "no_such_k4.db"
        result = read_tier_cost_rate_for_complexity("medium", db_path=absent_db)
        assert result == {}

    def test_multiple_tiers_same_complexity(self, tmp_path: "Path"):
        """K5: 同一 complexity で複数 tier が存在する場合、全て返る。"""
        db = _make_c3_db_v003(tmp_path)

        self._seed_session_with_tokens(
            db,
            complexity="medium",
            tier_for_outcome="haiku",
            model="claude-haiku-4-5",
            input_tokens=100,
            output_tokens=50,
            total_cost_usd=0.0009,
            session_id="k5-haiku",
        )
        self._seed_session_with_tokens(
            db,
            complexity="medium",
            tier_for_outcome="sonnet",
            model="claude-sonnet-4-6-20260101",
            input_tokens=100,
            output_tokens=50,
            total_cost_usd=0.0075,
            session_id="k5-sonnet",
        )

        result = read_tier_cost_rate_for_complexity("medium", db_path=db)

        assert set(result.keys()) == {"haiku", "sonnet"}
        # haiku: rate = 0.0009 / (150/1e6) = 6.0 USD/MTok
        assert abs(result["haiku"] - 6.0) < 1e-6
        # sonnet: rate = 0.0075 / (150/1e6) = 50.0 USD/MTok
        assert abs(result["sonnet"] - 50.0) < 1e-6

    def test_read_tier_cost_rate_summary_not_modified(self, tmp_path: "Path"):
        """K6: read_tier_cost_rate_summary が本関数追加後も不変（回帰なし）。

        J 群の代表シナリオを再実行し、read_tier_cost_rate_summary が
        read_tier_cost_rate_for_complexity の実装で一切変更されていないことを確認する。
        """
        db = _make_c3_db_v003(tmp_path)
        sess = "k6-backward-compat"

        record_agent_outcome_event(
            role="developer",
            complexity="medium", tier="sonnet", success=True,
            session_id=sess, db_path=db,
        )
        _seed_cost_run_with_tokens(
            db,
            session_id=sess,
            agent_id="agent-k6",
            agent_type="developer",
            model="claude-sonnet-4-6-20260101",
            input_tokens=100,
            output_tokens=50,
            total_cost_usd=0.0075,
        )

        # read_tier_cost_rate_summary は変更なしに動作する
        summary = read_tier_cost_rate_summary(db_path=db)
        assert len(summary) == 1
        assert summary[0]["complexity"] == "medium"
        assert summary[0]["tier"] == "sonnet"
        assert abs(summary[0]["rate_usd_per_mtok"] - 50.0) < 1e-6

        # read_tier_cost_rate_for_complexity も同じ結果を反映する
        for_complexity = read_tier_cost_rate_for_complexity("medium", db_path=db)
        assert abs(for_complexity["sonnet"] - 50.0) < 1e-6


# ---------------------------------------------------------------------------
# SR-R-001 統一: 例外ログが type(exc).__name__ を出力することを検証
# ---------------------------------------------------------------------------


class TestExceptionLogTypeName:
    """C-(1): 例外発生時にログ本文へ型名が出て生 exc message が出ないことを検証。

    read_agent_tier_params を代表例として使用する。
    元は read_tier_params を代表例にしていたが、v2.41.0 db-shims-and-cost タスクで
    read_tier_params は DB に一切接続しない deprecated シムに置換された（ADR-5）ため、
    DB 例外パスを持つ後継の read_agent_tier_params に差し替えた（実装・ログ規約は同一）。
    corrupt なバイナリを DB として渡すことで sqlite3.DatabaseError
    （not an SQLite3 database）を発生させ、caplog でログ本文に型名が含まれ
    生 message が含まれないことを確認する。
    """

    def test_read_tier_params_logs_exception_type_not_message(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """SR-R-001: read_agent_tier_params は例外発生時に type(exc).__name__ をログに出す。

        corrupt DB を渡すと sqlite3.DatabaseError が上がる。
        - ログ本文に "DatabaseError" が含まれる
        - ログ本文に生 exc message（"not an SQLite3 database" 等）が含まれない
        - 戻り値は defaults（型名のみのログで例外を握る）
        """
        from c3.db import read_agent_tier_params  # noqa: PLC0415

        # corrupt なバイナリファイルを作成して DB として渡す
        corrupt_db = tmp_path / "corrupt.db"
        corrupt_db.write_bytes(b"this is not a valid sqlite3 database file")

        with caplog.at_level(logging.WARNING, logger="c3.db"):
            result = read_agent_tier_params("developer", "medium", db_path=corrupt_db)

        # 戻り値は defaults（エラー時も全 tier を初期値で返す）
        assert isinstance(result, dict), "Should return dict on error"
        assert "haiku" in result, "Defaults should include all tiers"

        # ログ本文に型名が含まれる
        assert "DatabaseError" in caplog.text, (
            f"Log must contain exception type name 'DatabaseError'. caplog.text={caplog.text!r}"
        )
        # 生 exc message が含まれない（SR-R-001: 情報漏洩防止）
        assert "not an SQLite3 database" not in caplog.text, (
            "Log must NOT contain raw exception message. caplog.text={caplog.text!r}"
        )


# 設計判断（db-shims-and-cost タスク）: L 群（TestSyncTierBanditCost /
# TestReadTierBanditCost）は旧 tier_bandit テーブルへの直接 SQL INSERT・
# sync_tier_bandit_cost の SET 同期・read_tier_bandit_cost の cost 列読み出しを
# 前提にしていたが、architecture-report ADR-4 により両関数とも廃止した
# （sync_tier_bandit_cost は DB 非接続の no-op シム、read_tier_bandit_cost は
# 完全削除）。tier_bandit テーブル自体も migration 004 で DROP 済みのため、
# 旧仕様を検証する L 群は恒久的に再現不能となり丸ごと削除する
# （_seed_bandit_row / _seed_cost_session も L 群専用ヘルパーのため合わせて削除）。
# シムの新挙動（DB 非接続 no-op）は TestDeprecatedShimBehavior /
# TestDeprecatedFunctionsRemoved が引き継ぐ。


# ---------------------------------------------------------------------------
# M 群: v2.26.0 定数 SSOT（COST_LAMBDA_DEFAULT / ESCALATION_THRESHOLD_DEFAULT）
# ---------------------------------------------------------------------------


class TestV226Constants:
    """M 群: v2.26.0 で追加した 2 つの定数の存在と値を確認する。"""

    def test_cost_lambda_default_is_none(self):
        """M1: COST_LAMBDA_DEFAULT は None（v2.25.0 互換センチネル）。"""
        import c3.db as db  # noqa: PLC0415
        assert db.COST_LAMBDA_DEFAULT is None

    def test_escalation_threshold_default_is_0_5(self):
        """M2: ESCALATION_THRESHOLD_DEFAULT は 0.5（既定 escalation 閾値）。"""
        import c3.db as db  # noqa: PLC0415
        assert db.ESCALATION_THRESHOLD_DEFAULT == 0.5


# ---------------------------------------------------------------------------
# N 群: v2.27.0 定数 SSOT（COST_LAMBDA_MIN / COST_LAMBDA_MAX）+ resolve_* 関数
# ---------------------------------------------------------------------------


class TestV227Constants:
    """N1 群: v2.27.0 で追加した 2 つの定数の存在と値を確認する（SSOT）。"""

    def test_cost_lambda_max_is_5(self):
        """N1-1: COST_LAMBDA_MAX == 5.0（v2.27.0 λ 上限拡張）。"""
        import c3.db as db  # noqa: PLC0415
        assert db.COST_LAMBDA_MAX == 5.0

    def test_cost_lambda_min_is_0(self):
        """N1-2: COST_LAMBDA_MIN == 0.0。"""
        import c3.db as db  # noqa: PLC0415
        assert db.COST_LAMBDA_MIN == 0.0


class TestResolveCostLambdaDb:
    """N2 群: db.resolve_cost_lambda() の env パース・バリデーションテスト。

    select_tier.py の _resolve_cost_lambda() と同一挙動（parity テストは N5 群）。
    """

    def test_unset_returns_none_no_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """N2-1: 未設定 → None・警告なし。"""
        import c3.db as db  # noqa: PLC0415
        monkeypatch.delenv("C3_TIER_COST_LAMBDA", raising=False)
        result = db.resolve_cost_lambda()
        assert result is None
        assert capsys.readouterr().err == ""

    def test_zero_returns_0_no_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """N2-2: "0" → 0.0・警告なし（cost 無視の明示オプト）。"""
        import c3.db as db  # noqa: PLC0415
        monkeypatch.setenv("C3_TIER_COST_LAMBDA", "0")
        result = db.resolve_cost_lambda()
        assert result == pytest.approx(0.0)
        assert capsys.readouterr().err == ""

    def test_middle_value_returns_float(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """N2-3: "2.5" → 2.5・警告なし。"""
        import c3.db as db  # noqa: PLC0415
        monkeypatch.setenv("C3_TIER_COST_LAMBDA", "2.5")
        result = db.resolve_cost_lambda()
        assert result == pytest.approx(2.5)
        assert capsys.readouterr().err == ""

    def test_new_upper_boundary_returns_float(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """N2-4: "5.0" → 5.0（新上限境界・許容）・警告なし。"""
        import c3.db as db  # noqa: PLC0415
        monkeypatch.setenv("C3_TIER_COST_LAMBDA", "5.0")
        result = db.resolve_cost_lambda()
        assert result == pytest.approx(5.0)
        assert capsys.readouterr().err == ""

    def test_above_max_returns_none_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """N2-5: "5.1" → None + stderr 警告（x > COST_LAMBDA_MAX は拒否）。"""
        import c3.db as db  # noqa: PLC0415
        monkeypatch.setenv("C3_TIER_COST_LAMBDA", "5.1")
        result = db.resolve_cost_lambda()
        assert result is None
        err = capsys.readouterr().err
        assert "C3_TIER_COST_LAMBDA" in err

    def test_negative_returns_none_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """N2-6: "-0.1" → None + stderr 警告（x < 0 は拒否）。"""
        import c3.db as db  # noqa: PLC0415
        monkeypatch.setenv("C3_TIER_COST_LAMBDA", "-0.1")
        result = db.resolve_cost_lambda()
        assert result is None
        err = capsys.readouterr().err
        assert "C3_TIER_COST_LAMBDA" in err

    def test_non_numeric_returns_none_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """N2-7: "abc" → None + stderr 警告。"""
        import c3.db as db  # noqa: PLC0415
        monkeypatch.setenv("C3_TIER_COST_LAMBDA", "abc")
        result = db.resolve_cost_lambda()
        assert result is None
        err = capsys.readouterr().err
        assert "C3_TIER_COST_LAMBDA" in err

    def test_nan_returns_none_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """N2-8: "nan" → None + stderr 警告。"""
        import c3.db as db  # noqa: PLC0415
        monkeypatch.setenv("C3_TIER_COST_LAMBDA", "nan")
        result = db.resolve_cost_lambda()
        assert result is None
        err = capsys.readouterr().err
        assert "C3_TIER_COST_LAMBDA" in err


class TestResolveEpsilonDb:
    """N3 群: db.resolve_epsilon() の env パース・バリデーションテスト。"""

    def test_unset_returns_epsilon_tiebreak(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """N3-1: 未設定 → EPSILON_TIEBREAK（0.05）を返す。"""
        import c3.db as db  # noqa: PLC0415
        monkeypatch.delenv("C3_TIER_EPSILON", raising=False)
        result = db.resolve_epsilon()
        assert result == pytest.approx(db.EPSILON_TIEBREAK)

    def test_zero_returns_default_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """N3-2: "0" → デフォルト返却 + stderr 警告（下限拒否）。"""
        import c3.db as db  # noqa: PLC0415
        monkeypatch.setenv("C3_TIER_EPSILON", "0")
        result = db.resolve_epsilon()
        assert result == pytest.approx(db.EPSILON_TIEBREAK)
        err = capsys.readouterr().err
        assert "C3_TIER_EPSILON" in err

    def test_valid_value_0_1(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """N3-3: "0.1" → 0.1・警告なし。"""
        import c3.db as db  # noqa: PLC0415
        monkeypatch.setenv("C3_TIER_EPSILON", "0.1")
        result = db.resolve_epsilon()
        assert result == pytest.approx(0.1)
        assert capsys.readouterr().err == ""

    def test_upper_boundary_1(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """N3-4: "1" → 1.0（上限境界・許容）・警告なし。"""
        import c3.db as db  # noqa: PLC0415
        monkeypatch.setenv("C3_TIER_EPSILON", "1")
        result = db.resolve_epsilon()
        assert result == pytest.approx(1.0)
        assert capsys.readouterr().err == ""

    def test_above_1_returns_default_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """N3-5: "1.1" → デフォルト返却 + stderr 警告（上限超過）。"""
        import c3.db as db  # noqa: PLC0415
        monkeypatch.setenv("C3_TIER_EPSILON", "1.1")
        result = db.resolve_epsilon()
        assert result == pytest.approx(db.EPSILON_TIEBREAK)
        err = capsys.readouterr().err
        assert "C3_TIER_EPSILON" in err


class TestResolveEscalationThresholdDb:
    """N4 群: db.resolve_escalation_threshold() の env パース・バリデーションテスト。"""

    def test_unset_returns_escalation_threshold_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """N4-1: 未設定 → ESCALATION_THRESHOLD_DEFAULT（0.5）を返す。"""
        import c3.db as db  # noqa: PLC0415
        monkeypatch.delenv("C3_ESCALATION_THRESHOLD", raising=False)
        result = db.resolve_escalation_threshold()
        assert result == pytest.approx(db.ESCALATION_THRESHOLD_DEFAULT)

    def test_valid_value_0_7(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """N4-2: "0.7" → 0.7・警告なし。"""
        import c3.db as db  # noqa: PLC0415
        monkeypatch.setenv("C3_ESCALATION_THRESHOLD", "0.7")
        result = db.resolve_escalation_threshold()
        assert result == pytest.approx(0.7)
        assert capsys.readouterr().err == ""

    def test_out_of_range_above_1_returns_default_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """N4-3: "1.1" → デフォルト返却 + stderr 警告。"""
        import c3.db as db  # noqa: PLC0415
        monkeypatch.setenv("C3_ESCALATION_THRESHOLD", "1.1")
        result = db.resolve_escalation_threshold()
        assert result == pytest.approx(db.ESCALATION_THRESHOLD_DEFAULT)
        err = capsys.readouterr().err
        assert "C3_ESCALATION_THRESHOLD" in err

    def test_out_of_range_zero_returns_default_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """N4-4: "0" → デフォルト返却 + stderr 警告（下限拒否）。"""
        import c3.db as db  # noqa: PLC0415
        monkeypatch.setenv("C3_ESCALATION_THRESHOLD", "0")
        result = db.resolve_escalation_threshold()
        assert result == pytest.approx(db.ESCALATION_THRESHOLD_DEFAULT)
        err = capsys.readouterr().err
        assert "C3_ESCALATION_THRESHOLD" in err


class TestDbParamsReexport:
    """N6 群: tier-routing パラメータが `_db_params` へ分離され、`c3.db` から
    後方互換 re-export されている契約を固定する（refactor: _db_params 抽出）。

    将来 `_db_params` に定数/関数を追加して `db.py` への re-export を忘れた場合、
    本群が drift を検出する。
    """

    _CONSTS = (
        "LEARNING_THRESHOLD",
        "EPSILON_TIEBREAK",
        "COST_LAMBDA_DEFAULT",
        "ESCALATION_THRESHOLD_DEFAULT",
        "COST_LAMBDA_MIN",
        "COST_LAMBDA_MAX",
    )
    _FUNCS = (
        "resolve_cost_lambda",
        "resolve_epsilon",
        "resolve_escalation_threshold",
    )

    def test_db_params_directly_importable(self) -> None:
        """N6-1: 新モジュール `c3._db_params` から直接 import できる。"""
        from c3 import _db_params  # noqa: PLC0415

        for name in self._CONSTS + self._FUNCS:
            assert hasattr(_db_params, name), f"_db_params に {name} が無い"

    def test_db_reexports_same_objects(self) -> None:
        """N6-2: `c3.db` の re-export は `_db_params` と同一オブジェクト/値。"""
        import c3.db as db  # noqa: PLC0415
        from c3 import _db_params  # noqa: PLC0415

        for name in self._CONSTS:
            assert getattr(db, name) == getattr(_db_params, name)
        for name in self._FUNCS:
            # 関数はファサード越しでも同一オブジェクト（is）であること
            assert getattr(db, name) is getattr(_db_params, name)

    def test_resolve_via_both_paths_agree(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """N6-3: env 上書き時、`db.*` と `_db_params.*` の解決結果が一致。"""
        import c3.db as db  # noqa: PLC0415
        from c3 import _db_params  # noqa: PLC0415

        monkeypatch.setenv("C3_TIER_EPSILON", "0.2")
        assert db.resolve_epsilon() == pytest.approx(0.2)
        assert _db_params.resolve_epsilon() == pytest.approx(0.2)


class TestBanditGatesAndFailureWindowConstants:
    """Q 群（tier-routing フェーズ2.5・ADR-25-1/25-2）:
    `BANDIT_GATES` / `FAILURE_WINDOW_DAYS_DEFAULT` / `resolve_failure_window_days`
    の `_db_params` 定義 + `c3.db` re-export（既存 N6 群と同じ SSOT パターン）。
    """

    def test_bandit_gates_defined_in_db_params(self) -> None:
        """Q1: `_db_params.BANDIT_GATES` が D 系 + D-2.5-stuck の 4 要素タプル。"""
        from c3 import _db_params  # noqa: PLC0415

        assert hasattr(_db_params, "BANDIT_GATES"), "_db_params.BANDIT_GATES が未定義"
        assert _db_params.BANDIT_GATES == ("D-2.5", "D-3", "D-5", "D-2.5-stuck")

    def test_bandit_gates_reexported_from_db(self) -> None:
        """Q2: `c3.db.BANDIT_GATES` が `_db_params.BANDIT_GATES` と同一オブジェクト。"""
        import c3.db as db  # noqa: PLC0415
        from c3 import _db_params  # noqa: PLC0415

        assert hasattr(db, "BANDIT_GATES"), "c3.db.BANDIT_GATES が re-export されていない"
        assert db.BANDIT_GATES is _db_params.BANDIT_GATES

    def test_failure_window_days_default_reexported(self) -> None:
        """Q3: `FAILURE_WINDOW_DAYS_DEFAULT`（14.0）が db/_db_params 両方にあり一致。"""
        import c3.db as db  # noqa: PLC0415
        from c3 import _db_params  # noqa: PLC0415

        assert _db_params.FAILURE_WINDOW_DAYS_DEFAULT == pytest.approx(14.0)
        assert db.FAILURE_WINDOW_DAYS_DEFAULT == pytest.approx(
            _db_params.FAILURE_WINDOW_DAYS_DEFAULT
        )

    def test_resolve_failure_window_days_reexported_and_callable(self) -> None:
        """Q4: `resolve_failure_window_days` が db/_db_params 両方から同一関数として呼べる。"""
        import c3.db as db  # noqa: PLC0415
        from c3 import _db_params  # noqa: PLC0415

        assert hasattr(db, "resolve_failure_window_days")
        assert db.resolve_failure_window_days is _db_params.resolve_failure_window_days

    def test_resolve_failure_window_days_unset_returns_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Q5: 未設定 → FAILURE_WINDOW_DAYS_DEFAULT（14.0）を返す。"""
        import c3.db as db  # noqa: PLC0415
        monkeypatch.delenv("C3_FAILURE_WINDOW_DAYS", raising=False)
        assert db.resolve_failure_window_days() == pytest.approx(14.0)

    def test_resolve_failure_window_days_valid_value(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    ) -> None:
        """Q6: "7" → 7.0・無警告。"""
        import c3.db as db  # noqa: PLC0415
        monkeypatch.setenv("C3_FAILURE_WINDOW_DAYS", "7")
        assert db.resolve_failure_window_days() == pytest.approx(7.0)
        assert capsys.readouterr().err == ""

    def test_resolve_failure_window_days_zero_rejected(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    ) -> None:
        """Q7: "0" は半開区間 (0, 3650] の下限外 → default + stderr 警告。"""
        import c3.db as db  # noqa: PLC0415
        monkeypatch.setenv("C3_FAILURE_WINDOW_DAYS", "0")
        assert db.resolve_failure_window_days() == pytest.approx(14.0)
        assert "C3_FAILURE_WINDOW_DAYS" in capsys.readouterr().err

    def test_resolve_failure_window_days_too_large_rejected(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    ) -> None:
        """Q8: "3651" は上限 3650 超 → default + stderr 警告。"""
        import c3.db as db  # noqa: PLC0415
        monkeypatch.setenv("C3_FAILURE_WINDOW_DAYS", "3651")
        assert db.resolve_failure_window_days() == pytest.approx(14.0)
        assert "C3_FAILURE_WINDOW_DAYS" in capsys.readouterr().err

    def test_resolve_failure_window_days_non_numeric_rejected(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    ) -> None:
        """Q9: 非数値 → default + stderr 警告。"""
        import c3.db as db  # noqa: PLC0415
        monkeypatch.setenv("C3_FAILURE_WINDOW_DAYS", "abc")
        assert db.resolve_failure_window_days() == pytest.approx(14.0)
        assert "C3_FAILURE_WINDOW_DAYS" in capsys.readouterr().err


class TestMissingTableLogsDebugNotWarning:
    """N7 群: 想定内の missing-table（sqlite3.OperationalError）は debug でログし
    WARNING を出さない（bare except 整理＝OperationalError/Exception 分類の一貫化）。

    対となる TestExceptionLogTypeName は corrupt-DB（DatabaseError）が WARNING で
    出ることを固定している。本群は「テーブル未作成は静かに（debug）」を固定する。
    """

    def test_read_tier_params_missing_table_is_debug_not_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """代表例は read_agent_tier_params（v2.41.0 db-shims-and-cost で read_tier_params が
        DB 非接続の deprecated シムに置換されたため、DB 例外パスを持つ後継関数に差し替え。
        ADR-5 / 実装・ログ規約は read_tier_params と同一）。"""
        import sqlite3  # noqa: PLC0415

        from c3.db import read_agent_tier_params  # noqa: PLC0415

        # マイグレーション未適用＝agent_tier_bandit テーブルが無い有効な空 DB
        empty_db = tmp_path / "empty.db"
        sqlite3.connect(str(empty_db)).close()

        with caplog.at_level(logging.DEBUG, logger="c3.db"):
            result = read_agent_tier_params("developer", "medium", db_path=empty_db)

        # 戻り値は defaults（graceful degradation 維持）
        assert "haiku" in result
        # 想定内の missing-table は WARNING を出さない
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert not warnings, (
            f"missing-table は WARNING を出すべきでない: {[r.getMessage() for r in warnings]}"
        )
        # debug に table 関連メッセージが出る
        assert "table not found or inaccessible" in caplog.text

    def test_insert_review_decision_missing_table_is_debug_not_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """書き込み系（bool 返却）でも missing-table は debug・False 返却・WARNING なし。"""
        import sqlite3  # noqa: PLC0415

        from c3.db import insert_review_decision  # noqa: PLC0415

        empty_db = tmp_path / "empty.db"
        sqlite3.connect(str(empty_db)).close()

        with caplog.at_level(logging.DEBUG, logger="c3.db"):
            ok = insert_review_decision(
                checklist_id="CR-X-001",
                finding_text="t",
                decision="fixed",
                reviewer="code-reviewer",
                db_path=empty_db,
            )

        # 書き込み失敗（テーブル無し）→ False（graceful degradation）
        assert ok is False
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert not warnings, (
            f"missing-table は WARNING を出すべきでない: {[r.getMessage() for r in warnings]}"
        )
        assert "table not found or inaccessible" in caplog.text


# ---------------------------------------------------------------------------
# T2 test-record: insert_review_decision の severity 拡張 + レガシーフォールバック
# plan-report-20260706-221212.md T2 / architecture-report-20260706-213701.md §2-3(1)
# ---------------------------------------------------------------------------


def _make_legacy_review_decisions_db(tmp_path: Path) -> Path:
    """severity 列を持たない旧スキーマ（migration 006 適用前相当）の
    review_decisions テーブルのみを持つ DB を作った（レガシーフォールバック検証専用）。
    """
    import sqlite3  # noqa: PLC0415

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE review_decisions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "checklist_id TEXT NOT NULL,"
            "finding_text TEXT NOT NULL,"
            "decision TEXT NOT NULL,"
            "reason TEXT,"
            "context_summary TEXT,"
            "decided_at TEXT NOT NULL,"
            "reviewer TEXT NOT NULL"
            ")"
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


class TestInsertReviewDecisionSeverity:
    """insert_review_decision の severity 列往復・省略時 NULL・レガシーフォールバックを
    固定した（T2 test-record・architecture-report §2-3(1)）。"""

    def test_insert_with_severity_roundtrip(self, tmp_path: Path) -> None:
        """severity 付きで INSERT した値が SELECT で往復して取得できた。"""
        import sqlite3  # noqa: PLC0415

        from c3.db import insert_review_decision  # noqa: PLC0415

        db_path = _make_c3_db(tmp_path)
        ok = insert_review_decision(
            checklist_id="CR-X-001",
            finding_text="f",
            decision="fixed",
            reviewer="code-reviewer",
            severity="high",
            db_path=db_path,
        )
        assert ok is True

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT severity FROM review_decisions WHERE checklist_id = ?",
                ("CR-X-001",),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "high"

    def test_insert_without_severity_is_null(self, tmp_path: Path) -> None:
        """severity 省略時は NULL で記録された。"""
        import sqlite3  # noqa: PLC0415

        from c3.db import insert_review_decision  # noqa: PLC0415

        db_path = _make_c3_db(tmp_path)
        ok = insert_review_decision(
            checklist_id="CR-X-002",
            finding_text="f",
            decision="fixed",
            reviewer="code-reviewer",
            db_path=db_path,
        )
        assert ok is True

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT severity FROM review_decisions WHERE checklist_id = ?",
                ("CR-X-002",),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] is None

    def test_insert_legacy_schema_falls_back_to_seven_columns(self, tmp_path: Path) -> None:
        """severity 列不在のレガシー DB では旧 7 列 INSERT に 1 回だけリトライして
        True を返し、行が記録された。"""
        import sqlite3  # noqa: PLC0415

        from c3.db import insert_review_decision  # noqa: PLC0415

        db_path = _make_legacy_review_decisions_db(tmp_path)
        ok = insert_review_decision(
            checklist_id="CR-X-003",
            finding_text="f",
            decision="fixed",
            reviewer="code-reviewer",
            severity="high",
            db_path=db_path,
        )
        assert ok is True

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT checklist_id, decision FROM review_decisions WHERE checklist_id = ?",
                ("CR-X-003",),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[1] == "fixed"


# ---------------------------------------------------------------------------
# [item4 SR-V-001] insert_review_decision 自体の軽量検証（多層防御）
# security-review-report-20260707-015605.md 指摘4・plan-report-20260707-020503.md
# fc1-tester。record_review_decision.py（アプリ層）の検証は維持される前提で、
# db 層にも二層目の検証を設けフェイルセーフ（例外を投げず False 返却/NULL
# 化/切り詰め）で防御することを固定する。impl 前は Red だった（当時 insert_review_decision
# は checklist_id 形式・severity 語彙・文字列長のいずれも検証しなかった）。
# ---------------------------------------------------------------------------


class TestInsertReviewDecisionInputValidation:
    """insert_review_decision 自体の軽量検証（SR-V-001 item4）を固定した。"""

    def test_invalid_checklist_id_format_returns_false_without_exception(
        self, tmp_path: Path,
    ) -> None:
        """形式不正な checklist_id（CR-/SR-/DC- 接頭辞ですらない）は
        例外を投げず False を返す。"""
        from c3.db import insert_review_decision  # noqa: PLC0415

        db_path = _make_c3_db(tmp_path)
        ok = insert_review_decision(
            checklist_id="bogus",
            finding_text="f",
            decision="fixed",
            reviewer="code-reviewer",
            db_path=db_path,
        )
        assert ok is False

    def test_severity_outside_vocabulary_is_nulled_and_insert_succeeds(
        self, tmp_path: Path,
    ) -> None:
        """語彙外 severity（例: 大文字 "Med"）は NULL 化されて INSERT が成功する
        （例外を投げない・フェイルセーフ規律維持）。"""
        import sqlite3  # noqa: PLC0415

        from c3.db import insert_review_decision  # noqa: PLC0415

        db_path = _make_c3_db(tmp_path)
        ok = insert_review_decision(
            checklist_id="CR-V-001",
            finding_text="f",
            decision="fixed",
            reviewer="code-reviewer",
            severity="Med",
            db_path=db_path,
        )
        assert ok is True

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT severity FROM review_decisions WHERE checklist_id = ?",
                ("CR-V-001",),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] is None, "語彙外 severity は NULL 化されるべき"

    def test_overlong_finding_text_reason_context_summary_are_truncated(
        self, tmp_path: Path,
    ) -> None:
        """過長な finding_text/reason/context_summary は上限で切り詰められて
        INSERT が成功する（例外を投げない）。"""
        import sqlite3  # noqa: PLC0415

        from c3.db import insert_review_decision  # noqa: PLC0415

        db_path = _make_c3_db(tmp_path)
        overlong = "x" * 10_000

        ok = insert_review_decision(
            checklist_id="CR-V-002",
            finding_text=overlong,
            decision="fixed",
            reason=overlong,
            context_summary=overlong,
            reviewer="code-reviewer",
            db_path=db_path,
        )
        assert ok is True

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT finding_text, reason, context_summary FROM review_decisions "
                "WHERE checklist_id = ?",
                ("CR-V-002",),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert len(row[0]) < len(overlong), "finding_text が上限で切り詰められるべき"
        assert len(row[1]) < len(overlong), "reason が上限で切り詰められるべき"
        assert len(row[2]) < len(overlong), "context_summary が上限で切り詰められるべき"


# ---------------------------------------------------------------------------
# [SR新規1 SR-V-001] insert_review_decision の非 str 型入力に対するフェイルセーフ
# security-review-report-20260707-022656.md 新規1・plan-report-20260707-023441.md
# fd1-tester。item4 の検証コード（A: checklist_id prefix / B: severity 正規化 /
# C: _truncate_review_text）が DB アクセスを保護する try/except の外にあり、
# 非 str 型（None/int 等）を渡すと TypeError/AttributeError が呼び出し元へ
# 未捕捉のまま伝播していた。本 Red は impl 前時点で、非 str 型入力に対しても
# 例外が伝播せずフェイルセーフ（False 返却/NULL 化/素通し）になることを固定する。
# ---------------------------------------------------------------------------


class TestInsertReviewDecisionNonStrTypeFailSafe:
    """insert_review_decision に非 str 型を渡しても例外が伝播しないことを固定した
    （SR-V-001 新規1）。"""

    def test_non_str_checklist_id_returns_false_without_exception(
        self, tmp_path: Path,
    ) -> None:
        """checklist_id が None/int（非 str）でも例外を送出せず False を返す。"""
        from c3.db import insert_review_decision  # noqa: PLC0415

        db_path = _make_c3_db(tmp_path)
        for bad_checklist_id in (None, 123):
            ok = insert_review_decision(
                checklist_id=bad_checklist_id,
                finding_text="f",
                decision="fixed",
                reviewer="code-reviewer",
                db_path=db_path,
            )
            assert ok is False

    def test_non_str_severity_is_nulled_without_exception(
        self, tmp_path: Path,
    ) -> None:
        """severity が int（非 str）でも例外を送出せず NULL 化されて
        INSERT が成功する。"""
        import sqlite3  # noqa: PLC0415

        from c3.db import insert_review_decision  # noqa: PLC0415

        db_path = _make_c3_db(tmp_path)
        ok = insert_review_decision(
            checklist_id="CR-X-010",
            finding_text="f",
            decision="fixed",
            reviewer="code-reviewer",
            severity=123,
            db_path=db_path,
        )
        assert ok is True

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT severity FROM review_decisions WHERE checklist_id = ?",
                ("CR-X-010",),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] is None, "非 str severity は NULL 化されるべき"

    def test_non_str_finding_text_does_not_raise(self, tmp_path: Path) -> None:
        """非 str truthy 値の finding_text（int）を渡しても例外を送出せず
        INSERT が成功する。"""
        from c3.db import insert_review_decision  # noqa: PLC0415

        db_path = _make_c3_db(tmp_path)
        ok = insert_review_decision(
            checklist_id="CR-X-011",
            finding_text=123,
            decision="fixed",
            reviewer="code-reviewer",
            db_path=db_path,
        )
        assert ok is True


# ---------------------------------------------------------------------------
# O 群: agent-tier-routing 学習シグナル再設計（v2.41.0 db-foundation・Red 先行）
#
# architecture-report-20260702-214748.md §3-2/§3-3 に対応。新シンボル
# （AGENT_ROLES / read_agent_tier_params / update_agent_tier_params /
#  record_agent_outcome_event / read_agent_failure_rate / read_recent_agent_outcomes）
# は本タスク開始時点では未実装だったため、モジュール冒頭の import には加えず各テスト内で
# ローカル import した（本ファイル全体の collection を壊さないため。既存 N7 群までの
# パターンを踏襲）。
# ---------------------------------------------------------------------------


def _make_c3_db_v004(tmp_path: Path) -> Path:
    """tmp_path に c3.db を作成し、004 (agent_tier_bandit/agent_outcomes) まで
    migration を適用する。

    NOTE: 004_agent_outcomes.sql が未実装の間は 001〜003 までしか適用されない
    （apply_pending_migrations はディレクトリに存在するファイルのみを対象にする）。
    004 実装後は本ヘルパーで agent_tier_bandit / agent_outcomes が使える DB を返す。
    """
    from c3.migrate import apply_pending_migrations  # noqa: PLC0415
    db_path = tmp_path / "c3.db"
    apply_pending_migrations(db_path)
    return db_path


class TestAgentRolesConstant:
    """O0 群: `_db_params.AGENT_ROLES` の存在と値（v2.41.0 db-foundation）。"""

    def test_agent_roles_defined_and_ordered(self):
        """O0-1: AGENT_ROLES が定義されており、要件どおりの 5 role である。"""
        import c3._db_params as db_params_mod  # noqa: PLC0415

        assert hasattr(db_params_mod, "AGENT_ROLES"), "_db_params.AGENT_ROLES が未定義"
        assert db_params_mod.AGENT_ROLES == (
            "interviewer", "architect", "planner", "developer", "tester"
        )


class TestReadAgentTierParams:
    """O1 群: read_agent_tier_params(role, complexity, *, db_path=None) のテスト。

    v2.42.5（tier-routing フェーズ2.5・ADR-25-3）で agent_tier_bandit 直読みから
    agent_outcomes の BANDIT_GATES（D-2.5/D-3/D-5/D-2.5-stuck）導出集計へ移行。
    旧テストは update_agent_tier_params で agent_tier_bandit を直接更新していたが、
    同関数自体が削除されるため、agent_outcomes への record_agent_outcome_event
    経由 seed へ全面移行する。
    """

    def _seed(self, db, *, role, complexity, tier, gate, outcomes, prefix):
        for i, success in enumerate(outcomes):
            record_agent_outcome_event(
                role=role, complexity=complexity, tier=tier, success=success,
                gate=gate, session_id=f"{prefix}-{i}", db_path=db,
            )

    def test_defaults_when_no_rows(self, tmp_path: Path):
        """O1-1: 行が無い role/complexity は全 tier (1.0, 1.0, 0) で返る。"""
        from c3.db import read_agent_tier_params  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        result = read_agent_tier_params("developer", "medium", db_path=db)

        assert result == {
            "haiku": (1.0, 1.0, 0),
            "sonnet": (1.0, 1.0, 0),
            "opus": (1.0, 1.0, 0),
        }

    def test_bandit_gate_events_are_aggregated(self, tmp_path: Path):
        """O1-2（④・ADR-25-3）: BANDIT_GATES 該当 gate（D-2.5）の agent_outcomes
        イベントが alpha/beta/trials に導出集計される。"""
        from c3.db import read_agent_tier_params  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        self._seed(
            db, role="developer", complexity="medium", tier="sonnet",
            gate="D-2.5", outcomes=[True], prefix="o1-2",
        )

        result = read_agent_tier_params("developer", "medium", db_path=db)
        assert result["sonnet"] == (2.0, 1.0, 1)
        # 未更新の他 tier は初期値のまま
        assert result["haiku"] == (1.0, 1.0, 0)

    def test_e1_e2_symmetrically_excluded(self, tmp_path: Path):
        """O1-3（②）: E-1/E-2 の success/failure はどちらも無視される（対称除外）。"""
        from c3.db import read_agent_tier_params  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        record_agent_outcome_event(
            role="developer", complexity="medium", tier="sonnet",
            success=True, gate="E-1", session_id="o1-3-e1-succ", db_path=db,
        )
        record_agent_outcome_event(
            role="developer", complexity="medium", tier="sonnet",
            success=False, gate="E-2", session_id="o1-3-e2-fail", db_path=db,
        )

        result = read_agent_tier_params("developer", "medium", db_path=db)
        assert result["sonnet"] == (1.0, 1.0, 0), (
            "E-1/E-2 の success/failure はどちらも BANDIT_GATES 対象外のため "
            "集計に反映されてはいけない"
        )

    def test_stuck_gate_counts_as_failure(self, tmp_path: Path):
        """O1-4（ADR-25-6）: D-2.5-stuck の failure が beta に算入される。"""
        from c3.db import read_agent_tier_params  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        record_agent_outcome_event(
            role="developer", complexity="medium", tier="sonnet",
            success=False, gate="D-2.5-stuck", session_id="o1-4-stuck", db_path=db,
        )

        result = read_agent_tier_params("developer", "medium", db_path=db)
        assert result["sonnet"] == (1.0, 2.0, 1)

    def test_role_isolation(self, tmp_path: Path):
        """O1-5: role が異なれば同一 complexity/tier でも別セルとして扱われる。"""
        from c3.db import read_agent_tier_params  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        self._seed(
            db, role="developer", complexity="medium", tier="sonnet",
            gate="D-2.5", outcomes=[True], prefix="o1-5-dev",
        )

        developer_result = read_agent_tier_params("developer", "medium", db_path=db)
        tester_result = read_agent_tier_params("tester", "medium", db_path=db)

        assert developer_result["sonnet"] == (2.0, 1.0, 1)
        assert tester_result["sonnet"] == (1.0, 1.0, 0), (
            "tester role には developer の更新が漏れてはいけない"
        )

    def test_db_absent_returns_defaults(self, tmp_path: Path):
        """O1-6: DB 不在で全 tier 初期値を返す（静かな失敗）。"""
        from c3.db import read_agent_tier_params  # noqa: PLC0415
        absent_db = tmp_path / "no_such.db"

        result = read_agent_tier_params("developer", "simple", db_path=absent_db)
        assert result == {
            "haiku": (1.0, 1.0, 0),
            "sonnet": (1.0, 1.0, 0),
            "opus": (1.0, 1.0, 0),
        }

    def test_non_developer_role_with_only_e_gates_is_uniform(self, tmp_path: Path):
        """O1-7（C-3 DC-AS-002）: E-1/E-2 のみを seed した非 developer role
        （code-reviewer 相当）は全 tier uniform (1.0,1.0,0) を返す。

        BANDIT_GATES は D 系 + D-2.5-stuck のみで reviewer 系 role が使う
        E-1/E-2 を含まないため、恒久的に uniform になる（意図どおり・退行ではない）。
        """
        from c3.db import read_agent_tier_params  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        for gate, success in (("E-1", True), ("E-1", False), ("E-2", True), ("E-2", False)):
            record_agent_outcome_event(
                role="code-reviewer", complexity="medium", tier="sonnet",
                success=success, gate=gate, session_id=f"o1-7-{gate}-{success}", db_path=db,
            )

        result = read_agent_tier_params("code-reviewer", "medium", db_path=db)
        assert result == {
            "haiku": (1.0, 1.0, 0),
            "sonnet": (1.0, 1.0, 0),
            "opus": (1.0, 1.0, 0),
        }

    def test_gate_in_placeholder_follows_bandit_gates_length(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """O1-8（C-3 DC-GP-001）: BANDIT_GATES の長さが変わっても gate IN
        プレースホルダが追随し、バインド個数エラーにならない。

        literal '(?, ?, ?, ?)' 固定だと BANDIT_GATES を短くした際に
        sqlite3 のバインド個数エラーになる。動的生成なら短い BANDIT_GATES でも
        正しく集計される。
        """
        import c3.db as db_module  # noqa: PLC0415
        from c3.db import read_agent_tier_params  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        monkeypatch.setattr(db_module, "BANDIT_GATES", ("ONLY-GATE",))

        record_agent_outcome_event(
            role="developer", complexity="medium", tier="haiku",
            success=True, gate="ONLY-GATE", session_id="o1-8", db_path=db,
        )

        result = read_agent_tier_params("developer", "medium", db_path=db)
        assert result["haiku"] == (2.0, 1.0, 1), (
            "BANDIT_GATES を 1 要素に縮めても placeholder が追随し正しく集計されるはず"
        )


class TestRecordAgentOutcomeEvent:
    """O3 群: record_agent_outcome_event(*, role, complexity, tier, success, gate=None,
    note=None, session_id=None, db_path=None)。"""

    def test_insert_succeeds_and_row_readable(self, tmp_path: Path):
        """O3-1: INSERT 成功で True を返し、agent_outcomes に行が現れる。"""
        import sqlite3  # noqa: PLC0415
        from c3.db import record_agent_outcome_event  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        ok = record_agent_outcome_event(
            role="developer", complexity="medium", tier="sonnet",
            success=True, gate="D-2.5", note="impl done",
            session_id="sess-o3-1", db_path=db,
        )
        assert ok is True

        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT role, task_complexity, tier, success, gate, note, session_id "
            "FROM agent_outcomes WHERE session_id = 'sess-o3-1'"
        ).fetchone()
        conn.close()
        assert row == ("developer", "medium", "sonnet", 1, "D-2.5", "impl done", "sess-o3-1")

    def test_optional_fields_default_to_null(self, tmp_path: Path):
        """O3-2: gate/note/session_id 省略で NULL 保存。"""
        import sqlite3  # noqa: PLC0415
        from c3.db import record_agent_outcome_event  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        record_agent_outcome_event(
            role="architect", complexity="complex", tier="opus", success=False, db_path=db,
        )

        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT gate, note, session_id, success FROM agent_outcomes "
            "WHERE role='architect' AND task_complexity='complex' AND tier='opus'"
        ).fetchone()
        conn.close()
        assert row == (None, None, None, 0)

    def test_multiple_events_all_recorded(self, tmp_path: Path):
        """O3-3: 複数回呼ぶと全件が別行として蓄積される（bandit セルの上書きと異なり履歴保持）。"""
        import sqlite3  # noqa: PLC0415
        from c3.db import record_agent_outcome_event  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        for i in range(3):
            record_agent_outcome_event(
                role="developer", complexity="medium", tier="sonnet",
                success=True, session_id=f"sess-{i}", db_path=db,
            )

        conn = sqlite3.connect(str(db))
        count = conn.execute(
            "SELECT COUNT(*) FROM agent_outcomes WHERE role='developer'"
        ).fetchone()[0]
        conn.close()
        assert count == 3

    def test_db_absent_returns_false(self, tmp_path: Path):
        """O3-4: DB 不在で False を返す。"""
        from c3.db import record_agent_outcome_event  # noqa: PLC0415
        absent_db = tmp_path / "no_such.db"

        ok = record_agent_outcome_event(
            role="developer", complexity="medium", tier="sonnet",
            success=True, db_path=absent_db,
        )
        assert ok is False


class TestReadAgentFailureRate:
    """O4 群: read_agent_failure_rate(role, complexity, tier, *, window_days=None, db_path=None)。

    v2.42.5（tier-routing フェーズ2.5・ADR-25-2）で last_n（直近件数窓）から
    window_days（時間窓）+ gate IN BANDIT_GATES フィルタへ全面移行した。
    `_FAILURE_RATE_MIN_SAMPLES=5` は維持。イベントは BANDIT_GATES 対象 gate
    （既定 "D-2.5"）で seed する（gate 未指定=NULL は集計対象外になるため）。
    """

    def _seed_events(self, db, *, role, complexity, tier, outcomes, session_prefix, gate="D-2.5"):
        from c3.db import record_agent_outcome_event  # noqa: PLC0415
        for i, success in enumerate(outcomes):
            record_agent_outcome_event(
                role=role, complexity=complexity, tier=tier, success=success,
                gate=gate, session_id=f"{session_prefix}-{i}", db_path=db,
            )

    def _seed_at_ts(self, db, *, role, complexity, tier, success, gate, ts, session_id):
        import sqlite3  # noqa: PLC0415
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "INSERT INTO agent_outcomes "
                "(role, task_complexity, tier, success, gate, session_id, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (role, complexity, tier, 1 if success else 0, gate, session_id, ts),
            )
            conn.commit()
        finally:
            conn.close()

    def test_signature_no_longer_accepts_last_n(self, tmp_path: Path):
        """O4-0（ADR-25-2）: last_n キーワード引数は完全撤去され TypeError になる。"""
        from c3.db import read_agent_failure_rate  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        with pytest.raises(TypeError):
            read_agent_failure_rate(  # type: ignore[call-arg]
                "developer", "medium", "sonnet", last_n=10, db_path=db,
            )

    def test_below_min_samples_returns_none(self, tmp_path: Path):
        """O4-1: サンプル数が 5 未満なら failure_rate=None・sample_count は実数。"""
        from c3.db import read_agent_failure_rate  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        self._seed_events(
            db, role="developer", complexity="medium", tier="sonnet",
            outcomes=[True, False, True], session_prefix="o4-1",
        )

        rate, count = read_agent_failure_rate("developer", "medium", "sonnet", db_path=db)
        assert rate is None
        assert count == 3

    def test_at_min_samples_computes_rate(self, tmp_path: Path):
        """O4-2: サンプル数が 5 以上なら failure_rate を計算する（2/5=0.4）。"""
        from c3.db import read_agent_failure_rate  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        self._seed_events(
            db, role="developer", complexity="medium", tier="sonnet",
            outcomes=[True, True, False, True, False], session_prefix="o4-2",
        )

        rate, count = read_agent_failure_rate("developer", "medium", "sonnet", db_path=db)
        assert count == 5
        assert rate == pytest.approx(0.4)

    def test_role_isolation(self, tmp_path: Path):
        """O4-3: role が違うイベントは failure_rate 計算に混ざらない。"""
        from c3.db import read_agent_failure_rate  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        # developer: 5 件全部成功
        self._seed_events(
            db, role="developer", complexity="medium", tier="sonnet",
            outcomes=[True] * 5, session_prefix="o4-3-dev",
        )
        # tester: 5 件全部失敗（developer の計算に混ざってはいけない）
        self._seed_events(
            db, role="tester", complexity="medium", tier="sonnet",
            outcomes=[False] * 5, session_prefix="o4-3-test",
        )

        dev_rate, dev_count = read_agent_failure_rate(
            "developer", "medium", "sonnet", db_path=db
        )
        assert dev_count == 5
        assert dev_rate == pytest.approx(0.0)

    def test_db_absent_returns_none_zero(self, tmp_path: Path):
        """O4-4: DB 不在で (None, 0) を返す。"""
        from c3.db import read_agent_failure_rate  # noqa: PLC0415
        absent_db = tmp_path / "no_such.db"

        rate, count = read_agent_failure_rate(
            "developer", "medium", "sonnet", db_path=absent_db
        )
        assert rate is None
        assert count == 0

    def test_table_absent_is_debug_not_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """CR-T-002: DB ファイルは存在するが agent_outcomes テーブルが無い（マイグレーション
        未適用）経路は sqlite3.OperationalError の except 節（db.py L481-483）に入り、
        debug ログ・(None, 0) を返し WARNING は出さない。O4-4（DB ファイル自体が不在）とは
        別経路であり、TestMissingTableLogsDebugNotWarning.
        test_read_tier_params_missing_table_is_debug_not_warning と対称のテスト。"""
        import sqlite3  # noqa: PLC0415

        from c3.db import read_agent_failure_rate  # noqa: PLC0415

        # マイグレーション未適用＝agent_outcomes テーブルが無い有効な空 DB
        empty_db = tmp_path / "empty.db"
        sqlite3.connect(str(empty_db)).close()

        with caplog.at_level(logging.DEBUG, logger="c3.db"):
            rate, count = read_agent_failure_rate(
                "developer", "medium", "sonnet", db_path=empty_db
            )

        assert rate is None
        assert count == 0
        # 想定内の missing-table は WARNING を出さない
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert not warnings, (
            f"missing-table は WARNING を出すべきでない: {[r.getMessage() for r in warnings]}"
        )
        # debug に table 関連メッセージが出る
        assert "table not found or inaccessible" in caplog.text

    def test_gate_filter_excludes_e1_e2(self, tmp_path: Path):
        """O4-5（②）: E-1/E-2 の failure は gate IN BANDIT_GATES で除外される。"""
        from c3.db import read_agent_failure_rate  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        # E-1/E-2 failure を 5 件（BANDIT_GATES 対象外・除外されるはず）
        for i in range(5):
            self._seed_at_ts(
                db, role="developer", complexity="medium", tier="sonnet",
                success=False, gate="E-1" if i % 2 == 0 else "E-2",
                ts=_now_iso(), session_id=f"o4-5-e-{i}",
            )
        # D-2.5 success を 2 件（BANDIT_GATES 対象・少数のためサンプル不足になる想定）
        for i in range(2):
            self._seed_at_ts(
                db, role="developer", complexity="medium", tier="sonnet",
                success=True, gate="D-2.5", ts=_now_iso(), session_id=f"o4-5-d-{i}",
            )

        rate, count = read_agent_failure_rate("developer", "medium", "sonnet", db_path=db)
        assert count == 2, (
            "E-1/E-2 の 5 件は BANDIT_GATES フィルタで除外され D-2.5 の 2 件のみ数えるはず"
        )
        assert rate is None  # サンプル 5 未満

    def test_window_days_cutoff_excludes_old_events(self, tmp_path: Path):
        """O4-6（③）: window_days より古い ts のイベントは cutoff で除外される。"""
        from c3.db import read_agent_failure_rate  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        old_ts = _iso_days_ago(30)
        # 窓外（30 日前）の failure を 5 件
        for i in range(5):
            self._seed_at_ts(
                db, role="developer", complexity="medium", tier="sonnet",
                success=False, gate="D-2.5", ts=old_ts, session_id=f"o4-6-old-{i}",
            )
        # 窓内（1 日前）の success を 2 件
        for i in range(2):
            self._seed_at_ts(
                db, role="developer", complexity="medium", tier="sonnet",
                success=True, gate="D-2.5", ts=_iso_days_ago(1), session_id=f"o4-6-new-{i}",
            )

        rate, count = read_agent_failure_rate(
            "developer", "medium", "sonnet", window_days=14.0, db_path=db,
        )
        assert count == 2, "window_days=14 で 30 日前の 5 件は cutoff 外のはず"
        assert rate is None  # サンプル 5 未満（窓内 2 件のみ）

    def test_window_days_explicit_argument_overrides_default(self, tmp_path: Path):
        """O4-7: window_days を明示指定すると env / default より優先される。"""
        from c3.db import read_agent_failure_rate  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        # 10 日前のイベント 5 件（デフォルト 14 日窓なら含まれるが、window_days=5 なら除外）
        ts_10d = _iso_days_ago(10)
        for i in range(5):
            self._seed_at_ts(
                db, role="developer", complexity="medium", tier="sonnet",
                success=False, gate="D-2.5", ts=ts_10d, session_id=f"o4-7-{i}",
            )

        rate_default, count_default = read_agent_failure_rate(
            "developer", "medium", "sonnet", db_path=db,
        )
        assert count_default == 5  # デフォルト 14 日窓なら含まれる

        rate_narrow, count_narrow = read_agent_failure_rate(
            "developer", "medium", "sonnet", window_days=5.0, db_path=db,
        )
        assert count_narrow == 0, "window_days=5 なら 10 日前のイベントは除外されるはず"
        assert rate_narrow is None

    def test_env_override_window_days(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """O4-8: C3_FAILURE_WINDOW_DAYS env で窓長が変わる（window_days 省略時）。"""
        from c3.db import read_agent_failure_rate  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        ts_10d = _iso_days_ago(10)
        for i in range(5):
            self._seed_at_ts(
                db, role="developer", complexity="medium", tier="sonnet",
                success=False, gate="D-2.5", ts=ts_10d, session_id=f"o4-8-{i}",
            )

        monkeypatch.setenv("C3_FAILURE_WINDOW_DAYS", "5")
        rate, count = read_agent_failure_rate("developer", "medium", "sonnet", db_path=db)
        assert count == 0, "env で 5 日窓に狭めたら 10 日前のイベントは除外されるはず"

    def test_env_invalid_falls_back_to_default_window(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    ):
        """O4-9: C3_FAILURE_WINDOW_DAYS が不正値なら default（14 日）に戻り stderr 警告が出る。"""
        from c3.db import read_agent_failure_rate  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        ts_10d = _iso_days_ago(10)
        for i in range(5):
            self._seed_at_ts(
                db, role="developer", complexity="medium", tier="sonnet",
                success=False, gate="D-2.5", ts=ts_10d, session_id=f"o4-9-{i}",
            )

        monkeypatch.setenv("C3_FAILURE_WINDOW_DAYS", "not-a-number")
        rate, count = read_agent_failure_rate("developer", "medium", "sonnet", db_path=db)
        assert count == 5, "不正値なら default 14 日窓に戻り 10 日前のイベントも含むはず"
        err = capsys.readouterr().err
        assert "C3_FAILURE_WINDOW_DAYS" in err


class TestFailureRateBlockageScenario:
    """T1-4（①閉塞シナリオの単体化・C-3 DC-AM-001）: 窓外の古い D-gate failure が
    多数あっても窓内 BANDIT_GATES failure が 0 件ならサンプル不足で escalation
    しないことを固定する（②③の構造的効果の単体再現）。"""

    def test_stale_failures_outside_window_do_not_trigger_escalation(
        self, tmp_path: Path,
    ):
        import sqlite3  # noqa: PLC0415
        from c3.db import read_agent_failure_rate  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        # 窓外（30 日前）の D-gate failure を多数 seed（旧ルールなら escalation を
        # 押し上げていたが、時間窓で失効するはず）
        old_ts = _iso_days_ago(30)
        conn = sqlite3.connect(str(db))
        try:
            for i in range(8):
                conn.execute(
                    "INSERT INTO agent_outcomes "
                    "(role, task_complexity, tier, success, gate, session_id, ts) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("developer", "medium", "sonnet", 0, "D-2.5", f"blockage-old-{i}", old_ts),
                )
            conn.commit()
        finally:
            conn.close()

        rate, count = read_agent_failure_rate("developer", "medium", "sonnet", db_path=db)
        assert rate is None, "窓内 D-gate failure が 0 件ならサンプル不足で None のはず"
        assert count == 0

        # maybe_escalate へ注入した failure_rate 経由で非昇格を確認する
        import importlib.util  # noqa: PLC0415
        hook_path = Path(__file__).parents[1] / ".claude" / "hooks" / "select_tier.py"
        spec = importlib.util.spec_from_file_location("select_tier_blockage", hook_path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[attr-defined]

        tier, reason = mod.maybe_escalate(
            "medium", "sonnet",
            failure_rate_fn=lambda c, t: read_agent_failure_rate(
                "developer", c, t, db_path=db,
            ),
        )
        assert tier == "sonnet", "窓内 failure=0 件なら sonnet のまま昇格しないはず"
        assert reason is None


class TestReadRecentAgentOutcomes:
    """O5 群: read_recent_agent_outcomes(*, limit=10, role=None, db_path=None)（cli_tier 用）。"""

    def test_returns_events_ordered_desc(self, tmp_path: Path):
        """O5-1: 直近順（ts 降順）で返る。

        record_agent_outcome_event は ts をその場の現在時刻（秒精度）で決めるため、
        短時間の連続呼び出しでは ts が同値になりうる（同一秒内実行のフレーク要因）。
        順序を厳密に検証するため、ここでは直接 SQL で ts の異なる 2 行を投入する。
        """
        import sqlite3  # noqa: PLC0415
        from c3.db import read_recent_agent_outcomes  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "INSERT INTO agent_outcomes (role, task_complexity, tier, success, gate, ts) "
                "VALUES ('developer', 'medium', 'sonnet', 1, 'D-2.5', '2026-01-01T00:00:00')"
            )
            conn.execute(
                "INSERT INTO agent_outcomes (role, task_complexity, tier, success, gate, ts) "
                "VALUES ('developer', 'medium', 'sonnet', 0, 'D-3', '2026-01-02T00:00:00')"
            )
            conn.commit()
        finally:
            conn.close()

        result = read_recent_agent_outcomes(db_path=db)
        assert len(result) == 2
        # 新しい ts (2026-01-02) を持つ行が先頭
        assert result[0]["gate"] == "D-3"
        assert result[1]["gate"] == "D-2.5"

    def test_role_filter(self, tmp_path: Path):
        """O5-2: role 指定で絞り込まれる。"""
        from c3.db import read_recent_agent_outcomes, record_agent_outcome_event  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        record_agent_outcome_event(
            role="developer", complexity="medium", tier="sonnet",
            success=True, session_id="o5-2-dev", db_path=db,
        )
        record_agent_outcome_event(
            role="tester", complexity="medium", tier="sonnet",
            success=True, session_id="o5-2-test", db_path=db,
        )

        result = read_recent_agent_outcomes(role="tester", db_path=db)
        assert len(result) == 1
        assert result[0]["role"] == "tester"

    def test_limit_applied(self, tmp_path: Path):
        """O5-3: limit で件数が制限される。"""
        from c3.db import read_recent_agent_outcomes, record_agent_outcome_event  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        for i in range(5):
            record_agent_outcome_event(
                role="developer", complexity="medium", tier="sonnet",
                success=True, session_id=f"o5-3-{i}", db_path=db,
            )

        result = read_recent_agent_outcomes(limit=2, db_path=db)
        assert len(result) == 2

    def test_db_absent_returns_empty_list(self, tmp_path: Path):
        """O5-4: DB 不在で [] を返す。"""
        from c3.db import read_recent_agent_outcomes  # noqa: PLC0415
        absent_db = tmp_path / "no_such.db"

        result = read_recent_agent_outcomes(db_path=absent_db)
        assert result == []


# ---------------------------------------------------------------------------
# P 群: agent-tier-routing 学習シグナル再設計
#
# P0/P1（旧 TestDeprecatedShimBehavior / TestShimSignatureCompat・
# v2.41.0 db-shims-and-cost で追加）はシム 5 関数の no-op 挙動・旧呼び出し
# シグネチャ互換を検証していたが、tier-routing フェーズ2.5（⑤・C-3 DC-GP-002）
# で当該シム自体が db.py から完全削除されるため、恒久的に再現不能になった。
# シムの「no-op だった」という過去挙動を検証する意味が失われたため丸ごと削除し
# （memory: 削除と判断したら完全に削除する）、置換として
# TestDeprecatedFunctionsRemoved（P3 群）に「5 関数 + update_agent_tier_params
# が c3.db に存在しない」ことを固定する新規テストを追加する。
# P2: 【DC-GP-003】cost JOIN 差替（tier_recent_outcomes → agent_outcomes）の
#     結果同値性・DISTINCT 二重計上なし（旧 v2.41.0 タスクの成果・本タスクでは不変）。
# P3: 廃止確認（旧 read_tier_bandit_cost 等 + 本タスクで削除する 6 関数）。
# ---------------------------------------------------------------------------


def _make_v004_db(tmp_path: Path) -> Path:
    """P 群専用エイリアス。実体は O 群の `_make_c3_db_v004` と同じ
    （004 まで migration 適用済みの c3.db を作る）。可読性のため別名で参照する。
    """
    return _make_c3_db_v004(tmp_path)


class TestCostJoinAgentOutcomes:
    """P2 群: 【DC-GP-003】read_tier_cost_rate_summary / read_tier_cost_rate_for_complexity の
    JOIN 先差替（tier_recent_outcomes → agent_outcomes）の結果同値性・DISTINCT 二重計上なし。

    旧テーブル tier_recent_outcomes は 004 で DROP 済みのため、現行実装（JOIN 元が
    旧テーブルのまま）は必ず sqlite3.OperationalError 経由で空リストを返す。
    agent_outcomes に実データを積んでも空リストのままであることが Red の実体。
    """

    def test_read_tier_cost_rate_summary_reflects_agent_outcomes(self, tmp_path: Path):
        """P2-1: agent_outcomes + agent_cost_runs を積んだら空でない集計が返る
        （現行実装は JOIN 元が旧テーブルのままのため [] のまま＝Red）。"""
        db = _make_v004_db(tmp_path)
        sess = "p2-1-sess"
        insert_agent_cost_run(
            session_id=sess, agent_id="agent-1", agent_type="developer",
            description=None, model="claude-sonnet-4-6-20260101",
            attribution_skill=None, input_tokens=100, output_tokens=50,
            cache_read_tokens=0, cache_create_tokens=0, total_cost_usd=0.05,
            db_path=db,
        )
        record_agent_outcome_event(
            role="developer", complexity="medium", tier="sonnet",
            success=True, session_id=sess, db_path=db,
        )

        result = read_tier_cost_rate_summary(db_path=db)

        assert result != [], (
            "agent_outcomes に実データがあるのに [] のままなら JOIN 元が "
            "tier_recent_outcomes のまま未差替（Red）"
        )
        match = [r for r in result if r["complexity"] == "medium" and r["tier"] == "sonnet"]
        assert len(match) == 1
        assert match[0]["sessions"] == 1
        assert match[0]["billable_tokens"] == 150
        assert abs(match[0]["total_cost_usd"] - 0.05) < 1e-9

    def test_result_matches_pure_function_with_equivalent_rows(self, tmp_path: Path):
        """P2-2: 旧新結果同値性。DB 経由の結果が、同一データを直接
        _compute_tier_cost_rate_summary に渡した場合と一致すること
        （JOIN 元テーブルが変わっても集計アルゴリズム自体は不変であることの固定）。"""
        db = _make_v004_db(tmp_path)
        sess = "p2-2-sess"
        model = "claude-sonnet-4-6-20260101"
        insert_agent_cost_run(
            session_id=sess, agent_id="agent-1", agent_type="developer",
            description=None, model=model,
            attribution_skill=None, input_tokens=200, output_tokens=100,
            cache_read_tokens=0, cache_create_tokens=0, total_cost_usd=0.08,
            db_path=db,
        )
        record_agent_outcome_event(
            role="developer", complexity="complex", tier="sonnet",
            success=True, session_id=sess, db_path=db,
        )

        actual = read_tier_cost_rate_summary(db_path=db)

        cost_rows = [(sess, model, 0.08, 200, 100)]
        outcome_rows = [(sess, "complex", "sonnet")]
        expected = _compute_tier_cost_rate_summary(cost_rows, outcome_rows)

        assert actual == expected

    def test_multiple_agent_outcomes_rows_same_session_not_double_counted(
        self, tmp_path: Path
    ):
        """P2-3: 同一 (session,complexity,tier) の agent_outcomes 行が role 違いで複数あっても
        DISTINCT で sessions が二重計上されない。"""
        db = _make_v004_db(tmp_path)
        sess = "p2-3-sess"
        insert_agent_cost_run(
            session_id=sess, agent_id="agent-1", agent_type="developer",
            description=None, model="claude-sonnet-4-6-20260101",
            attribution_skill=None, input_tokens=100, output_tokens=50,
            cache_read_tokens=0, cache_create_tokens=0, total_cost_usd=0.02,
            db_path=db,
        )
        # 同じ session×complexity×tier に対して role 違いで 2 行 INSERT
        record_agent_outcome_event(
            role="developer", complexity="medium", tier="sonnet",
            success=True, session_id=sess, db_path=db,
        )
        record_agent_outcome_event(
            role="tester", complexity="medium", tier="sonnet",
            success=False, session_id=sess, db_path=db,
        )

        result = read_tier_cost_rate_summary(db_path=db)
        match = [r for r in result if r["complexity"] == "medium" and r["tier"] == "sonnet"]
        assert len(match) == 1
        assert match[0]["sessions"] == 1, (
            "同一 session の agent_outcomes 複数行が DISTINCT で 1 session に潰されるはず"
        )
        assert abs(match[0]["total_cost_usd"] - 0.02) < 1e-9

    def test_read_tier_cost_rate_for_complexity_reflects_agent_outcomes(self, tmp_path: Path):
        """P2-4: read_tier_cost_rate_for_complexity も agent_outcomes を反映する。"""
        db = _make_v004_db(tmp_path)
        sess = "p2-4-sess"
        insert_agent_cost_run(
            session_id=sess, agent_id="agent-1", agent_type="developer",
            description=None, model="claude-sonnet-4-6-20260101",
            attribution_skill=None, input_tokens=100, output_tokens=50,
            cache_read_tokens=0, cache_create_tokens=0, total_cost_usd=0.05,
            db_path=db,
        )
        record_agent_outcome_event(
            role="developer", complexity="medium", tier="sonnet",
            success=True, session_id=sess, db_path=db,
        )

        result = read_tier_cost_rate_for_complexity("medium", db_path=db)

        assert result != {}, (
            "agent_outcomes に実データがあるのに {} のままなら JOIN 元が未差替（Red）"
        )
        assert "sonnet" in result
        assert result["sonnet"] > 0


class TestDeprecatedFunctionsRemoved:
    """P3 群: 廃止確認。read_tier_bandit_cost / 旧 read_recent_outcomes /
    read_tier_cost_summary が db.py から消える。

    NOTE (CR-Q-005 で事実誤認を修正): read_tier_cost_for_complexity（旧・
    avg_cost_usd 版）は grep 調査の結果 production コードから未参照と確認済み。
    cost 表示に実際に使われているのは read_tier_cost_rate_summary（新・
    agent_outcomes ベースの rate 版）であり、read_tier_cost_summary ではない
    （旧コメントの「実際に cost 表示へ使われているのは read_tier_cost_summary の方」
    は事実誤認だった。cli_tier.py から read_tier_cost_summary への参照は 0 件）。
    read_tier_cost_summary 自体も、migration 004 で DROP 済みの
    tier_recent_outcomes を直接参照する恒久デッドコードのため、本 Round で
    削除対象に追加する。
    """

    def test_read_tier_bandit_cost_removed(self):
        """P3-1: read_tier_bandit_cost が db.py に存在しない。"""
        import c3.db as db_module  # noqa: PLC0415
        assert not hasattr(db_module, "read_tier_bandit_cost"), (
            "read_tier_bandit_cost は ADR-4 により削除対象（cost 列キャッシュ廃止）"
        )

    def test_read_recent_outcomes_removed(self):
        """P3-2: 旧 read_recent_outcomes（tier_recent_outcomes 版）が db.py に存在しない。

        read_recent_agent_outcomes（新・agent_outcomes 版）とは別関数。
        """
        import c3.db as db_module  # noqa: PLC0415
        assert not hasattr(db_module, "read_recent_outcomes"), (
            "旧 read_recent_outcomes は read_recent_agent_outcomes に置換され削除対象"
        )

    def test_read_tier_cost_for_complexity_removed(self):
        """P3-3: 旧 read_tier_cost_for_complexity（avg_cost_usd 版・v2.23.0）が
        residual として db.py に存在しない
        （read_tier_cost_rate_for_complexity という同名に近い rate 版は残る）。"""
        import c3.db as db_module  # noqa: PLC0415
        assert not hasattr(db_module, "read_tier_cost_for_complexity"), (
            "旧 read_tier_cost_for_complexity は production コード未参照の residual"
        )

    def test_read_tier_cost_summary_removed(self):
        """P3-4: CR-Q-005 read_tier_cost_summary が db.py から削除される。

        DROP 済み tier_recent_outcomes を直接参照する恒久デッドコードであり
        （呼び出し元 0 件を grep で確認済み）、ADR-5 の deprecated シムとも異なり
        Deprecated: タグも付いていなかったため完全削除の対象とする。
        """
        import c3.db as db_module  # noqa: PLC0415
        assert not hasattr(db_module, "read_tier_cost_summary"), (
            "read_tier_cost_summary は DROP 済み tier_recent_outcomes を参照する"
            "恒久デッドコードのため削除対象（CR-Q-005）"
        )

    # -----------------------------------------------------------------------
    # P3-5〜P3-10: tier-routing フェーズ2.5（⑤・ADR-25-4）シム 5 関数 +
    # update_agent_tier_params の削除確認。旧 TestDeprecatedShimBehavior /
    # TestShimSignatureCompat（no-op 挙動・旧シグネチャ互換の検証）はシム自体の
    # 削除で恒久的に再現不能なため丸ごと削除し、本クラスへ「消えていること」の
    # 確認として統合する。
    # -----------------------------------------------------------------------

    def test_read_tier_params_removed(self):
        """P3-5（⑤）: read_tier_params が db.py から消えている。"""
        import c3.db as db_module  # noqa: PLC0415
        assert not hasattr(db_module, "read_tier_params"), (
            "read_tier_params はシム削除（⑤・ADR-25-4）の対象"
        )

    def test_update_tier_params_removed(self):
        """P3-6（⑤）: update_tier_params が db.py から消えている。"""
        import c3.db as db_module  # noqa: PLC0415
        assert not hasattr(db_module, "update_tier_params"), (
            "update_tier_params はシム削除（⑤・ADR-25-4）の対象"
        )

    def test_record_tier_recent_outcome_removed(self):
        """P3-7（⑤）: record_tier_recent_outcome が db.py から消えている。"""
        import c3.db as db_module  # noqa: PLC0415
        assert not hasattr(db_module, "record_tier_recent_outcome"), (
            "record_tier_recent_outcome はシム削除（⑤・ADR-25-4）の対象"
        )

    def test_read_tier_failure_rate_removed(self):
        """P3-8（⑤）: read_tier_failure_rate が db.py から消えている。"""
        import c3.db as db_module  # noqa: PLC0415
        assert not hasattr(db_module, "read_tier_failure_rate"), (
            "read_tier_failure_rate はシム削除（⑤・ADR-25-4）の対象"
        )

    def test_sync_tier_bandit_cost_removed(self):
        """P3-9（⑤・C-3 DC-GP-003）: sync_tier_bandit_cost が db.py から消えている。"""
        import c3.db as db_module  # noqa: PLC0415
        assert not hasattr(db_module, "sync_tier_bandit_cost"), (
            "sync_tier_bandit_cost はシム削除（⑤・ADR-25-4）の対象"
        )

    def test_update_agent_tier_params_removed(self):
        """P3-10（④・ADR-25-4）: update_agent_tier_params が db.py から消えている。"""
        import c3.db as db_module  # noqa: PLC0415
        assert not hasattr(db_module, "update_agent_tier_params"), (
            "update_agent_tier_params は agent_tier_bandit DROP に伴い削除対象"
        )

    def test_tier_bandit_tiers_and_min_samples_constants_retained(self):
        """P3-11（R-5・C-3 DC-GP-002）: `_TIER_BANDIT_TIERS` /
        `_FAILURE_RATE_MIN_SAMPLES` は誤って削除されず残置されている
        （read_agent_tier_params の defaults 生成 / escalation の下限判定で
        引き続き使用されるため、シム削除の巻き添えにしてはいけない）。"""
        import c3.db as db_module  # noqa: PLC0415
        assert hasattr(db_module, "_TIER_BANDIT_TIERS"), (
            "_TIER_BANDIT_TIERS は read_agent_tier_params が使用中のため残置が必須"
        )
        assert hasattr(db_module, "_FAILURE_RATE_MIN_SAMPLES"), (
            "_FAILURE_RATE_MIN_SAMPLES は read_agent_failure_rate が使用中のため残置が必須"
        )


# ---------------------------------------------------------------------------
# Q 群: c3 metrics 効果総括ヘルパー 6 本 + METRICS_* 定数（test-metrics・Red 先行）
#
# plan-report-20260706-221212.md T3 / architecture-report-20260706-213701.md
# §2-3(2)(3) / §2-6 / §3 に対応。以下 6 ヘルパーと _db_params.METRICS_REVIEW_GATES /
# METRICS_DEV_GATES は本タスク開始時点では未実装だったため、モジュール冒頭の import には
# 加えず各テスト内でローカル import した（O 群までのパターンを踏襲。本ファイル
# 全体の collection を壊さないため）:
#   read_review_decision_matrix / fetch_prevented_findings / read_rework_trend /
#   read_rework_role_distribution / read_session_fix_cycles /
#   read_rework_session_cost
# ---------------------------------------------------------------------------


def _seed_review_decision(
    db: Path,
    *,
    checklist_id: str,
    decision: str,
    reviewer: str,
    severity: str | None,
    decided_at_iso: str,
    finding_text: str = "finding text",
) -> None:
    """review_decisions に任意の decided_at（ISO8601 文字列）で 1 行 INSERT した。

    insert_review_decision(decided_at=...) は datetime を受け取り
    isoformat(timespec="seconds") で保存するため、本ヘルパーは ISO 文字列を
    datetime.fromisoformat() で変換して渡す（since 境界を厳密に制御するため）。
    """
    from datetime import datetime as _datetime  # noqa: PLC0415

    from c3.db import insert_review_decision  # noqa: PLC0415

    insert_review_decision(
        checklist_id=checklist_id,
        finding_text=finding_text,
        decision=decision,
        reviewer=reviewer,
        severity=severity,
        decided_at=_datetime.fromisoformat(decided_at_iso),
        db_path=db,
    )


def _seed_agent_outcome_at(
    db: Path,
    *,
    role: str = "developer",
    complexity: str = "medium",
    tier: str = "sonnet",
    success: bool,
    gate: str | None,
    session_id: str | None,
    ts_iso: str,
) -> None:
    """agent_outcomes に任意の ts（ISO8601 文字列）で直接 INSERT した。

    record_agent_outcome_event は ts を常に datetime.now(UTC) で確定するため、
    --since 境界を制御するテストでは raw SQL で直接挿入する。
    """
    import sqlite3 as _sqlite3  # noqa: PLC0415

    conn = _sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO agent_outcomes "
            "(role, task_complexity, tier, success, gate, note, session_id, ts) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
            (role, complexity, tier, 1 if success else 0, gate, session_id, ts_iso),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_cost_run_at(
    db: Path,
    *,
    session_id: str,
    agent_id: str,
    agent_type: str = "developer",
    model: str = "claude-sonnet-4-6-20260101",
    total_cost_usd: float,
    recorded_at_iso: str,
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> None:
    """agent_cost_runs に任意の recorded_at（ISO8601 文字列）で直接 INSERT した。

    insert_agent_cost_run は recorded_at を常に datetime.now(UTC) で確定するため、
    --since 境界を制御するテストでは raw SQL で直接挿入する。
    """
    import sqlite3 as _sqlite3  # noqa: PLC0415

    conn = _sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO agent_cost_runs "
            "(session_id, agent_id, agent_type, description, model, "
            " attribution_skill, input_tokens, output_tokens, "
            " cache_read_tokens, cache_create_tokens, total_cost_usd, recorded_at) "
            "VALUES (?, ?, ?, NULL, ?, NULL, ?, ?, 0, 0, ?, ?)",
            (
                session_id, agent_id, agent_type, model,
                input_tokens, output_tokens, total_cost_usd, recorded_at_iso,
            ),
        )
        conn.commit()
    finally:
        conn.close()


class TestMetricsDbParamsConstants:
    """Q0 群: _db_params.METRICS_REVIEW_GATES / METRICS_DEV_GATES（独立定義・
    bandit 学習シグナル不干渉の絶対制約・architecture §2-3(3)）。"""

    def test_metrics_review_gates_value(self):
        """Q0-1: METRICS_REVIEW_GATES == ("E-1", "E-2", "C-3")。"""
        from c3._db_params import METRICS_REVIEW_GATES  # noqa: PLC0415
        assert METRICS_REVIEW_GATES == ("E-1", "E-2", "C-3")

    def test_metrics_dev_gates_value(self):
        """Q0-2: METRICS_DEV_GATES == ("D-3", "D-5")。"""
        from c3._db_params import METRICS_DEV_GATES  # noqa: PLC0415
        assert METRICS_DEV_GATES == ("D-3", "D-5")

    def test_bandit_gates_unchanged(self):
        """Q0-3（絶対制約）: BANDIT_GATES は本機能追加後も従来値のままであり、
        E-1/E-2 の bandit 除外分岐に diff が生じていないことの間接確認。"""
        from c3._db_params import BANDIT_GATES  # noqa: PLC0415
        assert BANDIT_GATES == ("D-2.5", "D-3", "D-5", "D-2.5-stuck")

    def test_metrics_gates_do_not_alias_bandit_gates(self):
        """Q0-4: METRICS_REVIEW_GATES / METRICS_DEV_GATES は BANDIT_GATES を
        再利用しない独立定義である（read-only 消費者の構造的ガード）。"""
        from c3._db_params import (  # noqa: PLC0415
            BANDIT_GATES,
            METRICS_DEV_GATES,
            METRICS_REVIEW_GATES,
        )
        assert METRICS_REVIEW_GATES != BANDIT_GATES
        assert METRICS_DEV_GATES != BANDIT_GATES


class TestReadReviewDecisionMatrix:
    """Q1 群: read_review_decision_matrix(db_path=None, since=None) -> list[dict]。"""

    def test_reviewer_severity_decision_counts(self, tmp_path: Path):
        """Q1-1: reviewer×severity×decision の件数が正しく集計される。"""
        from c3.db import read_review_decision_matrix  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        _seed_review_decision(db, checklist_id="CR-Q-001", decision="fixed",
                               reviewer="code-reviewer", severity="high",
                               decided_at_iso=_now_iso())
        _seed_review_decision(db, checklist_id="CR-Q-002", decision="fixed",
                               reviewer="code-reviewer", severity="high",
                               decided_at_iso=_now_iso())
        _seed_review_decision(db, checklist_id="SR-A-001", decision="accepted",
                               reviewer="security-reviewer", severity="medium",
                               decided_at_iso=_now_iso())

        result = read_review_decision_matrix(db_path=db)

        buckets = {(r["reviewer"], r["severity"], r["decision"]): r["count"] for r in result}
        assert buckets[("code-reviewer", "high", "fixed")] == 2
        assert buckets[("security-reviewer", "medium", "accepted")] == 1

    def test_severity_null_becomes_unknown_bucket(self, tmp_path: Path):
        """Q1-2: severity=NULL は "unknown" バケットに集計される。"""
        from c3.db import read_review_decision_matrix  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        _seed_review_decision(db, checklist_id="CR-NEW", decision="fixed",
                               reviewer="code-reviewer", severity=None,
                               decided_at_iso=_now_iso())

        result = read_review_decision_matrix(db_path=db)
        buckets = {(r["reviewer"], r["severity"], r["decision"]): r["count"] for r in result}
        assert buckets[("code-reviewer", "unknown", "fixed")] == 1

    def test_since_filter_excludes_older_rows(self, tmp_path: Path):
        """Q1-3: since 指定時、decided_at < since の行は除外される。"""
        from c3.db import read_review_decision_matrix  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        _seed_review_decision(db, checklist_id="CR-OLD", decision="fixed",
                               reviewer="code-reviewer", severity="high",
                               decided_at_iso="2020-01-01T00:00:00+00:00")
        _seed_review_decision(db, checklist_id="CR-NEW2", decision="fixed",
                               reviewer="code-reviewer", severity="high",
                               decided_at_iso=_now_iso())

        result = read_review_decision_matrix(db_path=db, since="2025-01-01")
        buckets = {(r["reviewer"], r["severity"], r["decision"]): r["count"] for r in result}
        assert buckets[("code-reviewer", "high", "fixed")] == 1, (
            "since 指定時は 2020 年の古い行が除外され新しい行のみ 1 件になるはず"
        )

    def test_fixed_unknown_bucket_matches_expected_count(self, tmp_path: Path):
        """Q1-4（DC-AM-001）: fixed×unknown バケットの合算が CLI 層 fixed_unknown
        導出の単一算出源として正しい件数を返す（severity 未記録の fixed 行が
        2 件なら count=2、severity 記録済み行は混ざらない）。"""
        from c3.db import read_review_decision_matrix  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        _seed_review_decision(db, checklist_id="CR-U1", decision="fixed",
                               reviewer="code-reviewer", severity=None,
                               decided_at_iso=_now_iso())
        _seed_review_decision(db, checklist_id="CR-U2", decision="fixed",
                               reviewer="security-reviewer", severity=None,
                               decided_at_iso=_now_iso())
        _seed_review_decision(db, checklist_id="CR-K1", decision="fixed",
                               reviewer="code-reviewer", severity="high",
                               decided_at_iso=_now_iso())

        result = read_review_decision_matrix(db_path=db)
        fixed_unknown_total = sum(
            r["count"] for r in result if r["decision"] == "fixed" and r["severity"] == "unknown"
        )
        assert fixed_unknown_total == 2

    def test_fixed_medium_plus_breakdown_derivable_from_matrix(self, tmp_path: Path):
        """Q1-5（DC-AM-001 round 3）: fixed×critical/high/medium 各バケットから
        headline の内訳・fixed_medium_plus 合計が導出可能な件数で返る
        （low は含まない）。"""
        from c3.db import read_review_decision_matrix  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        for i, sev in enumerate(["critical", "high", "high", "medium", "low"]):
            _seed_review_decision(
                db, checklist_id=f"SR-X-{i:03d}", decision="fixed",
                reviewer="security-reviewer", severity=sev, decided_at_iso=_now_iso(),
            )

        result = read_review_decision_matrix(db_path=db)
        fixed_by_severity = {
            r["severity"]: r["count"] for r in result if r["decision"] == "fixed"
        }
        assert fixed_by_severity.get("critical", 0) == 1
        assert fixed_by_severity.get("high", 0) == 2
        assert fixed_by_severity.get("medium", 0) == 1
        fixed_medium_plus = (
            fixed_by_severity.get("critical", 0)
            + fixed_by_severity.get("high", 0)
            + fixed_by_severity.get("medium", 0)
        )
        assert fixed_medium_plus == 4, "critical+high+medium の fixed 件数（low は含まない）"

    def test_empty_db_returns_empty_list(self, tmp_path: Path):
        """Q1-6: 空 DB（review_decisions 行なし）で [] を返し例外を出さない。"""
        from c3.db import read_review_decision_matrix  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        assert read_review_decision_matrix(db_path=db) == []

    def test_db_absent_returns_empty_list(self, tmp_path: Path):
        """Q1-7: DB 不在で [] を返し例外を出さない。"""
        from c3.db import read_review_decision_matrix  # noqa: PLC0415
        absent_db = tmp_path / "nonexistent.db"
        assert read_review_decision_matrix(db_path=absent_db) == []


class TestFetchPreventedFindings:
    """Q2 群: fetch_prevented_findings(db_path=None, limit=5, since=None) -> list[dict]。"""

    def test_only_fixed_critical_high_medium_included(self, tmp_path: Path):
        """Q2-1: decision='fixed' AND severity IN (critical,high,medium) のみ
        対象。accepted / low / unknown(NULL) は含まれない。"""
        from c3.db import fetch_prevented_findings  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        _seed_review_decision(db, checklist_id="CR-IN-1", decision="fixed",
                               reviewer="code-reviewer", severity="high",
                               decided_at_iso=_now_iso())
        _seed_review_decision(db, checklist_id="CR-OUT-1", decision="accepted",
                               reviewer="code-reviewer", severity="high",
                               decided_at_iso=_now_iso())
        _seed_review_decision(db, checklist_id="CR-OUT-2", decision="fixed",
                               reviewer="code-reviewer", severity="low",
                               decided_at_iso=_now_iso())
        _seed_review_decision(db, checklist_id="CR-OUT-3", decision="fixed",
                               reviewer="code-reviewer", severity=None,
                               decided_at_iso=_now_iso())

        result = fetch_prevented_findings(db_path=db)
        ids = {r["checklist_id"] for r in result}
        assert ids == {"CR-IN-1"}

    def test_ordered_by_decided_at_desc(self, tmp_path: Path):
        """Q2-2: decided_at DESC で返る（新しい判断が先頭）。"""
        from c3.db import fetch_prevented_findings  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        _seed_review_decision(db, checklist_id="CR-OLD-1", decision="fixed",
                               reviewer="code-reviewer", severity="high",
                               decided_at_iso="2026-01-01T00:00:00+00:00")
        _seed_review_decision(db, checklist_id="CR-NEW-1", decision="fixed",
                               reviewer="code-reviewer", severity="high",
                               decided_at_iso="2026-06-01T00:00:00+00:00")

        result = fetch_prevented_findings(db_path=db)
        assert [r["checklist_id"] for r in result] == ["CR-NEW-1", "CR-OLD-1"]

    def test_limit_truncates_but_matrix_keeps_full_count(self, tmp_path: Path):
        """Q2-3（DC-AM-001）: limit で examples は打ち切られるが、matrix の
        fixed×medium バケット件数は limit に張り付かず全件を保持する
        （fetch_prevented_findings は実例表示専用で件数集計には使わない）。"""
        from c3.db import fetch_prevented_findings, read_review_decision_matrix  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        for i in range(7):
            _seed_review_decision(
                db, checklist_id=f"CR-LIM-{i:03d}", decision="fixed",
                reviewer="code-reviewer", severity="medium",
                decided_at_iso=_iso_days_ago(i),
            )

        limited = fetch_prevented_findings(db_path=db, limit=5)
        assert len(limited) == 5, "limit=5 で examples は 5 件に打ち切られる"

        matrix = read_review_decision_matrix(db_path=db)
        medium_fixed = sum(
            r["count"] for r in matrix if r["decision"] == "fixed" and r["severity"] == "medium"
        )
        assert medium_fixed == 7, (
            "matrix の fixed×medium 件数は examples の limit に張り付かず 7 のまま"
        )

    def test_since_filter(self, tmp_path: Path):
        """Q2-4: since 指定時 decided_at < since の行は除外される。"""
        from c3.db import fetch_prevented_findings  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        _seed_review_decision(db, checklist_id="CR-OLD-2", decision="fixed",
                               reviewer="code-reviewer", severity="high",
                               decided_at_iso="2020-01-01T00:00:00+00:00")
        _seed_review_decision(db, checklist_id="CR-NEW-2", decision="fixed",
                               reviewer="code-reviewer", severity="high",
                               decided_at_iso=_now_iso())

        result = fetch_prevented_findings(db_path=db, since="2025-01-01")
        assert [r["checklist_id"] for r in result] == ["CR-NEW-2"]

    def test_severity_unknown_fixed_rows_excluded(self, tmp_path: Path):
        """Q2-5: severity=NULL(unknown) の fixed 行は examples に現れない。"""
        from c3.db import fetch_prevented_findings  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        _seed_review_decision(db, checklist_id="CR-UNK-1", decision="fixed",
                               reviewer="code-reviewer", severity=None,
                               decided_at_iso=_now_iso())

        assert fetch_prevented_findings(db_path=db) == []

    def test_empty_db_returns_empty_list(self, tmp_path: Path):
        """Q2-6: 空 DB で [] を返す。"""
        from c3.db import fetch_prevented_findings  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        assert fetch_prevented_findings(db_path=db) == []

    def test_db_absent_returns_empty_list(self, tmp_path: Path):
        """Q2-7: DB 不在で [] を返す。"""
        from c3.db import fetch_prevented_findings  # noqa: PLC0415
        absent_db = tmp_path / "nonexistent.db"
        assert fetch_prevented_findings(db_path=absent_db) == []


class TestReadReworkTrend:
    """Q3 群: read_rework_trend(db_path=None, months=12, since=None) -> list[dict]。"""

    @staticmethod
    def _current_month_str() -> str:
        from datetime import datetime, timezone  # noqa: PLC0415
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def test_calendar_month_zero_fill(self, tmp_path: Path):
        """Q3-1（DC-AM-002）: 差し戻し 0 件の月もゼロ埋めで行として現れる
        （欠落しない）。当月に 1 件の差し戻しを seed し、months=3 で要求すると
        当月以外の 2 ヶ月も rework_count=0/session_count=0/per_session=0.0 で
        現れる。"""
        from c3.db import read_rework_trend  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        _seed_agent_outcome_at(
            db, success=False, gate="E-1", session_id="sess-trend-1", ts_iso=_now_iso(),
        )

        result = read_rework_trend(db_path=db, months=3)
        assert len(result) == 3, "months=3 は暦月ゼロ埋めで必ず 3 行を返す"
        months = [r["month"] for r in result]
        assert self._current_month_str() in months

        zero_rows = [r for r in result if r["rework_count"] == 0]
        assert len(zero_rows) == 2, "データの無い 2 ヶ月がゼロ埋めで現れる"
        for r in zero_rows:
            assert r["session_count"] == 0
            assert r["per_session"] == 0.0

    def test_per_session_computation(self, tmp_path: Path):
        """Q3-2: per_session = rework_count / session_count（0 除算は 0.0）。"""
        from c3.db import read_rework_trend  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        _seed_agent_outcome_at(db, success=False, gate="E-1", session_id="sess-a", ts_iso=_now_iso())
        _seed_agent_outcome_at(db, success=False, gate="E-2", session_id="sess-a", ts_iso=_now_iso())

        result = read_rework_trend(db_path=db, months=1)
        assert len(result) == 1
        row = result[0]
        assert row["rework_count"] == 2
        assert row["session_count"] == 1
        assert row["per_session"] == pytest.approx(2.0)

    def test_success_true_rows_excluded(self, tmp_path: Path):
        """Q3-3: success=1 の行は差し戻しとしてカウントされない。"""
        from c3.db import read_rework_trend  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        _seed_agent_outcome_at(db, success=True, gate="E-1", session_id="sess-ok", ts_iso=_now_iso())

        result = read_rework_trend(db_path=db, months=1)
        assert result[0]["rework_count"] == 0

    def test_gate_outside_metrics_review_gates_excluded(self, tmp_path: Path):
        """Q3-4: gate が METRICS_REVIEW_GATES 外（D-3 等）は trend に集計されない。"""
        from c3.db import read_rework_trend  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        _seed_agent_outcome_at(db, success=False, gate="D-3", session_id="sess-dev", ts_iso=_now_iso())

        result = read_rework_trend(db_path=db, months=1)
        assert result[0]["rework_count"] == 0

    def test_since_filters_rows_within_zero_filled_buckets(self, tmp_path: Path):
        """Q3-5: since 指定時、ts < since の差し戻し行はゼロ埋め済みバケットの
        合算からも除外される（35 日前の古い行と当日行を seed し、since=当日で
        古い行だけが除外されることを固定。月境界の日付に依存しない設計）。"""
        from datetime import datetime, timezone  # noqa: PLC0415

        from c3.db import read_rework_trend  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        _seed_agent_outcome_at(db, success=False, gate="E-1", session_id="sess-old",
                                ts_iso=_iso_days_ago(35))
        _seed_agent_outcome_at(db, success=False, gate="E-1", session_id="sess-new",
                                ts_iso=_now_iso())

        since_today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        result = read_rework_trend(db_path=db, months=2, since=since_today)
        total_rework = sum(r["rework_count"] for r in result)
        assert total_rework == 1, (
            "since 指定時は 35 日前の古い行が除外され当日分の 1 件のみ集計される"
        )

    def test_months_caps_bucket_count(self, tmp_path: Path):
        """Q3-6: months で返却バケット数の上限が制御される。"""
        from c3.db import read_rework_trend  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        result_1 = read_rework_trend(db_path=db, months=1)
        result_6 = read_rework_trend(db_path=db, months=6)
        assert len(result_1) == 1
        assert len(result_6) == 6

    def test_empty_db_returns_zero_filled_months(self, tmp_path: Path):
        """Q3-7: 空 DB でも months 個ぶんゼロ埋め行を返し例外を出さない。"""
        from c3.db import read_rework_trend  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        result = read_rework_trend(db_path=db, months=3)
        assert len(result) == 3
        assert all(r["rework_count"] == 0 and r["per_session"] == 0.0 for r in result)

    def test_db_absent_returns_empty_list(self, tmp_path: Path):
        """Q3-8: DB 不在では共通規約どおり [] を返す
        （ゼロ埋めは「schema はあるが行が無い」場合の業務ロジックであり、
        DB 不在時の共通失敗規約より優先されない）。"""
        from c3.db import read_rework_trend  # noqa: PLC0415
        absent_db = tmp_path / "nonexistent.db"
        assert read_rework_trend(db_path=absent_db) == []


class TestReadReworkRoleDistribution:
    """Q4 群: read_rework_role_distribution(db_path=None, since=None) -> list[dict]。"""

    def test_all_gates_returned_including_uncategorized(self, tmp_path: Path):
        """Q4-1（DC-AM-003）: METRICS_REVIEW_GATES / METRICS_DEV_GATES いずれにも
        属さない分類外 gate（NULL 含む）も脱落せず全件返す。"""
        from c3.db import read_rework_role_distribution  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        _seed_agent_outcome_at(db, role="tester", success=False, gate="E-1",
                                session_id="s1", ts_iso=_now_iso())
        _seed_agent_outcome_at(db, role="developer", success=False, gate="D-3",
                                session_id="s2", ts_iso=_now_iso())
        _seed_agent_outcome_at(db, role="developer", success=False, gate="D-2.5",
                                session_id="s3", ts_iso=_now_iso())
        _seed_agent_outcome_at(db, role="developer", success=False, gate=None,
                                session_id="s4", ts_iso=_now_iso())

        result = read_rework_role_distribution(db_path=db)
        gates = {r["gate"] for r in result}
        assert "E-1" in gates
        assert "D-3" in gates
        assert "D-2.5" in gates, (
            "METRICS_REVIEW_GATES/METRICS_DEV_GATES いずれにも属さない gate も脱落しない"
        )
        assert None in gates, "gate=NULL の行も脱落しない"

    def test_role_gate_counts(self, tmp_path: Path):
        """Q4-2: role×gate の件数集計が正しい。"""
        from c3.db import read_rework_role_distribution  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        _seed_agent_outcome_at(db, role="tester", success=False, gate="E-1",
                                session_id="s1", ts_iso=_now_iso())
        _seed_agent_outcome_at(db, role="tester", success=False, gate="E-1",
                                session_id="s2", ts_iso=_now_iso())

        result = read_rework_role_distribution(db_path=db)
        row = next(r for r in result if r["role"] == "tester" and r["gate"] == "E-1")
        assert row["count"] == 2

    def test_success_true_excluded(self, tmp_path: Path):
        """Q4-3: success=1 の行は集計されない。"""
        from c3.db import read_rework_role_distribution  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        _seed_agent_outcome_at(db, role="tester", success=True, gate="E-1",
                                session_id="s1", ts_iso=_now_iso())

        assert read_rework_role_distribution(db_path=db) == []

    def test_since_filter(self, tmp_path: Path):
        """Q4-4: since 指定時 ts < since の行は除外される。"""
        from c3.db import read_rework_role_distribution  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        _seed_agent_outcome_at(db, role="tester", success=False, gate="E-1",
                                session_id="s-old", ts_iso="2020-01-01T00:00:00+00:00")
        _seed_agent_outcome_at(db, role="tester", success=False, gate="E-1",
                                session_id="s-new", ts_iso=_now_iso())

        result = read_rework_role_distribution(db_path=db, since="2025-01-01")
        row = next(r for r in result if r["role"] == "tester" and r["gate"] == "E-1")
        assert row["count"] == 1

    def test_empty_db_returns_empty_list(self, tmp_path: Path):
        """Q4-5: 空 DB で [] を返す。"""
        from c3.db import read_rework_role_distribution  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        assert read_rework_role_distribution(db_path=db) == []

    def test_db_absent_returns_empty_list(self, tmp_path: Path):
        """Q4-6: DB 不在で [] を返す。"""
        from c3.db import read_rework_role_distribution  # noqa: PLC0415
        absent_db = tmp_path / "nonexistent.db"
        assert read_rework_role_distribution(db_path=absent_db) == []


class TestReadSessionFixCycles:
    """Q5 群: read_session_fix_cycles(db_path=None, since=None) -> dict。"""

    def test_distribution_buckets_0_1_2plus(self, tmp_path: Path):
        """Q5-1: session 単位の fix-cycle（E-1/E-2/C-3 差し戻し件数）分布が
        0/1/2plus に振り分けられる。"""
        from c3.db import read_session_fix_cycles  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        # sess-0: 差し戻しなし（success=1 のみ）→ 0 回
        _seed_agent_outcome_at(db, success=True, gate="E-1", session_id="sess-0", ts_iso=_now_iso())
        # sess-1: 差し戻し 1 回
        _seed_agent_outcome_at(db, success=False, gate="E-1", session_id="sess-1", ts_iso=_now_iso())
        # sess-2: 差し戻し 2 回
        _seed_agent_outcome_at(db, success=False, gate="E-1", session_id="sess-2", ts_iso=_now_iso())
        _seed_agent_outcome_at(db, success=False, gate="E-2", session_id="sess-2", ts_iso=_now_iso())

        result = read_session_fix_cycles(db_path=db)
        assert result["distribution"]["0"] == 1
        assert result["distribution"]["1"] == 1
        assert result["distribution"]["2plus"] == 1

    def test_mean_max_total_sessions(self, tmp_path: Path):
        """Q5-2: mean/max/total_sessions が分布と整合する。"""
        from c3.db import read_session_fix_cycles  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        _seed_agent_outcome_at(db, success=False, gate="E-1", session_id="sess-a", ts_iso=_now_iso())
        _seed_agent_outcome_at(db, success=False, gate="E-1", session_id="sess-b", ts_iso=_now_iso())
        _seed_agent_outcome_at(db, success=False, gate="E-2", session_id="sess-b", ts_iso=_now_iso())

        result = read_session_fix_cycles(db_path=db)
        assert result["total_sessions"] == 2
        assert result["max"] == 2
        assert result["mean"] == pytest.approx((1 + 2) / 2)

    def test_granularity_and_note_keys(self, tmp_path: Path):
        """Q5-3: granularity == "session-approximation" と note キーが存在する。"""
        from c3.db import read_session_fix_cycles  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        result = read_session_fix_cycles(db_path=db)
        assert result["granularity"] == "session-approximation"
        assert isinstance(result["note"], str)
        assert result["note"] != ""

    def test_note_conveys_gist_without_internal_terms(self, tmp_path: Path):
        """Q5-4（DC-AM-001 round 4/5）: note は近似の趣旨キーワードを含み、
        内部監査 finding ID パターン・DB 内部名を含まない（negative assertion の
        スコープは note フィールド値のみ）。"""
        from c3.db import read_session_fix_cycles  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        note = read_session_fix_cycles(db_path=db)["note"]

        assert any(kw in note for kw in ("近似", "session", "セッション")), (
            f"note に近似・session 粒度の趣旨キーワードが含まれるはず: {note!r}"
        )
        assert not re.search(r"\b(DC|CR|SR)-[A-Z0-9]+-\d{3,}\b", note), (
            f"note に監査 finding ID が混入してはならない: {note!r}"
        )
        assert "round " not in note.lower(), f"note に内部レビューラウンド表記が混入: {note!r}"
        for internal_term in ("agent_outcomes", "agent_cost_runs", "recorded_at"):
            assert internal_term not in note, f"note に DB 内部名 {internal_term!r} が混入: {note!r}"

    def test_since_filters_sessions(self, tmp_path: Path):
        """Q5-5（DC-AM-003）: since 指定時 ts < since の行は fix-cycle 集計から
        除外される（--since 適用対象 6 経路の 1 つ・fix_cycles の適用漏れを検出）。"""
        from c3.db import read_session_fix_cycles  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        _seed_agent_outcome_at(db, success=False, gate="E-1", session_id="sess-old",
                                ts_iso="2020-01-01T00:00:00+00:00")
        _seed_agent_outcome_at(db, success=False, gate="E-1", session_id="sess-new",
                                ts_iso=_now_iso())

        result = read_session_fix_cycles(db_path=db, since="2025-01-01")
        assert result["total_sessions"] == 1, (
            "since 指定時は 2020 年の古い session が除外され新しい 1 session のみ集計される"
        )

    def test_empty_db_returns_zero_filled_dict(self, tmp_path: Path):
        """Q5-6: 空 DB で 0 埋め dict を返し例外を出さない。"""
        from c3.db import read_session_fix_cycles  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        result = read_session_fix_cycles(db_path=db)
        assert result["distribution"] == {"0": 0, "1": 0, "2plus": 0}
        assert result["mean"] == 0.0
        assert result["max"] == 0
        assert result["total_sessions"] == 0

    def test_db_absent_returns_zero_filled_dict(self, tmp_path: Path):
        """Q5-7: DB 不在で例外を出さず 0 埋め dict を返す。"""
        from c3.db import read_session_fix_cycles  # noqa: PLC0415
        absent_db = tmp_path / "nonexistent.db"
        result = read_session_fix_cycles(db_path=absent_db)
        assert result["total_sessions"] == 0
        assert result["distribution"] == {"0": 0, "1": 0, "2plus": 0}


class TestReadReworkSessionCost:
    """Q6 群: read_rework_session_cost(db_path=None, since=None) -> dict。"""

    def test_rework_usd_and_tokens_aggregated_for_rework_sessions(self, tmp_path: Path):
        """Q6-1: 差し戻しあり session（success=0 AND gate IN METRICS_REVIEW_GATES）
        の USD/トークン合算が正しい。差し戻しなしの別 session のコストは
        分子に含まれない。"""
        from c3.db import read_rework_session_cost  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        _seed_agent_outcome_at(db, success=False, gate="E-1", session_id="sess-rw",
                                ts_iso=_now_iso())
        _seed_cost_run_at(db, session_id="sess-rw", agent_id="agent-1",
                           total_cost_usd=0.5, recorded_at_iso=_now_iso(),
                           input_tokens=1000, output_tokens=500)
        _seed_cost_run_at(db, session_id="sess-clean", agent_id="agent-2",
                           total_cost_usd=0.3, recorded_at_iso=_now_iso(),
                           input_tokens=100, output_tokens=50)

        result = read_rework_session_cost(db_path=db)
        assert result["rework_session_count"] == 1
        assert result["rework_total_usd"] == pytest.approx(0.5)
        assert result["rework_total_tokens"] == 1500

    def test_overall_is_sum_over_all_cost_runs_no_limit(self, tmp_path: Path):
        """Q6-2（DC-GP-002）: overall（分母）は agent_cost_runs 全行の SUM
        （LIMIT なし・read_agent_cost_summary の limit=50 list 合算に依存しない）。
        agent_type 51 種以上でも切り捨てられず overall_ratio <= 1.0 を維持する。"""
        from c3.db import read_rework_session_cost  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        _seed_agent_outcome_at(db, success=False, gate="E-1", session_id="sess-rw",
                                ts_iso=_now_iso())
        _seed_cost_run_at(db, session_id="sess-rw", agent_id="agent-rw",
                           total_cost_usd=0.1, recorded_at_iso=_now_iso())
        for i in range(51):
            _seed_cost_run_at(
                db, session_id=f"sess-other-{i:03d}", agent_id=f"agent-{i:03d}",
                agent_type=f"agent-type-{i:03d}",
                total_cost_usd=1.0, recorded_at_iso=_now_iso(),
            )

        result = read_rework_session_cost(db_path=db)
        assert result["overall_total_usd"] == pytest.approx(0.1 + 51 * 1.0)
        assert result["overall_ratio"] <= 1.0

    def test_overall_ratio_zero_division_is_zero(self, tmp_path: Path):
        """Q6-3: overall_total_usd == 0 のとき overall_ratio は 0 除算せず 0.0。"""
        from c3.db import read_rework_session_cost  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        assert read_rework_session_cost(db_path=db)["overall_ratio"] == 0.0

    def test_since_uses_recorded_at_not_ts_and_does_not_collapse_to_zero(self, tmp_path: Path):
        """Q6-4（DC-AS-001）: --since 併用時 overall は recorded_at で例外なく
        集計され overall_ratio が 0.0 に潰れない（agent_cost_runs に ts 列は
        存在しないため、誤って ts を突き当てると no such column: ts が共通規約で
        握り潰され 0.0 に静かに倒れる回帰を検出する）。"""
        from c3.db import read_rework_session_cost  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        _seed_agent_outcome_at(db, success=False, gate="E-1", session_id="sess-rw",
                                ts_iso=_now_iso())
        _seed_cost_run_at(db, session_id="sess-rw", agent_id="agent-rw",
                           total_cost_usd=0.2, recorded_at_iso=_now_iso())

        result = read_rework_session_cost(db_path=db, since="2025-01-01")
        assert result["overall_total_usd"] == pytest.approx(0.2), (
            "recorded_at >= since で例外なく集計されるはず（ts 誤参照だと 0.0 に潰れる）"
        )
        assert result["overall_ratio"] > 0.0

    def test_since_numerator_denominator_symmetric_ratio_le_1(self, tmp_path: Path):
        """Q6-5（DC-GP-001）: --since 併用時も分子・分母が同一 recorded_at>=since
        フィルタで対称になり overall_ratio <= 1.0 が常に成立する。"""
        from c3.db import read_rework_session_cost  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        _seed_agent_outcome_at(db, success=False, gate="E-1", session_id="sess-rw",
                                ts_iso=_now_iso())
        _seed_cost_run_at(db, session_id="sess-rw", agent_id="agent-old",
                           total_cost_usd=10.0, recorded_at_iso="2020-01-01T00:00:00+00:00")
        _seed_cost_run_at(db, session_id="sess-rw", agent_id="agent-new",
                           total_cost_usd=0.1, recorded_at_iso=_now_iso())
        _seed_cost_run_at(db, session_id="sess-other", agent_id="agent-other",
                           total_cost_usd=0.05, recorded_at_iso=_now_iso())

        result = read_rework_session_cost(db_path=db, since="2025-01-01")
        assert result["overall_ratio"] <= 1.0
        assert result["rework_total_usd"] == pytest.approx(0.1), (
            "分子は recorded_at>=since の cost 行のみ合算（2020 年の行は除外）"
        )

    def test_since_population_mismatch_reflected_in_count_and_cost(self, tmp_path: Path):
        """Q6-6（DC-AM-002 round 3）: --since 併用時、rework 判定は ts 基準・
        コスト合算は recorded_at 基準の別クロックで母集団が完全一致しない場合を
        固定する。ts>=since で rework 判定された session の cost 行が全て
        recorded_at<since なら、rework_session_count には 1 とカウントされるが
        rework_total_usd には寄与しない。"""
        from c3.db import read_rework_session_cost  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        _seed_agent_outcome_at(db, success=False, gate="E-1", session_id="sess-mismatch",
                                ts_iso=_now_iso())
        _seed_cost_run_at(db, session_id="sess-mismatch", agent_id="agent-mismatch",
                           total_cost_usd=5.0, recorded_at_iso="2020-01-01T00:00:00+00:00")

        result = read_rework_session_cost(db_path=db, since="2025-01-01")
        assert result["rework_session_count"] == 1, (
            "ts 基準では since 以降の rework session として 1 カウントされる"
        )
        assert result["rework_total_usd"] == pytest.approx(0.0), (
            "コスト行が全て recorded_at < since のため寄与は $0（母集団非対称）"
        )

    def test_granularity_and_note_keys(self, tmp_path: Path):
        """Q6-7: granularity == "session-approximation" と note キーが存在する。"""
        from c3.db import read_rework_session_cost  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        result = read_rework_session_cost(db_path=db)
        assert result["granularity"] == "session-approximation"
        assert isinstance(result["note"], str)
        assert result["note"] != ""

    def test_note_conveys_population_mismatch_gist_without_internal_terms(self, tmp_path: Path):
        """Q6-8（DC-AM-001 round 4/5）: note は母集団非一致の趣旨キーワードを
        含み、内部監査 finding ID パターン・DB 内部名を含まない（negative
        assertion のスコープは note フィールド値のみ。rework_session_count は
        JSON 公開キーであって禁止語ではないため検査対象に含めない）。"""
        from c3.db import read_rework_session_cost  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        note = read_rework_session_cost(db_path=db)["note"]

        assert any(kw in note for kw in ("母集団", "目安", "近似")), (
            f"note に母集団非一致・近似の趣旨キーワードが含まれるはず: {note!r}"
        )
        assert not re.search(r"\b(DC|CR|SR)-[A-Z0-9]+-\d{3,}\b", note), (
            f"note に監査 finding ID が混入してはならない: {note!r}"
        )
        assert "round " not in note.lower(), f"note に内部レビューラウンド表記が混入: {note!r}"
        for internal_term in ("agent_outcomes", "agent_cost_runs", "recorded_at"):
            assert internal_term not in note, f"note に DB 内部名 {internal_term!r} が混入: {note!r}"

    def test_empty_db_returns_zero_filled_dict(self, tmp_path: Path):
        """Q6-9: 空 DB で 0 埋め dict を返し例外を出さない。"""
        from c3.db import read_rework_session_cost  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        result = read_rework_session_cost(db_path=db)
        assert result["rework_session_count"] == 0
        assert result["rework_total_usd"] == 0.0
        assert result["rework_total_tokens"] == 0
        assert result["overall_total_usd"] == 0.0
        assert result["overall_ratio"] == 0.0

    def test_db_absent_returns_zero_filled_dict(self, tmp_path: Path):
        """Q6-10: DB 不在で例外を出さず 0 埋め dict を返す。"""
        from c3.db import read_rework_session_cost  # noqa: PLC0415
        absent_db = tmp_path / "nonexistent.db"
        result = read_rework_session_cost(db_path=absent_db)
        assert result["rework_session_count"] == 0
        assert result["overall_ratio"] == 0.0

    @pytest.mark.skipif(
        sys.version_info < (3, 11),
        reason="sqlite3.Connection.setlimit は Python 3.11 で追加されたため"
               "（v2.46.0 CI の Python 3.10 ジョブで AttributeError となった。"
               "プロダクト実装の TEMP テーブル方式は setlimit 非依存で、"
               "本テストのシミュレーション手法のみが 3.11+ を要する）",
    )
    def test_large_rework_session_count_survives_low_variable_number_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Q6-11（code-review-report-20260707-011501.md item6）: rework
        セッション数が 1000 超（1200）でも動的 `IN (?,?,...)` 句がバインド
        変数上限に抵触せず、`overall_ratio` が 0 に崩れず正しい合算を返す。

        本環境の SQLite ビルド（3.45.1）の実上限は 32766 のため 1200 件では
        自然には再現しないが、`sqlite3.Connection.setlimit` で
        `SQLITE_LIMIT_VARIABLE_NUMBER` を 999 に強制し、旧ビルド（上限 999）
        相当の環境をシミュレートして固定する。本タスク開始時点の実装は
        動的 `IN` 句のプレースホルダ数がセッション数に比例して増えるため、
        999 ビルド相当の環境では `sqlite3.OperationalError`（too many SQL
        variables）が共通例外規約で握り潰され、`rework_session_count` /
        `overall_ratio` が静かに 0 に崩れていた（Red）。"""
        from c3.db import read_rework_session_cost  # noqa: PLC0415
        import sqlite3 as _sqlite3  # noqa: PLC0415

        db = _make_c3_db(tmp_path)
        session_count = 1200
        ts = _now_iso()

        conn = _sqlite3.connect(str(db))
        try:
            conn.executemany(
                "INSERT INTO agent_outcomes "
                "(role, task_complexity, tier, success, gate, note, session_id, ts) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
                [
                    ("developer", "medium", "sonnet", 0, "E-1", f"sess-large-{i:04d}", ts)
                    for i in range(session_count)
                ],
            )
            conn.executemany(
                "INSERT INTO agent_cost_runs "
                "(session_id, agent_id, agent_type, description, model, "
                " attribution_skill, input_tokens, output_tokens, "
                " cache_read_tokens, cache_create_tokens, total_cost_usd, recorded_at) "
                "VALUES (?, ?, ?, NULL, ?, NULL, ?, ?, 0, 0, ?, ?)",
                [
                    (
                        f"sess-large-{i:04d}", f"agent-large-{i:04d}", "developer",
                        "claude-sonnet-4-6-20260101", 100, 50, 1.0, ts,
                    )
                    for i in range(session_count)
                ],
            )
            conn.commit()
        finally:
            conn.close()

        real_connect = _sqlite3.connect

        def _connect_with_lowered_variable_limit(*args, **kwargs):
            new_conn = real_connect(*args, **kwargs)
            new_conn.setlimit(_sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
            return new_conn

        monkeypatch.setattr(_sqlite3, "connect", _connect_with_lowered_variable_limit)

        result = read_rework_session_cost(db_path=db)

        assert result["rework_session_count"] == session_count, (
            "999 ビルド相当の環境でも rework セッション数は 0 に崩れないはず"
        )
        assert result["rework_total_usd"] == pytest.approx(float(session_count)), (
            "999 ビルド相当の環境でも rework コスト合算は 0 に崩れないはず"
        )
        assert result["overall_total_usd"] == pytest.approx(float(session_count))
        assert result["overall_ratio"] == pytest.approx(1.0)


class TestMetricsTimeFormatPrecondition:
    """Q7 群（DC-AS-001）: 時刻比較は全行 UTC ISO8601（+00:00 秒精度）前提であり、
    YYYY-MM-DD プレフィックス >= 比較が全行で日付順=時系列順と一致することを
    固定する。agent_cost_runs の突き当ては recorded_at（ts 列は存在しない）
    ことも固定する。"""

    def test_decided_at_stored_as_utc_iso8601_with_offset(self, tmp_path: Path):
        """Q7-1: insert_review_decision の decided_at が UTC ISO8601（+00:00
        秒精度）で格納され、YYYY-MM-DD プレフィックスの since 比較が成立する。"""
        import sqlite3 as _sqlite3  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        _seed_review_decision(db, checklist_id="CR-TS-1", decision="fixed",
                               reviewer="code-reviewer", severity="high",
                               decided_at_iso=_now_iso())

        conn = _sqlite3.connect(str(db))
        try:
            row = conn.execute(
                "SELECT decided_at FROM review_decisions WHERE checklist_id='CR-TS-1'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        stored = row[0]
        assert stored.endswith("+00:00"), f"decided_at は UTC オフセット付きであるはず: {stored!r}"
        assert re.match(r"^\d{4}-\d{2}-\d{2}T", stored), (
            f"YYYY-MM-DD プレフィックス比較が成立するはず: {stored!r}"
        )

    def test_ts_stored_as_utc_iso8601_with_offset(self, tmp_path: Path):
        """Q7-2: record_agent_outcome_event の ts が UTC ISO8601（+00:00 秒精度）
        で格納される。"""
        import sqlite3 as _sqlite3  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        record_agent_outcome_event(role="tester", complexity="medium", tier="sonnet",
                                    success=False, gate="E-1", session_id="s1", db_path=db)

        conn = _sqlite3.connect(str(db))
        try:
            row = conn.execute(
                "SELECT ts FROM agent_outcomes WHERE session_id='s1'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0].endswith("+00:00"), f"ts は UTC オフセット付きであるはず: {row[0]!r}"

    def test_recorded_at_stored_as_utc_iso8601_with_offset_no_ts_column(self, tmp_path: Path):
        """Q7-3: insert_agent_cost_run の recorded_at が UTC ISO8601（+00:00
        秒精度）で格納される。agent_cost_runs には ts 列が存在しない
        （突き当ては recorded_at）。"""
        import sqlite3 as _sqlite3  # noqa: PLC0415
        db = _make_c3_db(tmp_path)
        insert_agent_cost_run(
            session_id="s1", agent_id="agent-1", agent_type="developer",
            description=None, model="claude-sonnet-4-6-20260101", attribution_skill=None,
            input_tokens=10, output_tokens=5, cache_read_tokens=0, cache_create_tokens=0,
            total_cost_usd=0.01, db_path=db,
        )

        conn = _sqlite3.connect(str(db))
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(agent_cost_runs)").fetchall()]
            row = conn.execute(
                "SELECT recorded_at FROM agent_cost_runs WHERE session_id='s1'"
            ).fetchone()
        finally:
            conn.close()
        assert "ts" not in cols, "agent_cost_runs に ts 列は存在しないはず（recorded_at のみ）"
        assert row is not None
        assert row[0].endswith("+00:00")


class TestMetricsHelpersEmptyDbSmoke:
    """Q8 群: 6 ヘルパー全てが空 DB / DB 不在で例外を出さず空値/0 埋めを
    返すことの結合スモーク。"""

    def test_all_six_helpers_no_exception_on_empty_db(self, tmp_path: Path):
        """Q8-1: 空 DB（migration 適用済み・行なし）で 6 ヘルパー全てが
        例外を出さない。"""
        from c3.db import (  # noqa: PLC0415
            fetch_prevented_findings,
            read_review_decision_matrix,
            read_rework_role_distribution,
            read_rework_session_cost,
            read_rework_trend,
            read_session_fix_cycles,
        )
        db = _make_c3_db(tmp_path)

        assert read_review_decision_matrix(db_path=db) == []
        assert fetch_prevented_findings(db_path=db) == []
        trend = read_rework_trend(db_path=db, months=3)
        assert len(trend) == 3
        assert read_rework_role_distribution(db_path=db) == []
        fix_cycles = read_session_fix_cycles(db_path=db)
        assert fix_cycles["total_sessions"] == 0
        cost = read_rework_session_cost(db_path=db)
        assert cost["rework_session_count"] == 0

    def test_all_six_helpers_no_exception_on_absent_db(self, tmp_path: Path):
        """Q8-2: DB ファイル自体が不在でも 6 ヘルパー全てが例外を出さない。"""
        from c3.db import (  # noqa: PLC0415
            fetch_prevented_findings,
            read_review_decision_matrix,
            read_rework_role_distribution,
            read_rework_session_cost,
            read_rework_trend,
            read_session_fix_cycles,
        )
        absent_db = tmp_path / "nonexistent.db"

        assert read_review_decision_matrix(db_path=absent_db) == []
        assert fetch_prevented_findings(db_path=absent_db) == []
        assert read_rework_trend(db_path=absent_db) == []
        assert read_rework_role_distribution(db_path=absent_db) == []
        fix_cycles = read_session_fix_cycles(db_path=absent_db)
        assert fix_cycles["total_sessions"] == 0
        cost = read_rework_session_cost(db_path=absent_db)
        assert cost["rework_session_count"] == 0
