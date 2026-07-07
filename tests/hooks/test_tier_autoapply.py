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

import collections
import importlib.util
import json
import os
import re
import subprocess
import sys
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
    """HOOK_PATH をプロセス内 import で直接ロードした（F-7: `_os_lock` monkeypatch 用）。

    `TestConcurrency` 等は subprocess 経由（プロセス境界を跨ぐため内部関数を
    monkeypatch できない）だが、F-7 のロック取得失敗フォールバック検証は
    `_os_lock`/`_append_applied_state` を直接差し替える必要があるため、
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

    round 1 DC-AM-002 → architecture §3-4 は当初「単一 write 追記のみ・排他
    ロック／専用 writer 化は本リリース非対象」を合格ゲートとしていた。しかし
    Windows 実測で 20 並行追記のうち 18/20 行が欠落する事象を観測したため、
    OS ファイルロック（Windows `msvcrt.locking` / POSIX `fcntl.flock`）による
    直列化＋ロック取得失敗時のベストエフォート追記フォールバックを必要機構
    として正式採用した（fix-cycle-1 / code-review-report-20260707-110524.md
    F-2 対応・architecture-report §3-4 改訂）。本テストは「OS ファイルロック
    ＋ベストエフォートフォールバックで直列化された前提で 20 並行破損 0」を
    判定基準として固定した。
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
# F-7: ロック取得失敗時のベストエフォート追記フォールバック
# ---------------------------------------------------------------------------


class TestLockFailureFallback:
    """F-7: `_os_lock` がロック取得に失敗しても追記が欠落しないフォールバックを固定した。

    code-review-report-20260707-110524.md F-7 の指摘（ロック取得失敗
    〔`locked = False`〕分岐の回帰テストが無い）に対応した。現行実装は
    `_append_applied_state` が `_os_lock` の `OSError` を捕捉し
    `locked = False` のままベストエフォートで単一 write 追記へ進む設計が
    既に入っていたため、本テストは実装追加なしで緑だった（設計上 Green。
    plan-report-20260707-111519.md FA1 の受け入れ条件どおり、既存分岐の
    回帰カバレッジとして追加した）。
    """

    def test_append_still_happens_when_os_lock_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """`_os_lock` が OSError を送出しロック未取得のままでも 1 行が欠落せず追記されたことを確認した。"""
        mod = _load_autoapply_module()
        jsonl_path = tmp_path / "tier_autoapply.jsonl"
        monkeypatch.setattr(mod, "APPLIED_STATE_PATH", str(jsonl_path))

        def _raise_oserror(lock_f: object) -> None:
            raise OSError("lock unavailable (simulated for F-7 regression test)")

        monkeypatch.setattr(mod, "_os_lock", _raise_oserror)

        row = {
            "ts": "2026-07-07T00:00:00+00:00",
            "session_id": "sess-lockfail",
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
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["session_id"] == "sess-lockfail"


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
