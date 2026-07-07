"""Tests for .claude/hooks/tier_gap_check.py (新規・未実装)

tier-routing P3 学習記録の欠落検知（session_stop.py Phase 4）の Red フェーズ
テストだった。architecture-report-20260707-065043.md §5・
plan-report-20260707-065732.md test-gap-check（T4 Red）に基づく。

対象 hook は本 Red フェーズ時点で未作成のため、本ファイルの全テストは
「.claude/hooks/tier_gap_check.py が存在しない（FileNotFoundError）」という
単一の原因で失敗した（tester/MEMORY.md の record_agent_outcome.py Red 実装
パターンを踏襲し、pytest.mark.skipif ではなく明示的な例外送出で「失敗する
Red」の証跡を残す設計にした。fixture 経由のテストは setup ERROR になる）。

テストが要求する hook 契約（developer への実装契約。plan/architecture に
明記が無い実装詳細は本ファイルで固定した）:

- パス: `.claude/hooks/tier_gap_check.py`
- 公開関数: `run(payload: dict) -> None`（`session_stop.py` Phase 4 が
  `gap_module.run(payload)` で呼ぶ。戻り値は使われない。例外を投げないこと）
- モジュール属性（monkeypatch 対象として固定）: `APPLIED_STATE_PATH`
  （`.claude/state/tier_autoapply.jsonl` の絶対パス文字列・§3-7 の
  `_CLAUDE_DIR` 機構で解決）・`TIER_SELECTION_PATH`（G3 否定時の session_id
  fallback 源・`record_agent_outcome.py` と同名の意味）。
- DB アクセス: `from c3 import db as c3_db` を用い `c3_db.locate_c3_db()`
  （引数無し）で `.claude/state/c3.db` を解決する（`record_agent_outcome.py`
  と同じ呼び出し規約）。DB 不在時は沈黙 return。
- 突合ロジック（§5-2）: N = jsonl の `role_recorded ∈ {developer, tester}`
  かつ `session_id` が payload の session_id に一致する行の COUNT（直近5分＝
  `now - ts < 5min` の行は除外・`session_id` が null/欠落の jsonl 行は
  対象外）。M = `SELECT COUNT(*) FROM agent_outcomes WHERE session_id = ?
  GROUP BY role`（`session_id` が NULL の agent_outcomes 行は一致せず M に
  含まれない）。`Z_role` = 当該 session の jsonl 最古行 ts（`ts_floor`）を
  下限として `SELECT COUNT(*) FROM agent_outcomes WHERE session_id IS NULL
  AND ts >= ? AND role = ?`。`K' = N - M - Z_role`（下限 0）で `K' > 0` の
  ときのみ role ごとに stderr へ 1 行警告する。
- 警告文言: 「可能性」「誤検知」を含む文言＋ role 名。NULL 非対称抑止に
  関する注記（「非対称」「抑止」を含む）は §5-3 の文言テンプレートにより
  常に警告メッセージへ含まれる。
- payload に `session_id` が無い場合は `TIER_SELECTION_PATH` の
  `session_id` フィールドへ fallback。両方無ければ突合対象外として沈黙
  return。
- 全エラー（jsonl 不在・DB 不在・SQL エラー・JSON パース失敗）は沈黙
  return（例外を外へ伝播させない）。kill-switch（tier_autoapply.py が
  `C3_TIER_AUTOAPPLY_DISABLE=1` で jsonl に何も書かなかった状態）は
  jsonl 不在と同型のため N=0 で自然に沈黙する。

ts フォーマット（DC-AS-001 round4）: 本ファイルのテストは jsonl 側 ts・
agent_outcomes 側 ts のいずれも production の実フォーマット
（`datetime.now(timezone.utc).isoformat(timespec="seconds")` と同一の
UTC・秒精度・`+00:00`・小数秒なしプロファイル）で構築した。テスト都合の
簡略書式で `ts_floor` 跨りソース比較を緑にせず、production の生成式で
実際に辞書順比較が成立することを固定した（round3 の
TestNullAsymmetrySuppression が矛盾を検出できなかった構図の再発防止）。
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from c3 import db as c3_db

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "tier_gap_check.py"

# jsonl / agent_outcomes.ts が同一 UTC ISO8601 秒精度プロファイルであることの
# 検証パターン（agent_outcomes.ts / db.py:1046 と同一生成式）。
_TS_UTC_SECONDS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")


def _load_hook_module(name: str = "tier_gap_check_t") -> types.ModuleType:
    """HOOK_PATH からモジュールをロードした。

    HOOK_PATH が存在しない場合（Red フェーズ）は FileNotFoundError を送出する。
    pytest.mark.skipif を使うと未実装時に全テストが SKIP になり「失敗する
    Red」の証跡が残らないため、明示的に例外を送出する設計にした
    （tester/MEMORY.md「.dev/hooks テストの pytestmark skipif 回避パターン」
    を踏襲した）。
    """
    if not HOOK_PATH.is_file():
        raise FileNotFoundError(
            f"tier_gap_check.py が未作成だった（Red フェーズの想定挙動）: {HOOK_PATH}"
        )
    spec = importlib.util.spec_from_file_location(name, HOOK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


def _prod_ts(dt: datetime) -> str:
    """production 実フォーマット（UTC・秒精度・`+00:00`・小数秒なし）で ts 文字列を構築した。

    `datetime.now(timezone.utc).isoformat(timespec="seconds")`（jsonl 側
    tier_autoapply.py・DB 側 db.py:1046）と同一の生成式を用いた。
    """
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _create_c3_db(db_path: Path) -> None:
    from c3.migrate import apply_pending_migrations

    apply_pending_migrations(db_path)


def _append_jsonl_row(
    path: Path,
    *,
    ts: str,
    session_id: str | None,
    role_recorded: str,
    model_applied: str | None = "sonnet",
    source: str = "injected",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": ts,
        "session_id": session_id,
        "subagent_type": role_recorded,
        "role_recorded": role_recorded,
        "model_applied": model_applied,
        "source": source,
        "prompt_prefix": "",
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _insert_outcome(
    db_path: Path,
    *,
    role: str,
    session_id: str | None,
    ts: str,
    complexity: str = "medium",
    tier: str = "sonnet",
    success: int = 1,
) -> None:
    """agent_outcomes へ 1 行を raw INSERT した（ts・session_id を精密制御するため）。"""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO agent_outcomes "
            "(role, task_complexity, tier, success, gate, note, session_id, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (role, complexity, tier, success, None, None, session_id, ts),
        )
        conn.commit()
    finally:
        conn.close()


def _patch_paths(
    mod: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    jsonl_path: Path,
    db_path: Path | None,
    tier_selection_path: Path,
) -> None:
    """3 パス（jsonl / DB / tier_selection fallback）を全て tmp 隔離先へ差し替えた。

    実リポジトリには既に `.claude/state/tier_autoapply.jsonl` /
    `.claude/state/c3.db` / `.claude/state/tier_selection.json` が実在する
    ため、常に全て明示的に差し替えないと実 state を汚染しうる（非隔離）。
    """
    monkeypatch.setattr(mod, "APPLIED_STATE_PATH", str(jsonl_path))
    monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(tier_selection_path))
    monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: db_path)


@pytest.fixture()
def gap_mod() -> types.ModuleType:
    return _load_hook_module()


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "c3.db"
    _create_c3_db(p)
    return p


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "tier_autoapply.jsonl"


@pytest.fixture()
def absent_tier_selection_path(tmp_path: Path) -> Path:
    """存在しない tier_selection.json パス（session_id fallback 無し用）。"""
    return tmp_path / "state" / "tier_selection.json"


def _capture_stderr(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """capsys ではなく明示的な StringIO 差し替えで stderr を捕捉した。

    reconfigure 済み hook（`sys.stderr.reconfigure(encoding="utf-8")` を
    import 時に呼ぶ想定）は capsys でキャプチャできないことがあるため
    （tester/MEMORY.md 既知の I-01 パターン）、この方式を採用した。
    """
    fake_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", fake_stderr)
    return fake_stderr


# ---------------------------------------------------------------------------
# 基本の欠落警告 / 沈黙ケース
# ---------------------------------------------------------------------------


class TestGapWarning:
    """起動数 N が記録数 M を上回るとき role 別に stderr 1 行警告することを固定した。"""

    def test_warns_when_launches_exceed_outcomes(
        self,
        gap_mod: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        jsonl_path: Path,
        db_path: Path,
        absent_tier_selection_path: Path,
    ) -> None:
        """N=1・M=0・Z_role=0（K'=1>0）で developer の欠落を警告したことを確認した。"""
        _patch_paths(
            gap_mod,
            monkeypatch,
            jsonl_path=jsonl_path,
            db_path=db_path,
            tier_selection_path=absent_tier_selection_path,
        )
        old_ts = _prod_ts(datetime.now(timezone.utc) - timedelta(hours=1))
        _append_jsonl_row(
            jsonl_path, ts=old_ts, session_id="sess-warn-1", role_recorded="developer"
        )
        # agent_outcomes には該当 session の記録が無い（M=0）。

        fake_stderr = _capture_stderr(monkeypatch)
        gap_mod.run({"session_id": "sess-warn-1"})

        captured = fake_stderr.getvalue()
        assert "developer" in captured
        assert "可能性" in captured
        assert "誤検知" in captured
        # §5-3: NULL 非対称抑止に関する注記は警告テンプレートに常に含まれる。
        assert "抑止" in captured


