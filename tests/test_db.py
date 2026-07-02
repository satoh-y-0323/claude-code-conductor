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
    read_tier_failure_rate,
    read_tier_params,
    record_agent_outcome_event,
    record_tier_recent_outcome,
    set_ingest_offset,
    sync_tier_bandit_cost,
    update_tier_params,
)


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
# O 群: agent-tier-routing 学習シグナル再設計（v2.41.0 db-foundation・Red 先行）
#
# architecture-report-20260702-214748.md §3-2/§3-3 に対応。新シンボル
# （AGENT_ROLES / read_agent_tier_params / update_agent_tier_params /
#  record_agent_outcome_event / read_agent_failure_rate / read_recent_agent_outcomes）
# は本タスク時点で未実装のため、モジュール冒頭の import には加えず各テスト内で
# ローカル import する（本ファイル全体の collection を壊さないため。既存 N7 群までの
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
    """O1 群: read_agent_tier_params(role, complexity, *, db_path=None) のテスト。"""

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

    def test_reflects_updated_params(self, tmp_path: Path):
        """O1-2: update_agent_tier_params で更新した値が読み出せる。"""
        from c3.db import read_agent_tier_params, update_agent_tier_params  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        update_agent_tier_params("developer", "medium", "sonnet", success=True, db_path=db)

        result = read_agent_tier_params("developer", "medium", db_path=db)
        assert result["sonnet"] == (2.0, 1.0, 1)
        # 未更新の他 tier は初期値のまま
        assert result["haiku"] == (1.0, 1.0, 0)

    def test_role_isolation(self, tmp_path: Path):
        """O1-3: role が異なれば同一 complexity/tier でも別セルとして扱われる。"""
        from c3.db import read_agent_tier_params, update_agent_tier_params  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        update_agent_tier_params("developer", "medium", "sonnet", success=True, db_path=db)

        developer_result = read_agent_tier_params("developer", "medium", db_path=db)
        tester_result = read_agent_tier_params("tester", "medium", db_path=db)

        assert developer_result["sonnet"] == (2.0, 1.0, 1)
        assert tester_result["sonnet"] == (1.0, 1.0, 0), (
            "tester role には developer の更新が漏れてはいけない"
        )

    def test_db_absent_returns_defaults(self, tmp_path: Path):
        """O1-4: DB 不在で全 tier 初期値を返す（静かな失敗）。"""
        from c3.db import read_agent_tier_params  # noqa: PLC0415
        absent_db = tmp_path / "no_such.db"

        result = read_agent_tier_params("developer", "simple", db_path=absent_db)
        assert result == {
            "haiku": (1.0, 1.0, 0),
            "sonnet": (1.0, 1.0, 0),
            "opus": (1.0, 1.0, 0),
        }


