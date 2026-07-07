"""Tests for .claude/skills/dev-workflow/scripts/record_agent_outcome.py (新規)

tier-routing 学習シグナル再設計（architecture-report-20260702-214748.md §3-4・
ADR-2（C-3 監査改訂版）/ADR-6）に基づく Red フェーズテスト。

対象スクリプトは未作成のため、本ファイルのテストは
「ファイル不在（FileNotFoundError）」で失敗することが正しい Red。

テストが要求するスクリプト契約（developer への実装契約。plan-report /
architecture-report に明記が無い実装詳細は以下の通り固定する）:

- パス: .claude/skills/dev-workflow/scripts/record_agent_outcome.py
- 引数: --role/--outcome/--gate/--execution/--complexity が必須（省略時は
  argparse SystemExit ではなく stderr 警告 + exit 0 で記録スキップ。
  「全エラー exit 0 流儀」を貫くため required=False + main() 内チェックとする）
- --role: _db_params.AGENT_ROLES のいずれか。不正値は警告 + exit 0 + 記録スキップ
- --outcome: success|failure。不正値は警告 + exit 0 + 記録スキップ
- --execution: persona|subagent。不正値は警告 + exit 0 + 記録スキップ
- --complexity: simple|medium|complex。不正値・省略は警告 + exit 0 + 記録スキップ
  （DC-AM-005: tier_selection.json への fallback は実装しない）
- --tier 省略時は module 定数 AGENTS_DIR（.claude/agents/）配下の {role}.md の
  `model:` 行を単一行正規表現でパースし pricing.resolve_tier で正規化する
  （ADR-2 DC-AS-002/003）。解決不能時: --execution=subagent は警告 + 記録スキップ、
  --execution=persona は tier="unknown" でイベントログのみ
- --execution=subagent: update_agent_tier_params（bandit 更新）+
  record_agent_outcome_event の両方を呼ぶ
- --execution=persona: record_agent_outcome_event のみ（bandit 不変）
- dedupe: 同一 (session_id, gate, role, outcome) が直近 5 分以内に既存なら
  2 回目は記録スキップ（agent_outcomes 1 行のまま）。session_id は
  tier_selection.json の "session_id" キーから読む（旧 record_tier_outcome.py
  の session_id 取得経路を踏襲。architecture-report に明記が無いためこの経路を
  契約として固定する）。tier_selection.json が無い/session_id キーが無い場合は
  session_id=None となり dedupe しない（ADR-6: 保守的に必ず記録）
- --gate E-2 の記録時は成否問わず prompt-history.jsonl に 1 行追記する
  （tier_selection.json の prompt_prefix/prompt_hash を使用。無ければ追記しない
  = 旧実装と同じ後方互換規則）。U+2028/U+2029 エスケープは旧実装から移植
- --final: tier_selection.json を削除する（--final 無しでは削除しない）
- DB 不在: exit 0 でクラッシュしない
- モジュール属性名（monkeypatch 対象として固定): TIER_SELECTION_PATH /
  PROMPT_HISTORY_PATH / AGENTS_DIR

Round 1 修正契約（code-review-report-20260703-014202.md [対応予定] 反映）:
- --task <id>（任意引数）: dedupe キーを (session_id, gate, role, outcome, task) に
  拡張する。--task 省略時同士は従来通り (session_id, gate, role, outcome) のみで
  判定（後方互換）。--task 明示時は task も一致した場合のみ duplicate。
  NULL（--task なし）と非 NULL（--task あり）は別物として扱い、どちらも記録される
- --tier override: 明示時も TIERS（haiku/sonnet/opus）外なら警告 + 記録スキップと
  する（frontmatter 経路と同じ扱い）。ただし --execution=persona かつ
  --tier unknown はイベントログ用の明示的な escape 値として許容し記録する
- --execution=persona で --tier 省略時は、frontmatter が解決可能であっても
  tier="unknown" 固定とする（frontmatter は subagent の実使用 tier を表すもので
  あり、persona の実行時には親モデルが効くため fallback すると DC-AS-001 の
  誤帰属が再発する）
- tier 未解決時の stderr（--execution=subagent）は f-string 適用漏れを修正し、
  実際の role 名を含む "agents/{role}.md" 形式（リテラル "{role}" ではない）で
  出力する
- dedupe の busy_timeout は c3_db.BUSY_TIMEOUT_MS を参照する（ハードコード禁止）
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import types
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = (
    WORKTREE_ROOT
    / ".claude"
    / "skills"
    / "dev-workflow"
    / "scripts"
    / "record_agent_outcome.py"
)

# U+2028 LINE SEPARATOR / U+2029 PARAGRAPH SEPARATOR（実体文字を埋め込まず chr() で参照）
_LS = chr(0x2028)
_PS = chr(0x2029)


def _load_hook_module(name: str = "record_agent_outcome_t") -> types.ModuleType:
    """HOOK_PATH からモジュールをロードする。

    HOOK_PATH が存在しない場合（Red フェーズ）は FileNotFoundError を送出する。
    pytest.mark.skipif を使うと未実装時に全テストが SKIP になり「失敗する Red」の
    証跡が残らないため、明示的に例外を送出する設計にする
    （tester/MEMORY.md「.dev/hooks テストの pytestmark skipif 回避パターン」を踏襲）。
    """
    if not HOOK_PATH.is_file():
        raise FileNotFoundError(
            f"record_agent_outcome.py が未作成です（Red フェーズの想定挙動）: {HOOK_PATH}"
        )
    spec = importlib.util.spec_from_file_location(name, HOOK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


def _create_c3_db(db_path: Path) -> None:
    from c3.migrate import apply_pending_migrations

    apply_pending_migrations(db_path)


def _write_agent_frontmatter(agents_dir: Path, role: str, model_line: str) -> None:
    agents_dir.mkdir(parents=True, exist_ok=True)
    content = f"---\nname: {role}\n{model_line}\n---\n\nBody.\n"
    (agents_dir / f"{role}.md").write_text(content, encoding="utf-8")


def _write_tier_selection(path: Path, **fields: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields), encoding="utf-8")


def _count_agent_outcomes(db_path: Path, **where: object) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        clauses = " AND ".join(f"{k} = ?" for k in where)
        sql = "SELECT COUNT(*) FROM agent_outcomes"
        if clauses:
            sql += f" WHERE {clauses}"
        cur = conn.execute(sql, tuple(where.values()))
        return cur.fetchone()[0]
    finally:
        conn.close()


def _record_pragmas(monkeypatch: pytest.MonkeyPatch, mod: types.ModuleType) -> list[str]:
    """mod 内の sqlite3.connect 呼び出しで実行された SQL 文をすべて記録する。

    busy_timeout ハードコード検証（CR-M-002）用。実接続はそのまま行い、
    execute() に渡された SQL 文だけを captured に積む透過プロキシを返す。
    """
    captured: list[str] = []
    real_connect = sqlite3.connect

    class _RecordingConn:
        def __init__(self, real_conn: sqlite3.Connection) -> None:
            self._real = real_conn

        def execute(self, sql, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            captured.append(sql)
            return self._real.execute(sql, *args, **kwargs)

        def close(self) -> None:
            self._real.close()

        def __getattr__(self, name: str):  # noqa: ANN001
            return getattr(self._real, name)

    def _fake_connect(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        return _RecordingConn(real_connect(path, *args, **kwargs))

    monkeypatch.setattr(mod.sqlite3, "connect", _fake_connect)
    return captured


def _latest_agent_outcome(db_path: Path, role: str) -> tuple[str | None, str | None]:
    """role の直近 1 件（id 降順）の (note, gate) を返す。

    ts は秒精度のため同一秒内の複数 insert で順序が曖昧になる
    （tester/MEMORY.md「秒精度 datetime.now() ベースの記録関数」の教訓）。
    AUTOINCREMENT の id 列で確実に最新行を特定する。
    """
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT note, gate FROM agent_outcomes WHERE role = ? "
            "ORDER BY id DESC LIMIT 1",
            (role,),
        )
        return cur.fetchone()
    finally:
        conn.close()


def _bandit_row(db_path: Path, role: str, complexity: str, tier: str):
    """(role, complexity, tier) の agent_outcomes 行から (alpha, beta, trials) を
    Beta(1,1) 事前分布 + 観測で導出して返す（行が無ければ None）。

    旧 ``agent_tier_bandit`` 累積テーブル（migration 005 で DROP 済み・
    フェーズ2.5 ADR-25-4）は本関数が直接読んでいたが、テーブル自体が存在
    しなくなったため ``agent_outcomes`` から同じ数式（alpha=1+succ /
    beta=1+fail / trials=count）で導出する。これにより、本ファイルの
    既存テスト（「record_agent_outcome.py が正しい tier セルにイベントを
    記録したか」を検証する意図）の assertion 形（row[0]/row[1]/row[2] /
    is None 判定）を書き換えずに新スキーマへ移行できる。

    gate によるフィルタは行わない（db.read_agent_tier_params の
    BANDIT_GATES フィルタとは別物。本ヘルパーは record_agent_outcome.py が
    「どの (role, complexity, tier) にイベントを書いたか」の単体確認用）。
    """
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT SUM(success), SUM(1 - success), COUNT(*) FROM agent_outcomes "
            "WHERE role = ? AND task_complexity = ? AND tier = ?",
            (role, complexity, tier),
        )
        succ, fail, trials = cur.fetchone()
        if not trials:
            return None
        return (1.0 + (succ or 0), 1.0 + (fail or 0), trials)
    finally:
        conn.close()


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "c3.db"
    _create_c3_db(p)
    return p


@pytest.fixture()
def agents_dir(tmp_path: Path) -> Path:
    d = tmp_path / "agents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _patch_common(
    mod: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    db_path: Path,
    agents_dir: Path,
    sel_path: Path | None = None,
    history_path: Path | None = None,
    applied_state_path: Path | None = None,
) -> None:
    from c3 import db as c3_db

    monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: db_path)
    monkeypatch.setattr(mod, "AGENTS_DIR", str(agents_dir))
    if sel_path is not None:
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(sel_path))
    if history_path is not None:
        monkeypatch.setattr(mod, "PROMPT_HISTORY_PATH", str(history_path))
    if applied_state_path is not None:
        # T3（フェーズ3）未実装の現行実装は APPLIED_STATE_PATH 属性を持たない
        # ため、この monkeypatch.setattr 自体が AttributeError を送出した
        # （Red フェーズの正しい失敗理由。機能未実装が原因でありテスト側の
        # 誤記ではない）。
        monkeypatch.setattr(mod, "APPLIED_STATE_PATH", str(applied_state_path))


# ---------------------------------------------------------------------------
# Group 1: 必須引数の検証
# ---------------------------------------------------------------------------


class TestRequiredArgs:
    """--role/--outcome/--gate/--execution/--complexity の欠落・不正値検証。"""

    def test_missing_role_warns_and_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        mod = _load_hook_module("rao_missing_role")
        _patch_common(mod, monkeypatch, db_path=db_path, agents_dir=agents_dir)
        rc = mod.main([
            "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert "role" in capsys.readouterr().err.lower()
        assert _count_agent_outcomes(db_path) == 0

    def test_missing_outcome_warns_and_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        mod = _load_hook_module("rao_missing_outcome")
        _patch_common(mod, monkeypatch, db_path=db_path, agents_dir=agents_dir)
        rc = mod.main([
            "--role", "developer", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert _count_agent_outcomes(db_path) == 0

    def test_missing_gate_warns_and_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        mod = _load_hook_module("rao_missing_gate")
        _patch_common(mod, monkeypatch, db_path=db_path, agents_dir=agents_dir)
        rc = mod.main([
            "--role", "developer", "--outcome", "success",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert _count_agent_outcomes(db_path) == 0

    def test_missing_execution_warns_and_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        mod = _load_hook_module("rao_missing_execution")
        _patch_common(mod, monkeypatch, db_path=db_path, agents_dir=agents_dir)
        rc = mod.main([
            "--role", "developer", "--outcome", "success",
            "--gate", "D-2.5", "--complexity", "medium",
        ])
        assert rc == 0
        assert _count_agent_outcomes(db_path) == 0

    def test_missing_complexity_warns_and_exits_zero_no_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """DC-AM-005: --complexity 省略時に tier_selection.json への fallback は
        実装しない。json に complexity が存在していても記録スキップすること。"""
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, complexity="complex", tier="opus")
        mod = _load_hook_module("rao_missing_complexity")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )
        rc = mod.main([
            "--role", "developer", "--outcome", "success",
            "--gate", "D-2.5", "--execution", "subagent",
        ])
        assert rc == 0
        assert _count_agent_outcomes(db_path) == 0

    def test_invalid_role_warns_and_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        mod = _load_hook_module("rao_invalid_role")
        _patch_common(mod, monkeypatch, db_path=db_path, agents_dir=agents_dir)
        rc = mod.main([
            "--role", "not-a-role", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert _count_agent_outcomes(db_path) == 0

    def test_invalid_outcome_warns_and_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        mod = _load_hook_module("rao_invalid_outcome")
        _patch_common(mod, monkeypatch, db_path=db_path, agents_dir=agents_dir)
        rc = mod.main([
            "--role", "developer", "--outcome", "maybe", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert _count_agent_outcomes(db_path) == 0

    def test_invalid_execution_warns_and_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        mod = _load_hook_module("rao_invalid_execution")
        _patch_common(mod, monkeypatch, db_path=db_path, agents_dir=agents_dir)
        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "bogus", "--complexity", "medium",
        ])
        assert rc == 0
        assert _count_agent_outcomes(db_path) == 0

    def test_invalid_complexity_warns_and_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        mod = _load_hook_module("rao_invalid_complexity")
        _patch_common(mod, monkeypatch, db_path=db_path, agents_dir=agents_dir)
        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "huge",
        ])
        assert rc == 0
        assert _count_agent_outcomes(db_path) == 0


# ---------------------------------------------------------------------------
# Group 2: --tier 解決（frontmatter 自己解決・正規化・override・解決不能）
# ---------------------------------------------------------------------------


class TestTierResolution:
    def test_tier_resolved_from_frontmatter_and_normalized(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """agents/developer.md の model: sonnet を resolve_tier で "sonnet" に正規化して
        bandit 更新に使うこと（--tier 省略時）。"""
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_tier_frontmatter")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=tmp_path / "state" / "nonexistent.json",
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        row = _bandit_row(db_path, "developer", "medium", "sonnet")
        assert row is not None
        assert row[0] == pytest.approx(2.0)  # alpha += 1

    def test_tier_override_takes_precedence_over_frontmatter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_tier_override")
        _patch_common(mod, monkeypatch, db_path=db_path, agents_dir=agents_dir)

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
            "--tier", "opus",
        ])
        assert rc == 0
        # override の opus が使われ、frontmatter の sonnet は使われない
        assert _bandit_row(db_path, "developer", "medium", "opus") is not None
        assert _bandit_row(db_path, "developer", "medium", "sonnet") is None

    def test_unresolvable_tier_subagent_skips_recording(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """frontmatter の model 値が TIERS に解決できない場合、
        --execution=subagent では警告 + 記録スキップ（bandit セル汚染防止）。"""
        _write_agent_frontmatter(agents_dir, "developer", "model: claude-future-x1")
        mod = _load_hook_module("rao_tier_unresolvable_subagent")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=tmp_path / "state" / "nonexistent.json",
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert _count_agent_outcomes(db_path, role="developer") == 0

    def test_unresolvable_tier_persona_logs_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """persona では frontmatter が解決不能でも tier="unknown" でイベントログのみ
        記録する（bandit は persona では元々更新しない）。"""
        mod = _load_hook_module("rao_tier_unresolvable_persona")
        _patch_common(mod, monkeypatch, db_path=db_path, agents_dir=agents_dir)
        # agents_dir に architect.md を置かない = 解決不能

        rc = mod.main([
            "--role", "architect", "--outcome", "success", "--gate", "E-1",
            "--execution", "persona", "--complexity", "medium",
        ])
        assert rc == 0
        assert _count_agent_outcomes(
            db_path, role="architect", tier="unknown"
        ) == 1

    def test_missing_agent_file_subagent_skips_recording(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """agents/{role}.md 自体が存在しない場合も解決不能として扱う
        （subagent は記録スキップ）。"""
        mod = _load_hook_module("rao_tier_missing_file_subagent")
        _patch_common(mod, monkeypatch, db_path=db_path, agents_dir=agents_dir)

        rc = mod.main([
            "--role", "tester", "--outcome", "success", "--gate", "D-3",
            "--execution", "subagent", "--complexity", "simple",
        ])
        assert rc == 0
        assert _count_agent_outcomes(db_path, role="tester") == 0

    def test_persona_omitted_tier_always_unknown_even_if_frontmatter_resolvable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """親Claude検出（Round 1）: persona は --tier 省略時、frontmatter が解決
        可能でも tier="unknown" 固定とする。frontmatter は subagent の実使用 tier
        であり、persona の実行時には親モデルが効くため fallback すると
        DC-AS-001 の誤帰属がイベントログに再発するため。"""
        _write_agent_frontmatter(agents_dir, "architect", "model: sonnet")
        mod = _load_hook_module("rao_persona_tier_always_unknown")
        _patch_common(mod, monkeypatch, db_path=db_path, agents_dir=agents_dir)

        rc = mod.main([
            "--role", "architect", "--outcome", "success", "--gate", "E-1",
            "--execution", "persona", "--complexity", "medium",
        ])
        assert rc == 0
        assert _count_agent_outcomes(db_path, role="architect", tier="unknown") == 1
        assert _count_agent_outcomes(db_path, role="architect", tier="sonnet") == 0

    def test_unresolvable_tier_subagent_stderr_shows_actual_role_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """CR-低 f-string 適用漏れ修正: stderr 2 行目の "(agents/{role}.md ..." が
        リテラル "{role}" ではなく実際の role 名で展開されること。"""
        _write_agent_frontmatter(agents_dir, "developer", "model: claude-future-x1")
        mod = _load_hook_module("rao_tier_fstring_fix")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=tmp_path / "state" / "nonexistent.json",
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        err = capsys.readouterr().err
        assert "agents/developer.md" in err
        assert "agents/{role}.md" not in err


# ---------------------------------------------------------------------------
# Group 2b: --tier override 検証（TIERS 外は警告 + 記録スキップ。ただし
# persona の "unknown" は escape 値として許容）
# ---------------------------------------------------------------------------


class TestTierOverrideValidation:
    def test_invalid_tier_override_subagent_skips_recording(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """CR-低 --tier override 未検証: TIERS(haiku/sonnet/opus) 外の値は
        frontmatter 経路と同じく警告 + 記録スキップとする。"""
        mod = _load_hook_module("rao_tier_override_invalid_subagent")
        _patch_common(mod, monkeypatch, db_path=db_path, agents_dir=agents_dir)

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
            "--tier", "gpt4",
        ])
        assert rc == 0
        assert _count_agent_outcomes(db_path, role="developer") == 0

    def test_persona_tier_override_unknown_literal_is_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """persona の --tier unknown は TIERS 外だが明示的な escape 値として
        許容され記録される。"""
        mod = _load_hook_module("rao_tier_override_persona_unknown")
        _patch_common(mod, monkeypatch, db_path=db_path, agents_dir=agents_dir)

        rc = mod.main([
            "--role", "architect", "--outcome", "success", "--gate", "E-1",
            "--execution", "persona", "--complexity", "medium",
            "--tier", "unknown",
        ])
        assert rc == 0
        assert _count_agent_outcomes(db_path, role="architect", tier="unknown") == 1

    def test_persona_tier_override_other_invalid_value_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """persona でも "unknown" 以外の TIERS 外オーバーライドは記録スキップする
        （許容されるのは "unknown" の escape 値のみ）。"""
        mod = _load_hook_module("rao_tier_override_persona_invalid")
        _patch_common(mod, monkeypatch, db_path=db_path, agents_dir=agents_dir)

        rc = mod.main([
            "--role", "architect", "--outcome", "success", "--gate", "E-1",
            "--execution", "persona", "--complexity", "medium",
            "--tier", "gpt4",
        ])
        assert rc == 0
        assert _count_agent_outcomes(db_path, role="architect") == 0


# ---------------------------------------------------------------------------
# Group 3: --execution 分岐（subagent=bandit更新+イベント / persona=イベントのみ）
# ---------------------------------------------------------------------------


class TestExecutionBranch:
    def test_subagent_updates_bandit_and_event_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_exec_subagent")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=tmp_path / "state" / "nonexistent.json",
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert _bandit_row(db_path, "developer", "medium", "sonnet")[2] == 1  # trials
        assert _count_agent_outcomes(db_path, role="developer") == 1

    def test_persona_records_event_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """persona はイベントログのみ記録する。

        フェーズ2.5（ADR-25-4）で agent_tier_bandit 累積テーブル自体が
        DROP され、T4 完了後は execution=="subagent" 分岐も
        update_agent_tier_params を呼ばなくなる。つまり persona/subagent の
        いずれの実行でも「bandit 更新」という別経路はもはや存在しない
        （旧テストが検証していた「persona は bandit 行を作らない」という
        区別自体が構造的に無意味化した）。したがって本テストは
        record_agent_outcome_event が --tier で明示した値・complexity で
        正しく 1 件記録されることのみを検証する。"""
        mod = _load_hook_module("rao_exec_persona")
        _patch_common(mod, monkeypatch, db_path=db_path, agents_dir=agents_dir)

        rc = mod.main([
            "--role", "planner", "--outcome", "failure", "--gate", "E-1",
            "--execution", "persona", "--complexity", "complex",
            "--tier", "opus",
        ])
        assert rc == 0
        assert _count_agent_outcomes(
            db_path, role="planner", task_complexity="complex",
            tier="opus", success=0,
        ) == 1


# ---------------------------------------------------------------------------
# Group 4: dedupe（session_id + gate + role + outcome / 5分）
# ---------------------------------------------------------------------------


class TestDedupe:
    def test_duplicate_within_5min_with_session_id_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, session_id="sess-dedupe-1")
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_dedupe_same_session")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        argv = [
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ]
        assert mod.main(argv) == 0
        assert mod.main(argv) == 0
        assert _count_agent_outcomes(db_path, role="developer") == 1

    def test_no_session_id_does_not_dedupe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """session_id が無い（tier_selection.json 無し）場合は dedupe せず
        2 回とも記録する（ADR-6: 保守的に記録）。"""
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_dedupe_no_session")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=tmp_path / "state" / "nonexistent.json",
        )

        argv = [
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ]
        assert mod.main(argv) == 0
        assert mod.main(argv) == 0
        assert _count_agent_outcomes(db_path, role="developer") == 2


# ---------------------------------------------------------------------------
# Group 4b: --task による dedupe キー拡張（CR-NEW High: dedupe 粒度不足）
# ---------------------------------------------------------------------------


class TestTaskDedupeKey:
    """dedupe キーを (session_id, gate, role, outcome, task) に拡張する契約。

    parallel-agents の 2-F-4/2-E のように同一 wave 内で複数タスクが同一
    gate/role/outcome を持つケースで正当な別イベントが握り潰されないことを
    検証する（code-review-report [CR-NEW] dedupe 粒度不足）。
    """

    def test_different_task_ids_both_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """同一 (session_id, gate, role, outcome) でも --task が異なれば
        2 件とも記録される。"""
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, session_id="sess-task-diff")
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_task_diff_ids")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        base_argv = [
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ]
        assert mod.main(base_argv + ["--task", "task-a"]) == 0
        assert mod.main(base_argv + ["--task", "task-b"]) == 0
        assert _count_agent_outcomes(db_path, role="developer") == 2

    def test_same_task_id_within_5min_deduped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """同一 --task の 5 分内 2 回目は従来通り skip される。"""
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, session_id="sess-task-same")
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_task_same_id")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        argv = [
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
            "--task", "task-x",
        ]
        assert mod.main(argv) == 0
        assert mod.main(argv) == 0
        assert _count_agent_outcomes(db_path, role="developer") == 1

    def test_task_present_vs_absent_treated_as_different(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """NULL（--task なし）と非 NULL（--task あり）は別物として扱い、
        どちらも記録される。"""
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, session_id="sess-task-null-vs-nonnull")
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_task_null_vs_nonnull")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        base_argv = [
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ]
        assert mod.main(base_argv) == 0
        assert mod.main(base_argv + ["--task", "task-y"]) == 0
        assert _count_agent_outcomes(db_path, role="developer") == 2

    def test_legacy_no_task_dedupe_still_works(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """--task 省略時同士は従来通り (session_id, gate, role, outcome) のみで
        dedupe する（後方互換の回帰確認。--task 引数追加後も TestDedupe と
        同じ結果になること）。"""
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, session_id="sess-task-legacy")
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_task_legacy_no_task")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        argv = [
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ]
        assert mod.main(argv) == 0
        assert mod.main(argv) == 0
        assert _count_agent_outcomes(db_path, role="developer") == 1


# ---------------------------------------------------------------------------
# Group 3b: LIKE ワイルドカード誤マッチ防止（ESCAPE 句・code-review-report-
# 20260703-021609.md [対応予定] Medium 対応）
# ---------------------------------------------------------------------------


class TestTaskDedupeLikeEscape:
    """--task に SQL LIKE のワイルドカード文字（% / _）が含まれる場合でも、
    無関係な task が誤って重複判定されないことを検証する（回帰防止）。

    修正前は _is_duplicate() の LIKE 検索が task マーカーをそのまま
    パターン文字列へ埋め込んでおり ESCAPE 句が無かったため、task_id に
    % / _ を含むと無関係な行を誤マッチしていた（レビュアーが sqlite3 で
    実再現: task="abc%" が "abcXYZ" にマッチ、task="task_1" が "taskX1"
    にマッチ）。現在は _escape_like_pattern() で task マーカー中の % / _ /
    \\ をエスケープした上で ESCAPE '\\' 付き LIKE を使うため、この誤マッチは
    発生しない。

    誤マッチが顕在化するのは「後から記録する側の --task」が SQL パターンとして
    使われる場合のみ（LIKE の右辺＝パターン側にのみワイルドカード解釈が効き、
    左辺の note カラム値は常にリテラル比較されるため）。このためテストは
    「ワイルドカード文字を含まない task を先に記録 → ワイルドカード文字を
    含む task を後で記録」の順序で書く（修正前の誤マッチを sqlite3 で
    実再現した際の順序と同じにし、回帰時に確実に検知できるようにしている）。
    """

    def test_underscore_wildcard_task_id_not_confused_with_unrelated_task(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """task="taskX1" を先に記録し、5 分以内に task="task_1" を記録すると
        両方とも記録される。_is_duplicate() は _escape_like_pattern() で
        task マーカー中の "_" をエスケープしてから ESCAPE '\\' 付き LIKE で
        検索するため、"_" が単一文字ワイルドカードとして解釈されて
        "taskX1" に誤マッチすることはない（誤マッチ修正の回帰防止テスト）。"""
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, session_id="sess-like-underscore")
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_like_underscore")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        base_argv = [
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ]
        assert mod.main(base_argv + ["--task", "taskX1"]) == 0
        assert mod.main(base_argv + ["--task", "task_1"]) == 0
        assert _count_agent_outcomes(db_path, role="developer") == 2

    def test_percent_wildcard_task_id_not_confused_with_unrelated_task(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """task="abcXYZ" を先に記録し、5 分以内に task="abc%" を記録すると
        両方とも記録される。_is_duplicate() は _escape_like_pattern() で
        task マーカー中の "%" をエスケープしてから ESCAPE '\\' 付き LIKE で
        検索するため、"%" が任意長ワイルドカードとして解釈されて
        "abcXYZ" に誤マッチすることはない（誤マッチ修正の回帰防止テスト）。"""
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, session_id="sess-like-percent")
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_like_percent")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        base_argv = [
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ]
        assert mod.main(base_argv + ["--task", "abcXYZ"]) == 0
        assert mod.main(base_argv + ["--task", "abc%"]) == 0
        assert _count_agent_outcomes(db_path, role="developer") == 2

    def test_backslash_in_task_id_dedupe_still_correct(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """--task にバックスラッシュを含む場合でも、同一 task の 2 回目は
        skip され、別 task は記録される。SQLite の LIKE はデフォルトでは
        バックスラッシュを特殊文字として扱わないため、現状（ESCAPE 句なし）
        でも正しく動作する。ESCAPE 句導入後の回帰防止ガードとして機能する
        （導入時にバックスラッシュ自体のエスケープを怠ると壊れるテスト）。"""
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, session_id="sess-like-backslash")
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_like_backslash")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        argv_same = [
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
            "--task", "a\\b",
        ]
        assert mod.main(argv_same) == 0
        assert mod.main(argv_same) == 0
        assert _count_agent_outcomes(db_path, role="developer") == 1

        assert mod.main(
            [
                "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
                "--execution", "subagent", "--complexity", "medium",
                "--task", "ab",
            ]
        ) == 0
        assert _count_agent_outcomes(db_path, role="developer") == 2


# ---------------------------------------------------------------------------
# Group 4c: busy_timeout の SSOT 参照（CR-M-002）
# ---------------------------------------------------------------------------


class TestBusyTimeoutConstant:
    def test_dedupe_busy_timeout_uses_c3_db_constant(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """dedupe 内の PRAGMA busy_timeout がハードコード値ではなく
        c3_db.BUSY_TIMEOUT_MS を参照すること。定数を差し替えて実際に発行された
        PRAGMA 文にその値が反映されるかで検証する。"""
        from c3 import db as c3_db

        monkeypatch.setattr(c3_db, "BUSY_TIMEOUT_MS", 12345)
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, session_id="sess-busy-timeout")
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_busy_timeout")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )
        captured = _record_pragmas(monkeypatch, mod)

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        pragma_sqls = [sql for sql in captured if "busy_timeout" in sql.lower()]
        assert pragma_sqls, "dedupe 中に busy_timeout PRAGMA が発行されていない"
        assert any("12345" in sql for sql in pragma_sqls), (
            "busy_timeout は c3_db.BUSY_TIMEOUT_MS を参照すべきだが反映されて"
            f"いない: {pragma_sqls}"
        )


# ---------------------------------------------------------------------------
# Group 5: prompt-history 追記（E-2 のみ・成否問わず）
# ---------------------------------------------------------------------------


class TestPromptHistoryAppend:
    def _write_selection_with_prompt(
        self, path: Path, *, prompt_prefix: str, prompt_hash: str,
        session_id: str = "sess-e2",
    ) -> None:
        _write_tier_selection(
            path,
            prompt_prefix=prompt_prefix,
            prompt_hash=prompt_hash,
            session_id=session_id,
        )

    def test_e2_success_appends_prompt_history(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        sel_path = tmp_path / "state" / "tier_selection.json"
        history_path = tmp_path / "logs" / "prompt-history.jsonl"
        self._write_selection_with_prompt(
            sel_path, prompt_prefix="実装完了の確認", prompt_hash="abc123",
        )
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_e2_success")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path, history_path=history_path,
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "E-2",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert history_path.is_file()
        record = json.loads(history_path.read_text(encoding="utf-8").strip())
        assert record["outcome"] == "success"

    def test_e2_failure_also_appends_prompt_history(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """DC-GP-005: E-2 は成否問わず追記する。"""
        sel_path = tmp_path / "state" / "tier_selection.json"
        history_path = tmp_path / "logs" / "prompt-history.jsonl"
        self._write_selection_with_prompt(
            sel_path, prompt_prefix="差し戻し", prompt_hash="def456",
        )
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_e2_failure")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path, history_path=history_path,
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "failure", "--gate", "E-2",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert history_path.is_file()
        record = json.loads(history_path.read_text(encoding="utf-8").strip())
        assert record["outcome"] == "failure"

    def test_non_e2_gate_does_not_append_prompt_history(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        sel_path = tmp_path / "state" / "tier_selection.json"
        history_path = tmp_path / "logs" / "prompt-history.jsonl"
        self._write_selection_with_prompt(
            sel_path, prompt_prefix="通常ゲート", prompt_hash="ghi789",
        )
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_non_e2")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path, history_path=history_path,
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert not history_path.exists()

    def test_u2028_escaped_in_prompt_history(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """回帰防止移植: prompt_prefix に U+2028 が含まれても
        jsonl の生行には残らないこと（旧 record_tier_outcome.py の実装を移植）。"""
        sel_path = tmp_path / "state" / "tier_selection.json"
        history_path = tmp_path / "logs" / "prompt-history.jsonl"
        self._write_selection_with_prompt(
            sel_path,
            prompt_prefix="前" + _LS + "後",
            prompt_hash="jkl012",
        )
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_u2028")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path, history_path=history_path,
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "E-2",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        raw_content = history_path.read_text(encoding="utf-8")
        raw_line = raw_content.split("\n")[0]
        assert _LS not in raw_line


# ---------------------------------------------------------------------------
# Group 6: --final（tier_selection.json 削除）
# ---------------------------------------------------------------------------


class TestFinalFlag:
    def test_final_deletes_tier_selection_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, session_id="sess-final")
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_final_delete")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "E-2",
            "--execution", "subagent", "--complexity", "medium", "--final",
        ])
        assert rc == 0
        assert not sel_path.exists()

    def test_without_final_keeps_tier_selection_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, session_id="sess-no-final")
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_no_final_keep")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert sel_path.is_file()


# ---------------------------------------------------------------------------
# Group 7: DB 不在
# ---------------------------------------------------------------------------


class TestDbUnavailable:
    def test_db_unavailable_exits_zero_no_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agents_dir: Path,
    ) -> None:
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_db_unavailable")
        from c3 import db as c3_db

        monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: None)
        monkeypatch.setattr(mod, "AGENTS_DIR", str(agents_dir))
        monkeypatch.setattr(
            mod, "TIER_SELECTION_PATH", str(tmp_path / "state" / "nonexistent.json"),
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0


# ---------------------------------------------------------------------------
# Group 8: --note/--gate/--task 長さ上限・秘密情報マスク（Round 4・
# security-review-report-20260703-023754.md [対応予定] F-2 [SR-V-001] /
# F-3 [SR-K-003] 対応）
#
# 実装契約（plan-report-20260703-024104.md fix-note-hardening 準拠）:
# - --note は record_review_decision.py の MAX_REASON_LEN と同水準（2000 文字
#   相当 + バイト上限）へ切り詰める
# - --gate / --task は 200 文字相当へ切り詰める。切り詰め後の値で dedupe 判定
#   の一貫性を保つ（同一 5 分窓で切り詰め後に一致する 2 値は重複扱いになる）
# - --note には select_tier.py の _MASK_PATTERNS と同等の秘密情報マスクを
#   DB 保存前に適用する
# - 適用順は「mask → truncate」（マスク済み文字列を切る）。逆順だと PEM 等の
#   複数行パターンが truncate で分断され検出漏れする
# - note 先頭の [task:<id>] マーカーは mask/truncate の対象外に保つ（マーカー
#   自体を切り詰め・マスクしてしまうと dedupe の LIKE 照合が壊れるため）
# ---------------------------------------------------------------------------


class TestNoteHardening:
    def test_long_note_truncated_to_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """3000 文字超の --note は 2000 文字相当へ切り詰められて保存される。"""
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_long_note_truncated")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=tmp_path / "state" / "nonexistent.json",
        )

        long_note = "N" * 3000
        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
            "--note", long_note,
        ])
        assert rc == 0
        note, _gate = _latest_agent_outcome(db_path, "developer")
        assert note is not None
        assert len(note) <= 2000, (
            f"--note が上限まで切り詰められていない（実長: {len(note)}）"
        )
        assert len(note) < len(long_note), "--note が切り詰められた形跡がない"

    def test_long_gate_truncated_to_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """200 文字超の --gate は 200 文字相当へ切り詰められて保存される。"""
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_long_gate_truncated")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=tmp_path / "state" / "nonexistent.json",
        )

        long_gate = "G" * 300
        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", long_gate,
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        _note, gate = _latest_agent_outcome(db_path, "developer")
        assert gate is not None
        assert len(gate) <= 200, (
            f"--gate が上限まで切り詰められていない（実長: {len(gate)}）"
        )
        assert len(gate) < len(long_gate), "--gate が切り詰められた形跡がない"

    def test_long_task_truncated_and_dedupe_consistent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """--task が 200 文字超のとき切り詰め後の値で dedupe 判定が一貫する。

        200 文字までは同一で末尾のみ異なる 2 つの --task を渡すと、切り詰め後は
        同一文字列になるため、2 回目は重複として skip される（1 件のみ記録）。
        現行（切り詰め未実装）では切り詰め前の値がそのまま異なるため 2 件とも
        記録されてしまい、このテストは正しい理由で失敗する。
        """
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, session_id="sess-task-truncate-dedupe")
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_task_truncate_dedupe")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        base = "T" * 200
        task_1 = base + "AAAA"
        task_2 = base + "BBBB"
        base_argv = [
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ]
        assert mod.main(base_argv + ["--task", task_1]) == 0
        assert mod.main(base_argv + ["--task", task_2]) == 0
        assert _count_agent_outcomes(db_path, role="developer") == 1, (
            "切り詰め後に一致するはずの --task が別物として扱われている"
            "（dedupe が切り詰め前の値を見ている疑い）"
        )

    def test_note_masks_password(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """--note 中の password=xxx は *** にマスクされて保存される。"""
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_note_mask_password")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=tmp_path / "state" / "nonexistent.json",
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
            "--note", "設定ミスで password=secret123 が残っていた",
        ])
        assert rc == 0
        note, _gate = _latest_agent_outcome(db_path, "developer")
        assert note is not None
        assert "secret123" not in note
        assert "password=***" in note

    def test_note_masks_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """--note 中の api_key=xxx は *** にマスクされて保存される。"""
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_note_mask_api_key")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=tmp_path / "state" / "nonexistent.json",
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
            "--note", "コード片に api_key=abcdef123456 がハードコードされていた",
        ])
        assert rc == 0
        note, _gate = _latest_agent_outcome(db_path, "developer")
        assert note is not None
        assert "abcdef123456" not in note
        assert "api_key=***" in note

    def test_note_masks_bearer_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """--note 中の Bearer トークンは *** にマスクされて保存される。"""
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_note_mask_bearer")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=tmp_path / "state" / "nonexistent.json",
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
            "--note", "Authorization: Bearer abc.def-ghi123 が引用されていた",
        ])
        assert rc == 0
        note, _gate = _latest_agent_outcome(db_path, "developer")
        assert note is not None
        assert "abc.def-ghi123" not in note
        assert "Bearer ***" in note

    def test_marker_preserved_after_mask_and_truncate_with_dedupe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """マスク・切り詰め後も note 先頭の [task:<id>] マーカーは保全され、
        dedupe が機能する（マーカー自体はマスク/切り詰めの対象外）。"""
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, session_id="sess-marker-preserved")
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_marker_preserved")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        argv = [
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
            "--task", "task-secure",
            "--note", "password=hunter2 が混入していた",
        ]
        assert mod.main(argv) == 0
        note, _gate = _latest_agent_outcome(db_path, "developer")
        assert note is not None
        assert note.startswith("[task:task-secure]"), (
            f"マスク/切り詰め処理でマーカーが破壊されている: {note!r}"
        )
        assert "hunter2" not in note
        assert "password=***" in note

        # 同一 (session_id, gate, role, outcome, task) の 2 回目は dedupe される。
        assert mod.main(argv) == 0
        assert _count_agent_outcomes(db_path, role="developer") == 1, (
            "マーカーがマスク/切り詰めで変質し dedupe の LIKE 照合が壊れている疑い"
        )

    def test_mask_before_truncate_order_pem_not_split(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """適用順は mask→truncate（マスク済み文字列を切る）であること。

        PEM ブロック（BEGIN...END）を含む長大な note を渡す。もし truncate が
        mask より先に適用されると、2000 文字上限で PEM の END タグより手前
        (仮の秘密情報である "A" の羅列の途中) で切られてしまい、
        BEGIN...END にまたがる _MASK_PATTERNS の正規表現がマッチしなくなり
        secret 相当の内容がマスクされないまま漏洩する。
        逆に mask→truncate の順であれば PEM ブロックは短い "***" 表現に
        圧縮された後に truncate されるため、2000 文字には収まりきり、
        末尾の suffix テキストまで保存される。
        """
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_mask_before_truncate_order")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=tmp_path / "state" / "nonexistent.json",
        )

        secret_filler = "A" * 3000
        note = (
            "prefix text\n"
            "-----BEGIN PRIVATE KEY-----\n"
            f"{secret_filler}\n"
            "-----END PRIVATE KEY-----\n"
            "suffix text"
        )
        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
            "--note", note,
        ])
        assert rc == 0
        stored, _gate = _latest_agent_outcome(db_path, "developer")
        assert stored is not None
        assert "AAAA" not in stored, (
            "PEM ブロック内の秘密情報がマスクされずに残っている"
            "（mask が truncate より後に適用されている疑い）"
        )
        assert "suffix text" in stored, (
            "PEM 圧縮前に truncate が先に走り、末尾テキストが失われている"
            "（truncate が mask より先に適用されている疑い）"
        )
        assert "-----END PRIVATE KEY-----" in stored


# ---------------------------------------------------------------------------
# Group 9: soft-apply tier 解決（architecture-report-20260703-081149.md §3-1・
# ADR-AS-1〜ADR-AS-4・plan-report-20260703-082727.md A1 観点 1〜7）
#
# 対象: developer + --execution subagent + --tier 省略時、tier_selection.json
# の tier（無ければ suggested_model）を resolve_tier で正規化して優先 2 として
# 採用する（_SOFT_APPLY_ROLES = ("developer",) の gating。ADR-AS-1）。
#
# 現行実装（本ファイル対象の record_agent_outcome.py）は tier_selection.json
# を tier 解決に一切使わず常に frontmatter を解決するため、観点 1
# （TestSoftApplyTierResolution 全 3 件）は現行実装に対して **赤**。
# fallback（観点 2）・エスケープハッチ（観点 3）・並列 --tier 明示（観点
# 3-B）・非対象 role 負テスト（観点 4）・帰属語彙拡張（観点 5）・persona
# 不変（観点 6）・不変性回帰（観点 7）は、現行実装がもともと
# tier_selection.json を読まない（＝常にフロントマター/明示 --tier のみを
# 見る）ため新規挙動を要求せず、**現行実装に対して緑**（soft-apply 実装
# 後の非破壊確認のための回帰保護として先行追加する）。
# どの観点が赤/緑かは test-report に実行結果として明記する。
# ---------------------------------------------------------------------------


class TestSoftApplyTierResolution:
    """観点 1: developer subagent の soft-apply 解決（tier_selection.json 優先 2）。"""

    def test_soft_apply_haiku_from_tier_selection_wins_over_frontmatter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """developer + subagent + --tier 省略 + tier_selection.json tier=haiku
        + frontmatter model: sonnet → (developer, *, haiku) で記録される
        （frontmatter の sonnet にはならない）。"""
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, tier="haiku", session_id="sess-soft-haiku")
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_soft_apply_haiku")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert _bandit_row(db_path, "developer", "medium", "haiku") is not None, (
            "soft-apply 未実装のため tier_selection.json の tier=haiku が"
            "無視されている（Red フェーズの想定挙動）"
        )
        assert _bandit_row(db_path, "developer", "medium", "sonnet") is None, (
            "frontmatter の sonnet に fallback してしまっている"
            "（soft-apply 未実装時の現行挙動）"
        )

    def test_soft_apply_opus_from_tier_selection_wins_over_frontmatter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """tier=opus でも同様（探索データが正しい tier セルに帰属すること）。"""
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, tier="opus", session_id="sess-soft-opus")
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_soft_apply_opus")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "complex",
        ])
        assert rc == 0
        assert _bandit_row(db_path, "developer", "complex", "opus") is not None
        assert _bandit_row(db_path, "developer", "complex", "sonnet") is None

    def test_soft_apply_falls_back_to_suggested_model_when_tier_key_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """tier_selection.json に "tier" キーが無く "suggested_model" のみの
        場合でも soft-apply は suggested_model を採用する（ADR-AS-1: tier
        無ければ suggested_model）。"""
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(
            sel_path, suggested_model="haiku", session_id="sess-soft-suggested",
        )
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_soft_apply_suggested_model")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert _bandit_row(db_path, "developer", "medium", "haiku") is not None


class TestSoftApplyFallback:
    """観点 2: fallback（tier_selection.json 不在 / tier 欠落 / 不正値 →
    frontmatter）。現行実装は元々 tier_selection.json を一切読まないため、
    以下はいずれも現行実装に対して緑（回帰保護テスト）。soft-apply 実装後も
    frontmatter fallback の宛先が変わらないことを固定する。
    """

    def test_fallback_when_tier_selection_file_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_soft_fallback_absent")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=tmp_path / "state" / "nonexistent.json",
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert _bandit_row(db_path, "developer", "medium", "sonnet") is not None

    def test_fallback_when_tier_and_suggested_model_both_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """tier_selection.json は存在するが tier/suggested_model キーが無い
        （session_id のみ）場合も frontmatter fallback する。"""
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, session_id="sess-fallback-nofield")
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_soft_fallback_nofield")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert _bandit_row(db_path, "developer", "medium", "sonnet") is not None

    def test_fallback_when_tier_value_unresolvable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """tier_selection.json の tier が resolve_tier で正規化不能な値
        （TIERS 語彙外）の場合、frontmatter fallback する。"""
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(
            sel_path, tier="claude-future-x1", session_id="sess-fallback-invalid",
        )
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_soft_fallback_invalid")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert _bandit_row(db_path, "developer", "medium", "sonnet") is not None


class TestSoftApplyEscapeHatch:
    """観点 3: エスケープハッチ（ADR-AS-2）。--tier 明示（優先 1）は
    tier_selection.json の soft-apply（優先 2）より常に優先する。現行実装は
    元々 --tier 優先 1 を実装済みのため、この観点は現行実装に対して緑
    （soft-apply 実装後も優先順位が壊れないことを固定する回帰テスト）。
    """

    def test_explicit_tier_overrides_soft_apply_tier_selection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, tier="haiku", session_id="sess-escape")
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_escape_hatch")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
            "--tier", "sonnet",
        ])
        assert rc == 0
        assert _bandit_row(db_path, "developer", "medium", "sonnet") is not None
        assert _bandit_row(db_path, "developer", "medium", "haiku") is None


class TestParallelExplicitTierResolution:
    """観点 3-B（C-3 DC-GP-001 対応・ADR-AS-4）: 並列（worktree）経路は親が
    --tier を明示して申告する。tier_selection.json の状態（不在／別値）に
    かかわらず --tier の値がそのまま記録されることを固定する（worktree の
    state 分離の影響を受けないことの単体保証）。現行実装は既に --tier 優先 1
    を実装済みのため、この観点は現行実装に対して緑（並列経路対応の追加
    コードが不要であること自体を裏付ける回帰テスト）。
    """

    def test_explicit_tier_wins_when_tier_selection_file_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_parallel_tier_absent_selection")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=tmp_path / "state" / "nonexistent.json",
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-3",
            "--execution", "subagent", "--complexity", "medium",
            "--tier", "haiku",
        ])
        assert rc == 0
        assert _bandit_row(db_path, "developer", "medium", "haiku") is not None

    def test_explicit_tier_wins_over_different_tier_selection_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """tier_selection.json が存在し別値（opus）を持っていても
        --tier haiku が優先され、tier_selection.json は読まれない。"""
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, tier="opus", session_id="sess-parallel-diff")
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_parallel_tier_diff_selection")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-3",
            "--execution", "subagent", "--complexity", "medium",
            "--tier", "haiku",
        ])
        assert rc == 0
        assert _bandit_row(db_path, "developer", "medium", "haiku") is not None
        assert _bandit_row(db_path, "developer", "medium", "opus") is None


class TestSoftApplyRoleGating:
    """観点 4（重要）: 非対象 role（tester）は tier_selection.json を読まない。
    _SOFT_APPLY_ROLES = ("developer",) の gating 固定。現行実装はそもそも
    tier_selection.json を tier 解決に使わないため、この観点は現行実装に
    対して緑（soft-apply 実装後も tester が巻き込まれないことを固定する
    回帰テスト・最重要ケース）。
    """

    def test_tester_role_ignores_tier_selection_and_resolves_frontmatter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, tier="haiku", session_id="sess-tester-gating")
        _write_agent_frontmatter(agents_dir, "tester", "model: sonnet")
        mod = _load_hook_module("rao_tester_role_gating")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        rc = mod.main([
            "--role", "tester", "--outcome", "success", "--gate", "D-3",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert _bandit_row(db_path, "tester", "medium", "sonnet") is not None, (
            "tester は frontmatter 解決のままであるべき"
        )
        assert _bandit_row(db_path, "tester", "medium", "haiku") is None, (
            "tester が tier_selection.json の tier=haiku を読んでしまっている"
            "（role gating の欠陥）"
        )


class TestAttributionVocabularyTester:
    """観点 5（§3-7 帰属語彙拡張）: tester + subagent + gate=E-1/E-2 +
    outcome=failure → tester bandit セル更新・frontmatter=sonnet 解決。
    D-3 の欠陥所在分岐で --role tester / --role developer がそれぞれ正しい
    role セルに記録されること。現行実装は role・gate 名で分岐せず汎用的に
    動くため、この観点は現行実装に対して緑（帰属語彙の拡張が
    record_agent_outcome.py 側の新規コードを要さないことの裏付け）。
    """

    @pytest.mark.parametrize("gate", ["E-1", "E-2"])
    def test_tester_failure_recorded_to_tester_cell(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path, gate: str,
    ) -> None:
        _write_agent_frontmatter(agents_dir, "tester", "model: sonnet")
        mod = _load_hook_module(f"rao_attrib_tester_{gate.replace('-', '_')}")
        _patch_common(mod, monkeypatch, db_path=db_path, agents_dir=agents_dir)

        rc = mod.main([
            "--role", "tester", "--outcome", "failure", "--gate", gate,
            "--execution", "subagent", "--complexity", "medium",
            "--note", "テストコード欠陥によりアサーションが誤っていた",
        ])
        assert rc == 0
        row = _bandit_row(db_path, "tester", "medium", "sonnet")
        assert row is not None
        assert row[1] == pytest.approx(2.0)  # beta += 1（failure）

    def test_d3_role_gating_developer_vs_tester_distinct_cells(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """D-3 の欠陥所在判定: --role developer / --role tester がそれぞれ
        正しい role の bandit セルに記録され、互いに混線しない。"""
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        _write_agent_frontmatter(agents_dir, "tester", "model: sonnet")
        mod = _load_hook_module("rao_d3_role_distinct_cells")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=tmp_path / "state" / "nonexistent.json",
        )

        rc1 = mod.main([
            "--role", "developer", "--outcome", "failure", "--gate", "D-3",
            "--execution", "subagent", "--complexity", "medium",
            "--note", "プロダクトコード欠陥",
        ])
        rc2 = mod.main([
            "--role", "tester", "--outcome", "failure", "--gate", "D-3",
            "--execution", "subagent", "--complexity", "medium",
            "--note", "テストコード欠陥",
        ])
        assert rc1 == 0 and rc2 == 0
        dev_row = _bandit_row(db_path, "developer", "medium", "sonnet")
        tester_row = _bandit_row(db_path, "tester", "medium", "sonnet")
        assert dev_row is not None and dev_row[1] == pytest.approx(2.0)
        assert tester_row is not None and tester_row[1] == pytest.approx(2.0)


class TestPersonaInvariantIgnoresTierSelection:
    """観点 6: persona は --tier 省略時、tier_selection.json に tier
    フィールドがあっても "unknown" 固定のまま（tier_selection.json を
    読まない）。現行実装は persona を常に "unknown" 固定しているため、この
    観点は現行実装に対して緑（soft-apply 実装後も persona が巻き込まれ
    ないことを固定する回帰テスト）。
    """

    def test_persona_stays_unknown_even_when_tier_selection_has_tier(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, tier="haiku", session_id="sess-persona-gating")
        mod = _load_hook_module("rao_persona_ignores_tier_selection")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        rc = mod.main([
            "--role", "architect", "--outcome", "success", "--gate", "E-1",
            "--execution", "persona", "--complexity", "medium",
        ])
        assert rc == 0
        assert _count_agent_outcomes(db_path, role="architect", tier="unknown") == 1
        assert _count_agent_outcomes(db_path, role="architect", tier="haiku") == 0


class TestSoftApplyInvarianceRegression:
    """観点 7: 不変性回帰。soft-apply 経路（tier_selection.json に tier
    フィールドがある状態）でも dedupe / exit 0 / E-2 prompt-history /
    --final の既存挙動が壊れないこと。現行実装ではこれらの経路は soft-apply
    未実装でも同一コードパスを通るため、この観点は現行実装に対して緑
    （soft-apply 実装後の非破壊確認のベースライン）。
    """

    def test_dedupe_key_unaffected_by_soft_apply_tier_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """tier 解決元（soft-apply か frontmatter か）が変わっても dedupe
        キー (session_id, gate, role, outcome, task) は tier を含まないため、
        同一イベントの 2 回目は skip される。"""
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(
            sel_path, tier="haiku", session_id="sess-soft-dedupe",
        )
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_soft_dedupe")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        argv = [
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ]
        assert mod.main(argv) == 0
        assert mod.main(argv) == 0
        assert _count_agent_outcomes(db_path, role="developer") == 1

    def test_e2_prompt_history_still_appends_with_soft_apply_selection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        sel_path = tmp_path / "state" / "tier_selection.json"
        history_path = tmp_path / "logs" / "prompt-history.jsonl"
        _write_tier_selection(
            sel_path, tier="haiku", session_id="sess-soft-e2",
            prompt_prefix="ソフト適用確認", prompt_hash="soft001",
        )
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_soft_e2_history")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path, history_path=history_path,
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "E-2",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert history_path.is_file()
        record = json.loads(history_path.read_text(encoding="utf-8").strip())
        assert record["outcome"] == "success"

    def test_final_flag_deletes_selection_even_when_soft_apply_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, tier="haiku", session_id="sess-soft-final")
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_soft_final")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "E-2",
            "--execution", "subagent", "--complexity", "medium", "--final",
        ])
        assert rc == 0
        assert not sel_path.exists()


# ---------------------------------------------------------------------------
# Group 10: CR-E-001 回帰テスト（code-review-report-20260703-094145.md）
#
# soft-apply tier 解決（優先 2・_SOFT_APPLY_ROLES）が tier_selection.json の
# "tier"/"suggested_model" フィールドへ型チェックを行わず c3_pricing.resolve_tier
# へ渡している欠陥（record_agent_outcome.py:561-566）の回帰テスト。
# resolve_tier(model: str) は内部で無条件に model.lower() を呼ぶため、フィールド
# 値が非文字列（int/list/dict）だと AttributeError が main() から非捕捉のまま
# 伝播し、「全エラー exit 0 流儀」の不変が破られる。
#
# 現行実装（isinstance ガード未実装）に対しては、mod.main() 呼び出し自体が
# AttributeError を送出してテスト実行が異常終了するため、以下は正しい理由で
# 赤になる（Red フェーズの想定挙動）。F1 で isinstance(soft_apply_raw, str)
# ガードを追加し、非文字列値では frontmatter fallback（優先 3）へ落とすことで
# 緑になる。
# ---------------------------------------------------------------------------


class TestSoftApplyNonStringTierGuard:
    """CR-E-001: tier_selection.json の tier/suggested_model が非文字列でも
    クラッシュせず exit 0・frontmatter へ fallback して記録されること。"""

    def test_non_string_tier_int_falls_back_to_frontmatter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """tier が int（例: 123）の場合。resolve_tier(123) は 123.lower() で
        AttributeError を送出するため、ガード未実装の現行実装では
        mod.main() 呼び出し自体が例外で異常終了し、本テストは赤になる。"""
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, tier=123, session_id="sess-nonstr-tier-int")
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_nonstring_tier_int")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert _bandit_row(db_path, "developer", "medium", "sonnet") is not None, (
            "非文字列 tier(int) で AttributeError がクラッシュせず frontmatter "
            "(sonnet) へ fallback すること"
        )

    def test_non_string_tier_list_falls_back_to_frontmatter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """tier が list（例: ["opus"]）の場合。同様に list には .lower() が無く
        AttributeError となるため、現行実装では赤になる。"""
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(
            sel_path, tier=["opus"], session_id="sess-nonstr-tier-list",
        )
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_nonstring_tier_list")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert _bandit_row(db_path, "developer", "medium", "sonnet") is not None, (
            "非文字列 tier(list) で AttributeError がクラッシュせず frontmatter "
            "(sonnet) へ fallback すること"
        )

    def test_non_string_tier_dict_falls_back_to_frontmatter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """tier が dict（例: {"x": 1}）の場合。同様に dict には .lower() が無く
        AttributeError となるため、現行実装では赤になる。"""
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(
            sel_path, tier={"x": 1}, session_id="sess-nonstr-tier-dict",
        )
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_nonstring_tier_dict")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert _bandit_row(db_path, "developer", "medium", "sonnet") is not None, (
            "非文字列 tier(dict) で AttributeError がクラッシュせず frontmatter "
            "(sonnet) へ fallback すること"
        )

    def test_non_string_suggested_model_falls_back_to_frontmatter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """"tier" キーが無く "suggested_model" が非文字列（例: 123）の場合も
        同じ経路（soft_apply_raw = selection.get("tier") or
        selection.get("suggested_model")）を通るため、現行実装では赤になる。"""
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(
            sel_path, suggested_model=123, session_id="sess-nonstr-suggested",
        )
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_nonstring_suggested_model")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert _bandit_row(db_path, "developer", "medium", "sonnet") is not None, (
            "非文字列 suggested_model(int) で AttributeError がクラッシュせず "
            "frontmatter (sonnet) へ fallback すること"
        )


# ---------------------------------------------------------------------------
# Group 11（フェーズ2.5・T3 test-record-skill・plan-report-20260703-151314.md
# T3 / architecture-report-20260703-150507.md ADR-25-4・ADR-25-6）
#
# 対象: (1) execution=="subagent" 分岐から update_agent_tier_params 呼び出しが
# 削除され persona/subagent 両分岐が record_agent_outcome_event のみへ縮退する
# こと。(2)(3) D-2.5-stuck の記録経路と D-2.5 本体との dedupe 非衝突（共存）。
#
# 現行実装（T4 未実施）は L634-638 相当で依然 update_agent_tier_params を
# 呼んでおり、c3.db から当該関数は T2 で削除済みのため AttributeError が
# 発生し記録全体がスキップされる。よって本節のテストは正しい理由
# （T4 未実施＝機能未実装）で赤になる（tester/T3 完了条件）。
# ---------------------------------------------------------------------------


class TestUpdateCallRemoved:
    """ADR-25-4: subagent 記録は update_agent_tier_params を呼ばず
    record_agent_outcome_event のみで完結すること。"""

    def test_subagent_records_event_without_update_agent_tier_params(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        from c3 import db as c3_db

        assert not hasattr(c3_db, "update_agent_tier_params"), (
            "c3.db.update_agent_tier_params は T2 で削除済みのはず"
            "（このテストの前提が崩れている）"
        )
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_update_call_removed")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=tmp_path / "state" / "nonexistent.json",
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert _count_agent_outcomes(
            db_path, role="developer", task_complexity="medium", tier="sonnet",
        ) == 1, (
            "record_agent_outcome.py が依然 update_agent_tier_params を呼んで"
            "おり、c3.db から削除済み（T2 完了）のため AttributeError で記録"
            "全体がスキップされている（T4 で呼び出しを削除すれば緑化する想定"
            "の Red）"
        )

    def test_subagent_does_not_raise_attribute_error_internally(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """T4 完了後は update_agent_tier_params 呼び出し自体が無くなるため、
        stderr に AttributeError 由来の recording failed ログが出ないこと。"""
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_no_attribute_error_stderr")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=tmp_path / "state" / "nonexistent.json",
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        err = capsys.readouterr().err
        assert "AttributeError" not in err, (
            "update_agent_tier_params 呼び出しの残骸で AttributeError が"
            f"発生している（T4 待ち）: {err!r}"
        )

    def test_persona_branch_also_reduces_to_event_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """persona 分岐は元々 record_agent_outcome_event のみを呼んでおり、
        T4 後も subagent と同じ「記録のみ」に統一される（縮退の対称性）。
        本テストは現行実装でも persona 経路自体は既に緑のはずだが、
        「両分岐が記録のみへ縮退したこと」を明示的に固定する回帰ガードとして
        Group 11 に置く。"""
        mod = _load_hook_module("rao_persona_reduced_to_event_only")
        _patch_common(mod, monkeypatch, db_path=db_path, agents_dir=agents_dir)

        rc = mod.main([
            "--role", "architect", "--outcome", "success", "--gate", "E-1",
            "--execution", "persona", "--complexity", "medium",
        ])
        assert rc == 0
        assert _count_agent_outcomes(
            db_path, role="architect", tier="unknown", success=1,
        ) == 1


class TestStuckGateRecording:
    """ADR-25-6: D-2.5-stuck の failure 記録（--tier 省略・soft-apply 解決）が
    agent_outcomes に記録され、記録後の導出 read_agent_tier_params（db.py・
    BANDIT_GATES フィルタ）に D-2.5-stuck が算入されること（β 増加）。"""

    def test_stuck_gate_failure_recorded_with_soft_apply_tier(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, tier="opus", session_id="sess-stuck-1")
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_stuck_gate")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "failure",
            "--gate", "D-2.5-stuck", "--execution", "subagent",
            "--complexity", "medium",
            "--note", "stuck: debug-needed 検出（自力完走不能）",
        ])
        assert rc == 0
        assert _count_agent_outcomes(
            db_path, role="developer", gate="D-2.5-stuck",
            task_complexity="medium", tier="opus", success=0,
        ) == 1, (
            "D-2.5-stuck failure が soft-apply 解決 tier(opus) で記録される"
            "べきだが、update_agent_tier_params 呼び出し残存の AttributeError "
            "で記録全体がスキップされている（T4 待ち）"
        )

    def test_stuck_failure_increases_beta_in_derived_bandit_params(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """記録した D-2.5-stuck failure が read_agent_tier_params（BANDIT_GATES
        経由の導出集計）の beta に反映されること（ADR-25-1: D-2.5-stuck は
        BANDIT_GATES に含まれる客観 gate）。"""
        from c3 import db as c3_db

        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, tier="opus", session_id="sess-stuck-beta")
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_stuck_beta_increase")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )
        monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: db_path)

        rc = mod.main([
            "--role", "developer", "--outcome", "failure",
            "--gate", "D-2.5-stuck", "--execution", "subagent",
            "--complexity", "medium",
        ])
        assert rc == 0
        params = c3_db.read_agent_tier_params("developer", "medium", db_path=db_path)
        alpha, beta, trials = params["opus"]
        assert trials == 1, (
            "D-2.5-stuck failure が導出集計（BANDIT_GATES）に算入されていない"
            "（record_agent_outcome.py 側の記録自体が T4 待ちで欠落している"
            "疑い）"
        )
        assert beta == pytest.approx(2.0), "beta（1.0+fail）が増加していない"
        assert alpha == pytest.approx(1.0), "failure のため alpha は増加しない"


class TestStuckAndD25Coexist:
    """ADR-25-6・plan T3-3: 同一 session_id で D-2.5-stuck(failure) と
    D-2.5(success) が別 gate として共存記録されること（dedupe が gate 違いを
    弾かないこと）。C-3 DC-AM-002 対応: 本単体テストが stuck 記録経路の
    唯一の権威的保証（ワークフロー内の非決定的な発生に頼らない）。"""

    def test_stuck_failure_and_d25_success_both_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, tier="opus", session_id="sess-stuck-dedupe")
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_stuck_dedupe_coexist")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        rc_stuck = mod.main([
            "--role", "developer", "--outcome", "failure",
            "--gate", "D-2.5-stuck", "--execution", "subagent",
            "--complexity", "medium",
        ])
        rc_success = mod.main([
            "--role", "developer", "--outcome", "success",
            "--gate", "D-2.5", "--execution", "subagent",
            "--complexity", "medium",
        ])
        assert rc_stuck == 0 and rc_success == 0
        assert _count_agent_outcomes(
            db_path, role="developer", gate="D-2.5-stuck", success=0,
        ) == 1, "D-2.5-stuck(failure) が dedupe に弾かれて記録されていない"
        assert _count_agent_outcomes(
            db_path, role="developer", gate="D-2.5", success=1,
        ) == 1, "D-2.5(success) が D-2.5-stuck と衝突して記録されていない"

    def test_same_session_stuck_twice_within_5min_still_deduped_by_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        """同一 (session_id, gate=D-2.5-stuck, role, outcome) が 5 分以内に
        2 回発生した場合は dedupe が効き 1 件のみ記録される（gate 単位の
        dedupe が壊れていないことの回帰確認。stuck という gate が特別扱いで
        dedupe をすり抜けないことを固定する）。"""
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, tier="opus", session_id="sess-stuck-twice")
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_stuck_twice_dedupe")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
        )

        argv = [
            "--role", "developer", "--outcome", "failure",
            "--gate", "D-2.5-stuck", "--execution", "subagent",
            "--complexity", "medium",
        ]
        assert mod.main(argv) == 0
        assert mod.main(argv) == 0
        assert _count_agent_outcomes(
            db_path, role="developer", gate="D-2.5-stuck",
        ) == 1


# ---------------------------------------------------------------------------
# Group 12（フェーズ3・T3・plan-report-20260707-065732.md test-record-priority・
# architecture-report-20260707-065043.md §4・§7-2）
#
# 対象: tier_autoapply.py（T1・実装済み）が書く applied-state
# （.claude/state/tier_autoapply.jsonl）の実適用値（model_applied）を、
# record の tier 解決で新・優先2として採用した。旧・優先2（tier_selection.json
# の tier→suggested_model）は優先3へ降格した（§4-1）。
#
# 現行実装（本ファイル対象の record_agent_outcome.py・T3 未実施）は
# APPLIED_STATE_PATH モジュール属性も _read_applied_tier() 関数も持たない。
# 本 Group のテストはいずれも _patch_common(..., applied_state_path=...) 内の
# monkeypatch.setattr(mod, "APPLIED_STATE_PATH", ...) が AttributeError を
# 送出して赤になった、または（TestAppliedStatePathResolution のみ）
# mod.APPLIED_STATE_PATH への直接アクセスが AttributeError を送出して赤に
# なった（いずれも T3 未実装＝機能未実装が単一の原因であり、テスト側の
# タイポ・記法崩れによる赤ではない）。
# ---------------------------------------------------------------------------


def _write_applied_state(path: Path, *rows: dict) -> None:
    """applied-state（tier_autoapply.jsonl 相当）へ複数行を書き込んだ（テスト用）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(row, ensure_ascii=False) for row in rows]
    content = "\n".join(lines)
    if lines:
        content += "\n"
    path.write_text(content, encoding="utf-8")