class TestSilentWhenCountsMatch:
    """起動数と記録数が一致するとき（K'<=0）沈黙することを固定した。"""

    def test_silent_when_n_equals_m(
        self,
        gap_mod: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        jsonl_path: Path,
        db_path: Path,
        absent_tier_selection_path: Path,
    ) -> None:
        """N=1・M=1（K'=0）で警告が出ず沈黙したことを確認した。"""
        _patch_paths(
            gap_mod,
            monkeypatch,
            jsonl_path=jsonl_path,
            db_path=db_path,
            tier_selection_path=absent_tier_selection_path,
        )
        old_ts = _prod_ts(datetime.now(timezone.utc) - timedelta(hours=1))
        _append_jsonl_row(
            jsonl_path, ts=old_ts, session_id="sess-match-1", role_recorded="developer"
        )
        _insert_outcome(
            db_path, role="developer", session_id="sess-match-1", ts=old_ts
        )

        fake_stderr = _capture_stderr(monkeypatch)
        gap_mod.run({"session_id": "sess-match-1"})

        assert fake_stderr.getvalue() == ""


class TestSilentWhenSourcesAbsent:
    """jsonl・DB いずれかが不在のとき例外を投げず沈黙することを固定した。"""

    def test_silent_when_db_absent(
        self,
        gap_mod: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        jsonl_path: Path,
        absent_tier_selection_path: Path,
    ) -> None:
        """DB 不在（locate_c3_db が None）でも例外を投げず沈黙したことを確認した。"""
        _patch_paths(
            gap_mod,
            monkeypatch,
            jsonl_path=jsonl_path,
            db_path=None,
            tier_selection_path=absent_tier_selection_path,
        )
        old_ts = _prod_ts(datetime.now(timezone.utc) - timedelta(hours=1))
        _append_jsonl_row(
            jsonl_path, ts=old_ts, session_id="sess-nodb", role_recorded="developer"
        )

        fake_stderr = _capture_stderr(monkeypatch)
        gap_mod.run({"session_id": "sess-nodb"})

        assert fake_stderr.getvalue() == ""


