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
    read_tier_bandit_cost,
    read_tier_cost_rate_for_complexity,
    read_tier_cost_rate_summary,
    read_tier_cost_summary,
    read_tier_cost_for_complexity,
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


def _seed_cost_run(db: Path, *, session_id: str, agent_id: str,
                   agent_type: str, total_cost_usd: float,
                   model: str = "claude-sonnet-4-6-20260101") -> None:
    """agent_cost_runs に seed を挿入するヘルパー。"""
    insert_agent_cost_run(
        session_id=session_id,
        agent_id=agent_id,
        agent_type=agent_type,
        description=None,
        model=model,
        attribution_skill=None,
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=0,
        cache_create_tokens=0,
        total_cost_usd=total_cost_usd,
        db_path=db,
    )


class TestTierCostHelpers:
    """H 群: v2.22.0 で追加した session_id 拡張 + read_tier_cost_summary テスト。"""

    def test_session_id_saved(self, tmp_path: Path):
        """H1: session_id 付き呼び出しで DB に session_id が保存される。"""
        import sqlite3 as _sqlite3  # noqa: PLC0415
        db = _make_c3_db_v003(tmp_path)

        ok = record_tier_recent_outcome(
            complexity="medium",
            tier="sonnet",
            success=True,
            session_id="sess-h1",
            db_path=db,
        )
        assert ok is True

        conn = _sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT session_id FROM tier_recent_outcomes WHERE session_id = ?",
            ("sess-h1",),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "sess-h1"

    def test_session_id_null_backward_compat(self, tmp_path: Path):
        """H2: session_id 省略（デフォルト None）で NULL 保存・既存呼び出しが壊れない。"""
        import sqlite3 as _sqlite3  # noqa: PLC0415
        db = _make_c3_db_v003(tmp_path)

        ok = record_tier_recent_outcome(
            complexity="simple",
            tier="haiku",
            success=True,
            db_path=db,
        )
        assert ok is True

        conn = _sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT session_id FROM tier_recent_outcomes "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] is None, "session_id を省略したら NULL で保存されるべき"

    def test_join_basic(self, tmp_path: Path):
        """H3: JOIN 基本 — 同一 session_id で outcome + cost を seed → 期待コストが返る。"""
        db = _make_c3_db_v003(tmp_path)
        sess = "sess-h3"

        record_tier_recent_outcome(
            complexity="medium", tier="sonnet", success=True,
            session_id=sess, db_path=db,
        )
        _seed_cost_run(db, session_id=sess, agent_id="agent-x",
                       agent_type="developer", total_cost_usd=0.01)

        result = read_tier_cost_summary(db_path=db)
        assert len(result) == 1
        row = result[0]
        assert row["complexity"] == "medium"
        assert row["tier"] == "sonnet"
        assert row["sessions"] == 1
        assert abs(row["total_cost_usd"] - 0.01) < 1e-9
        assert abs(row["avg_cost_usd"] - 0.01) < 1e-9

    def test_mainline_excluded(self, tmp_path: Path):
        """H4: mainline の cost 行は集計に含まれない。"""
        db = _make_c3_db_v003(tmp_path)
        sess = "sess-h4"

        record_tier_recent_outcome(
            complexity="simple", tier="haiku", success=True,
            session_id=sess, db_path=db,
        )
        # mainline のみ seed（集計除外対象）
        _seed_cost_run(db, session_id=sess, agent_id="mainline",
                       agent_type="mainline", total_cost_usd=0.05)

        result = read_tier_cost_summary(db_path=db)
        assert result == [], "mainline のみの session は JOIN 後に残らないはず"

    def test_no_duplicate_cost_multiple_agent_rows(self, tmp_path: Path):
        """H5: 同一 session_id で複数 agent_id×model 行 → session cost が 1 回だけ計上される。"""
        db = _make_c3_db_v003(tmp_path)
        sess = "sess-h5"

        record_tier_recent_outcome(
            complexity="complex", tier="opus", success=True,
            session_id=sess, db_path=db,
        )
        # 同一 session で 3 agent_id×model（合計 0.03 USD）
        _seed_cost_run(db, session_id=sess, agent_id="agent-a",
                       agent_type="developer", total_cost_usd=0.01)
        _seed_cost_run(db, session_id=sess, agent_id="agent-b",
                       agent_type="developer", total_cost_usd=0.01,
                       model="claude-haiku-4-5-20260101")
        _seed_cost_run(db, session_id=sess, agent_id="agent-c",
                       agent_type="tester", total_cost_usd=0.01)

        result = read_tier_cost_summary(db_path=db)
        assert len(result) == 1
        row = result[0]
        # session_cost CTE で先に SUM → 0.03 USD が 1 回だけ計上される
        assert abs(row["total_cost_usd"] - 0.03) < 1e-9
        assert row["sessions"] == 1

    def test_no_duplicate_sessions_multiple_outcome_rows(self, tmp_path: Path):
        """H5b: 同一 (session,complexity,tier) の outcome 複数行 → sessions が二重カウントされない。"""
        db = _make_c3_db_v003(tmp_path)
        sess = "sess-h5b"

        # 同じ session×complexity×tier の outcome を 2 行 INSERT
        record_tier_recent_outcome(
            complexity="medium", tier="sonnet", success=True,
            session_id=sess, db_path=db,
        )
        record_tier_recent_outcome(
            complexity="medium", tier="sonnet", success=False,
            session_id=sess, db_path=db,
        )
        _seed_cost_run(db, session_id=sess, agent_id="agent-x",
                       agent_type="developer", total_cost_usd=0.02)

        result = read_tier_cost_summary(db_path=db)
        assert len(result) == 1
        row = result[0]
        # outcome_sessions CTE の DISTINCT で 1 行に潰されるため sessions=1
        assert row["sessions"] == 1
        assert abs(row["total_cost_usd"] - 0.02) < 1e-9

    def test_null_session_id_excluded(self, tmp_path: Path):
        """H6: session_id=NULL の outcome は集計対象外。"""
        db = _make_c3_db_v003(tmp_path)

        # session_id=None（NULL）で outcome を記録
        record_tier_recent_outcome(
            complexity="simple", tier="haiku", success=True,
            db_path=db,  # session_id 省略 → NULL
        )
        # cost も seed するが、NULL session は JOIN 不能
        _seed_cost_run(db, session_id="some-other-sess", agent_id="agent-x",
                       agent_type="developer", total_cost_usd=0.01)

        result = read_tier_cost_summary(db_path=db)
        assert result == [], "session_id=NULL の outcome は集計対象外"

    def test_empty_tables_returns_empty_list(self, tmp_path: Path):
        """H7: テーブル空（seed なし）で [] を返す。"""
        db = _make_c3_db_v003(tmp_path)
        result = read_tier_cost_summary(db_path=db)
        assert result == []

    def test_db_absent_returns_empty_list(self, tmp_path: Path):
        """H8: DB 不在で [] を返す（静かに失敗）。"""
        absent_db = tmp_path / "no_such.db"
        result = read_tier_cost_summary(db_path=absent_db)
        assert result == []


