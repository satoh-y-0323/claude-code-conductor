"""Tests for src/c3/cli_metrics.py (`c3 metrics` P4 効果の総括メトリクス CLI)。

test-cli タスク（plan-report-20260706-221212.md T4）の Red テスト。
tests/test_cli_tier.py を雛形とし、architecture-report-20260706-213701.md
§2-4（cli_metrics.py 出力仕様）/ §3（テスト戦略）に準拠する。

観点（plan T4 / architecture §2-4・§3 より）:
- `--json` トップレベルスキーマ（generated_at/since/months/examples/
  prevented_detection/rework/rework_cost）と section 別 data_available。
- headline（fixed_medium_plus・critical/high/medium 内訳・fixed_unknown）は
  `read_review_decision_matrix` から CLI 層で導出される単一算出源であり、
  `--examples` 上限を超える fixed 行があっても過小計上されないこと。
- role_distribution の review/development/other 3 分類振り分け。
- 人間向け出力: ヘッドライン文字列・「収集中（forward-only）」表示は
  [1] 事前検出実績セクション限定（[2]/[3] は独立表示）・※ 注記行。
- note 検証: `rework_cost.note` / `fix_cycles.note` の文字列値と ※ 行のみを
  対象に、要旨キーワード部分一致＋監査 finding ID パターン/DB 内部名の
  非混入 negative assertion（`--json` 全体・`examples[].checklist_id`・
  JSON キー名は対象外・DC-AM-001 round 5）。
- DB 不在 → stderr + exit 1 / 不正 `--since` 書式 → DB アクセス前に
  stderr + exit 1 / `--months`・`--examples` の非負整数検証（0・負値の境界）。
- `--since` / `--months` / `--examples` の反映。
- `rework.trend` の JSON と人間向けが同一のゼロ埋め済み暦月集合を持つこと。

c3.cli_metrics は本タスク開始時点では未実装だったため、本ファイルの import は
ModuleNotFoundError で失敗していた（Red フェーズの単一起因）。
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from c3 import cli_metrics
from c3 import db as c3_db


WORKTREE_ROOT = Path(__file__).parents[1]


# ---------------------------------------------------------------------------
# seed ヘルパー（tests/test_db.py の Q 群パターンを踏襲）
# ---------------------------------------------------------------------------


def _create_c3_db(db_path: Path) -> None:
    from c3.migrate import apply_pending_migrations  # noqa: PLC0415
    apply_pending_migrations(db_path)


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
    """review_decisions に任意の decided_at（ISO8601 文字列）で 1 行 INSERT した。"""
    c3_db.insert_review_decision(
        checklist_id=checklist_id,
        finding_text=finding_text,
        decision=decision,
        reviewer=reviewer,
        severity=severity,
        decided_at=datetime.fromisoformat(decided_at_iso),
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
    """agent_outcomes に任意の ts（ISO8601 文字列）で直接 INSERT した。"""
    conn = sqlite3.connect(str(db))
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
    """agent_cost_runs に任意の recorded_at（ISO8601 文字列）で直接 INSERT した。"""
    conn = sqlite3.connect(str(db))
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


def _make_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        as_json=False,
        since=None,
        months=12,
        examples=5,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _run(
    args: argparse.Namespace, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> int:
    monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: db_path)
    return cli_metrics.handle_metrics(args)


def _seed_review_decision_raw(
    db: Path,
    *,
    checklist_id: str,
    decision: str,
    severity: str | None,
    decided_at: str,
    reviewer: str = "code-reviewer",
    finding_text: str = "finding text",
) -> None:
    """review_decisions に ``insert_review_decision`` の検証・整形を経由せず
    任意の生文字列（制御文字を含む checklist_id/decided_at や、
    ``fetch_prevented_findings``/``read_review_decision_matrix`` の
    フィルタ判定に使わない decision/severity）で直接 INSERT した。

    ``insert_review_decision`` は ``decided_at`` に ``datetime`` しか受け付けない
    ため、制御文字を含む生文字列をシードするには本ヘルパーで直接 INSERT する
    必要がある（item6 の制御文字混入シード専用）。
    """
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO review_decisions "
            "(checklist_id, finding_text, decision, reason, "
            " context_summary, decided_at, reviewer, severity) "
            "VALUES (?, ?, ?, NULL, NULL, ?, ?, ?)",
            (checklist_id, finding_text, decision, decided_at, reviewer, severity),
        )
        conn.commit()
    finally:
        conn.close()


def _never_call_locate_c3_db(monkeypatch: pytest.MonkeyPatch) -> dict:
    """locate_c3_db が呼ばれたら記録する spy を仕込み、呼び出し検出用 dict を返す。

    入力検証（--since 書式・--months/--examples 非負整数）が DB アクセス前に
    行われることを固定するため、DB 探索そのものが発生しないことを確認する。
    """
    called = {"value": False}

    def _spy(start=None):
        called["value"] = True
        return None

    monkeypatch.setattr(c3_db, "locate_c3_db", _spy)
    return called


# ---------------------------------------------------------------------------
# DB 不在
# ---------------------------------------------------------------------------


class TestDbMissing:

    def test_db_missing_returns_exit1_with_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """DB 不在時は exit 1 + stderr エラーメッセージを返す。"""
        nonexistent = tmp_path / "nonexistent.db"
        monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: nonexistent)

        rc = cli_metrics.handle_metrics(_make_args())

        assert rc == 1
        err = capsys.readouterr().err
        assert "DB が見つかりません" in err

    def test_db_missing_json_mode_also_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        nonexistent = tmp_path / "nonexistent.db"
        monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: nonexistent)

        rc = cli_metrics.handle_metrics(_make_args(as_json=True))

        assert rc == 1


# ---------------------------------------------------------------------------
# 入力検証（DB アクセス前に exit 1）
# ---------------------------------------------------------------------------


class TestInputValidationBeforeDbAccess:

    def test_invalid_since_format_returns_exit1_before_db_access(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    ) -> None:
        """不正な --since 書式は DB アクセス前に stderr + exit 1 で弾かれる。"""
        called = _never_call_locate_c3_db(monkeypatch)

        rc = cli_metrics.handle_metrics(_make_args(since="not-a-date"))

        assert rc == 1
        assert capsys.readouterr().err
        assert called["value"] is False, "--since 検証前に DB へアクセスしている"

    def test_invalid_since_wrong_separator_returns_exit1(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    ) -> None:
        called = _never_call_locate_c3_db(monkeypatch)

        rc = cli_metrics.handle_metrics(_make_args(since="2026/07/07"))

        assert rc == 1
        assert called["value"] is False

    @pytest.mark.parametrize("months", [0, -1])
    def test_months_non_positive_returns_exit1_before_db_access(
        self, months: int, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """--months 0 / 負値は DB アクセス前に stderr + exit 1（--since と同じ扱い）。"""
        called = _never_call_locate_c3_db(monkeypatch)

        rc = cli_metrics.handle_metrics(_make_args(months=months))

        assert rc == 1
        assert capsys.readouterr().err
        assert called["value"] is False

    @pytest.mark.parametrize("examples", [0, -1])
    def test_examples_non_positive_returns_exit1_before_db_access(
        self, examples: int, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """--examples 0 / 負値は DB アクセス前に stderr + exit 1。"""
        called = _never_call_locate_c3_db(monkeypatch)

        rc = cli_metrics.handle_metrics(_make_args(examples=examples))

        assert rc == 1
        assert capsys.readouterr().err
        assert called["value"] is False

    def test_default_months_and_examples_are_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """既定値（months=12 / examples=5）は正常に受理される（回帰防止）。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0

    def test_valid_since_format_is_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(since="2026-01-01"), db, monkeypatch)

        assert rc == 0