class TestKillSwitchSilence:
    """kill-switch（tier_autoapply.py が jsonl に行を書かなかった状態）下流復帰を固定した。

    C3_TIER_AUTOAPPLY_DISABLE=1 のときは tier_autoapply.jsonl 自体が
    書かれないため、gap_check から見ると「jsonl 不在」と同型の状況になる
    （DC-GP-003・旧来動作への完全復帰）。
    """

    def test_jsonl_absent_is_silent(
        self,
        gap_mod: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        jsonl_path: Path,
        db_path: Path,
        absent_tier_selection_path: Path,
    ) -> None:
        """jsonl 不在（N=0）のとき agent_outcomes に記録があっても誤警告せず沈黙したことを確認した。"""
        _patch_paths(
            gap_mod,
            monkeypatch,
            jsonl_path=jsonl_path,
            db_path=db_path,
            tier_selection_path=absent_tier_selection_path,
        )
        # jsonl ファイル自体を作らない（kill-switch 相当）。
        old_ts = _prod_ts(datetime.now(timezone.utc) - timedelta(hours=1))
        _insert_outcome(
            db_path, role="developer", session_id="sess-killswitch", ts=old_ts
        )

        fake_stderr = _capture_stderr(monkeypatch)
        gap_mod.run({"session_id": "sess-killswitch"})

        assert fake_stderr.getvalue() == ""


