"""Tests for .claude/hooks/tier_autoapply.py (新規・未実装)

tier-routing 機械適用（フェーズ3）の PreToolUse(Agent) hook。
architecture-report-20260707-065043.md §3・plan-report-20260707-065732.md
test-tier-autoapply（T1 Red）に基づく Red フェーズテストだった。

対象 hook は本 Red フェーズ時点で未作成のため、本ファイルの全テストは
「.claude/hooks/tier_autoapply.py が存在しない（FileNotFoundError）」という
単一の原因で失敗した（tester/MEMORY.md の record_agent_outcome.py Red 実装
パターンを踏襲し、pytest.mark.skipif ではなく明示的な例外送出で「失敗する Red」の
証跡を残す設計にした）。

テストが要求する hook 契約（developer への実装契約。plan/architecture に
明記が無い実装詳細は本ファイルで固定する）:

- パス: `.claude/hooks/tier_autoapply.py`
- 入力: PreToolUse stdin JSON（`tool_name` / `tool_input` / 任意で `session_id`）
- 出力: 注入時のみ stdout に
  `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": {...}}}`
  （T0 実測により `permissionDecision` は省略形を正とする。注入しない場合は
  stdout 空文字列）
- 副作用: `.claude/state/tier_autoapply.jsonl` への 1 行追記（LAUNCH_LOG_ROLES
  対象のみ。reviewer 系・その他 role は記録なし）
- `APPLY_ROLES = {developer, wt_developer}` のみ updatedInput 注入対象
- `LAUNCH_LOG_ROLES = {developer, wt_developer, tester, wt_tester}` が記録対象
- `role_recorded` は `wt_developer` → `developer` / `wt_tester` → `tester` に正規化
- kill-switch: 環境変数 `C3_TIER_AUTOAPPLY_DISABLE=1` で注入・記録とも行わず exit 0
- fail-safe: 不正 JSON・非 Agent・非 dict tool_input・空 stdin は exit 0 かつ
  stdout 空文字列
- jsonl 1 行のフィールド: `ts`（`agent_outcomes.ts` と同一 UTC ISO8601 秒精度
  プロファイル。`datetime.now(timezone.utc).isoformat(timespec="seconds")` と
  同一生成式）・`session_id`・`subagent_type`・`role_recorded`・`model_applied`・
  `source`（`injected`/`explicit`/`frontmatter-default`）・`prompt_prefix`
  （先頭200字・制御文字/U+2028/U+2029 除去）
- パス解決: `_CLAUDE_DIR` 機構（`record_agent_outcome.py` L82-98 と同じ SSOT。
  `.claude/hooks/` は 1 階層遡り・`.claude/skills/dev-workflow/scripts/` は
  3 階層遡りで、両者が同一 `.claude/state/tier_autoapply.jsonl` に解決する）
"""

from __future__ import annotations

import builtins
import collections
import contextlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import types
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "tier_autoapply.py"
STATE_JSONL_PATH = WORKTREE_ROOT / ".claude" / "state" / "tier_autoapply.jsonl"
TIER_SELECTION_PATH = WORKTREE_ROOT / ".claude" / "state" / "tier_selection.json"
RECORD_SCRIPT_PATH = (
    WORKTREE_ROOT
    / ".claude"
    / "skills"
    / "dev-workflow"
    / "scripts"
    / "record_agent_outcome.py"
)

KILL_SWITCH_ENV = "C3_TIER_AUTOAPPLY_DISABLE"

# U+2028 LINE SEPARATOR / U+2029 PARAGRAPH SEPARATOR（実体文字を埋め込まず chr() で参照。
# tester/MEMORY.md の「Edit/Write に実体文字を直接タイプすると転送経路で化ける」対策）。
_LS = chr(0x2028)
_PS = chr(0x2029)

# jsonl の ts が同一 UTC ISO8601 秒精度プロファイルであることの検証パターン
# （agent_outcomes.ts / db.py:1046 と同一生成式: 秒精度・+00:00・小数秒なし）。
_TS_UTC_SECONDS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")

# 記録処理到達後の断念 4 経路（architecture 改訂 3・P3 の原因識別子 4 値）と、その
# stderr 固定文言テンプレート。文言は hook 実装と 1 文字も違わない完全一致で固定する。
_ABORT_REASONS = ("deadline", "lockfile-open-failed", "mkdir-failed", "write-failed")
_WARNING_TEXT_TEMPLATE = "[tier_autoapply] append skipped: reason={reason}"

# 外部由来文字列（row の秘密値）が stderr に混入しないことを検査するための仕込み値。
_SECRET_PROMPT = "秘密のプロンプト内容-SHOULD-NOT-LEAK"


def _assert_fixed_warning(stderr_text: str, reason: str, row: dict) -> None:
    """断念時 stderr の水準統一検査（DC-GP-004・SR-NEW）。

    4 経路すべてに対し同水準で以下 2 点を検査する:
      1. stderr 末尾の警告行が原因別の固定文言と**完全一致**すること（部分一致にしない）
      2. `row["prompt_prefix"]` に仕込んだ外部由来文字列が stderr に**出現しない**こと

    2 の検査が空回りしないよう、呼び出し側の row が実際に秘密値を持つことを先に検査する
    （仕込み忘れによる空の緑の防止）。
    """
    assert reason in _ABORT_REASONS, f"未知の原因識別子: {reason!r}"
    assert row["prompt_prefix"] == _SECRET_PROMPT, (
        "row に外部由来文字列が仕込まれていない（秘密非混入 assert が空回りする）"
    )
    stderr_lines = [line.strip() for line in stderr_text.splitlines() if line.strip()]
    assert len(stderr_lines) > 0, f"stderr に警告が出ていない（reason={reason}）"
    expected = _WARNING_TEXT_TEMPLATE.format(reason=reason)
    assert stderr_lines[-1] == expected, (
        f"stderr 警告文が固定文言と完全一致しない: {stderr_lines[-1]!r}（期待: {expected!r}）"
    )
    assert _SECRET_PROMPT not in stderr_text, (
        "stderr に外部由来文字列（prompt）が混入している（P3 違反）"
    )


def _run_hook(
    payload: dict | None = None,
    *,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """tier_autoapply.py を subprocess で 1 回起動した。

    HOOK_PATH が存在しない場合（Red フェーズの想定挙動）は FileNotFoundError を
    送出する。pytest.mark.skipif で全テストを SKIP にすると「失敗する Red」の
    証跡が残らないため、明示的に例外を送出する設計にした
    （tester/MEMORY.md「.dev/hooks テストの pytestmark skipif 回避パターン」を踏襲）。
    """
    if not HOOK_PATH.is_file():
        raise FileNotFoundError(
            f"tier_autoapply.py が未作成だった（Red フェーズの想定挙動）: {HOOK_PATH}"
        )
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)
    stdin_data = input_text if input_text is not None else json.dumps(payload, ensure_ascii=False)
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=stdin_data,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(WORKTREE_ROOT),
        env=merged_env,
    )