def _write_applied_state_raw_lines(path: Path, lines: list[str]) -> None:
    """生テキスト行（壊れ行を含む）をそのまま applied-state へ書き込んだ（テスト用）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestAppliedStatePriority:
    """優先2（新規・§4-1）: developer + tier_selection.json あり + applied-state
    （同一 session_id・role_recorded 一致の行）あり → applied-state の
    model_applied を採用し、tier_selection.json（優先3へ降格）は採用され
    なかった（適用者=記録 SSOT）。"""

    def test_applied_state_wins_over_tier_selection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        sel_path = tmp_path / "state" / "tier_selection.json"
        applied_path = tmp_path / "state" / "tier_autoapply.jsonl"
        _write_tier_selection(sel_path, tier="sonnet", session_id="sess-applied-1")
        _write_applied_state(
            applied_path,
            {
                "ts": "2026-07-07T08:00:00+00:00",
                "session_id": "sess-applied-1",
                "subagent_type": "developer",
                "role_recorded": "developer",
                "model_applied": "haiku",
                "source": "injected",
                "prompt_prefix": "",
            },
        )
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_applied_state_wins")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path, applied_state_path=applied_path,
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert _bandit_row(db_path, "developer", "medium", "haiku") is not None, (
            "applied-state 未実装のため tier_selection.json の sonnet が"
            "採用された（Red フェーズの想定挙動）"
        )
        assert _bandit_row(db_path, "developer", "medium", "sonnet") is None, (
            "applied-state（新優先2）より tier_selection.json（優先3へ降格"
            "したはず）が優先されている"
        )


class TestAppliedStateLatestRowSelection:
    """同一 session_id×role_recorded の複数行がある場合は最新（ts 最大・末尾側）の
    model_applied を採用し、別 session の行は突合対象から無視した。"""

    def test_latest_row_among_multiple_same_session_rows_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        sel_path = tmp_path / "state" / "tier_selection.json"
        applied_path = tmp_path / "state" / "tier_autoapply.jsonl"
        _write_tier_selection(sel_path, tier="sonnet", session_id="sess-applied-latest")
        _write_applied_state(
            applied_path,
            {
                "ts": "2026-07-07T08:00:00+00:00",
                "session_id": "sess-applied-latest",
                "subagent_type": "developer",
                "role_recorded": "developer",
                "model_applied": "opus",
                "source": "injected",
                "prompt_prefix": "",
            },
            {
                "ts": "2026-07-07T08:05:00+00:00",
                "session_id": "sess-applied-latest",
                "subagent_type": "developer",
                "role_recorded": "developer",
                "model_applied": "haiku",
                "source": "injected",
                "prompt_prefix": "",
            },
        )
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_applied_state_latest_row")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path, applied_state_path=applied_path,
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert _bandit_row(db_path, "developer", "medium", "haiku") is not None, (
            "複数行中、最新行（ts 最大）の model_applied=haiku が採用されなかった"
        )
        assert _bandit_row(db_path, "developer", "medium", "opus") is None, (
            "先頭の古い行（opus）が誤って採用された"
        )

    def test_different_session_rows_ignored_falls_back_to_tier_selection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        sel_path = tmp_path / "state" / "tier_selection.json"
        applied_path = tmp_path / "state" / "tier_autoapply.jsonl"
        _write_tier_selection(sel_path, tier="sonnet", session_id="sess-applied-current")
        _write_applied_state(
            applied_path,
            {
                "ts": "2026-07-07T08:00:00+00:00",
                "session_id": "sess-applied-other",
                "subagent_type": "developer",
                "role_recorded": "developer",
                "model_applied": "haiku",
                "source": "injected",
                "prompt_prefix": "",
            },
        )
        _write_agent_frontmatter(agents_dir, "developer", "model: opus")
        mod = _load_hook_module("rao_applied_state_other_session")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path, applied_state_path=applied_path,
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert _bandit_row(db_path, "developer", "medium", "haiku") is None, (
            "別 session_id の applied-state 行が誤って突合された"
        )
        assert _bandit_row(db_path, "developer", "medium", "sonnet") is not None, (
            "別 session 行が無視された後、優先3（tier_selection.json）へ"
            "落ちていない"
        )


class TestAppliedStateBrokenRowsAndTypeGuard:
    """壊れ行（JSON パース不能）は skip し、model_applied が非文字列の行も
    resolve_tier の内部 .lower() でクラッシュせず skip した
    （_read_selection() の非文字列ガードと同じ防御思想）。"""

    def test_broken_json_line_skipped_valid_row_still_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        sel_path = tmp_path / "state" / "tier_selection.json"
        applied_path = tmp_path / "state" / "tier_autoapply.jsonl"
        _write_tier_selection(sel_path, tier="sonnet", session_id="sess-applied-broken")
        valid_row = json.dumps(
            {
                "ts": "2026-07-07T08:00:00+00:00",
                "session_id": "sess-applied-broken",
                "subagent_type": "developer",
                "role_recorded": "developer",
                "model_applied": "haiku",
                "source": "injected",
                "prompt_prefix": "",
            },
            ensure_ascii=False,
        )
        _write_applied_state_raw_lines(applied_path, ["{not valid json,,,", valid_row])
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_applied_state_broken_row")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path, applied_state_path=applied_path,
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert _bandit_row(db_path, "developer", "medium", "haiku") is not None, (
            "壊れ行の直後にある正常行が skip された、または例外で処理全体が"
            "落ちた疑いがある"
        )
        assert _bandit_row(db_path, "developer", "medium", "sonnet") is None

    def test_non_string_model_applied_row_skipped_falls_back_to_tier_selection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        sel_path = tmp_path / "state" / "tier_selection.json"
        applied_path = tmp_path / "state" / "tier_autoapply.jsonl"
        _write_tier_selection(sel_path, tier="sonnet", session_id="sess-applied-nonstr")
        _write_applied_state(
            applied_path,
            {
                "ts": "2026-07-07T08:00:00+00:00",
                "session_id": "sess-applied-nonstr",
                "subagent_type": "developer",
                "role_recorded": "developer",
                "model_applied": 123,
                "source": "injected",
                "prompt_prefix": "",
            },
        )
        _write_agent_frontmatter(agents_dir, "developer", "model: opus")
        mod = _load_hook_module("rao_applied_state_nonstring_model")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path, applied_state_path=applied_path,
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert _bandit_row(db_path, "developer", "medium", "sonnet") is not None, (
            "model_applied(int) の非文字列ガードが無いため例外で落ちたか、"
            "誤って frontmatter（opus）へ落ちた疑いがある"
        )
        assert _bandit_row(db_path, "developer", "medium", "opus") is None


class TestKillSwitchFallback:
    """DC-GP-003: applied-state 不在（kill-switch で tier_autoapply.py が
    jsonl に行を書かない状態相当）のとき、record の優先2 が不成立となり
    優先3（tier_selection.json）へ正しく落ちた（旧来動作への完全復帰・
    要件 §④安全弁）。既存挙動（フェーズ2 のソフト適用）への回帰確認を兼ねる。"""

    def test_applied_state_file_absent_falls_back_to_tier_selection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection(sel_path, tier="haiku", session_id="sess-killswitch-1")
        _write_agent_frontmatter(agents_dir, "developer", "model: opus")
        mod = _load_hook_module("rao_kill_switch_fallback")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path,
            applied_state_path=tmp_path / "state" / "tier_autoapply_absent.jsonl",
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert _bandit_row(db_path, "developer", "medium", "haiku") is not None, (
            "applied-state 不在時に優先3（tier_selection.json）へ落ちておらず、"
            "旧来動作へ復帰していない"
        )
        assert _bandit_row(db_path, "developer", "medium", "opus") is None


class TestAppliedStateSessionIdNoneSkipsToFrontmatter:
    """session_id が None（tier_selection.json 不在・--final 削除後相当）の
    場合、applied-state 側に session_id 付きの行があっても NULL 同士を
    突き合わせず、優先3（tier_selection 不在のため不成立）を経て優先4
    （frontmatter）へ落ちた（§0-4(b)・§4-2 項1）。"""

    def test_session_id_none_does_not_match_applied_state_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        applied_path = tmp_path / "state" / "tier_autoapply.jsonl"
        _write_applied_state(
            applied_path,
            {
                "ts": "2026-07-07T08:00:00+00:00",
                "session_id": "sess-should-not-match",
                "subagent_type": "developer",
                "role_recorded": "developer",
                "model_applied": "haiku",
                "source": "injected",
                "prompt_prefix": "",
            },
        )
        _write_agent_frontmatter(agents_dir, "developer", "model: sonnet")
        mod = _load_hook_module("rao_applied_state_session_none")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=tmp_path / "state" / "nonexistent.json",
            applied_state_path=applied_path,
        )

        rc = mod.main([
            "--role", "developer", "--outcome", "success", "--gate", "D-2.5",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert _bandit_row(db_path, "developer", "medium", "sonnet") is not None, (
            "session_id=None のとき applied-state の session_id 付き行に"
            "誤って突合し frontmatter へ落ちていない"
        )
        assert _bandit_row(db_path, "developer", "medium", "haiku") is None


class TestAppliedStateTesterRoleGating:
    """tester は _SOFT_APPLY_ROLES（developer のみ）に含まれないため、
    applied-state に tester 向けの行があっても読まず、従来どおり
    frontmatter で解決した（role gating の維持）。"""

    def test_tester_role_ignores_applied_state_and_resolves_frontmatter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        db_path: Path, agents_dir: Path,
    ) -> None:
        sel_path = tmp_path / "state" / "tier_selection.json"
        applied_path = tmp_path / "state" / "tier_autoapply.jsonl"
        _write_tier_selection(sel_path, tier="haiku", session_id="sess-tester-applied")
        _write_applied_state(
            applied_path,
            {
                "ts": "2026-07-07T08:00:00+00:00",
                "session_id": "sess-tester-applied",
                "subagent_type": "tester",
                "role_recorded": "tester",
                "model_applied": "opus",
                "source": "injected",
                "prompt_prefix": "",
            },
        )
        _write_agent_frontmatter(agents_dir, "tester", "model: sonnet")
        mod = _load_hook_module("rao_tester_applied_state_gating")
        _patch_common(
            mod, monkeypatch, db_path=db_path, agents_dir=agents_dir,
            sel_path=sel_path, applied_state_path=applied_path,
        )

        rc = mod.main([
            "--role", "tester", "--outcome", "success", "--gate", "D-3",
            "--execution", "subagent", "--complexity", "medium",
        ])
        assert rc == 0
        assert _bandit_row(db_path, "tester", "medium", "sonnet") is not None, (
            "tester は frontmatter 解決のままであるべきだが崩れている"
        )
        assert _bandit_row(db_path, "tester", "medium", "opus") is None, (
            "tester が applied-state の model_applied=opus を読んでしまっている"
            "（role gating の欠陥）"
        )
        assert _bandit_row(db_path, "tester", "medium", "haiku") is None, (
            "tester が tier_selection.json の tier=haiku も読んでしまっている"
        )


class TestAppliedStatePathResolution:
    """DC-AS-003: record が計算する applied-state の絶対パスが、
    tier_autoapply.py（T1・.claude/hooks/ 配置・1階層遡り）が計算する
    APPLIED_STATE_PATH と一致した（T1 TestPathResolution と対称）。"""

    def test_applied_state_path_matches_tier_autoapply_hook_path(self) -> None:
        mod = _load_hook_module("rao_path_resolution")
        hook_path = (
            WORKTREE_ROOT / ".claude" / "hooks" / "tier_autoapply.py"
        )
        assert hook_path.is_file(), (
            "tier_autoapply.py（T1）が見つからずパス一致を比較できなかった"
        )
        spec = importlib.util.spec_from_file_location(
            "tier_autoapply_path_check", hook_path
        )
        assert spec is not None and spec.loader is not None
        hook_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook_mod)  # type: ignore[attr-defined]

        assert os.path.abspath(mod.APPLIED_STATE_PATH) == os.path.abspath(
            hook_mod.APPLIED_STATE_PATH
        ), (
            "record と tier_autoapply.py（hook）が計算する applied-state の"
            "絶対パスが一致しなかった（_CLAUDE_DIR 機構の遡り段数不一致の疑い）"
        )