class TestRecentWindowExclusion:
    """直近5分の jsonl 行は中間状態として N から除外することを固定した。"""

    def test_recent_launch_excluded_from_launch_count(
        self,
        gap_mod: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        jsonl_path: Path,
        db_path: Path,
        absent_tier_selection_path: Path,
    ) -> None:
        """直近30秒の jsonl 行が N=0 相当に除外され警告が出なかったことを確認した。"""
        _patch_paths(
            gap_mod,
            monkeypatch,
            jsonl_path=jsonl_path,
            db_path=db_path,
            tier_selection_path=absent_tier_selection_path,
        )
        recent_ts = _prod_ts(datetime.now(timezone.utc) - timedelta(seconds=30))
        _append_jsonl_row(
            jsonl_path,
            ts=recent_ts,
            session_id="sess-recent",
            role_recorded="developer",
        )
        # agent_outcomes には記録なし（M=0 だが N も 0 相当のため警告不要）。

        fake_stderr = _capture_stderr(monkeypatch)
        gap_mod.run({"session_id": "sess-recent"})

        assert fake_stderr.getvalue() == ""


class TestSilentWhenSessionIdAbsent:
    """payload・tier_selection.json いずれにも session_id が無いとき沈黙することを固定した。"""

    def test_no_session_id_anywhere_is_silent(
        self,
        gap_mod: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        jsonl_path: Path,
        db_path: Path,
        absent_tier_selection_path: Path,
    ) -> None:
        """session_id が完全に不明のとき突合対象外として例外なく沈黙したことを確認した。"""
        _patch_paths(
            gap_mod,
            monkeypatch,
            jsonl_path=jsonl_path,
            db_path=db_path,
            tier_selection_path=absent_tier_selection_path,
        )
        old_ts = _prod_ts(datetime.now(timezone.utc) - timedelta(hours=1))
        _append_jsonl_row(
            jsonl_path, ts=old_ts, session_id="sess-orphan", role_recorded="developer"
        )

        fake_stderr = _capture_stderr(monkeypatch)
        gap_mod.run({})  # session_id キーなし

        assert fake_stderr.getvalue() == ""


class TestJsonlNullSessionIdNotCounted:
    """jsonl 側の session_id NULL 行が N に数えられないことを固定した。"""

    def test_null_session_id_row_excluded_from_n(
        self,
        gap_mod: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        jsonl_path: Path,
        db_path: Path,
        absent_tier_selection_path: Path,
    ) -> None:
        """session_id 一致行(N=1)に加え NULL 行が誤って N に加算されず K'=0 で沈黙したことを確認した。

        session_id=NULL の jsonl 行を N に誤加算する実装だと N=2・M=1 で
        K'=1>0 の誤警告になる。正しい実装は NULL 行を除外し N=1・M=1 で
        K'=0（沈黙）になる。
        """
        _patch_paths(
            gap_mod,
            monkeypatch,
            jsonl_path=jsonl_path,
            db_path=db_path,
            tier_selection_path=absent_tier_selection_path,
        )
        old_ts = _prod_ts(datetime.now(timezone.utc) - timedelta(hours=1))
        _append_jsonl_row(
            jsonl_path, ts=old_ts, session_id="sess-nullrow", role_recorded="developer"
        )
        _append_jsonl_row(
            jsonl_path, ts=old_ts, session_id=None, role_recorded="developer"
        )
        _insert_outcome(
            db_path, role="developer", session_id="sess-nullrow", ts=old_ts
        )

        fake_stderr = _capture_stderr(monkeypatch)
        gap_mod.run({"session_id": "sess-nullrow"})

        assert fake_stderr.getvalue() == ""