class TestUpdateAgentTierParams:
    """O2 群: update_agent_tier_params(role, complexity, tier, *, success, db_path=None)。

    【DC-GP-002】success=True → alpha+=1, success=False → beta+=1、いずれも trials+=1・
    last_updated 更新（旧 update_tier_params と同一規則）。
    """

    def test_success_increments_alpha_and_trials(self, tmp_path: Path):
        """O2-1: 初回 success=True で (2.0, 1.0, 1) になる。"""
        import sqlite3  # noqa: PLC0415
        from c3.db import update_agent_tier_params  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        ok = update_agent_tier_params("developer", "simple", "haiku", success=True, db_path=db)
        assert ok is True

        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT alpha, beta, trials FROM agent_tier_bandit "
            "WHERE role='developer' AND task_complexity='simple' AND tier='haiku'"
        ).fetchone()
        conn.close()
        assert row == (2.0, 1.0, 1)

    def test_failure_increments_beta_and_trials(self, tmp_path: Path):
        """O2-2: 初回 success=False で (1.0, 2.0, 1) になる。"""
        import sqlite3  # noqa: PLC0415
        from c3.db import update_agent_tier_params  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        update_agent_tier_params("developer", "simple", "haiku", success=False, db_path=db)

        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT alpha, beta, trials FROM agent_tier_bandit "
            "WHERE role='developer' AND task_complexity='simple' AND tier='haiku'"
        ).fetchone()
        conn.close()
        assert row == (1.0, 2.0, 1)

    def test_upsert_accumulates_across_calls(self, tmp_path: Path):
        """O2-3: 複数回呼ぶと UPSERT で加算蓄積される（success→success→failure）。"""
        import sqlite3  # noqa: PLC0415
        from c3.db import update_agent_tier_params  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        update_agent_tier_params("developer", "medium", "sonnet", success=True, db_path=db)
        update_agent_tier_params("developer", "medium", "sonnet", success=True, db_path=db)
        update_agent_tier_params("developer", "medium", "sonnet", success=False, db_path=db)

        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT alpha, beta, trials FROM agent_tier_bandit "
            "WHERE role='developer' AND task_complexity='medium' AND tier='sonnet'"
        ).fetchone()
        conn.close()
        assert row == (3.0, 2.0, 3)

    def test_role_pk_isolation(self, tmp_path: Path):
        """O2-4: PK に role を含むため、同じ complexity/tier でも role が違えば別行になる。"""
        import sqlite3  # noqa: PLC0415
        from c3.db import update_agent_tier_params  # noqa: PLC0415
        db = _make_c3_db_v004(tmp_path)

        update_agent_tier_params("developer", "medium", "sonnet", success=True, db_path=db)
        update_agent_tier_params("tester", "medium", "sonnet", success=True, db_path=db)

        conn = sqlite3.connect(str(db))
        rows = conn.execute(
            "SELECT role, alpha, beta, trials FROM agent_tier_bandit "
            "WHERE task_complexity='medium' AND tier='sonnet' ORDER BY role"
        ).fetchall()
        conn.close()
        assert len(rows) == 2, "role が異なれば別行になるべき"
        assert {r[0] for r in rows} == {"developer", "tester"}

    def test_db_absent_returns_false(self, tmp_path: Path):
        """O2-5: DB 不在で False を返す。"""
        from c3.db import update_agent_tier_params  # noqa: PLC0415
        absent_db = tmp_path / "no_such.db"

        ok = update_agent_tier_params(
            "developer", "simple", "haiku", success=True, db_path=absent_db
        )
        assert ok is False


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
    """O4 群: read_agent_failure_rate(role, complexity, tier, *, last_n=10, db_path=None)。

    `_FAILURE_RATE_MIN_SAMPLES=5` を継承（旧 read_tier_failure_rate と同一規則）。
    """

    def _seed_events(self, db, *, role, complexity, tier, outcomes, session_prefix):
        from c3.db import record_agent_outcome_event  # noqa: PLC0415
        for i, success in enumerate(outcomes):
            record_agent_outcome_event(
                role=role, complexity=complexity, tier=tier, success=success,
                session_id=f"{session_prefix}-{i}", db_path=db,
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
# P 群: agent-tier-routing 学習シグナル再設計（v2.41.0 db-shims-and-cost・Red 先行）
#
# architecture-report-20260702-214748.md ADR-4/ADR-5・§3-3・§5 に対応。
#
# P0: deprecated シム 5 関数（read_tier_params/read_tier_failure_rate/
#     update_tier_params/record_tier_recent_outcome/sync_tier_bandit_cost）の
#     初期値/no-op 挙動。「DB に一切触れない」ことを sqlite3.connect スパイで検証する
#     （旧テーブル tier_bandit/tier_recent_outcomes は 004 で DROP 済みのため、
#     現行実装は例外捕捉経由でも初期値/False を返すことがあり、戻り値だけでは
#     shim 化の Red を検出できないケースがある。connect 未呼び出しの確認で
#     「例外に頼った偶然の初期値返却」と「真の no-op shim」を区別する）。
# P1: 【DC-GP-004】旧 select_tier.py / 旧 record_tier_outcome.py の実呼び出し形
#     （位置引数・キーワード）でシムが TypeError なく呼べることの固定。
# P2: 【DC-GP-003】cost JOIN 差替（tier_recent_outcomes → agent_outcomes）の
#     結果同値性・DISTINCT 二重計上なし。
# P3: 廃止確認（read_tier_bandit_cost / 旧 read_recent_outcomes が db.py から消える）。
#
# NOTE(developer への引き継ぎ): 本ファイル冒頭の
# `from c3.db import (..., read_tier_bandit_cost, ..., read_tier_cost_for_complexity, ...)`
# は該当関数削除と同時に ImportError で本ファイル全体の collection が壊れる。
# Green フェーズで対応が必要（詳細は test-report 参照）。
# ---------------------------------------------------------------------------


def _make_v004_db(tmp_path: Path) -> Path:
    """P 群専用エイリアス。実体は O 群の `_make_c3_db_v004` と同じ
    （004 まで migration 適用済みの c3.db を作る）。可読性のため別名で参照する。
    """
    return _make_c3_db_v004(tmp_path)


class TestDeprecatedShimBehavior:
    """P0 群: ADR-5 deprecated シム 5 関数の初期値/no-op 挙動 + DB 非アクセス確認。"""

    def _spy_connect(self, monkeypatch: pytest.MonkeyPatch) -> list:
        """c3.db モジュールが使う sqlite3.connect を監視するスパイを仕込む。

        真の no-op shim なら DB へ一切接続しないはず。呼び出し回数のリストを返す。
        """
        import sqlite3 as _sqlite3  # noqa: PLC0415
        import c3.db as db_module  # noqa: PLC0415

        calls: list = []
        real_connect = _sqlite3.connect

        def _tracking_connect(*args, **kwargs):
            calls.append((args, kwargs))
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(db_module.sqlite3, "connect", _tracking_connect)
        return calls

    def test_read_tier_params_shim_all_tiers_uniform_no_db_access(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """P0-1: read_tier_params は常に全 tier (1.0,1.0,0) を返し DB に接続しない。"""
        db = _make_v004_db(tmp_path)
        calls = self._spy_connect(monkeypatch)

        result = read_tier_params("medium", db_path=db)

        assert result == {
            "haiku": (1.0, 1.0, 0),
            "sonnet": (1.0, 1.0, 0),
            "opus": (1.0, 1.0, 0),
        }
        assert calls == [], (
            "read_tier_params は deprecated shim として DB に一切接続してはいけない"
        )

    def test_read_tier_failure_rate_shim_returns_none_zero_no_db_access(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """P0-2: read_tier_failure_rate は常に (None, 0) を返し DB に接続しない。"""
        db = _make_v004_db(tmp_path)
        calls = self._spy_connect(monkeypatch)

        result = read_tier_failure_rate("medium", "sonnet", db_path=db)

        assert result == (None, 0)
        assert calls == [], (
            "read_tier_failure_rate は deprecated shim として DB に一切接続してはいけない"
        )

    def test_update_tier_params_shim_returns_true_no_db_access(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """P0-3: update_tier_params は no-op で True を返し DB に接続しない（False だと
        旧 record_tier_outcome.py が json を保持し続け警告が毎回出る/ADR-5）。"""
        db = _make_v004_db(tmp_path)
        calls = self._spy_connect(monkeypatch)

        ok = update_tier_params("medium", "sonnet", success=True, db_path=db)

        assert ok is True
        assert calls == [], (
            "update_tier_params は deprecated shim として DB に一切接続してはいけない"
        )

        # agent_tier_bandit（新テーブル）が不変であることも確認する
        import sqlite3 as _sqlite3  # noqa: PLC0415
        conn = _sqlite3.connect(str(db))
        try:
            count = conn.execute("SELECT COUNT(*) FROM agent_tier_bandit").fetchone()[0]
        finally:
            conn.close()
        assert count == 0

    def test_record_tier_recent_outcome_shim_returns_true_no_db_access(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """P0-4: record_tier_recent_outcome は no-op で True を返し DB に接続しない。"""
        db = _make_v004_db(tmp_path)
        calls = self._spy_connect(monkeypatch)

        ok = record_tier_recent_outcome(
            complexity="medium", tier="sonnet", success=True,
            session_id="p0-4-sess", db_path=db,
        )

        assert ok is True
        assert calls == [], (
            "record_tier_recent_outcome は deprecated shim として DB に一切接続してはいけない"
        )

        # agent_outcomes（新テーブル）が不変であることも確認する
        import sqlite3 as _sqlite3  # noqa: PLC0415
        conn = _sqlite3.connect(str(db))
        try:
            count = conn.execute("SELECT COUNT(*) FROM agent_outcomes").fetchone()[0]
        finally:
            conn.close()
        assert count == 0

    def test_sync_tier_bandit_cost_shim_no_db_access(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """P0-5: sync_tier_bandit_cost は no-op で DB に接続しない
        （旧実装は read_tier_cost_rate_summary 経由で必ず sqlite3.connect していた）。"""
        db = _make_v004_db(tmp_path)
        # agent_outcomes/agent_cost_runs にデータがあっても shim なら参照しないはず
        record_agent_outcome_event(
            role="developer", complexity="medium", tier="sonnet",
            success=True, session_id="p0-5-sess", db_path=db,
        )
        insert_agent_cost_run(
            session_id="p0-5-sess", agent_id="agent-1", agent_type="developer",
            description=None, model="claude-sonnet-4-6-20260101",
            attribution_skill=None, input_tokens=100, output_tokens=50,
            cache_read_tokens=0, cache_create_tokens=0, total_cost_usd=0.05,
            db_path=db,
        )
        calls = self._spy_connect(monkeypatch)

        result = sync_tier_bandit_cost(db_path=db)

        assert not result, "sync_tier_bandit_cost は no-op 相当（0 件）を返すはず"
        assert calls == [], (
            "sync_tier_bandit_cost は deprecated shim として DB に一切接続してはいけない"
        )


class TestShimSignatureCompat:
    """P1 群: 【DC-GP-004】旧 select_tier.py / 旧 record_tier_outcome.py の実呼び出し形で
    シムが TypeError なく呼べることを固定する（シグネチャ整合）。

    呼び出し形は現行コードから抽出:
      - .claude/hooks/select_tier.py L759: c3_db.read_tier_params(complexity)
      - .claude/hooks/select_tier.py L443: c3_db.read_tier_failure_rate(complexity, tier)
      - .claude/skills/dev-workflow/scripts/record_tier_outcome.py L205-209:
        c3_db.update_tier_params(complexity, tier, success=success)
      - .claude/skills/dev-workflow/scripts/record_tier_outcome.py L217-222:
        c3_db.record_tier_recent_outcome(complexity=..., tier=..., success=..., session_id=...)
      - .claude/hooks/session_stop.py L107: sync_tier_bandit_cost()（引数なし）
    """

    def test_read_tier_params_positional_call(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """P1-1: 旧 select_tier.py の呼び出し形 read_tier_params(complexity)（db_path キーワードなし）。

        旧呼び出し形は db_path を渡さないため、locate_c3_db() が実 FS を探索してしまわない
        よう C3_DB_PATH env で db を指す（呼び出し形 read_tier_params(complexity) は維持）。
        """
        db = _make_v004_db(tmp_path)
        monkeypatch.setenv("C3_DB_PATH", str(db))

        result = read_tier_params("medium")

        assert result == {
            "haiku": (1.0, 1.0, 0),
            "sonnet": (1.0, 1.0, 0),
            "opus": (1.0, 1.0, 0),
        }

    def test_read_tier_failure_rate_positional_call(self):
        """P1-2: 旧 select_tier.py の呼び出し形 read_tier_failure_rate(complexity, tier)。"""
        result = read_tier_failure_rate("medium", "sonnet")
        assert result == (None, 0)

    def test_update_tier_params_mixed_call(self, tmp_path: Path):
        """P1-3: 旧 record_tier_outcome.py の呼び出し形
        update_tier_params(complexity, tier, success=success)。"""
        db = _make_v004_db(tmp_path)
        ok = update_tier_params("medium", "sonnet", success=True, db_path=db)
        assert ok is True

    def test_record_tier_recent_outcome_kwargs_call(self, tmp_path: Path):
        """P1-4: 旧 record_tier_outcome.py の呼び出し形
        record_tier_recent_outcome(complexity=..., tier=..., success=..., session_id=...)。"""
        db = _make_v004_db(tmp_path)
        ok = record_tier_recent_outcome(
            complexity="medium", tier="sonnet", success=False, session_id="p1-4-sess",
            db_path=db,
        )
        assert ok is True

    def test_sync_tier_bandit_cost_no_args_call(self, monkeypatch: pytest.MonkeyPatch):
        """P1-5: 旧 session_stop.py の呼び出し形 sync_tier_bandit_cost()（引数なし）。

        locate_c3_db() が実 FS を探索して None を返す環境（本テストの tmp cwd 想定）で
        呼んでも TypeError にならないことのみを固定する（DB 内容は問わない）。
        """
        import c3.db as db_module  # noqa: PLC0415
        monkeypatch.setattr(db_module, "locate_c3_db", lambda *a, **kw: None)

        result = sync_tier_bandit_cost()
        assert result == 0


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