# ---------------------------------------------------------------------------
# I 群: read_tier_cost_for_complexity (v2.23.0 T2)
# ---------------------------------------------------------------------------


class TestReadTierCostForComplexity:
    """I 群: read_tier_cost_for_complexity のテスト。

    read_tier_cost_summary の薄いラッパーであるため、DB セットアップは
    H 群の _make_c3_db_v003 / _seed_cost_run / record_tier_recent_outcome を流用する。
    """

    def _seed_session(
        self,
        db: Path,
        *,
        complexity: str,
        tier: str,
        cost_usd: float,
        session_id: str,
    ) -> None:
        """outcome + cost_run を 1 セッション分まとめて seed するヘルパー。"""
        record_tier_recent_outcome(
            complexity=complexity,
            tier=tier,
            success=True,
            session_id=session_id,
            db_path=db,
        )
        _seed_cost_run(
            db,
            session_id=session_id,
            agent_id="agent-i",
            agent_type="developer",
            total_cost_usd=cost_usd,
        )

    def test_complexity_filter_returns_matching_rows_only(self, tmp_path: Path):
        """I1: complexity が一致する行のみ {tier: avg_cost} で返す。

        medium を指定したら medium 行の {tier: avg_cost} のみを返し、
        他の complexity (simple/complex) は含まない。
        """
        db = _make_c3_db_v003(tmp_path)

        self._seed_session(db, complexity="medium", tier="sonnet",
                           cost_usd=0.02, session_id="i1-medium-sonnet")
        self._seed_session(db, complexity="simple", tier="haiku",
                           cost_usd=0.01, session_id="i1-simple-haiku")
        self._seed_session(db, complexity="complex", tier="opus",
                           cost_usd=0.05, session_id="i1-complex-opus")

        result = read_tier_cost_for_complexity("medium", db_path=db)

        assert isinstance(result, dict)
        assert set(result.keys()) == {"sonnet"}
        assert abs(result["sonnet"] - 0.02) < 1e-9
        # simple / complex は含まない
        assert "haiku" not in result
        assert "opus" not in result

    def test_avg_cost_zero_or_negative_excluded(self, tmp_path: Path):
        """I2: avg_cost_usd <= 0 の行は除外される。

        0 コストのセッションを seed しても結果に含まれないことを確認する。
        ただし read_tier_cost_summary は avg_cost_usd が 0.0 の行を返しうるため、
        本関数がそれを除外することを直接テストする。
        本テストでは read_tier_cost_summary をモックして avg_cost_usd=0 の
        データを注入することで、フィルタ動作を独立して検証する。
        """
        from unittest.mock import patch  # noqa: PLC0415

        fake_rows = [
            {"complexity": "medium", "tier": "haiku",
             "sessions": 1, "total_cost_usd": 0.0, "avg_cost_usd": 0.0},
            {"complexity": "medium", "tier": "sonnet",
             "sessions": 1, "total_cost_usd": 0.01, "avg_cost_usd": 0.01},
        ]
        with patch("c3.db.read_tier_cost_summary", return_value=fake_rows):
            result = read_tier_cost_for_complexity("medium")

        # avg_cost_usd=0 の haiku は除外、sonnet のみ返る
        assert "haiku" not in result
        assert "sonnet" in result
        assert abs(result["sonnet"] - 0.01) < 1e-9

    def test_no_matching_complexity_returns_empty_dict(self, tmp_path: Path):
        """I3: 該当 complexity のデータが無い場合は {} を返す。"""
        db = _make_c3_db_v003(tmp_path)

        # simple のみ seed
        self._seed_session(db, complexity="simple", tier="haiku",
                           cost_usd=0.01, session_id="i3-simple")

        result = read_tier_cost_for_complexity("complex", db_path=db)
        assert result == {}

    def test_db_absent_returns_empty_dict(self, tmp_path: Path):
        """I4: DB 不在（存在しないパスを db_path に渡す）で {} を返す。"""
        absent_db = tmp_path / "no_such_i4.db"
        result = read_tier_cost_for_complexity("medium", db_path=absent_db)
        assert result == {}

    def test_multiple_tiers_same_complexity(self, tmp_path: Path):
        """I5: 同一 complexity で複数 tier が存在する場合、全て返る。"""
        db = _make_c3_db_v003(tmp_path)

        self._seed_session(db, complexity="medium", tier="haiku",
                           cost_usd=0.01, session_id="i5-haiku")
        self._seed_session(db, complexity="medium", tier="sonnet",
                           cost_usd=0.02, session_id="i5-sonnet")

        result = read_tier_cost_for_complexity("medium", db_path=db)

        assert set(result.keys()) == {"haiku", "sonnet"}
        assert abs(result["haiku"] - 0.01) < 1e-9
        assert abs(result["sonnet"] - 0.02) < 1e-9

    def test_existing_read_tier_cost_summary_tests_still_pass(self, tmp_path: Path):
        """I6: read_tier_cost_summary 既存テストの代表例が本関数追加後も green（不変確認）。

        H3 と同等のシナリオを再実行し、read_tier_cost_summary が
        read_tier_cost_for_complexity の実装で一切変更されていないことを確認する。
        """
        db = _make_c3_db_v003(tmp_path)
        sess = "i6-backward-compat"

        record_tier_recent_outcome(
            complexity="medium", tier="sonnet", success=True,
            session_id=sess, db_path=db,
        )
        _seed_cost_run(db, session_id=sess, agent_id="agent-x",
                       agent_type="developer", total_cost_usd=0.01)

        # read_tier_cost_summary は変更なしに動作する
        summary = read_tier_cost_summary(db_path=db)
        assert len(summary) == 1
        assert summary[0]["complexity"] == "medium"
        assert abs(summary[0]["avg_cost_usd"] - 0.01) < 1e-9

        # read_tier_cost_for_complexity も同じ結果を反映する
        for_complexity = read_tier_cost_for_complexity("medium", db_path=db)
        assert abs(for_complexity["sonnet"] - 0.01) < 1e-9


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

        record_tier_recent_outcome(
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

        record_tier_recent_outcome(
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

        record_tier_recent_outcome(
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

        record_tier_recent_outcome(
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

        record_tier_recent_outcome(
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

        record_tier_recent_outcome(
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
        record_tier_recent_outcome(
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

        record_tier_recent_outcome(
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

    read_tier_params を代表例として使用。corrupt なバイナリを DB として渡すことで
    sqlite3.DatabaseError（not an SQLite3 database）を発生させ、
    caplog でログ本文に型名が含まれ生 message が含まれないことを確認する。
    """

    def test_read_tier_params_logs_exception_type_not_message(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """SR-R-001: read_tier_params は例外発生時に type(exc).__name__ をログに出す。

        corrupt DB を渡すと sqlite3.DatabaseError が上がる。
        - ログ本文に "DatabaseError" が含まれる
        - ログ本文に生 exc message（"not an SQLite3 database" 等）が含まれない
        - 戻り値は defaults（型名のみのログで例外を握る）
        """
        from c3.db import read_tier_params  # noqa: PLC0415

        # corrupt なバイナリファイルを作成して DB として渡す
        corrupt_db = tmp_path / "corrupt.db"
        corrupt_db.write_bytes(b"this is not a valid sqlite3 database file")

        with caplog.at_level(logging.WARNING, logger="c3.db"):
            result = read_tier_params("medium", db_path=corrupt_db)

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


# ---------------------------------------------------------------------------
# L 群: sync_tier_bandit_cost / read_tier_bandit_cost (v2.25.0 T3)
# ---------------------------------------------------------------------------


def _seed_bandit_row(
    db: Path,
    *,
    complexity: str,
    tier: str,
    alpha: float = 2.0,
    beta: float = 1.0,
    trials: int = 3,
) -> None:
    """tier_bandit に 1 行 seed するヘルパー（L 群共通）。"""
    import sqlite3 as _sqlite3  # noqa: PLC0415
    from datetime import datetime, timezone  # noqa: PLC0415
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = _sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO tier_bandit "
            "(task_complexity, tier, alpha, beta, trials, last_updated) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (complexity, tier, alpha, beta, trials, ts),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_cost_session(
    db: Path,
    *,
    complexity: str,
    tier_outcome: str,
    model: str,
    session_id: str,
    total_cost_usd: float,
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> None:
    """outcome + cost_run を 1 セッション分 seed（L 群共通）。"""
    record_tier_recent_outcome(
        complexity=complexity,
        tier=tier_outcome,
        success=True,
        session_id=session_id,
        db_path=db,
    )
    insert_agent_cost_run(
        session_id=session_id,
        agent_id=f"agent-{session_id}",
        agent_type="developer",
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


class TestSyncTierBanditCost:
    """L 群: sync_tier_bandit_cost (A1: 冪等 SET 同期) のテスト。"""

    def test_idempotent_double_sync(self, tmp_path: Path):
        """L1: 冪等性（最重要）— 同一 DB 状態で連続 2 回 sync しても cost 列が完全同一。"""
        import sqlite3 as _sqlite3  # noqa: PLC0415
        db = _make_c3_db_v003(tmp_path)

        # tier_bandit に sonnet/medium 行を seed
        _seed_bandit_row(db, complexity="medium", tier="sonnet")

        # cost を生むセッションを seed
        _seed_cost_session(
            db,
            complexity="medium",
            tier_outcome="sonnet",
            model="claude-sonnet-4-6-20260101",
            session_id="l1-sess",
            total_cost_usd=0.02,
        )

        # 1 回目 sync
        count1 = sync_tier_bandit_cost(db_path=db)
        conn = _sqlite3.connect(str(db))
        rows1 = conn.execute(
            "SELECT task_complexity, tier, total_cost_usd, cost_samples "
            "FROM tier_bandit"
        ).fetchall()
        conn.close()

        # 2 回目 sync
        count2 = sync_tier_bandit_cost(db_path=db)
        conn = _sqlite3.connect(str(db))
        rows2 = conn.execute(
            "SELECT task_complexity, tier, total_cost_usd, cost_samples "
            "FROM tier_bandit"
        ).fetchall()
        conn.close()

        assert count1 == count2, "SET 行数が 1 回目と 2 回目で同一であるべき"
        assert rows1 == rows2, "cost 列が 2 回の sync で完全同一であるべき（冪等）"

    def test_values_match_rate_summary(self, tmp_path: Path):
        """L2: 値一致 — sync 後の tier_bandit.total_cost_usd/cost_samples が
        rate_summary の total_cost_usd/sessions と (complexity,tier) ごとに一致する。"""
        db = _make_c3_db_v003(tmp_path)

        # haiku/simple と sonnet/medium の 2 行を tier_bandit に seed
        _seed_bandit_row(db, complexity="simple", tier="haiku")
        _seed_bandit_row(db, complexity="medium", tier="sonnet")

        # haiku/simple: 2 セッション
        _seed_cost_session(
            db,
            complexity="simple",
            tier_outcome="haiku",
            model="claude-haiku-4-5-20260101",
            session_id="l2-haiku-1",
            total_cost_usd=0.005,
        )
        _seed_cost_session(
            db,
            complexity="simple",
            tier_outcome="haiku",
            model="claude-haiku-4-5-20260101",
            session_id="l2-haiku-2",
            total_cost_usd=0.003,
        )
        # sonnet/medium: 1 セッション
        _seed_cost_session(
            db,
            complexity="medium",
            tier_outcome="sonnet",
            model="claude-sonnet-4-6-20260101",
            session_id="l2-sonnet-1",
            total_cost_usd=0.015,
        )

        sync_tier_bandit_cost(db_path=db)

        summary = read_tier_cost_rate_summary(db_path=db)
        bandit_cost = read_tier_bandit_cost(db_path=db)

        for row in summary:
            key = (row["complexity"], row["tier"])
            assert key in bandit_cost, f"{key} が bandit_cost に存在しない"
            cost_usd, cost_samples = bandit_cost[key]
            assert abs(cost_usd - row["total_cost_usd"]) < 1e-9, (
                f"{key}: total_cost_usd 不一致 {cost_usd} vs {row['total_cost_usd']}"
            )
            assert cost_samples == row["sessions"], (
                f"{key}: cost_samples {cost_samples} vs sessions {row['sessions']}"
            )

    def test_alpha_beta_trials_unchanged(self, tmp_path: Path):
        """L3: alpha/beta/trials/last_updated が sync 前後で変わらない。"""
        import sqlite3 as _sqlite3  # noqa: PLC0415
        db = _make_c3_db_v003(tmp_path)

        _seed_bandit_row(db, complexity="medium", tier="sonnet",
                         alpha=3.5, beta=1.2, trials=7)
        _seed_cost_session(
            db,
            complexity="medium",
            tier_outcome="sonnet",
            model="claude-sonnet-4-6-20260101",
            session_id="l3-sess",
            total_cost_usd=0.01,
        )

        # sync 前の alpha/beta/trials を記録
        conn = _sqlite3.connect(str(db))
        before = conn.execute(
            "SELECT alpha, beta, trials, last_updated FROM tier_bandit "
            "WHERE task_complexity = 'medium' AND tier = 'sonnet'"
        ).fetchone()
        conn.close()

        sync_tier_bandit_cost(db_path=db)

        conn = _sqlite3.connect(str(db))
        after = conn.execute(
            "SELECT alpha, beta, trials, last_updated FROM tier_bandit "
            "WHERE task_complexity = 'medium' AND tier = 'sonnet'"
        ).fetchone()
        conn.close()

        assert before == after, (
            f"alpha/beta/trials/last_updated が sync で変わってはいけない: "
            f"before={before}, after={after}"
        )

    def test_reset_rows_not_in_summary(self, tmp_path: Path):
        """L4: 集計に現れない (complexity,tier) 行は cost 列が 0.0/0 にリセットされる。

        tier_bandit に opus/complex を seed し cost を手動で書いておく。
        集計には出ない（outcome が無い）ため sync 後に 0.0/0 になることを確認。
        """
        import sqlite3 as _sqlite3  # noqa: PLC0415
        db = _make_c3_db_v003(tmp_path)

        # opus/complex を tier_bandit に seed（cost を手動で書く）
        conn = _sqlite3.connect(str(db))
        try:
            from datetime import datetime as _dt, timezone as _tz  # noqa: PLC0415
            ts = _dt.now(_tz.utc).isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO tier_bandit "
                "(task_complexity, tier, alpha, beta, trials, "
                " total_cost_usd, cost_samples, last_updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("complex", "opus", 2.0, 1.0, 3, 9.99, 5, ts),
            )
            conn.commit()
        finally:
            conn.close()

        # 集計（rate_summary）は空 → sync で 0 リセットされるべき
        # （outcome・cost_run を seed しないので集計に出ない）
        sync_tier_bandit_cost(db_path=db)

        conn = _sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT total_cost_usd, cost_samples FROM tier_bandit "
            "WHERE task_complexity = 'complex' AND tier = 'opus'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert abs(row[0] - 0.0) < 1e-12, f"total_cost_usd は 0.0 にリセットされるべき: {row[0]}"
        assert row[1] == 0, f"cost_samples は 0 にリセットされるべき: {row[1]}"

    def test_update_only_no_insert(self, tmp_path: Path):
        """L5: UPDATE-only — 集計に出るが tier_bandit に行がない場合は INSERT されない。

        tier_bandit に sonnet/medium 行は作らず、outcome + cost_run だけ seed する。
        sync しても tier_bandit の行数が増えないことを確認。
        """
        import sqlite3 as _sqlite3  # noqa: PLC0415
        db = _make_c3_db_v003(tmp_path)

        # tier_bandit 行を作らずに集計データだけ seed
        _seed_cost_session(
            db,
            complexity="medium",
            tier_outcome="sonnet",
            model="claude-sonnet-4-6-20260101",
            session_id="l5-sess",
            total_cost_usd=0.01,
        )

        # sync 前の行数（tier_bandit は空のはず）
        conn = _sqlite3.connect(str(db))
        count_before = conn.execute("SELECT COUNT(*) FROM tier_bandit").fetchone()[0]
        conn.close()

        result = sync_tier_bandit_cost(db_path=db)

        conn = _sqlite3.connect(str(db))
        count_after = conn.execute("SELECT COUNT(*) FROM tier_bandit").fetchone()[0]
        conn.close()

        assert count_before == count_after == 0, (
            f"tier_bandit に行が無ければ INSERT されてはいけない: "
            f"before={count_before}, after={count_after}"
        )
        # rowcount=0 なので SET 行数は 0
        assert result == 0, f"INSERT されていないので戻り値は 0 であるべき: {result}"

    def test_db_absent_returns_zero_no_exception(self, tmp_path: Path):
        """L6: DB 不在パスを渡しても 0 を返し例外を投げない。"""
        absent_db = tmp_path / "no_such_l6.db"
        result = sync_tier_bandit_cost(db_path=absent_db)
        assert result == 0, f"DB 不在で 0 を返すべき: {result}"

    def test_return_value_is_set_count(self, tmp_path: Path):
        """L7: 戻り値が SET できた行数（rowcount > 0 の UPDATE 件数）である。"""
        db = _make_c3_db_v003(tmp_path)

        # 2 行を tier_bandit に seed
        _seed_bandit_row(db, complexity="simple", tier="haiku")
        _seed_bandit_row(db, complexity="medium", tier="sonnet")

        # 2 セッション seed（それぞれ別 complexity/tier）
        _seed_cost_session(
            db, complexity="simple", tier_outcome="haiku",
            model="claude-haiku-4-5-20260101",
            session_id="l7-haiku", total_cost_usd=0.005,
        )
        _seed_cost_session(
            db, complexity="medium", tier_outcome="sonnet",
            model="claude-sonnet-4-6-20260101",
            session_id="l7-sonnet", total_cost_usd=0.01,
        )

        result = sync_tier_bandit_cost(db_path=db)
        # 2 行が SET されたので 2 を返す
        assert result == 2, f"SET 行数は 2 であるべき: {result}"


class TestReadTierBanditCost:
    """L 群追加: read_tier_bandit_cost の単体テスト。"""

    def test_returns_cost_per_key(self, tmp_path: Path):
        """LR1: tier_bandit の cost 列を (complexity, tier) キーで返す。"""
        import sqlite3 as _sqlite3  # noqa: PLC0415
        db = _make_c3_db_v003(tmp_path)

        # cost 列も込みで直接 INSERT
        conn = _sqlite3.connect(str(db))
        try:
            from datetime import datetime as _dt, timezone as _tz  # noqa: PLC0415
            ts = _dt.now(_tz.utc).isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO tier_bandit "
                "(task_complexity, tier, alpha, beta, trials, "
                " total_cost_usd, cost_samples, last_updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("medium", "sonnet", 2.0, 1.0, 3, 0.05, 2, ts),
            )
            conn.commit()
        finally:
            conn.close()

        result = read_tier_bandit_cost(db_path=db)

        assert ("medium", "sonnet") in result
        cost_usd, cost_samples = result[("medium", "sonnet")]
        assert abs(cost_usd - 0.05) < 1e-9
        assert cost_samples == 2

    def test_db_absent_returns_empty_dict(self, tmp_path: Path):
        """LR2: DB 不在で {} を返す（例外なし）。"""
        absent_db = tmp_path / "no_such_lr2.db"
        result = read_tier_bandit_cost(db_path=absent_db)
        assert result == {}

    def test_empty_table_returns_empty_dict(self, tmp_path: Path):
        """LR3: tier_bandit が空なら {} を返す。"""
        db = _make_c3_db_v003(tmp_path)
        result = read_tier_bandit_cost(db_path=db)
        assert result == {}


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