class TestNullConstraintExcludedFromM:
    """agent_outcomes 側の session_id NULL 行が M に数えられないことを固定した。"""

    def test_null_session_id_outcome_not_counted_as_m(
        self,
        gap_mod: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        jsonl_path: Path,
        db_path: Path,
        absent_tier_selection_path: Path,
    ) -> None:
        """NULL outcome が M に誤加算されず（かつ ts_floor 前で Z_role にも数えられず）K'=1>0 で警告したことを確認した。

        `WHERE session_id = ?` が NULL 行を M に含めてしまう実装だと
        K'=1-1-0=0 で沈黙してしまう。NULL 行の ts を ts_floor（jsonl 唯一行の
        ts）より前に置いて Z_role からも除外し、M 側の除外のみを単独で
        判別できるようにした。
        """
        _patch_paths(
            gap_mod,
            monkeypatch,
            jsonl_path=jsonl_path,
            db_path=db_path,
            tier_selection_path=absent_tier_selection_path,
        )
        floor_dt = datetime.now(timezone.utc) - timedelta(hours=1)
        floor_ts = _prod_ts(floor_dt)
        _append_jsonl_row(
            jsonl_path,
            ts=floor_ts,
            session_id="sess-nullm",
            role_recorded="developer",
        )
        old_null_ts = _prod_ts(floor_dt - timedelta(days=2))
        _insert_outcome(
            db_path, role="developer", session_id=None, ts=old_null_ts
        )

        fake_stderr = _capture_stderr(monkeypatch)
        gap_mod.run({"session_id": "sess-nullm"})

        captured = fake_stderr.getvalue()
        assert "developer" in captured
        assert "可能性" in captured


class TestRoleGating:
    """F-1: role_recorded が developer のみ突合対象になることを固定した（tester は除外）。

    元は「role_recorded ∈ {developer, tester} のみ突合対象」を固定していたが、
    code-review-report-20260707-110524.md F-1（tester は起動:記録カーディナリティ
    不一致により恒常誤警告となる構造的欠陥）を受け、developer のみを突合対象と
    する期待へ移行した（tester 分は下記 TestTesterExcludedFromEvaluation へ分離）。
    """

    def test_only_developer_is_evaluated(
        self,
        gap_mod: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        jsonl_path: Path,
        db_path: Path,
        absent_tier_selection_path: Path,
    ) -> None:
        """developer は警告され、tester・reviewer 相当の role は無視されたことを確認した。"""
        _patch_paths(
            gap_mod,
            monkeypatch,
            jsonl_path=jsonl_path,
            db_path=db_path,
            tier_selection_path=absent_tier_selection_path,
        )
        old_ts = _prod_ts(datetime.now(timezone.utc) - timedelta(hours=1))
        _append_jsonl_row(
            jsonl_path, ts=old_ts, session_id="sess-role", role_recorded="developer"
        )
        _append_jsonl_row(
            jsonl_path, ts=old_ts, session_id="sess-role", role_recorded="tester"
        )
        _append_jsonl_row(
            jsonl_path, ts=old_ts, session_id="sess-role", role_recorded="reviewer"
        )
        # agent_outcomes には一切記録なし（M=0 全 role）。

        fake_stderr = _capture_stderr(monkeypatch)
        gap_mod.run({"session_id": "sess-role"})

        captured = fake_stderr.getvalue()
        assert "developer" in captured
        assert "tester" not in captured
        assert "reviewer" not in captured


class TestTesterExcludedFromEvaluation:
    """F-1 Red: tester のみ起動されたセッションでは記録が無くても警告が出ないことを固定した。

    dev-workflow の記録契約上 tester の record_agent_outcome.py 呼び出しは
    D-5（成功時 1 回）・D-3（失敗時のみ）に限定される一方、tester の Agent
    起動自体は Red 作成・confirm 等で 1 セッション中に複数回発生するため、
    tester を突合対象に含めると通常セッションで恒常的に誤警告が発火する
    （code-review-report-20260707-110524.md F-1）。本テストは是正前の実装
    （`_EVALUATED_ROLES` に tester を含む）で警告が出てしまうため、実装前は
    Red（失敗）になった。
    """

    def test_tester_only_launches_no_warning_even_when_unrecorded(
        self,
        gap_mod: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        jsonl_path: Path,
        db_path: Path,
        absent_tier_selection_path: Path,
    ) -> None:
        """tester 起動 N=3・記録 M=0 のセッションで警告が出なかったことを確認した。"""
        _patch_paths(
            gap_mod,
            monkeypatch,
            jsonl_path=jsonl_path,
            db_path=db_path,
            tier_selection_path=absent_tier_selection_path,
        )
        old_ts = _prod_ts(datetime.now(timezone.utc) - timedelta(hours=1))
        for _ in range(3):
            _append_jsonl_row(
                jsonl_path,
                ts=old_ts,
                session_id="sess-tester-only",
                role_recorded="tester",
            )
        # agent_outcomes には記録なし（M=0）。tester は突合対象外のため沈黙するはず。

        fake_stderr = _capture_stderr(monkeypatch)
        gap_mod.run({"session_id": "sess-tester-only"})

        assert fake_stderr.getvalue() == ""