def _load_autoapply_module(name: str = "tier_autoapply_direct_t") -> types.ModuleType:
    """HOOK_PATH をプロセス内 import で直接ロードした（F-7: `_try_os_lock`/`_acquire_lock` monkeypatch 用）。

    `TestConcurrency` 等は subprocess 経由（プロセス境界を跨ぐため内部関数を
    monkeypatch できない）だが、F-7 のロック取得失敗フォールバック検証は
    `_try_os_lock`/`_acquire_lock`/`_append_applied_state` を直接差し替える必要があるため、
    tests/hooks/test_tier_gap_check.py の `_load_hook_module` と同型の
    importlib ロードを用いた。
    """
    if not HOOK_PATH.is_file():
        raise FileNotFoundError(
            f"tier_autoapply.py が未作成だった（Red フェーズの想定挙動）: {HOOK_PATH}"
        )
    spec = importlib.util.spec_from_file_location(name, HOOK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


@contextlib.contextmanager
def _hold_os_lock(lock_path: Path):
    """別ハンドルで `lock_path` のバイト0に OS 排他ロックを保持した（**取得成功保証型**・CR-M-001/DC-GP-002）。

    hook 側 `_try_os_lock` と同一領域（`seek(0)` + 1 バイト）を対象にするため、
    このコンテキスト内で hook を呼ぶと必ずロック競合が発生する（＝リトライが実際に走る）。

    契約（DC-GP-002 (a)）:
    - **ロック保持に失敗したら `pytest.fail` で落とす**。「取得できなければ静かに何もしない」型の
      条件付きスキップ（旧 `TestP2Bounded` の `if lock_held:` ガード）はヘルパーにも利用側にも
      持ち込まない。したがって利用側の assert は全て**無条件**に書ける。
    - 解放は `finally` で構造的に保証する（DC-GP-002 (b) の「フルスイートをハングさせない」要件）。
      旧 `TestP5InjectionIndependent` はスレッド + `threading.Event` で保持していたが、本ヘルパーは
      呼び出しスレッドで保持し `with` を抜けた瞬間に解放するため、待機オブジェクト自体が不要になる
      （ロックは open file description / ファイルハンドル単位でありスレッド単位ではないため、
      別プロセス・同一プロセス別ハンドルのいずれに対しても同じ排他が成立する）。
    """
    try:
        import msvcrt as _msvcrt  # type: ignore[import-not-found]
    except ImportError:
        _msvcrt = None  # type: ignore[assignment]
    try:
        import fcntl as _fcntl  # type: ignore[import-not-found]
    except ImportError:
        _fcntl = None  # type: ignore[assignment]

    if _msvcrt is None and _fcntl is None:
        pytest.fail(
            "msvcrt / fcntl が両方不在で別ハンドルの実 OS ロックを保持できなかった"
            "（このテストはロック競合を前提とするため skip せず失敗させる）"
        )

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_f = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            if _msvcrt is not None:
                lock_f.seek(0)
                _msvcrt.locking(lock_f.fileno(), _msvcrt.LK_NBLCK, 1)
            else:
                _fcntl.flock(lock_f.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError as exc:
            pytest.fail(
                f"別ハンドルでの OS ロック取得に失敗した: {type(exc).__name__}: {exc}"
                f"（lock_path={lock_path}）"
            )
        try:
            yield lock_f
        finally:
            try:
                if _msvcrt is not None:
                    lock_f.seek(0)
                    _msvcrt.locking(lock_f.fileno(), _msvcrt.LK_UNLCK, 1)
                else:
                    _fcntl.flock(lock_f.fileno(), _fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        lock_f.close()


def _agent_payload(
    subagent_type: str,
    *,
    model: str | None = None,
    isolation: str | None = None,
    prompt: str = "テスト用プロンプト",
    session_id: str | None = None,
    **extra,
) -> dict:
    """Agent ツール呼び出し（PreToolUse）payload を模擬した。"""
    tool_input: dict = {"subagent_type": subagent_type, "prompt": prompt}
    if model is not None:
        tool_input["model"] = model
    if isolation is not None:
        tool_input["isolation"] = isolation
    tool_input.update(extra)
    payload: dict = {"tool_name": "Agent", "tool_input": tool_input}
    if session_id is not None:
        payload["session_id"] = session_id
    return payload


def _new_session_id() -> str:
    return "sess-" + uuid.uuid4().hex[:12]


def _read_jsonl_lines(path: Path = STATE_JSONL_PATH) -> list[dict]:
    if not path.is_file():
        return []
    lines = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        lines.append(json.loads(raw_line))
    return lines


def _write_tier_selection(**fields: object) -> None:
    TIER_SELECTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    TIER_SELECTION_PATH.write_text(json.dumps(fields, ensure_ascii=False), encoding="utf-8")


def _write_malformed_tier_selection(text: str) -> None:
    TIER_SELECTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    TIER_SELECTION_PATH.write_text(text, encoding="utf-8")


def _write_bulk_jsonl(path: Path, n_lines: int, filler_size: int = 350) -> None:
    """ローテーション検証用にダミー行を大量書き込みした（実 hook が書く形式とは無関係）。"""
    filler = "x" * filler_size
    with open(path, "w", encoding="utf-8") as f:
        for i in range(n_lines):
            row = {
                "ts": "2026-01-01T00:00:00+00:00",
                "session_id": f"old-{i}",
                "subagent_type": "developer",
                "role_recorded": "developer",
                "model_applied": "sonnet",
                "source": "frontmatter-default",
                "prompt_prefix": filler,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


@pytest.fixture(autouse=True)
def isolated_state_files():
    """各テストの前後で state/tier_autoapply.jsonl と state/tier_selection.json を退避・復元した。

    hook は実リポジトリの `.claude/state/` を対象に書く設計（環境変数によるパス
    差し替え機構は architecture に定義が無い）ため、実ファイルをテスト前に
    削除しテスト後に元の内容へ復元することで副作用を隔離した。
    """
    original_jsonl = STATE_JSONL_PATH.read_bytes() if STATE_JSONL_PATH.is_file() else None
    original_selection = (
        TIER_SELECTION_PATH.read_bytes() if TIER_SELECTION_PATH.is_file() else None
    )
    if STATE_JSONL_PATH.is_file():
        STATE_JSONL_PATH.unlink()
    if TIER_SELECTION_PATH.is_file():
        TIER_SELECTION_PATH.unlink()

    yield

    if STATE_JSONL_PATH.is_file():
        STATE_JSONL_PATH.unlink()
    if TIER_SELECTION_PATH.is_file():
        TIER_SELECTION_PATH.unlink()
    if original_jsonl is not None:
        STATE_JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_JSONL_PATH.write_bytes(original_jsonl)
    if original_selection is not None:
        TIER_SELECTION_PATH.parent.mkdir(parents=True, exist_ok=True)
        TIER_SELECTION_PATH.write_bytes(original_selection)


# ---------------------------------------------------------------------------
# TestInjection: model 無指定の developer/wt_developer に updatedInput 注入
# ---------------------------------------------------------------------------

class TestInjection:
    """model 無指定 + tier_selection.json ありで updatedInput.model を注入する契約を固定した。"""

    def test_no_model_developer_injects_recommended_tier(self) -> None:
        """developer + model 無指定 + tier_selection.tier=haiku → updatedInput.model=haiku を注入した。"""
        _write_tier_selection(tier="haiku", suggested_model="haiku", mode="thompson")
        sid = _new_session_id()
        result = _run_hook(
            _agent_payload("developer", session_id=sid)
        )
        assert result.returncode == 0
        stdout = json.loads(result.stdout)
        updated = stdout["hookSpecificOutput"]["updatedInput"]
        assert updated["model"] == "haiku"
        # 元の tool_input 全キーが保存されていた（subagent_type/prompt）。
        assert updated["subagent_type"] == "developer"
        assert updated["prompt"] == "テスト用プロンプト"
        # T0 実測（省略形が正）に基づき permissionDecision キーは無かった。
        assert "permissionDecision" not in stdout["hookSpecificOutput"]

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert lines[0]["source"] == "injected"
        assert lines[0]["role_recorded"] == "developer"
        assert lines[0]["model_applied"] == "haiku"
        assert lines[0]["session_id"] == sid

    def test_wt_developer_isolation_key_preserved_on_injection(self) -> None:
        """wt_developer + isolation=worktree + model 無指定 → 注入後も isolation が保持された。"""
        _write_tier_selection(tier="opus", suggested_model="opus", mode="thompson")
        sid = _new_session_id()
        result = _run_hook(
            _agent_payload("wt_developer", isolation="worktree", session_id=sid)
        )
        assert result.returncode == 0
        stdout = json.loads(result.stdout)
        updated = stdout["hookSpecificOutput"]["updatedInput"]
        assert updated["model"] == "opus"
        assert updated["isolation"] == "worktree"
        assert updated["subagent_type"] == "wt_developer"

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert lines[0]["subagent_type"] == "wt_developer"
        # role_recorded は正規化された（wt_developer → developer）。
        assert lines[0]["role_recorded"] == "developer"
        assert lines[0]["source"] == "injected"


# ---------------------------------------------------------------------------
# TestExplicitRespect: model 明示は素通り（明示尊重）
# ---------------------------------------------------------------------------

class TestExplicitRespect:
    """model 明示時は updatedInput を出さず素通りする契約を固定した。"""

    def test_explicit_model_developer_not_overridden(self) -> None:
        """developer + model=opus 明示 + tier_selection.tier=haiku → 注入せず素通りした。"""
        _write_tier_selection(tier="haiku", suggested_model="haiku", mode="thompson")
        sid = _new_session_id()
        result = _run_hook(
            _agent_payload("developer", model="opus", session_id=sid)
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert lines[0]["source"] == "explicit"
        assert lines[0]["model_applied"] == "opus"
        assert lines[0]["role_recorded"] == "developer"

    def test_explicit_model_wt_developer_not_overridden(self) -> None:
        """wt_developer + model=sonnet 明示 → 注入せず素通りし source=explicit で記録した。"""
        sid = _new_session_id()
        result = _run_hook(
            _agent_payload(
                "wt_developer", model="sonnet", isolation="worktree", session_id=sid
            )
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert lines[0]["source"] == "explicit"
        assert lines[0]["model_applied"] == "sonnet"


# ---------------------------------------------------------------------------
# TestSelectionAbsent: tier_selection 不在/破損/非文字列 → 注入なし
# ---------------------------------------------------------------------------

class TestSelectionAbsent:
    """tier_selection.json が不在・破損・非文字列 tier のとき注入せず記録のみを行う契約を固定した。"""

    def test_tier_selection_missing_no_injection(self) -> None:
        """tier_selection.json 不在 + developer + model 無指定 → 注入せず source=frontmatter-default だった。"""
        # isolated_state_files フィクスチャで既に不在。
        sid = _new_session_id()
        result = _run_hook(_agent_payload("developer", session_id=sid))
        assert result.returncode == 0
        assert result.stdout.strip() == ""

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert lines[0]["source"] == "frontmatter-default"
        assert lines[0]["model_applied"] in (None, "")

    def test_tier_selection_malformed_json_no_injection(self) -> None:
        """tier_selection.json が壊れた JSON → 注入せず source=frontmatter-default だった。"""
        _write_malformed_tier_selection("{not valid json")
        sid = _new_session_id()
        result = _run_hook(_agent_payload("developer", session_id=sid))
        assert result.returncode == 0
        assert result.stdout.strip() == ""

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert lines[0]["source"] == "frontmatter-default"

    def test_tier_selection_non_string_tier_no_injection(self) -> None:
        """tier_selection.json の tier が非文字列（数値）→ 正規化不能で注入しなかった。"""
        _write_tier_selection(tier=12345, suggested_model=12345, mode="thompson")
        sid = _new_session_id()
        result = _run_hook(_agent_payload("developer", session_id=sid))
        assert result.returncode == 0
        assert result.stdout.strip() == ""

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert lines[0]["source"] == "frontmatter-default"


# ---------------------------------------------------------------------------
# TestRoleGating: LAUNCH_LOG_ROLES / reviewer 系 / その他 role の分岐
# ---------------------------------------------------------------------------

class TestRoleGating:
    """role 種別ごとの注入/記録の可否を固定した。"""

    def test_tester_recorded_but_not_injected(self) -> None:
        """tester + tier_selection あり → 注入なし・記録のみ（注入対象外）だった。"""
        _write_tier_selection(tier="haiku", suggested_model="haiku", mode="thompson")
        sid = _new_session_id()
        result = _run_hook(_agent_payload("tester", session_id=sid))
        assert result.returncode == 0
        assert result.stdout.strip() == ""

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert lines[0]["role_recorded"] == "tester"
        assert lines[0]["source"] == "frontmatter-default"

    def test_wt_tester_role_recorded_is_normalized(self) -> None:
        """wt_tester → role_recorded は tester に正規化されて記録された。"""
        sid = _new_session_id()
        result = _run_hook(
            _agent_payload("wt_tester", isolation="worktree", session_id=sid)
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert lines[0]["subagent_type"] == "wt_tester"
        assert lines[0]["role_recorded"] == "tester"

    def test_code_reviewer_not_recorded_no_injection(self) -> None:
        """code-reviewer → 注入も記録もされなかった。"""
        sid = _new_session_id()
        result = _run_hook(_agent_payload("code-reviewer", session_id=sid))
        assert result.returncode == 0
        assert result.stdout.strip() == ""
        assert _read_jsonl_lines() == []

    def test_security_reviewer_not_recorded_no_injection(self) -> None:
        """security-reviewer → 注入も記録もされなかった。"""
        sid = _new_session_id()
        result = _run_hook(_agent_payload("security-reviewer", session_id=sid))
        assert result.returncode == 0
        assert result.stdout.strip() == ""
        assert _read_jsonl_lines() == []

    def test_other_role_not_recorded_no_injection(self) -> None:
        """LAUNCH_LOG_ROLES にも reviewer 系にも含まれない role（design-critic）は素通りだった。"""
        sid = _new_session_id()
        result = _run_hook(_agent_payload("design-critic", session_id=sid))
        assert result.returncode == 0
        assert result.stdout.strip() == ""
        assert _read_jsonl_lines() == []


# ---------------------------------------------------------------------------
# TestRedApplyInjection: RED_APPLY_ROLES（tester/wt_tester）の test- 限定注入
# （architecture §2-3・ADR-2・test-report §2-2）。
#
# 注入条件は「role ∈ {tester, wt_tester}」かつ「C3_TASK_ID: test- マーカー」かつ
# 「roles.tester.tier 解決可」の 3 条件全成立時のみ。tier 源はトップレベル tier
# ではなく additive な roles.tester.tier。
# ---------------------------------------------------------------------------

class TestRedApplyInjection:
    """tester/wt_tester の Red 限定注入（test- マーカー + roles.tester）を固定した。"""

    def test_tester_test_marker_injects_from_roles_tester(self) -> None:
        """(a) tester + C3_TASK_ID: test-x + roles.tester.tier=sonnet → model=sonnet 注入。

        トップレベル tier=haiku ではなく roles.tester.tier=sonnet が注入源になる。
        """
        _write_tier_selection(
            tier="haiku", suggested_model="haiku", mode="thompson",
            roles={"tester": {"tier": "sonnet", "mode": "thompson"}},
        )
        sid = _new_session_id()
        result = _run_hook(
            _agent_payload("tester", session_id=sid, prompt="C3_TASK_ID: test-login")
        )
        assert result.returncode == 0
        stdout = json.loads(result.stdout)
        updated = stdout["hookSpecificOutput"]["updatedInput"]
        assert updated["model"] == "sonnet"
        assert updated["subagent_type"] == "tester"
        assert "permissionDecision" not in stdout["hookSpecificOutput"]

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert lines[0]["source"] == "injected"
        assert lines[0]["role_recorded"] == "tester"
        assert lines[0]["model_applied"] == "sonnet"
        assert lines[0]["task_id"] == "test-login"

    def test_tester_confirm_marker_not_injected(self) -> None:
        """(b) tester + C3_TASK_ID: confirm-x → 注入なし・記録のみ・frontmatter-default。"""
        _write_tier_selection(
            tier="haiku", suggested_model="haiku", mode="thompson",
            roles={"tester": {"tier": "sonnet", "mode": "thompson"}},
        )
        sid = _new_session_id()
        result = _run_hook(
            _agent_payload("tester", session_id=sid, prompt="C3_TASK_ID: confirm-login")
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert lines[0]["source"] == "frontmatter-default"
        assert lines[0]["model_applied"] in (None, "")
        assert lines[0]["task_id"] == "confirm-login"

    def test_wt_tester_test_marker_injects_from_roles_tester(self) -> None:
        """(c) wt_tester + C3_TASK_ID: test-x → tester と同型で注入（role_recorded 正規化込み）。"""
        _write_tier_selection(
            tier="haiku", suggested_model="haiku", mode="thompson",
            roles={"tester": {"tier": "opus", "mode": "thompson"}},
        )
        sid = _new_session_id()
        result = _run_hook(
            _agent_payload(
                "wt_tester", isolation="worktree", session_id=sid,
                prompt="C3_TASK_ID: test-api",
            )
        )
        assert result.returncode == 0
        stdout = json.loads(result.stdout)
        updated = stdout["hookSpecificOutput"]["updatedInput"]
        assert updated["model"] == "opus"
        assert updated["isolation"] == "worktree"
        assert updated["subagent_type"] == "wt_tester"

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert lines[0]["subagent_type"] == "wt_tester"
        assert lines[0]["role_recorded"] == "tester"
        assert lines[0]["source"] == "injected"
        assert lines[0]["model_applied"] == "opus"

    def test_tester_test_marker_but_roles_missing_no_injection(self) -> None:
        """(e-1) tester + test- マーカー + roles キー無し → 注入なし（fail-safe）。"""
        _write_tier_selection(tier="haiku", suggested_model="haiku", mode="thompson")
        sid = _new_session_id()
        result = _run_hook(
            _agent_payload("tester", session_id=sid, prompt="C3_TASK_ID: test-x")
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert lines[0]["source"] == "frontmatter-default"

    def test_tester_test_marker_but_roles_tester_broken_no_injection(self) -> None:
        """(e-2) tester + test- マーカー + roles.tester 破損 → 注入なし（fail-safe）。

        非 dict roles / roles.tester が非 dict / tier キー欠落 / tier 非文字列 の
        いずれも注入させない。
        """
        broken_variants: list[object] = [
            "not-a-dict",
            {"tester": "x"},
            {"tester": {"mode": "thompson"}},
            {"tester": {"tier": 123}},
        ]
        for broken in broken_variants:
            _write_tier_selection(
                tier="haiku", suggested_model="haiku", mode="thompson", roles=broken,
            )
            sid = _new_session_id()
            result = _run_hook(
                _agent_payload("tester", session_id=sid, prompt="C3_TASK_ID: test-x")
            )
            assert result.returncode == 0
            assert result.stdout.strip() == "", f"破損 roles で注入された: {broken!r}"
            lines = _read_jsonl_lines()
            assert lines[-1]["source"] == "frontmatter-default", f"破損 roles: {broken!r}"

    def test_tester_test_marker_explicit_model_respected(self) -> None:
        """(f) tester + test- マーカー + model 明示 → 注入せず素通り・source=explicit。"""
        _write_tier_selection(
            tier="haiku", suggested_model="haiku", mode="thompson",
            roles={"tester": {"tier": "sonnet", "mode": "thompson"}},
        )
        sid = _new_session_id()
        result = _run_hook(
            _agent_payload(
                "tester", model="opus", session_id=sid, prompt="C3_TASK_ID: test-x"
            )
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert lines[0]["source"] == "explicit"
        assert lines[0]["model_applied"] == "opus"


def _read_frontmatter_model(path: Path) -> str | None:
    """agent 定義 md の YAML frontmatter から model: 値を読んだ（先頭 --- ブロック）。"""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"^model:\s*(\S+)\s*$", line)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# TestOpusFixedInvariant: RED_APPLY_ROLES ∪ APPLY_ROLES の全 role は
# frontmatter model: sonnet（ADR-6・opus 固定不変則の機械検査・恒久 CI 回帰網）。
# ---------------------------------------------------------------------------

class TestOpusFixedInvariant:
    """機械適用対象 role の agent frontmatter が model: sonnet であることを固定した。"""

    def test_all_apply_roles_have_sonnet_frontmatter(self) -> None:
        """APPLY_ROLES ∪ RED_APPLY_ROLES の 4 role が model: sonnet であること。

        opus frontmatter の agent（architect / planner / design-critic /
        doc-writer / project-setup）が機械適用対象へ混入すると本テストが Red になる
        （opus 固定不変則違反の検知・ADR-6）。
        """
        mod = _load_autoapply_module()
        roles = set(mod.APPLY_ROLES) | set(mod.RED_APPLY_ROLES)
        assert roles == {"developer", "wt_developer", "tester", "wt_tester"}, (
            f"機械適用対象 role 集合が想定外: {roles}"
        )
        agents_dir = WORKTREE_ROOT / ".claude" / "agents"
        for role in sorted(roles):
            agent_file = agents_dir / f"{role}.md"
            assert agent_file.is_file(), f"agent 定義が無い: {agent_file}"
            model = _read_frontmatter_model(agent_file)
            assert model == "sonnet", (
                f"{role}.md の frontmatter model が sonnet でない: {model!r}"
                f"（opus 固定不変則違反・ADR-6）"
            )


# ---------------------------------------------------------------------------
# TestKillSwitch: C3_TIER_AUTOAPPLY_DISABLE=1
# ---------------------------------------------------------------------------

class TestKillSwitch:
    """kill-switch 有効時は注入も記録も行わず旧来動作へ完全復帰する契約を固定した。"""

    def test_kill_switch_disables_injection_and_recording(self) -> None:
        """C3_TIER_AUTOAPPLY_DISABLE=1 → developer + tier_selection ありでも注入・記録とも無かった。"""
        _write_tier_selection(tier="haiku", suggested_model="haiku", mode="thompson")
        sid = _new_session_id()
        result = _run_hook(
            _agent_payload("developer", session_id=sid),
            env={KILL_SWITCH_ENV: "1"},
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""
        assert _read_jsonl_lines() == []


# ---------------------------------------------------------------------------
# TestFailSafe: 不正入力は exit 0・副作用なし
# ---------------------------------------------------------------------------

class TestFailSafe:
    """不正入力・想定外入力は全て exit 0・副作用なしで素通りする契約を固定した。"""

    def test_invalid_json_input_exit_zero(self) -> None:
        """不正な JSON 文字列を stdin に渡しても exit 0 でクラッシュしなかった。"""
        result = _run_hook(input_text="this is not valid json {{{")
        assert result.returncode == 0
        assert result.stdout.strip() == ""
        assert _read_jsonl_lines() == []

    def test_non_agent_tool_ignored(self) -> None:
        """tool_name が Agent 以外 → exit 0・副作用なしだった。"""
        result = _run_hook({"tool_name": "Write", "tool_input": {"file_path": "x.txt"}})
        assert result.returncode == 0
        assert result.stdout.strip() == ""
        assert _read_jsonl_lines() == []

    def test_non_dict_tool_input_ignored(self) -> None:
        """tool_input が dict でない → exit 0・副作用なしだった。"""
        result = _run_hook({"tool_name": "Agent", "tool_input": "invalid"})
        assert result.returncode == 0
        assert result.stdout.strip() == ""
        assert _read_jsonl_lines() == []

    def test_empty_stdin_exit_zero(self) -> None:
        """空の stdin → exit 0 でクラッシュしなかった。"""
        result = _run_hook(input_text="")
        assert result.returncode == 0
        assert result.stdout.strip() == ""
        assert _read_jsonl_lines() == []


# ---------------------------------------------------------------------------
# TestRotation: 1MB 超で末尾500行へローテーション
# ---------------------------------------------------------------------------

class TestRotation:
    """jsonl が 1MB を超えた場合に末尾500行へローテーションする NFR を固定した。"""

    def test_rotation_truncates_to_tail_when_over_1mb(self) -> None:
        """1MB 超のダミー jsonl に対し新規追記後、行数が500+1件以下に切り詰められた。"""
        STATE_JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
        _write_bulk_jsonl(STATE_JSONL_PATH, n_lines=3200, filler_size=350)
        pre_size = STATE_JSONL_PATH.stat().st_size
        assert pre_size > 1024 * 1024, "テスト前提: ダミー jsonl が 1MB を超えていなかった"

        _write_tier_selection(tier="sonnet", suggested_model="sonnet", mode="thompson")
        sid = _new_session_id()
        result = _run_hook(
            _agent_payload("developer", session_id=sid, prompt="ROTATION_MARKER")
        )
        assert result.returncode == 0

        lines = _read_jsonl_lines()
        assert 0 < len(lines) <= 501, f"ローテーション後の行数が想定外だった: {len(lines)}"
        # 全行が破損なく parse 済み（_read_jsonl_lines 内の json.loads で保証済み）。
        assert lines[-1]["session_id"] == sid
        assert lines[-1]["prompt_prefix"].startswith("ROTATION_MARKER")


# ---------------------------------------------------------------------------
# TestConcurrency: 20並行 append で破損 0・行数一致
# ---------------------------------------------------------------------------

class TestConcurrency:
    """20並行 subprocess 追記で全行 parse 可能（破損 0）・行数一致の NFR を固定した。

    改訂（2026-08-13・lock-retry フェーズ）: 新仕様では OS ファイルロック
    （Windows `msvcrt.locking LK_NBLCK` / POSIX `fcntl.flock LOCK_EX|LOCK_NB`）
    による非ブロック試行＋ms 粒度リトライ（ジッター付き・締切 5 秒）により
    直列化されます。締切超過またはロック open 失敗時は「追記せず stderr に
    固定文言」で断念し、無ロック追記の経路は完全に除去されました。
    本テストは新実装後も回帰性を維持し、「20 並行追記で全行 parse 可能・
    破損 0・行数一致」を判定基準として固定します（判定コード・assert 内容は
    変更しない）。
    """

    def test_20_parallel_appends_all_lines_parseable_and_count_matches(self) -> None:
        """20並行起動後、jsonl の行数が20と一致し全行 json.loads 可能だった。"""
        _write_tier_selection(tier="haiku", suggested_model="haiku", mode="thompson")
        session_ids = [_new_session_id() for _ in range(20)]

        def _invoke(sid: str) -> subprocess.CompletedProcess:
            return _run_hook(_agent_payload("developer", session_id=sid))

        with ThreadPoolExecutor(max_workers=20) as executor:
            results = list(executor.map(_invoke, session_ids))

        assert all(r.returncode == 0 for r in results)

        # 壊れ行は skip される設計のため、まず生の行数と parse 済み行数を両方確認する。
        raw_lines = [
            line
            for line in STATE_JSONL_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        parsed_lines = _read_jsonl_lines()
        assert len(raw_lines) == 20, f"生の行数が20と一致しなかった: {len(raw_lines)}"
        assert len(parsed_lines) == 20, (
            f"parse 可能な行数が20と一致しなかった（破損検出）: {len(parsed_lines)}"
        )
        assert {line["session_id"] for line in parsed_lines} == set(session_ids)


# ---------------------------------------------------------------------------
# F-7（改訂）: ロック取得失敗時は追記せず断念する
# ---------------------------------------------------------------------------


class TestLockAcquisitionFailure:
    """F-7（改訂）: `_acquire_lock` が False を返した場合（締切超過・ロックファイル open 失敗）、
    追記せずに stderr に原因別の警告文を出して return する新契約を固定した。

    v2.69 改訂（2026-08-13）: 旧契約「ロック取得失敗でもベストエフォートで追記」から
    新契約「ロック取得失敗なら追記なし・断念」へ逆転しました。
    無ロック追記は他プロセスの行を上書きしうる欠陥があるため、
    断念設計に統一し挙動を決定的にしました（architecture ADR-1）。

    検査水準（DC-GP-004）: 本クラスの 2 ケースも `TestP3FixedWarningText` と同水準
    （警告行の**完全一致**＋`row["prompt_prefix"]` に仕込んだ外部由来文字列が stderr に
    出現しないこと）で固定する。旧版は `"reason=..." in captured.err` の部分一致のみで、
    将来 `f"...{exc}"` のように外部由来文字列を足しても緑のまま通る弱い検査だった。
    """

    def test_no_append_when_acquire_lock_fails_and_stderr_warning_issued(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """_acquire_lock が False を返した（締切超過相当）場合、追記されず stderr に
        reason=deadline の警告が出た。"""
        mod = _load_autoapply_module()
        jsonl_path = tmp_path / "tier_autoapply.jsonl"
        monkeypatch.setattr(mod, "APPLIED_STATE_PATH", str(jsonl_path))

        # _acquire_lock を False 返しに差し替え（ロック取得失敗を模擬）
        monkeypatch.setattr(mod, "_acquire_lock", lambda lock_f: False)

        row = {
            "ts": "2026-08-13T00:00:00+00:00",
            "session_id": "sess-lockfail",
            "subagent_type": "developer",
            "role_recorded": "developer",
            "model_applied": "sonnet",
            "source": "injected",
            "prompt_prefix": _SECRET_PROMPT,
        }
        mod._append_applied_state(row)

        # jsonl は追記されず空
        if jsonl_path.is_file():
            lines = [
                line
                for line in jsonl_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            assert len(lines) == 0, "ロック取得失敗時に jsonl が追記されてしまった"

        # stderr の警告行が固定文言と完全一致し、外部由来文字列を含まない（DC-GP-004）
        captured = capsys.readouterr()
        _assert_fixed_warning(captured.err, "deadline", row)

    def test_no_append_when_lockfile_open_fails_and_stderr_warning_issued(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """ロックファイル open が失敗した場合（lock_f is None）、追記されず
        stderr に reason=lockfile-open-failed の警告が出た。"""
        mod = _load_autoapply_module()
        jsonl_path = tmp_path / "tier_autoapply.jsonl"
        lock_path = tmp_path / "tier_autoapply.lock"
        monkeypatch.setattr(mod, "APPLIED_STATE_PATH", str(jsonl_path))
        monkeypatch.setattr(mod, "LOCK_FILE_PATH", str(lock_path))

        # lock_path のディレクトリを削除してしまい、open が PermissionError / FileNotFoundError を発生させるよう設定
        # （簡易的には、ロックファイルのパスをアクセス不可のディレクトリに変更）
        monkeypatch.setattr(mod, "LOCK_FILE_PATH", "/invalid/path/that/cannot/be/opened.lock")

        row = {
            "ts": "2026-08-13T00:00:00+00:00",
            "session_id": "sess-open-fail",
            "subagent_type": "developer",
            "role_recorded": "developer",
            "model_applied": "sonnet",
            "source": "injected",
            "prompt_prefix": _SECRET_PROMPT,
        }
        mod._append_applied_state(row)

        # jsonl は追記されず空
        if jsonl_path.is_file():
            lines = [
                line
                for line in jsonl_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            assert len(lines) == 0, "open 失敗時に jsonl が追記されてしまった"

        # stderr の警告行が固定文言と完全一致し、外部由来文字列を含まない（DC-GP-004）
        captured = capsys.readouterr()
        _assert_fixed_warning(captured.err, "lockfile-open-failed", row)


# ---------------------------------------------------------------------------
# TestPathResolution: _CLAUDE_DIR 機構による writer/reader パス一致（DC-AS-003）
# ---------------------------------------------------------------------------

class TestPathResolution:
    """`_CLAUDE_DIR` 機構が hooks 配置・scripts 配置の両方から同一 .claude/state/ に解決する契約を固定した。

    T4（tier_gap_check.py）が実装されるまでは 3 者一致のうち writer（hooks/）
    と record（skills/dev-workflow/scripts/）の 2 者一致のみをここで固定し、
    gap_check との 3 者一致は T4 側の TestPathResolution で追加固定する
    （architecture §3-7・§7-1）。
    """

    def test_hook_writes_to_claude_dir_state_tier_autoapply_jsonl(self) -> None:
        """hooks/（1階層遡り）と scripts/（3階層遡り）が同一 .claude に解決し、hook が実際にその配下の tier_autoapply.jsonl へ書いた。

        パス算出そのもの（`hooks_claude_dir == scripts_claude_dir`）はリポジトリ
        構造から自明に成立するため、この事実確認だけを単独テストにはしない
        （hook 未実装でも Pass してしまい Red の単一起因を薄める）。hook を実際に
        起動しその書き込み先まで固定するテストに含めることで、Red 段階では
        FileNotFoundError で一貫して失敗する構成にした。
        """
        hooks_claude_dir = HOOK_PATH.parent.parent
        scripts_claude_dir = RECORD_SCRIPT_PATH.parent.parent.parent.parent
        assert hooks_claude_dir == scripts_claude_dir
        assert hooks_claude_dir.name == ".claude"

        _write_tier_selection(tier="haiku", suggested_model="haiku", mode="thompson")
        sid = _new_session_id()
        result = _run_hook(_agent_payload("developer", session_id=sid))
        assert result.returncode == 0

        expected_path = hooks_claude_dir / "state" / "tier_autoapply.jsonl"
        assert expected_path == STATE_JSONL_PATH
        lines = _read_jsonl_lines(expected_path)
        assert len(lines) == 1
        assert lines[0]["session_id"] == sid


# ---------------------------------------------------------------------------
# TestTsFormat: jsonl の ts が UTC ISO8601 秒精度プロファイルであること（round4）
# ---------------------------------------------------------------------------

class TestTsFormat:
    """jsonl 行の ts が agent_outcomes.ts（db.py:1046）と同一 UTC ISO8601 秒精度プロファイルである契約を固定した。

    跨りソース `ts_floor` 辞書順比較（T4・DC-AS-001 round4）はこのプロファイル
    統一を成立条件とするため、ローカルオフセット（+09:00）・naive・小数秒付きで
    書かれると T4 側の判定が静かに壊れる。この観点をここで先に固定する。
    """

    def test_ts_matches_utc_seconds_regex(self) -> None:
        """ts が `^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\+00:00$` に一致した。"""
        _write_tier_selection(tier="haiku", suggested_model="haiku", mode="thompson")
        sid = _new_session_id()
        result = _run_hook(_agent_payload("developer", session_id=sid))
        assert result.returncode == 0

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        ts = lines[0]["ts"]
        assert _TS_UTC_SECONDS_RE.match(ts), f"ts が UTC 秒精度プロファイルでなかった: {ts!r}"

    def test_ts_roundtrips_via_fromisoformat_without_microseconds(self) -> None:
        """ts が fromisoformat 往復で UTC offset・小数秒なしを保ったまま再構成一致した。"""
        _write_tier_selection(tier="sonnet", suggested_model="sonnet", mode="thompson")
        sid = _new_session_id()
        result = _run_hook(_agent_payload("developer", session_id=sid))
        assert result.returncode == 0

        lines = _read_jsonl_lines()
        ts = lines[0]["ts"]
        parsed = datetime.fromisoformat(ts)
        assert parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0
        assert parsed.microsecond == 0
        assert parsed.isoformat(timespec="seconds") == ts


# ---------------------------------------------------------------------------
# TestPromptPrefix: prompt_prefix の 200字切り詰め・制御文字除去
# ---------------------------------------------------------------------------

class TestPromptPrefix:
    """prompt_prefix が先頭200字に切り詰められ制御文字が除去される契約を固定した（architecture §3-3）。"""

    def test_prompt_prefix_truncated_to_200_chars(self) -> None:
        """201字超の prompt が prompt_prefix で200字に切り詰められた。"""
        long_prompt = "あ" * 250
        _write_tier_selection(tier="haiku", suggested_model="haiku", mode="thompson")
        sid = _new_session_id()
        result = _run_hook(_agent_payload("developer", session_id=sid, prompt=long_prompt))
        assert result.returncode == 0

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert len(lines[0]["prompt_prefix"]) <= 200

    def test_prompt_prefix_strips_control_characters(self) -> None:
        """prompt に含まれる制御文字（\\r\\n\\t・U+2028・U+2029）が prompt_prefix から除去された。"""
        dirty_prompt = f"line1\r\nline2\ttab{_LS}sep{_PS}para"
        _write_tier_selection(tier="haiku", suggested_model="haiku", mode="thompson")
        sid = _new_session_id()
        result = _run_hook(_agent_payload("developer", session_id=sid, prompt=dirty_prompt))
        assert result.returncode == 0

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        prefix = lines[0]["prompt_prefix"]
        for forbidden in ("\r", "\n", "\t", _LS, _PS):
            assert forbidden not in prefix, f"制御文字 {forbidden!r} が残存していた"


# ---------------------------------------------------------------------------
# TestTaskIdExtraction: C3_TASK_ID マーカー抽出（T8・Red フェーズ）
# ---------------------------------------------------------------------------
#
# `_extract_task_id` と row の `task_id` フィールドは本 Red フェーズ時点で
# 未実装だった（architecture-report-20260707-163654.md §3/§4）。当時の row は
# 7 フィールドのみで `task_id` キーを持たなかったため、以下は全て
# `KeyError`（"task_id" が row に存在しない）または `AssertionError`
# （値が期待と異なる）のいずれかで失敗するのが正しい Red 挙動だった。


class TestTaskIdExtraction:
    """起動プロンプトの `C3_TASK_ID:` マーカーから task_id を抽出し jsonl に
    載せる契約を固定した（architecture §3-1〜§3-6・plan test-t1 (a)〜(i)）。
    """

    def test_marker_present_extracts_exact_task_id(self) -> None:
        """(a) 正常マーカー `C3_TASK_ID: dev-login` → task_id が正確値 "dev-login" で記録された。"""
        sid = _new_session_id()
        result = _run_hook(
            _agent_payload("developer", session_id=sid, prompt="C3_TASK_ID: dev-login")
        )
        assert result.returncode == 0

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert "task_id" in lines[0], "task_id キーが row に存在しなかった（未実装）"
        assert lines[0]["task_id"] == "dev-login"

    def test_marker_absent_task_id_key_present_and_null(self) -> None:
        """(b) マーカー不在（逐次経路相当）→ task_id キーは常時出力され値は null（ADR-T8-3）。"""
        sid = _new_session_id()
        result = _run_hook(
            _agent_payload(
                "developer", session_id=sid, prompt="通常のタスク本文（マーカーなし）"
            )
        )
        assert result.returncode == 0

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert "task_id" in lines[0], "task_id キーが row に存在しなかった（未実装）"
        assert lines[0]["task_id"] is None

    def test_marker_not_at_line_start_is_ignored(self) -> None:
        """(c) 行頭以外に出現する偽マーカー `... C3_TASK_ID: fake ...` → 非マッチで null。"""
        sid = _new_session_id()
        result = _run_hook(
            _agent_payload(
                "developer",
                session_id=sid,
                prompt="本文中に ... C3_TASK_ID: fake ... という記述がある",
            )
        )
        assert result.returncode == 0

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert "task_id" in lines[0], "task_id キーが row に存在しなかった（未実装）"
        assert lines[0]["task_id"] is None

    def test_overlong_task_id_201_chars_is_non_matching(self) -> None:
        """(d) 過長（201字＝`{1,200}` 上限超）の id → 非マッチで null。"""
        overlong_id = "a" * 201
        sid = _new_session_id()
        result = _run_hook(
            _agent_payload(
                "developer", session_id=sid, prompt=f"C3_TASK_ID: {overlong_id}"
            )
        )
        assert result.returncode == 0

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert "task_id" in lines[0], "task_id キーが row に存在しなかった（未実装）"
        assert lines[0]["task_id"] is None

    def test_disallowed_characters_are_non_matching(self) -> None:
        """(e) 許容外文字（空白・`=`・日本語）を含む id → いずれも非マッチで null。"""
        disallowed_prompts = [
            "C3_TASK_ID: dev login",  # 空白混入
            "C3_TASK_ID: dev=login",  # '=' 混入
            "C3_TASK_ID: 日本語タスク",  # 日本語
        ]
        for prompt in disallowed_prompts:
            sid = _new_session_id()
            result = _run_hook(_agent_payload("developer", session_id=sid, prompt=prompt))
            assert result.returncode == 0

            lines = _read_jsonl_lines()
            assert "task_id" in lines[-1], "task_id キーが row に存在しなかった（未実装）"
            assert lines[-1]["task_id"] is None, f"許容外文字で誤マッチした: {prompt!r}"

    def test_multiple_markers_only_first_is_adopted(self) -> None:
        """(f) 複数行・複数マーカー → 最初の 1 個のみ採用される（re.search first-match）。"""
        sid = _new_session_id()
        prompt = "C3_TASK_ID: task-one\nC3_TASK_ID: task-two\n本文"
        result = _run_hook(_agent_payload("developer", session_id=sid, prompt=prompt))
        assert result.returncode == 0

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert "task_id" in lines[0], "task_id キーが row に存在しなかった（未実装）"
        assert lines[0]["task_id"] == "task-one"

    def test_delimiter_variance_is_non_matching(self) -> None:
        """(g) 区切りゆらぎ（タブ・スペース2個・全角コロン）→ いずれも非マッチで null。"""
        variant_prompts = [
            "C3_TASK_ID:\ttask-x",  # タブ区切り
            "C3_TASK_ID:  task-x",  # スペース2個
            "C3_TASK_ID：task-x",  # 全角コロン（U+FF1A）
        ]
        for prompt in variant_prompts:
            sid = _new_session_id()
            result = _run_hook(_agent_payload("developer", session_id=sid, prompt=prompt))
            assert result.returncode == 0

            lines = _read_jsonl_lines()
            assert "task_id" in lines[-1], "task_id キーが row に存在しなかった（未実装）"
            assert lines[-1]["task_id"] is None, f"区切りゆらぎで誤マッチした: {prompt!r}"

    def test_secret_pattern_in_prompt_does_not_contaminate_task_id(self) -> None:
        """(h) `token=...` 等の秘密パターンを含む prompt でも task_id が汚染されない。"""
        sid = _new_session_id()
        prompt = "C3_TASK_ID: dev-login\ntoken=sk-ABCDEFGHIJKLMNOP1234567890"
        result = _run_hook(_agent_payload("developer", session_id=sid, prompt=prompt))
        assert result.returncode == 0

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert "task_id" in lines[0], "task_id キーが row に存在しなかった（未実装）"
        assert lines[0]["task_id"] == "dev-login"
        assert "token" not in lines[0]["task_id"]
        assert "sk-" not in lines[0]["task_id"]

    def test_tester_role_also_gets_task_id_in_row(self) -> None:
        """(i) LAUNCH_LOG_ROLES の tester でも row に task_id が載る（抽出は全記録 role 共通・§3-5）。"""
        sid = _new_session_id()
        result = _run_hook(
            _agent_payload("tester", session_id=sid, prompt="C3_TASK_ID: qa-check")
        )
        assert result.returncode == 0

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert lines[0]["role_recorded"] == "tester"
        assert "task_id" in lines[0], "task_id キーが row に存在しなかった（未実装）"
        assert lines[0]["task_id"] == "qa-check"


# ---------------------------------------------------------------------------
# TestMarkerStringStartAnchor: マーカーの文字列先頭アンカー \A 化（SR-AI-001 /
# DC-AS-001）。行頭 ^（re.MULTILINE）が誤抽出した敵対的入力を None へ落とし、
# parallel の正しい配置（1 行目マーカー + 2 行目ガード指示）で抽出が成立する
# ことを固定する。
# ---------------------------------------------------------------------------

class TestMarkerStringStartAnchor:
    """`\\A` 文字列先頭アンカーにより本文孤立行・フェンス内・2 行目マーカーが
    非マッチになり、先頭 1 行目マーカーのみが抽出源になる契約を固定した。
    """

    def test_marker_on_body_isolated_line_is_ignored(self) -> None:
        """(1) 本文の孤立行に置かれたマーカー（先頭ではない）→ 非マッチで null。"""
        sid = _new_session_id()
        prompt = (
            "あなたは developer です。\n"
            "以下の作業を行ってください。\n"
            "---\n"
            "C3_TASK_ID: test-fake\n"
            "---\n"
            "実際の作業指示..."
        )
        result = _run_hook(_agent_payload("developer", session_id=sid, prompt=prompt))
        assert result.returncode == 0

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert lines[0]["task_id"] is None, "本文孤立行のマーカーが誤抽出された"

    def test_marker_inside_code_fence_is_ignored(self) -> None:
        """(2) コードフェンス内に置かれたマーカー（先頭ではない）→ 非マッチで null。"""
        sid = _new_session_id()
        prompt = "```\nC3_TASK_ID: test-x\n```"
        result = _run_hook(_agent_payload("developer", session_id=sid, prompt=prompt))
        assert result.returncode == 0

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert lines[0]["task_id"] is None, "フェンス内マーカーが誤抽出された"

    def test_marker_on_second_line_is_ignored(self) -> None:
        """(3) 2 行目に置かれたマーカー（旧 parallel 配置＝アンチパターン）→ 非マッチで null。

        `\\A` 化により、PO_WORKTREE_GUARD 行の直後（2 行目）へマーカーを置く旧配置は
        構造的に抽出されなくなった（DC-AS-001 が検出した衝突を固定）。
        """
        sid = _new_session_id()
        prompt = (
            "Bash でまず以下を実行: `export PO_WORKTREE_GUARD=1`\n"
            "C3_TASK_ID: test-login\n"
            "本文..."
        )
        result = _run_hook(_agent_payload("developer", session_id=sid, prompt=prompt))
        assert result.returncode == 0

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert lines[0]["task_id"] is None, "2 行目マーカーが誤抽出された"

    def test_marker_on_first_line_is_adopted(self) -> None:
        """(4) 先頭 1 行目のみに置かれたマーカー → 正確値で抽出（ベースライン不変）。"""
        sid = _new_session_id()
        prompt = "C3_TASK_ID: test-login\n本文..."
        result = _run_hook(_agent_payload("developer", session_id=sid, prompt=prompt))
        assert result.returncode == 0

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert lines[0]["task_id"] == "test-login"

    def test_real_parallel_structure_first_line_marker_injects(self) -> None:
        """(5) 実 parallel 構造正例（1 行目マーカー + 2 行目ガード指示文）→ 抽出成立し注入される。

        マーカー位置統一後の正しい配置。wt_tester + roles.tester.tier=sonnet で
        updatedInput.model が注入され task_id が "test-login" になることを固定する。
        """
        _write_tier_selection(
            tier="haiku", suggested_model="haiku", mode="thompson",
            roles={"tester": {"tier": "sonnet", "mode": "thompson"}},
        )
        sid = _new_session_id()
        prompt = (
            "C3_TASK_ID: test-login\n"
            "Bash でまず以下を実行: `export PO_WORKTREE_GUARD=1`\n"
            "本文..."
        )
        result = _run_hook(
            _agent_payload(
                "wt_tester", isolation="worktree", session_id=sid, prompt=prompt
            )
        )
        assert result.returncode == 0
        stdout = json.loads(result.stdout)
        updated = stdout["hookSpecificOutput"]["updatedInput"]
        assert updated["model"] == "sonnet"

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        assert lines[0]["source"] == "injected"
        assert lines[0]["role_recorded"] == "tester"
        assert lines[0]["model_applied"] == "sonnet"
        assert lines[0]["task_id"] == "test-login"


# ---------------------------------------------------------------------------
# TestP2Bounded: P2（有界性）- ロック取得待ちが締切内で収束
# ---------------------------------------------------------------------------


class TestP2Bounded:
    """P2（有界性）: hook が 1 回の追記でブロックする時間は締切（既定 5 秒）＋追記処理時間を超えない。

    別ハンドルでロックを保持し締切を短縮（monkeypatch）した状態で
    _append_applied_state を呼ぶと、経過時間が「締切＋余裕」以内で戻ることを
    assert する（architecture-report §4 テスト戦略 P2）。
    """

    def test_bounded_wait_time_respects_shortened_deadline(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """別ハンドルでロック保持＋締切 0.5 秒に短縮した場合、経過時間が 1 秒以内で戻る。

        ロック保持は取得成功保証型ヘルパー `_hold_os_lock` に集約した（CR-M-001）。
        保持に失敗した場合はヘルパー内で `pytest.fail` するため、以下の assert は
        **無条件**に実行される（旧 `if lock_held:` ガードを撤去・DC-GP-002）。
        """
        mod = _load_autoapply_module()
        jsonl_path = tmp_path / "tier_autoapply.jsonl"
        lock_path = tmp_path / "tier_autoapply.lock"
        monkeypatch.setattr(mod, "APPLIED_STATE_PATH", str(jsonl_path))
        monkeypatch.setattr(mod, "LOCK_FILE_PATH", str(lock_path))

        # 締切を 0.5 秒に短縮
        monkeypatch.setattr(mod, "_LOCK_DEADLINE_SEC", 0.5)

        row = {
            "ts": "2026-08-13T00:00:00+00:00",
            "session_id": "sess-bounded",
            "subagent_type": "developer",
            "role_recorded": "developer",
            "model_applied": "sonnet",
            "source": "injected",
            "prompt_prefix": "",
        }

        with _hold_os_lock(lock_path):
            start = time.monotonic()
            mod._append_applied_state(row)
            elapsed = time.monotonic() - start

        # 期待値: 締切 0.5 秒＋余裕 0.5 秒 ≈ 1.0 秒以内
        assert elapsed < 1.0, (
            f"経過時間が 1.0 秒を超過（締切内に戻らない）: {elapsed:.2f}s"
        )
        # 締切超過で断念したため追記は発生していない（P1・有界性の裏側の性質）。
        assert not jsonl_path.is_file() or jsonl_path.read_text(encoding="utf-8").strip() == ""


# ---------------------------------------------------------------------------
# TestP4NoSync: P4（無同期性）- ジッター脱同期検証
# ---------------------------------------------------------------------------


class TestP4NoSync:
    """P4（無同期性）: 複数プロセスのリトライが同一時刻境界に集中しない（ジッターにより脱同期）。

    別ハンドルで**実 OS ロックを保持**した状態で `_append_applied_state` を呼び、
    `_acquire_lock` のリトライループを実際に走らせる。`time.sleep` を記録付きに
    差し替えて待ち時間の列を収集し、(1) リトライが 2 回以上発生したこと・
    (2) 待ち時間が同一値に固定されていないこと の**両方を無条件 assert** する
    （architecture-report §4 テスト戦略 P4・CR-NEW 是正）。

    旧版はロックファイルを `touch()` するだけで競合を作らず、`sleep_durations` が
    常に空 → `if len(...) > 1:` ガードで assert が 1 度も実行されない「空の緑」だった
    （CR-NEW High）。本版は取得成功保証型ヘルパー `_hold_os_lock` で競合を保証し、
    条件付きガードを撤去している（DC-GP-002）。
    """

    def test_jitter_generates_varying_sleep_durations(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """実ロック競合下で複数回のリトライ sleep が発生し、待ち時間が同一値に固定されていない。"""
        mod = _load_autoapply_module()
        jsonl_path = tmp_path / "tier_autoapply.jsonl"
        lock_path = tmp_path / "tier_autoapply.lock"
        monkeypatch.setattr(mod, "APPLIED_STATE_PATH", str(jsonl_path))
        monkeypatch.setattr(mod, "LOCK_FILE_PATH", str(lock_path))

        # 期待リトライ回数の根拠（DC-AM-003）: 締切 0.25 秒 ÷ リトライ 1 回の最大待ち 0.04 秒
        # （_LOCK_RETRY_INTERVAL_SEC 0.02 + _LOCK_RETRY_JITTER_SEC 0.02）= 6.25 なので、
        # 待ちが毎回最大値を引く最悪ケースでも 6 回以上リトライする（下限 assert の 2 回に 3 倍のマージン）。
        monkeypatch.setattr(mod, "_LOCK_DEADLINE_SEC", 0.25)
        assert mod._LOCK_DEADLINE_SEC >= 5 * (
            mod._LOCK_RETRY_INTERVAL_SEC + mod._LOCK_RETRY_JITTER_SEC
        ), "短縮締切がリトライ 1 回の最大待ちの 5 倍未満（リトライ回数の下限が保証されない）"

        sleep_durations: list[float] = []
        real_sleep = time.sleep

        def _recording_sleep(duration: float) -> None:
            sleep_durations.append(duration)
            real_sleep(duration)  # 実待機を維持し締切との数値関係を壊さない

        row = {
            "ts": "2026-08-13T00:00:00+00:00",
            "session_id": "sess-jitter",
            "subagent_type": "developer",
            "role_recorded": "developer",
            "model_applied": "sonnet",
            "source": "injected",
            "prompt_prefix": "",
        }

        with _hold_os_lock(lock_path):
            with monkeypatch.context() as m:
                m.setattr(time, "sleep", _recording_sleep)
                mod._append_applied_state(row)

        # (1) リトライが実際に発生した（空の緑の再発検知・無条件）
        assert len(sleep_durations) >= 2, (
            f"リトライ sleep が 2 回未満（ロック競合が発生していない疑い）: {sleep_durations}"
        )
        # (2) 待ち時間が同一値に固定されていない＝ジッターによる脱同期（無条件）
        assert len(set(sleep_durations)) > 1, (
            f"sleep 時間がすべて同一値に固定されている（脱同期なし）: {sleep_durations}"
        )


# ---------------------------------------------------------------------------
# TestP5InjectionIndependent: P5（注入独立性）- 記録断念と注入の独立性
# ---------------------------------------------------------------------------


class TestP5InjectionIndependent:
    """P5（注入独立性・改訂 1・DC-AM-001）: 記録（_append_applied_state）の断念は
    注入判定・updatedInput の stdout 出力に一切影響しない。

    記録と注入は独立。main() は追記結果を制御フローに使わない。
    """

    def test_injection_continues_even_when_append_fails_subprocess(self) -> None:
        """別ハンドルでロック保持したまま hook を subprocess 起動。
        断念（stderr の reason=deadline）と同時に stdout に
        hookSpecificOutput.updatedInput.model が出力されることを確認した。

        このテスト 1 本は実締切約 5 秒かかることを許容する（DC-GP-002・plan 契約で
        明記。新実装の締切 `_LOCK_DEADLINE_SEC=5.0` 秒を待つため。単体実行の実測値も
        約 5.6 秒・CR-M-003）。

        新契約 P5 の検証: hook が実際に使うロックパス
        (`STATE_JSONL_PATH + ".lock"`) に別ハンドルでロックを保持
        したまま hook を subprocess 起動。

        （旧実装。参考）ブロッキング LK_LOCK＋無ロック追記では：
          - ロック取得失敗（10 秒リトライ枯渇）→ 無ロック追記に落ちて
            jsonl に行を追記・stdout に updatedInput あり
          - stderr には warning なし（無ロック追記が成功するため）

        現行（新）実装（非ブロック試行＋5 秒締切リトライ）では：
          - 5 秒待機 → 締切超過 → 断念
          - stderr に "reason=deadline" が出て、jsonl に追記なし
          - **注入は旧実装でも新実装でも継続する**（P5 の本質）
            新実装では記録は断念するが hookSpecificOutput.updatedInput
            は stdout に必ず出力される（記録と注入の独立性）

        ロック保持は取得成功保証型ヘルパー `_hold_os_lock` に集約した（CR-M-001）。
        旧版のスレッド + `threading.Event` 保持は畳んだが、解放は contextmanager の
        `finally` が構造的に保証するため「即時解放・フルスイートをハングさせない」性質は
        維持される（DC-GP-002 (b)。ロックはハンドル単位でありスレッド単位ではないため、
        呼び出しスレッドで保持したまま subprocess を起動しても排他は同じく成立する）。
        """
        # hook が実際に使うロックパス（hook 内では APPLIED_STATE_PATH + ".lock"）
        lock_path = STATE_JSONL_PATH.parent / (STATE_JSONL_PATH.name + ".lock")

        _write_tier_selection(tier="haiku", suggested_model="haiku", mode="thompson")
        sid = _new_session_id()

        with _hold_os_lock(lock_path):
            # この時点で本プロセスの別ハンドルが hook のロックパスを保持中。
            # hook subprocess はロック取得で締切まで待機してから断念する。
            result = _run_hook(_agent_payload("developer", session_id=sid))

        # hook が起動されたことを確認
        assert result.returncode == 0, f"hook 起動に失敗: {result.stderr}"

        # P5 新契約検証：ロック保持中でも注入は継続する
        assert "reason=deadline" in result.stderr, (
            "stderr に 'reason=deadline' が出ていない（断念経路に入っていない・ロック競合なし）"
        )

        # P5 の本質：記録の断念と注入の独立性
        assert result.stdout.strip(), "stdout が空（注入されていない）"
        stdout = json.loads(result.stdout)
        assert "hookSpecificOutput" in stdout, "hookSpecificOutput キーが無い"
        assert "updatedInput" in stdout["hookSpecificOutput"], "updatedInput キーが無い"
        assert stdout["hookSpecificOutput"]["updatedInput"].get("model") is not None, (
            "updatedInput.model が None（注入が成立していない）"
        )


# ---------------------------------------------------------------------------
# TestP1NoUnlockedAppend: P1（完全性）- 無ロック追記経路の廃止
# ---------------------------------------------------------------------------


class TestP1NoUnlockedAppend:
    """P1（完全性）: N 並列で追記した行は、ロックが取得できた分は 1 行も欠けず・
    1 行も壊れずに jsonl に残る。無ロックで jsonl へ書く経路はコード上存在しない。

    _acquire_lock を False 返しに固定した場合、追記が一切発生しないこと
    （断念経路のみ実行される）を確認する。
    """

    def test_no_append_without_lock(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """_acquire_lock が常に False を返す（ロック無し状態）場合、
        追記は発生せず jsonl は空のままだった。"""
        mod = _load_autoapply_module()
        jsonl_path = tmp_path / "tier_autoapply.jsonl"
        monkeypatch.setattr(mod, "APPLIED_STATE_PATH", str(jsonl_path))

        # _acquire_lock を常に False 返し
        monkeypatch.setattr(mod, "_acquire_lock", lambda lock_f: False)

        row = {
            "ts": "2026-08-13T00:00:00+00:00",
            "session_id": "sess-nolck",
            "subagent_type": "developer",
            "role_recorded": "developer",
            "model_applied": "sonnet",
            "source": "injected",
            "prompt_prefix": "",
        }

        mod._append_applied_state(row)

        # jsonl は追記されず空
        if jsonl_path.is_file():
            content = jsonl_path.read_text(encoding="utf-8").strip()
            assert content == "", "ロック無し状態でも jsonl に追記されてしまった（P1 違反）"


# ---------------------------------------------------------------------------
# TestP3FixedWarningText: P3（可観測性）- 原因別固定文言
# ---------------------------------------------------------------------------


def _abort_row(session_id: str) -> dict:
    """断念経路テスト用の row（外部由来文字列 `_SECRET_PROMPT` を仕込み済み）を返した。"""
    return {
        "ts": "2026-08-13T00:00:00+00:00",
        "session_id": session_id,
        "subagent_type": "developer",
        "role_recorded": "developer",
        "model_applied": "sonnet",
        "source": "injected",
        "prompt_prefix": _SECRET_PROMPT,  # 外部由来文字列
    }


def _assert_no_append(jsonl_path: Path, message: str) -> None:
    """jsonl が未作成または空であること（断念で追記が発生していないこと）を検査した。"""
    if jsonl_path.is_file():
        lines = [
            line
            for line in jsonl_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(lines) == 0, message


class TestP3FixedWarningText:
    """P3（可観測性）: 追記を断念した場合は stderr に 1 行の証跡を残す。
    警告文は固定文言＋原因識別子のみで構成し、外部由来文字列を含めない。

    原因識別子は **4 値**: reason=deadline / reason=lockfile-open-failed /
    reason=mkdir-failed / reason=write-failed（architecture 改訂 3・§1 P3・§2-2。
    改訂 1 時点の「2 値」からの拡張＝DC-AM-001 / SR-NEW / CR-E-002。DC-GP-005 で
    旧契約 docstring の残置を是正）。

    射程（改訂 3・DC-AM-002）: 本クラスが固定するのは `_append_applied_state` が
    記録処理（mkdir 以降）へ到達した後の断念 4 経路。冒頭の symlink 検査による
    沈黙 skip は断念でなく**入口ガード**であり射程外（5 値目の識別子は存在しない）。

    検査水準（DC-GP-004）: 4 経路すべてを `_assert_fixed_warning`（完全一致＋秘密非混入）で
    同水準に固定する。`TestLockAcquisitionFailure` の 2 ケース（deadline /
    lockfile-open-failed）も同じヘルパーへ引き上げ済みで、あちらは「追記されないこと」
    （F-7 の新契約）が主眼、本クラスは「文言そのもの」が主眼という分担にした。
    """

    def test_deadline_warning_is_fixed_text_with_reason_identifier(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """締切超過による断念の場合、stderr に '[tier_autoapply] append skipped: reason=deadline'
        という固定文言のみが出ている（外部由来文字列を含まない）。"""
        mod = _load_autoapply_module()
        jsonl_path = tmp_path / "tier_autoapply.jsonl"
        monkeypatch.setattr(mod, "APPLIED_STATE_PATH", str(jsonl_path))

        # _acquire_lock を False 返し（締切超過相当）
        monkeypatch.setattr(mod, "_acquire_lock", lambda lock_f: False)

        row = _abort_row("sess-deadline-warn")
        mod._append_applied_state(row)

        captured = capsys.readouterr()
        _assert_fixed_warning(captured.err, "deadline", row)
        _assert_no_append(jsonl_path, "締切超過の断念で jsonl が追記されてしまった")

    def test_lockfile_open_failed_warning_is_fixed_text(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """ロックファイル open 失敗による断念の場合、reason=lockfile-open-failed の
        固定文言のみが出ている（外部由来文字列を含まない）。"""
        mod = _load_autoapply_module()
        jsonl_path = tmp_path / "tier_autoapply.jsonl"
        monkeypatch.setattr(mod, "APPLIED_STATE_PATH", str(jsonl_path))
        # ロックファイルのパスを「存在しないディレクトリ配下」に向けて open を失敗させる
        # （POSIX/Windows いずれも OSError 派生になる）。
        monkeypatch.setattr(
            mod, "LOCK_FILE_PATH", str(tmp_path / "no-such-dir" / "x.lock")
        )

        row = _abort_row("sess-open-failed-warn")
        mod._append_applied_state(row)

        captured = capsys.readouterr()
        _assert_fixed_warning(captured.err, "lockfile-open-failed", row)
        _assert_no_append(jsonl_path, "open 失敗の断念で jsonl が追記されてしまった")

    def test_mkdir_failed_warning_is_fixed_text(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """state ディレクトリ作成失敗（os.makedirs が OSError）による断念の場合、
        reason=mkdir-failed の固定文言のみが出て追記されない（CR-T-001 Low）。"""
        mod = _load_autoapply_module()
        jsonl_path = tmp_path / "state" / "tier_autoapply.jsonl"
        monkeypatch.setattr(mod, "APPLIED_STATE_PATH", str(jsonl_path))
        monkeypatch.setattr(mod, "LOCK_FILE_PATH", str(jsonl_path) + ".lock")

        def _failing_makedirs(*args, **kwargs):
            raise OSError("mkdir denied (injected)")

        row = _abort_row("sess-mkdir-warn")
        # os.makedirs の差し替えはプロセス全体に効くため、対象呼び出しの間だけに閉じる。
        with monkeypatch.context() as m:
            m.setattr(mod.os, "makedirs", _failing_makedirs)
            mod._append_applied_state(row)

        captured = capsys.readouterr()
        _assert_fixed_warning(captured.err, "mkdir-failed", row)
        _assert_no_append(jsonl_path, "mkdir 失敗の断念で jsonl が追記されてしまった")

    def test_write_failed_warning_is_fixed_text(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """ロック取得**成功後**の追記本体（`open(APPLIED_STATE_PATH, "a")`）が OSError の場合、
        reason=write-failed の固定文言が出て追記されない（CR-E-002 のテスト側・**Red**）。

        注入点は実運用に存在する経路である追記本体の `open` に一本化した（DC-GP-003）。
        `_rotate_if_needed` は内部で OSError を全て握るため、そちらを差し替えると
        実運用に存在しない合成経路だけを緑にしてしまう（禁止）。
        """
        mod = _load_autoapply_module()
        jsonl_path = tmp_path / "tier_autoapply.jsonl"
        lock_path = tmp_path / "tier_autoapply.lock"
        monkeypatch.setattr(mod, "APPLIED_STATE_PATH", str(jsonl_path))
        monkeypatch.setattr(mod, "LOCK_FILE_PATH", str(lock_path))

        real_open = builtins.open
        target = str(jsonl_path)

        def _failing_open(file, mode="r", *args, **kwargs):
            # 追記本体（jsonl への追記モード open）だけを失敗させ、他は実 open に委譲する
            # （ロックファイル open・pytest 内部の I/O を巻き込まないため）。
            if isinstance(file, (str, bytes, os.PathLike)) and os.fspath(file) == target:
                if "a" in mode:
                    raise OSError("disk full (injected)")
            return real_open(file, mode, *args, **kwargs)

        row = _abort_row("sess-write-failed-warn")
        # builtins.open の差し替えはプロセス全体に効くため、対象呼び出しの間だけに閉じる。
        with monkeypatch.context() as m:
            m.setattr(builtins, "open", _failing_open)
            mod._append_applied_state(row)

        captured = capsys.readouterr()
        _assert_fixed_warning(captured.err, "write-failed", row)
        _assert_no_append(jsonl_path, "write 失敗の断念で jsonl が追記されてしまった")


# ---------------------------------------------------------------------------
# TestLockModulesAbsent: ロック機構不在環境（ADR-5・DC-AS-002・CR-T-001 Medium）
# ---------------------------------------------------------------------------


class TestLockModulesAbsent:
    """msvcrt / fcntl の**両方が不在**の環境では取得成功（True）とみなして追記する契約を固定した。

    素直な if/elif の bool 化（False 返し）にすると、この環境では毎回締切 5 秒を待って
    常時断念＝「記録の常時全損＋起動ごと 5 秒遅延」となり現行と真逆になる（architecture
    §2-1 ロック機構不在環境の契約・ADR-5）。この分岐を False へ変えるリグレッションを
    検知する回帰網が無かったため追加した（CR-T-001 Medium）。
    """

    def test_both_lock_modules_absent_treated_as_acquired_and_appends(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """msvcrt / fcntl を両方 None にした状態で `_try_os_lock` が True を返し、
        `_append_applied_state` が実際に 1 行追記した（断念の stderr 警告は出ない）。"""
        mod = _load_autoapply_module()
        jsonl_path = tmp_path / "tier_autoapply.jsonl"
        lock_path = tmp_path / "tier_autoapply.lock"
        monkeypatch.setattr(mod, "APPLIED_STATE_PATH", str(jsonl_path))
        monkeypatch.setattr(mod, "LOCK_FILE_PATH", str(lock_path))
        monkeypatch.setattr(mod, "msvcrt", None)
        monkeypatch.setattr(mod, "fcntl", None)

        with open(lock_path, "a+", encoding="utf-8") as lock_f:
            assert mod._try_os_lock(lock_f) is True, (
                "ロック機構両方不在で _try_os_lock が True を返さなかった（ADR-5 違反）"
            )

        row = {
            "ts": "2026-08-13T00:00:00+00:00",
            "session_id": "sess-no-lock-modules",
            "subagent_type": "developer",
            "role_recorded": "developer",
            "model_applied": "sonnet",
            "source": "injected",
            "prompt_prefix": "",
        }
        mod._append_applied_state(row)

        lines = [
            line
            for line in jsonl_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(lines) == 1, f"ロック機構不在環境で追記されなかった: {lines}"
        assert json.loads(lines[0])["session_id"] == "sess-no-lock-modules"

        captured = capsys.readouterr()
        assert "append skipped" not in captured.err, (
            f"ロック機構不在環境で断念していた（常時全損の回帰）: {captured.err!r}"
        )
