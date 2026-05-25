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
    get_ingest_offset,
    insert_agent_cost_run,
    locate_c3_db,
    read_agent_cost_summary,
    read_tier_cost_summary,
    read_tier_cost_for_complexity,
    record_tier_recent_outcome,
    set_ingest_offset,
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