# ---------------------------------------------------------------------------
# NULL 非対称の抑止補正（単一ルール K' = N - M - Z_role）
# ---------------------------------------------------------------------------


class TestNullAsymmetrySuppression:
    """単一ルール `K' = N - M - Z_role`（下限 0）による抑止補正を固定した。

    jsonl 側 NULL（G3 否定・起動時点）と agent_outcomes 側 NULL（record 時
    tier_selection 不在・記録時点）はトリガーが独立で「両側対称除外」が
    成立しない（DC-AS-001 round2）。round3 で単一ルールへ一本化された
    抑止補正（`Z_role` を当該 session の ts_floor 以降に限定）を、
    (a) 基本抑止・(b) N>1 の減算差分・(c) ts 下限による恒久抑止の排除、
    の 3 観点で固定する。ts はいずれも production 実フォーマットで構築した
    （DC-AS-001 round4）。
    """

    def test_basic_suppression_null_outcome_within_session(
        self,
        gap_mod: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        jsonl_path: Path,
        db_path: Path,
        absent_tier_selection_path: Path,
    ) -> None:
        """N=1・M=0・Z_role=1 で K'=0 となり誤警告を抑止したことを確認した。"""
        _patch_paths(
            gap_mod,
            monkeypatch,
            jsonl_path=jsonl_path,
            db_path=db_path,
            tier_selection_path=absent_tier_selection_path,
        )
        floor_dt = datetime.now(timezone.utc) - timedelta(hours=1)
        floor_ts = _prod_ts(floor_dt)
        assert _TS_UTC_SECONDS_RE.match(floor_ts)
        _append_jsonl_row(
            jsonl_path,
            ts=floor_ts,
            session_id="sess-suppress-basic",
            role_recorded="developer",
        )
        null_outcome_ts = _prod_ts(floor_dt + timedelta(minutes=10))
        _insert_outcome(
            db_path, role="developer", session_id=None, ts=null_outcome_ts
        )
        # session_id 一致の outcome（M）は無い。

        fake_stderr = _capture_stderr(monkeypatch)
        gap_mod.run({"session_id": "sess-suppress-basic"})

        assert fake_stderr.getvalue() == ""

    def test_partial_suppression_with_remainder(
        self,
        gap_mod: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        jsonl_path: Path,
        db_path: Path,
        absent_tier_selection_path: Path,
    ) -> None:
        """N=2・M=0・Z_role=1 で K'=1>0 となり完全抑止ではなく 1 件分だけ警告したことを確認した。

        round2 の完全抑止(a)（Z_role>=1 で全抑止）が実装されていると本テストは
        沈黙してしまい方式取り違えを検出できる（round3 で採用された方式(b)
        の K 下限減算のみ本テストを満たす）。
        """
        _patch_paths(
            gap_mod,
            monkeypatch,
            jsonl_path=jsonl_path,
            db_path=db_path,
            tier_selection_path=absent_tier_selection_path,
        )
        floor_dt = datetime.now(timezone.utc) - timedelta(hours=2)
        floor_ts = _prod_ts(floor_dt)
        second_ts = _prod_ts(floor_dt + timedelta(hours=1))
        _append_jsonl_row(
            jsonl_path,
            ts=floor_ts,
            session_id="sess-suppress-partial",
            role_recorded="developer",
        )
        _append_jsonl_row(
            jsonl_path,
            ts=second_ts,
            session_id="sess-suppress-partial",
            role_recorded="developer",
        )
        null_outcome_ts = _prod_ts(floor_dt + timedelta(minutes=10))
        _insert_outcome(
            db_path, role="developer", session_id=None, ts=null_outcome_ts
        )

        fake_stderr = _capture_stderr(monkeypatch)
        gap_mod.run({"session_id": "sess-suppress-partial"})

        captured = fake_stderr.getvalue()
        assert captured != ""
        assert "developer" in captured

    def test_old_null_outcome_excluded_by_ts_floor(
        self,
        gap_mod: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        jsonl_path: Path,
        db_path: Path,
        absent_tier_selection_path: Path,
    ) -> None:
        """ts_floor より前の古い NULL outcome は Z_role に数えず真の欠落を警告したことを確認した。

        N=1・M=0・古い NULL のみ（当該セッション開始前）のとき K'=1-0-0=1>0
        で警告が出ることを固定する（round3 の中核修正: 恒久抑止しない）。
        """
        _patch_paths(
            gap_mod,
            monkeypatch,
            jsonl_path=jsonl_path,
            db_path=db_path,
            tier_selection_path=absent_tier_selection_path,
        )
        floor_dt = datetime.now(timezone.utc) - timedelta(hours=1)
        floor_ts = _prod_ts(floor_dt)
        _append_jsonl_row(
            jsonl_path,
            ts=floor_ts,
            session_id="sess-suppress-tsfloor",
            role_recorded="developer",
        )
        old_null_ts = _prod_ts(floor_dt - timedelta(days=3))
        _insert_outcome(
            db_path, role="developer", session_id=None, ts=old_null_ts
        )

        fake_stderr = _capture_stderr(monkeypatch)
        gap_mod.run({"session_id": "sess-suppress-tsfloor"})

        captured = fake_stderr.getvalue()
        assert captured != ""
        assert "developer" in captured