# ---------------------------------------------------------------------------
# --json トップレベルスキーマ（section 別 data_available）
# ---------------------------------------------------------------------------


class TestJsonTopLevelSchema:

    def test_top_level_keys_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(as_json=True), db, monkeypatch)

        assert rc == 0
        data = _load_json(capsys)
        for key in (
            "generated_at", "since", "months", "examples",
            "prevented_detection", "rework", "rework_cost",
        ):
            assert key in data, f"トップレベルキー {key!r} が --json 出力に無い"

    def test_data_available_is_separated_per_section_not_top_level(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """data_available はトップレベル単一フラグでなく各 section dict 内に
        分離されている（DC-GP-003）。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(as_json=True), db, monkeypatch)

        assert rc == 0
        data = _load_json(capsys)
        assert "data_available" not in data
        assert "data_available" in data["prevented_detection"]
        assert "data_available" in data["rework"]
        assert "data_available" in data["rework_cost"]

    def test_empty_db_all_sections_data_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(as_json=True), db, monkeypatch)

        assert rc == 0
        data = _load_json(capsys)
        assert data["prevented_detection"]["data_available"] is False
        assert data["rework"]["data_available"] is False
        assert data["rework_cost"]["data_available"] is False


def _load_json(capsys: pytest.CaptureFixture) -> dict:
    import json as _json
    return _json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------------------
# headline（matrix 単一算出源・examples 上限非張り付き境界）
# ---------------------------------------------------------------------------


class TestHeadlineMatrixDerivation:

    def test_headline_counts_match_matrix_fixed_severity_buckets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """headline の fixed_medium_plus・critical/high/medium 内訳・
        fixed_unknown は matrix の fixed×severity バケットと一致する
        （single source of truth・DC-AM-001）。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_review_decision(
            db, checklist_id="CR-H-001", decision="fixed", reviewer="security-reviewer",
            severity="critical", decided_at_iso="2026-06-01T00:00:00+00:00",
        )
        _seed_review_decision(
            db, checklist_id="CR-H-002", decision="fixed", reviewer="code-reviewer",
            severity="high", decided_at_iso="2026-06-02T00:00:00+00:00",
        )
        _seed_review_decision(
            db, checklist_id="CR-H-003", decision="fixed", reviewer="code-reviewer",
            severity="medium", decided_at_iso="2026-06-03T00:00:00+00:00",
        )
        _seed_review_decision(
            db, checklist_id="CR-H-004", decision="fixed", reviewer="code-reviewer",
            severity=None, decided_at_iso="2026-06-04T00:00:00+00:00",
        )
        _seed_review_decision(
            db, checklist_id="SR-H-001", decision="accepted", reviewer="security-reviewer",
            severity="low", decided_at_iso="2026-06-05T00:00:00+00:00",
        )

        rc = _run(_make_args(as_json=True), db, monkeypatch)

        assert rc == 0
        data = _load_json(capsys)
        headline = data["prevented_detection"]["headline"]
        assert headline["critical"] == 1
        assert headline["high"] == 1
        assert headline["medium"] == 1
        assert headline["fixed_medium_plus"] == 3
        assert headline["fixed_unknown"] == 1

        matrix = data["prevented_detection"]["matrix"]
        fixed_by_severity: dict[str, int] = {}
        for row in matrix:
            if row["decision"] == "fixed":
                fixed_by_severity[row["severity"]] = (
                    fixed_by_severity.get(row["severity"], 0) + row["count"]
                )
        assert fixed_by_severity.get("critical", 0) == headline["critical"]
        assert fixed_by_severity.get("high", 0) == headline["high"]
        assert fixed_by_severity.get("medium", 0) == headline["medium"]
        assert fixed_by_severity.get("unknown", 0) == headline["fixed_unknown"]

    def test_fixed_medium_plus_not_undercounted_when_exceeding_examples_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """fixed かつ Medium 以上の行を --examples 既定上限（5）より多く（7 件）
        シードしても、fixed_medium_plus は 7 を返し examples 件数（5）に
        張り付いて過小計上されない（DC-AM-001 round 3 の境界）。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        for i in range(7):
            _seed_review_decision(
                db, checklist_id=f"CR-B-{i:03d}", decision="fixed",
                reviewer="code-reviewer", severity="high",
                decided_at_iso=f"2026-06-{i + 1:02d}T00:00:00+00:00",
            )

        rc = _run(_make_args(as_json=True), db, monkeypatch)

        assert rc == 0
        data = _load_json(capsys)
        headline = data["prevented_detection"]["headline"]
        assert headline["fixed_medium_plus"] == 7, (
            "fixed_medium_plus が --examples 上限に張り付いて過小計上されている"
        )
        assert len(data["prevented_detection"]["examples"]) <= 5

    def test_headline_string_shown_in_human_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """人間向け出力にヘッドライン件数（N・critical/high/medium 内訳・
        unknown 併記）が表示される。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_review_decision(
            db, checklist_id="CR-HS-001", decision="fixed", reviewer="security-reviewer",
            severity="critical", decided_at_iso="2026-06-01T00:00:00+00:00",
        )
        _seed_review_decision(
            db, checklist_id="CR-HS-002", decision="fixed", reviewer="code-reviewer",
            severity=None, decided_at_iso="2026-06-02T00:00:00+00:00",
        )

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "1" in out
        assert "unknown" in out or "未記録" in out


# ---------------------------------------------------------------------------
# role_distribution の 3 分類振り分け
# ---------------------------------------------------------------------------


class TestRoleDistributionOther:

    def test_json_role_distribution_has_three_categories(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_agent_outcome_at(
            db, role="tester", success=False, gate="E-1",
            session_id="sess-role-1", ts_iso="2026-06-01T00:00:00+00:00",
        )
        _seed_agent_outcome_at(
            db, role="developer", success=False, gate="D-3",
            session_id="sess-role-2", ts_iso="2026-06-01T00:00:00+00:00",
        )
        _seed_agent_outcome_at(
            db, role="developer", success=False, gate=None,
            session_id="sess-role-3", ts_iso="2026-06-01T00:00:00+00:00",
        )

        rc = _run(_make_args(as_json=True), db, monkeypatch)

        assert rc == 0
        data = _load_json(capsys)
        role_dist = data["rework"]["role_distribution"]
        assert set(role_dist.keys()) == {"review", "development", "other"}

        review_total = sum(r["count"] for r in role_dist["review"])
        dev_total = sum(r["count"] for r in role_dist["development"])
        other_total = sum(r["count"] for r in role_dist["other"])
        assert review_total == 1
        assert dev_total == 1
        assert other_total == 1

    def test_unclassified_gate_is_not_dropped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """METRICS_REVIEW_GATES / METRICS_DEV_GATES いずれにも属さない gate
        （NULL 含む）が黙って捨てられず other に現れる（DC-AM-003）。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_agent_outcome_at(
            db, role="developer", success=False, gate="D-9-unknown",
            session_id="sess-unclassified-1", ts_iso="2026-06-01T00:00:00+00:00",
        )

        rc = _run(_make_args(as_json=True), db, monkeypatch)

        assert rc == 0
        data = _load_json(capsys)
        other = data["rework"]["role_distribution"]["other"]
        assert any(r["gate"] == "D-9-unknown" for r in other)

    def test_human_output_shows_other_bucket_separately(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_agent_outcome_at(
            db, role="developer", success=False, gate="D-9-unknown",
            session_id="sess-unclassified-2", ts_iso="2026-06-01T00:00:00+00:00",
        )

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "other" in out or "その他" in out


# ---------------------------------------------------------------------------
# section 別「収集中」抑止（DC-GP-003）
# ---------------------------------------------------------------------------


class TestSectionSeparatedCollectingDisplay:

    def test_all_empty_shows_collecting_message_exit0(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "収集中" in out

    def test_prevented_detection_only_empty_json_data_available_separated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """prevented_detection のみ空で rework/rework_cost にデータがある場合、
        `data_available` が section 別に true/false 混在で正しく分離される
        （DC-GP-003）。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_agent_outcome_at(
            db, role="tester", success=False, gate="E-1",
            session_id="sess-sep-1", ts_iso="2026-06-01T00:00:00+00:00",
        )
        _seed_cost_run_at(
            db, session_id="sess-sep-1", agent_id="agent-sep-1",
            total_cost_usd=1.0, recorded_at_iso="2026-06-01T00:00:00+00:00",
        )

        data_json = _run_json(db, monkeypatch)

        assert data_json["prevented_detection"]["data_available"] is False
        assert data_json["rework"]["data_available"] is True
        assert data_json["rework_cost"]["data_available"] is True

    def test_prevented_detection_only_empty_human_output_exit0_and_shows_collecting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """[1] は「収集中」表示・[2] 差し戻し傾向・[3] コスト概況は独立に
        表示され exit 0 となる（DC-GP-003）。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_agent_outcome_at(
            db, role="tester", success=False, gate="E-1",
            session_id="sess-sep-2", ts_iso="2026-06-01T00:00:00+00:00",
        )
        _seed_cost_run_at(
            db, session_id="sess-sep-2", agent_id="agent-sep-2",
            total_cost_usd=1.0, recorded_at_iso="2026-06-01T00:00:00+00:00",
        )

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "収集中" in out
        assert "E-1" in out or "tester" in out


def _run_json(db: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """--json モードで再実行して parsed dict を返す（capsys は呼び出し側で
    別途 readouterr する必要があるため、本ヘルパー内で独立に capsys を
    使わずファイル記述子キャプチャに依存しない形で完結させる）。"""
    import io
    import contextlib
    import json as _json

    monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: db)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli_metrics.handle_metrics(_make_args(as_json=True))
    assert rc == 0
    return _json.loads(buf.getvalue())


# ---------------------------------------------------------------------------
# rework_cost.data_available は行の存在で判定する（code-review-report-
# 20260707-011501.md item1）
# ---------------------------------------------------------------------------


class TestReworkCostDataAvailableReflectsRowExistence:

    def test_zero_cost_rows_still_data_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """[item1] `agent_cost_runs` に `total_cost_usd=0.0` の行のみが存在する
        場合でも `rework_cost.data_available` は True（行が存在するため）で
        あるべき固定テスト。architecture §2-4 の定義は「集計対象行が存在すれば
        true」だが、本タスク開始時点の実装は合計 USD が正かどうかで判定して
        おり、無料枠・キャッシュヒット等で 0 円行のみが存在するケースを誤って
        「収集中」（data_available=False）扱いにしていた（Red）。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        # rework session を agent_outcomes に追加（success=0, gate=E-1）
        _seed_agent_outcome_at(
            db, role="tester", success=False, gate="E-1",
            session_id="sess-zero-1", ts_iso="2026-06-01T00:00:00+00:00",
        )
        # その session_id に対して 0 円の cost_run を追加
        _seed_cost_run_at(
            db, session_id="sess-zero-1", agent_id="agent-zero-1",
            total_cost_usd=0.0, recorded_at_iso="2026-06-01T00:00:00+00:00",
        )

        data_json = _run_json(db, monkeypatch)

        assert data_json["rework_cost"]["overall_total_usd"] == 0.0
        assert data_json["rework_cost"]["data_available"] is True, (
            "0 円行のみでも行は存在するため data_available は True であるべき"
        )


# ---------------------------------------------------------------------------
# note 検証（要旨キーワード一致＋スコープ限定 negative assertion）
# ---------------------------------------------------------------------------


_FORBIDDEN_FINDING_ID_PATTERN = re.compile(r"\b(?:DC|CR|SR)-[A-Z0-9]+-\d+\b")
_FORBIDDEN_ROUND_PATTERN = re.compile(r"\bround\s*\d+\b", re.IGNORECASE)
_FORBIDDEN_DB_INTERNAL_NAMES = (
    "agent_cost_runs",
    "agent_outcomes",
    "recorded_at",
)


def _assert_note_is_clean(note: str) -> None:
    """note 文字列値に監査 finding ID パターン・DB 内部名が含まれないことを
    固定する（DC-AM-001 round 5・スコープは note 文字列値そのもの）。"""
    assert not _FORBIDDEN_FINDING_ID_PATTERN.search(note), (
        f"note に監査 finding ID パターンが混入している: {note!r}"
    )
    assert not _FORBIDDEN_ROUND_PATTERN.search(note), (
        f"note に round N 参照が混入している: {note!r}"
    )
    for name in _FORBIDDEN_DB_INTERNAL_NAMES:
        assert name not in note, f"note に DB 内部名 {name!r} が混入している: {note!r}"


class TestNoteVerification:

    def test_rework_cost_note_conveys_population_mismatch_gist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """rework_cost.note は母集団非一致・session 粒度近似の要旨キーワード
        （例: 「近似」「session」「母集団」「目安」等）の部分一致で検証する
        （特定の内部 ID 文字列は固定しない）。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(as_json=True), db, monkeypatch)

        assert rc == 0
        data = _load_json(capsys)
        note = data["rework_cost"]["note"]
        assert any(kw in note for kw in ("近似", "session", "母集団", "目安"))

    def test_fix_cycles_note_conveys_approximation_gist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(as_json=True), db, monkeypatch)

        assert rc == 0
        data = _load_json(capsys)
        note = data["rework"]["fix_cycles"]["note"]
        assert any(kw in note for kw in ("近似", "session", "セッション"))

    def test_rework_cost_note_string_value_excludes_internal_terms(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(as_json=True, since="2026-01-01"), db, monkeypatch)

        assert rc == 0
        data = _load_json(capsys)
        _assert_note_is_clean(data["rework_cost"]["note"])

    def test_fix_cycles_note_string_value_excludes_internal_terms(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(as_json=True), db, monkeypatch)

        assert rc == 0
        data = _load_json(capsys)
        _assert_note_is_clean(data["rework"]["fix_cycles"]["note"])

    def test_human_output_note_line_excludes_internal_terms(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """人間向け出力の ※ 注記行（note 由来の 1 行）にも内部監査 ID・
        DB 内部名が混入していない。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        note_lines = [line for line in out.splitlines() if line.strip().startswith("※")]
        assert note_lines, "※ 注記行が人間向け出力に見つからない"
        for line in note_lines:
            _assert_note_is_clean(line)

    def test_note_negative_assertion_does_not_apply_to_examples_checklist_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """examples[].checklist_id に DC-XX-NNN 形式（design-critic 記録）が
        正当に出現しても、それは note の negative assertion 対象外である。
        --json 全体には DC- 形式の checklist_id が含まれる一方、note 文字列
        自体には混入していないことを両方固定する（DC-AM-001 round 5）。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_review_decision(
            db, checklist_id="DC-AS-001", decision="fixed", reviewer="design-critic",
            severity="high", decided_at_iso="2026-06-01T00:00:00+00:00",
        )

        rc = _run(_make_args(as_json=True), db, monkeypatch)

        assert rc == 0
        data = _load_json(capsys)
        checklist_ids = [e["checklist_id"] for e in data["prevented_detection"]["examples"]]
        assert "DC-AS-001" in checklist_ids, "design-critic 記録の checklist_id が正当に出現するはず"
        # note 値そのものには監査 ID は含まれない（examples とは別フィールド）
        _assert_note_is_clean(data["rework_cost"]["note"])
        _assert_note_is_clean(data["rework"]["fix_cycles"]["note"])

    def test_note_negative_assertion_does_not_apply_to_rework_session_count_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`rework_session_count` は `rework_cost` dict の JSON 公開キーであり
        禁止語ではない（DC-AM-001 round 5）。JSON キーとして存在することを
        許容しつつ、note 値の禁止語チェックとは独立であることを固定する。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(as_json=True), db, monkeypatch)

        assert rc == 0
        data = _load_json(capsys)
        assert "rework_session_count" in data["rework_cost"]
        assert "rework_session_count" not in data["rework_cost"]["note"]


# ---------------------------------------------------------------------------
# --since / --months / --examples の反映
# ---------------------------------------------------------------------------


class TestFlagReflection:

    def test_json_echoes_since_months_examples_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(
            _make_args(as_json=True, since="2026-05-01", months=6, examples=3),
            db, monkeypatch,
        )

        assert rc == 0
        data = _load_json(capsys)
        assert data["since"] == "2026-05-01"
        assert data["months"] == 6
        assert data["examples"] == 3

    def test_since_filters_out_rows_before_the_boundary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_review_decision(
            db, checklist_id="CR-SNC-OLD", decision="fixed", reviewer="code-reviewer",
            severity="high", decided_at_iso="2026-01-01T00:00:00+00:00",
        )
        _seed_review_decision(
            db, checklist_id="CR-SNC-NEW", decision="fixed", reviewer="code-reviewer",
            severity="high", decided_at_iso="2026-06-01T00:00:00+00:00",
        )

        rc = _run(_make_args(as_json=True, since="2026-05-01"), db, monkeypatch)

        assert rc == 0
        data = _load_json(capsys)
        assert data["prevented_detection"]["headline"]["fixed_medium_plus"] == 1

    def test_examples_limits_example_list_length(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        for i in range(4):
            _seed_review_decision(
                db, checklist_id=f"CR-EX-{i:03d}", decision="fixed",
                reviewer="code-reviewer", severity="high",
                decided_at_iso=f"2026-06-{i + 1:02d}T00:00:00+00:00",
            )

        rc = _run(_make_args(as_json=True, examples=2), db, monkeypatch)

        assert rc == 0
        data = _load_json(capsys)
        assert len(data["prevented_detection"]["examples"]) == 2

    def test_months_limits_trend_bucket_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(as_json=True, months=3), db, monkeypatch)

        assert rc == 0
        data = _load_json(capsys)
        assert len(data["rework"]["trend"]) == 3


# ---------------------------------------------------------------------------
# trend の JSON/人間向けゼロ埋め暦月集合一致（DC-AM-002）
# ---------------------------------------------------------------------------


class TestTrendZeroFillConsistency:

    def test_json_and_human_trend_share_same_zero_filled_months(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """`--json` の trend と人間向け trend が同一のゼロ埋め済み暦月集合を
        持つ（ゼロ埋めはヘルパー層で確定済みのため CLI/JSON 双方が素通し・
        追加ゼロ埋め・欠落が生じない・DC-AM-002）。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        json_data = _run_json(db, monkeypatch)
        json_months = {row["month"] for row in json_data["rework"]["trend"]}

        monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: db)
        rc = cli_metrics.handle_metrics(_make_args())
        assert rc == 0
        human_out = capsys.readouterr().out

        assert len(json_months) == 12
        for month in json_months:
            assert month in human_out, f"人間向け出力に暦月 {month!r} が現れない"


# ---------------------------------------------------------------------------
# [item1 SR-R-001] DB アクセス例外時の stderr は型名のみ（生メッセージ非露出）
# security-review-report-20260707-015605.md 指摘1・plan-report-20260707-020503.md
# fc1-tester。impl 前は Red（現状 handle_metrics は f"{exc}" を stderr に出力し
# 例外メッセージ本文がそのまま露出していた）。
# ---------------------------------------------------------------------------


class TestExceptionMessageSanitization:

    def test_db_access_error_stderr_omits_exception_message_body(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """[item1] `_collect_snapshot` が例外を送出した際、stderr に例外
        メッセージ本文（DB 内部のテーブル名・列名等）が含まれず
        `type(exc).__name__` のみが出力されることを固定した。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: db)

        sensitive_message = "no such column: secret_internal_column_xyz"

        def _raise_operational_error(*args, **kwargs):
            raise sqlite3.OperationalError(sensitive_message)

        monkeypatch.setattr(
            c3_db, "read_review_decision_matrix", _raise_operational_error,
        )

        rc = cli_metrics.handle_metrics(_make_args())

        assert rc == 1
        err = capsys.readouterr().err
        assert sensitive_message not in err, "例外メッセージ本文が stderr に露出している"
        assert "OperationalError" in err, "例外の型名が stderr に出力されていない"


# ---------------------------------------------------------------------------
# [item2 SR-INJ-003] --json 経路の finding_text サニタイズ（JSON 構造は維持）
# impl 前は Red だった（当時 --json 経路は sanitize_terminal_text 未適用のため
# 制御文字がそのまま出力されていた）。
# ---------------------------------------------------------------------------


class TestJsonOutputSanitization:

    def test_json_finding_text_control_chars_stripped_structure_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """[item2] `finding_text` に混入した ANSI エスケープ/制御文字が
        `--json` 出力から除去され、かつ JSON 構造（キー・ネスト）は維持される
        ことを固定した。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        malicious = "before\x1b[31mafter\x07tail"
        _seed_review_decision(
            db, checklist_id="CR-INJ-001", decision="fixed", reviewer="code-reviewer",
            severity="high", decided_at_iso="2026-06-01T00:00:00+00:00",
            finding_text=malicious,
        )

        rc = _run(_make_args(as_json=True), db, monkeypatch)

        assert rc == 0
        data = _load_json(capsys)
        examples = data["prevented_detection"]["examples"]
        assert len(examples) == 1
        finding_text = examples[0]["finding_text"]
        assert "\x1b" not in finding_text, "ANSI エスケープが --json 出力に残っている"
        assert "\x07" not in finding_text, "制御文字(BEL)が --json 出力に残っている"
        assert "before" in finding_text
        assert "after" in finding_text
        assert "tail" in finding_text
        # JSON 構造（キー・ネスト・checklist_id 値）は維持される
        assert examples[0]["checklist_id"] == "CR-INJ-001"
        for key in ("generated_at", "since", "months", "examples", "prevented_detection", "rework", "rework_cost"):
            assert key in data


# ---------------------------------------------------------------------------
# [item5 SR-NEW] --months 上限（120）超過は DB アクセス前に stderr + exit 1
# impl 前は Red だった（当時は下限のみ検証し上限検証が存在しなかった）。
# ---------------------------------------------------------------------------


class TestMonthsUpperBound:

    def test_months_over_max_returns_exit1_before_db_access(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    ) -> None:
        """[item5] `--months 121`（上限 120 超過）は DB アクセス前に
        stderr + exit 1 になることを固定した。"""
        called = _never_call_locate_c3_db(monkeypatch)

        rc = cli_metrics.handle_metrics(_make_args(months=121))

        assert rc == 1
        assert capsys.readouterr().err
        assert called["value"] is False, "--months 上限検証前に DB へアクセスしている"

    def test_months_at_max_boundary_is_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """[item5] `--months 120`（上限値ちょうど）は正常に受理される
        （下限 0/-1 の既存境界テストと同型の回帰防止・現状も上限が無いため
        通る＝impl 前後で不変の境界ガード）。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)

        rc = _run(_make_args(months=120), db, monkeypatch)

        assert rc == 0


# ---------------------------------------------------------------------------
# [item6 SR-INJ-003] 人間向け出力の DB 由来全フィールドのサニタイズ
# impl 前は Red だった（当時 _render_human は reviewer/finding_text のみサニタイズし
# severity/decision/checklist_id/decided_at は未適用だった）。
# ---------------------------------------------------------------------------


class TestHumanOutputFieldSanitization:

    def test_matrix_severity_and_decision_control_chars_stripped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """[item6] matrix 行の `severity`/`decision` に混入した制御文字が
        人間向け出力から除去されることを固定した。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_review_decision_raw(
            db, checklist_id="CR-INJ-010", decision="fixed\x1b[2J",
            severity="high\x07", decided_at="2026-06-01T00:00:00+00:00",
        )

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "\x1b" not in out, "matrix の decision に含まれる ANSI エスケープが出力に残っている"
        assert "\x07" not in out, "matrix の severity に含まれる制御文字が出力に残っている"

    def test_examples_checklist_id_and_decided_at_control_chars_stripped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """[item6] 実例行の `checklist_id`/`decided_at` に混入した制御文字が
        人間向け出力から除去されることを固定した。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        _seed_review_decision_raw(
            db, checklist_id="CR-INJ-011\x1b[2K", decision="fixed",
            severity="high", decided_at="2026-06-01T00:00:00\x07+00:00",
        )

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert "\x1b" not in out, "checklist_id に含まれる ANSI エスケープが出力に残っている"
        assert "\x07" not in out, "decided_at に含まれる制御文字が出力に残っている"


# ---------------------------------------------------------------------------
# [SR新規2 SR-NEW] sanitize_terminal_text の U+2028/U+2029 除去単体テスト
# security-review-report-20260707-022656.md 新規2・plan-report-20260707-023441.md
# fd1-tester。impl 前は Red だった（当時 _DISALLOWED_CONTROL_RE は ASCII C0/DEL
# のみを対象とし U+2028 LINE SEPARATOR / U+2029 PARAGRAPH SEPARATOR を除去
# しなかった）。
# ---------------------------------------------------------------------------


class TestSanitizeTerminalTextUnicodeLineSeparators:

    def test_sanitize_terminal_text_strips_u2028_and_u2029(self) -> None:
        """[SR新規2] `sanitize_terminal_text` が U+2028 (LINE SEPARATOR) /
        U+2029 (PARAGRAPH SEPARATOR) を除去することを固定した。"""
        from c3._terminal import sanitize_terminal_text  # noqa: PLC0415

        s = "before middle after"
        result = sanitize_terminal_text(s)
        assert " " not in result, "U+2028 が sanitize_terminal_text の出力に残っている"
        assert " " not in result, "U+2029 が sanitize_terminal_text の出力に残っている"
        assert "before" in result
        assert "middle" in result
        assert "after" in result


# ---------------------------------------------------------------------------
# [SR新規2 SR-NEW] c3 metrics 出力（--json / 人間向け）における U+2028 除去
# security-review-report-20260707-022656.md 新規2・plan-report-20260707-023441.md
# fd1-tester。impl 前は Red だった（当時 _DISALLOWED_CONTROL_RE が U+2028 を
# 除去対象に含まず、finding_text 経由で metrics 出力に露出していた）。
# ---------------------------------------------------------------------------


class TestUnicodeLineSeparatorSanitization:

    def test_json_finding_text_u2028_stripped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """[SR新規2] `finding_text` に混入した U+2028 (LINE SEPARATOR) が
        `--json` 出力から除去され、かつ JSON 構造（キー・ネスト）は維持される
        ことを固定した。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        malicious = "before after"
        _seed_review_decision(
            db, checklist_id="CR-INJ-020", decision="fixed", reviewer="code-reviewer",
            severity="high", decided_at_iso="2026-06-01T00:00:00+00:00",
            finding_text=malicious,
        )

        rc = _run(_make_args(as_json=True), db, monkeypatch)

        assert rc == 0
        data = _load_json(capsys)
        examples = data["prevented_detection"]["examples"]
        assert len(examples) == 1
        finding_text = examples[0]["finding_text"]
        assert " " not in finding_text, "U+2028 が --json 出力の finding_text に残っている"
        assert "before" in finding_text
        assert "after" in finding_text
        assert examples[0]["checklist_id"] == "CR-INJ-020"
        for key in ("generated_at", "since", "months", "examples", "prevented_detection", "rework", "rework_cost"):
            assert key in data

    def test_human_finding_text_u2028_stripped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """[SR新規2] `finding_text` に混入した U+2028 (LINE SEPARATOR) が
        人間向け出力から除去されることを固定した。"""
        db = tmp_path / "c3.db"
        _create_c3_db(db)
        malicious = "before after"
        _seed_review_decision(
            db, checklist_id="CR-INJ-021", decision="fixed", reviewer="code-reviewer",
            severity="high", decided_at_iso="2026-06-02T00:00:00+00:00",
            finding_text=malicious,
        )

        rc = _run(_make_args(), db, monkeypatch)

        assert rc == 0
        out = capsys.readouterr().out
        assert " " not in out, "U+2028 が人間向け出力に残っている"