# ---------------------------------------------------------------------------
# F-4: jsonl 非 dict／型不正行の行単位 skip（全滅防止）
# ---------------------------------------------------------------------------


class TestNonDictRowsSkipped:
    """F-4 Red: jsonl に非 dict／型不正行が混入しても他の正常行の欠落検知が機能したことを固定した。

    code-review-report-20260707-110524.md F-4 の指摘どおり、是正前の実装は
    `json.loads` に成功したが dict でない行（JSON 配列・文字列・数値）や
    `role_recorded` が非文字列の dict 行に対して型ガードを持たず、
    `row.get(...)`／`.lower()` 呼び出しで送出された `AttributeError` が
    行単位 try/except の外（`run()` の `except Exception: pass`）まで伝播して
    セッション全体の gap_check が沈黙していた。本テストは「1 行の型不正混入で
    セッション全体の gap_check が全滅しない」ことを固定するため、実装前は
    型不正行によって本来発火するはずの developer 欠落警告が出ず Red（失敗）
    になった。
    """

    def test_non_dict_and_type_invalid_rows_do_not_suppress_real_gap_detection(
        self,
        gap_mod: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        jsonl_path: Path,
        db_path: Path,
        absent_tier_selection_path: Path,
    ) -> None:
        """JSON配列行・文字列行・数値行・role_recorded 非文字列行が混入しても developer 欠落警告が発火したことを確認した。"""
        _patch_paths(
            gap_mod,
            monkeypatch,
            jsonl_path=jsonl_path,
            db_path=db_path,
            tier_selection_path=absent_tier_selection_path,
        )
        old_ts = _prod_ts(datetime.now(timezone.utc) - timedelta(hours=1))
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with jsonl_path.open("a", encoding="utf-8") as f:
            # (a) JSON 配列行（有効な JSON だが dict でない）。
            f.write(json.dumps([1, 2, 3]) + "\n")
            # (b) 文字列行（有効な JSON だが dict でない）。
            f.write(json.dumps("just a string") + "\n")
            # (c) 数値行（有効な JSON だが dict でない）。
            f.write(json.dumps(123) + "\n")
            # (d) dict だが role_recorded が非文字列（int）の行。
            f.write(
                json.dumps(
                    {
                        "ts": old_ts,
                        "session_id": "sess-f4-typeerr",
                        "subagent_type": 12345,
                        "role_recorded": 12345,
                        "model_applied": "sonnet",
                        "source": "injected",
                        "prompt_prefix": "",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        # 正常な developer 行を 1 件追加した（真の欠落として検知されるべき対象）。
        _append_jsonl_row(
            jsonl_path,
            ts=old_ts,
            session_id="sess-f4-typeerr",
            role_recorded="developer",
        )
        # agent_outcomes には記録なし（M=0）。真の欠落が検知されるはず。

        fake_stderr = _capture_stderr(monkeypatch)
        gap_mod.run({"session_id": "sess-f4-typeerr"})

        captured = fake_stderr.getvalue()
        assert "developer" in captured
        assert "可能性" in captured


# ---------------------------------------------------------------------------
# F-9: jsonl の ts フィールド型不正行の行単位 skip（全滅防止）
# ---------------------------------------------------------------------------


class TestTsTypeInvalidRowsSkipped:
    """F-9 Red: jsonl の `ts` フィールドが型不正（null／int）でも他の正常行の欠落検知が機能したことを固定した。

    code-review-report-20260707-113712.md F-9 の指摘どおり、是正前の
    `_count_launches` は `ts = row.get("ts", "")` を使っており、`"ts"` キーが
    存在し値が非文字列（`None`／`int`）の行では既定値 `""` に置き換わらず
    `None`／`int` がそのまま返っていた。直後の `if ts >= recent_ts_str:` は
    姉妹関数 `_get_ts_floor`（`isinstance(ts, str)` ガード済み）と非対称
    だったため `TypeError: '>=' not supported between instances of
    'NoneType' and 'str'` を送出し、`except (IOError, OSError)` に捕捉
    されず for ループが途中で打ち切られていた。これは F-4 が閉じたはずの
    「型不正 1 行でセッション全体の gap_check が沈黙する」問題クラスの
    再発だった。本テストは「ts 型不正行が混入しても同一 session の他の
    正常な developer 起動行に基づく真の欠落検知が機能する」ことを固定する
    ため、実装前は ts 型不正行の直後に置いた正常行が処理されず、期待した
    developer 欠落警告が出ないか、あるいは `TypeError` が `run()` の
    `except Exception: pass` まで伝播した末に沈黙するかたちで Red（失敗）
    になった。
    """

    def test_null_ts_row_does_not_suppress_real_gap_detection(
        self,
        gap_mod: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        jsonl_path: Path,
        db_path: Path,
        absent_tier_selection_path: Path,
    ) -> None:
        """`{"ts": null}` 行が混入しても developer 欠落警告が発火したことを確認した。"""
        _patch_paths(
            gap_mod,
            monkeypatch,
            jsonl_path=jsonl_path,
            db_path=db_path,
            tier_selection_path=absent_tier_selection_path,
        )
        old_ts = _prod_ts(datetime.now(timezone.utc) - timedelta(hours=1))
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with jsonl_path.open("a", encoding="utf-8") as f:
            # ts が null（キーは存在するが値が非文字列）の型不正行。
            f.write(
                json.dumps(
                    {
                        "ts": None,
                        "session_id": "sess-f9-tsnull",
                        "subagent_type": "developer",
                        "role_recorded": "developer",
                        "model_applied": "sonnet",
                        "source": "injected",
                        "prompt_prefix": "",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        # 同一 session の正常な developer 起動行（真の欠落として検知されるべき対象）。
        _append_jsonl_row(
            jsonl_path,
            ts=old_ts,
            session_id="sess-f9-tsnull",
            role_recorded="developer",
        )
        # agent_outcomes には記録なし（M=0）。真の欠落が検知されるはず。

        fake_stderr = _capture_stderr(monkeypatch)
        gap_mod.run({"session_id": "sess-f9-tsnull"})

        captured = fake_stderr.getvalue()
        assert "developer" in captured
        assert "可能性" in captured

    def test_int_ts_row_does_not_suppress_real_gap_detection(
        self,
        gap_mod: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        jsonl_path: Path,
        db_path: Path,
        absent_tier_selection_path: Path,
    ) -> None:
        """`{"ts": 12345}`（int）行が混入しても developer 欠落警告が発火したことを確認した。"""
        _patch_paths(
            gap_mod,
            monkeypatch,
            jsonl_path=jsonl_path,
            db_path=db_path,
            tier_selection_path=absent_tier_selection_path,
        )
        old_ts = _prod_ts(datetime.now(timezone.utc) - timedelta(hours=1))
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with jsonl_path.open("a", encoding="utf-8") as f:
            # ts が int（キーは存在するが値が非文字列）の型不正行。
            f.write(
                json.dumps(
                    {
                        "ts": 12345,
                        "session_id": "sess-f9-tsint",
                        "subagent_type": "developer",
                        "role_recorded": "developer",
                        "model_applied": "sonnet",
                        "source": "injected",
                        "prompt_prefix": "",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        # 同一 session の正常な developer 起動行（真の欠落として検知されるべき対象）。
        _append_jsonl_row(
            jsonl_path,
            ts=old_ts,
            session_id="sess-f9-tsint",
            role_recorded="developer",
        )
        # agent_outcomes には記録なし（M=0）。真の欠落が検知されるはず。

        fake_stderr = _capture_stderr(monkeypatch)
        gap_mod.run({"session_id": "sess-f9-tsint"})

        captured = fake_stderr.getvalue()
        assert "developer" in captured
        assert "可能性" in captured
