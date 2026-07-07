"""Tests for fix-cycle-3 security fixes (Med1 + Low5).

security-review-report-20260707-115403.md の指摘 6 件（Med1 + Low5）に対する
Red フェーズテストだった（plan-report-20260707-115920.md fc1-tests）。
対象: `.claude/hooks/tier_gap_check.py` / `.claude/hooks/tier_autoapply.py`。
参照実装: `.claude/hooks/stop.py`（`_INHERIT_SANITIZE_RE`）・
`.claude/skills/dev-workflow/scripts/record_agent_outcome.py`（`_mask_secrets`）。

対象 hook ファイル自体は既に存在するため（tier_gap_check.py・tier_autoapply.py
は fix-cycle-1/2 で実装済み）、本ファイルの Red は「ファイル不在」ではなく
「未実装の是正ロジックによる挙動不一致（AssertionError）」または「型不正値を
直接 `_run_impl`/`_read_applied_tier` へ渡した際に例外が伝播すること」で
成立する（tester/MEMORY.md「fail-safe hook の Red 単一起因は `_run_impl` 等の
内部関数を直接呼んで例外種別を裏取りする」パターンを踏襲）。

各テストクラスが対応する SR item（[対応予定] の指摘番号）:

- item1 (Med・[SR-NEW] stderr 未サニタイズ session_id):
  `TestSessionIdSanitizationBeforeWarn`
- item4 (Low・[SR-V-001] session_id 型検証なし):
  `TestNonStrSessionIdFromTierSelectionFailSafe`
- item3 (Low・[SR-K-003] prompt_prefix 秘密情報平文保存):
  `TestPromptPrefixSecretMasking`
- item2 (Low・[SR-V-002] jsonl/lock symlink 未検証):
  `TestSymlinkSkip`
- item5 (Low・[SR-NEW] 読み取り側サイズ上限なし):
  `TestJsonlReadSizeCap`
- item6 (Low・[SR-NEW] role 集合排他性の機械検証なし):
  `TestLaunchLogRolesDisjointFromReviewerTypes`
  （このクラスのみ現状で Green の想定。実装変更なしでテストのみ追加する
  設計のため、plan-report fc1-tests の指示どおり Red ではなく設計上 Green
  として提出する）

閾値実装の詳細（item5 の 5MB 具体的な打ち切り方式など）は plan/architecture
に明記が無いため、本ファイルが「挙動差（under-detection・fail-safe 側への
倒し）」を検証することで実装契約として固定した。
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from c3 import db as c3_db

WORKTREE_ROOT = Path(__file__).parents[2]
GAP_HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "tier_gap_check.py"
AUTOAPPLY_HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "tier_autoapply.py"
CHECK_INVOCATION_HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "check_agent_invocation.py"
RECORD_SCRIPT_PATH = (
    WORKTREE_ROOT
    / ".claude"
    / "skills"
    / "dev-workflow"
    / "scripts"
    / "record_agent_outcome.py"
)

STATE_JSONL_PATH = WORKTREE_ROOT / ".claude" / "state" / "tier_autoapply.jsonl"
TIER_SELECTION_PATH = WORKTREE_ROOT / ".claude" / "state" / "tier_selection.json"

# U+2028 LINE SEPARATOR / U+2029 PARAGRAPH SEPARATOR（実体文字を埋め込まず chr() で参照。
# tester/MEMORY.md の「Edit/Write に実体文字を直接タイプすると転送経路で化ける」対策）。
_LS = chr(0x2028)
_PS = chr(0x2029)


# ---------------------------------------------------------------------------
# モジュールローダ（対象 hook/script は既に実在するため FileNotFoundError は
# 通常発火しない前提だが、tester/MEMORY.md の既存パターンを踏襲し防御的に
# チェックする）。
# ---------------------------------------------------------------------------


def _load_module(path: Path, name: str) -> types.ModuleType:
    if not path.is_file():
        raise FileNotFoundError(f"対象ファイルが存在しなかった: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


def _load_gap_module() -> types.ModuleType:
    return _load_module(GAP_HOOK_PATH, "tier_gap_check_sec_t")


def _load_autoapply_module() -> types.ModuleType:
    return _load_module(AUTOAPPLY_HOOK_PATH, "tier_autoapply_sec_t")


def _load_check_invocation_module() -> types.ModuleType:
    return _load_module(CHECK_INVOCATION_HOOK_PATH, "check_agent_invocation_sec_t")


def _load_record_module() -> types.ModuleType:
    return _load_module(RECORD_SCRIPT_PATH, "record_agent_outcome_sec_t")


# ---------------------------------------------------------------------------
# gap_check 用共通ヘルパ（test_tier_gap_check.py と同型）
# ---------------------------------------------------------------------------


def _prod_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _create_c3_db(db_path: Path) -> None:
    from c3.migrate import apply_pending_migrations

    apply_pending_migrations(db_path)


def _append_jsonl_row(
    path: Path,
    *,
    ts: str,
    session_id: object,
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
    import sqlite3

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
    """3 パス（jsonl / DB / tier_selection fallback）を全て tmp 隔離先へ差し替えた。"""
    monkeypatch.setattr(mod, "APPLIED_STATE_PATH", str(jsonl_path))
    monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(tier_selection_path))
    monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: db_path)


def _capture_stderr(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """capsys ではなく明示的な StringIO 差し替えで stderr を捕捉した（reconfigure 済み hook対策）。"""
    fake_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", fake_stderr)
    return fake_stderr


@pytest.fixture()
def gap_mod() -> types.ModuleType:
    return _load_gap_module()


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
    return tmp_path / "state" / "tier_selection.json"


def _write_tier_selection_file(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# tier_autoapply.py 用共通ヘルパ（test_tier_autoapply.py と同型・subprocess 経由）
# ---------------------------------------------------------------------------


def _new_session_id() -> str:
    import uuid

    return "sess-secfix-" + uuid.uuid4().hex[:12]


def _agent_payload(
    subagent_type: str,
    *,
    model: str | None = None,
    prompt: str = "テスト用プロンプト",
    session_id: str | None = None,
) -> dict:
    tool_input: dict = {"subagent_type": subagent_type, "prompt": prompt}
    if model is not None:
        tool_input["model"] = model
    payload: dict = {"tool_name": "Agent", "tool_input": tool_input}
    if session_id is not None:
        payload["session_id"] = session_id
    return payload


def _run_autoapply_hook(payload: dict) -> "subprocess.CompletedProcess":
    import subprocess

    return subprocess.run(
        [sys.executable, str(AUTOAPPLY_HOOK_PATH)],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(WORKTREE_ROOT),
    )


def _write_tier_selection_real(**fields: object) -> None:
    TIER_SELECTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    TIER_SELECTION_PATH.write_text(json.dumps(fields, ensure_ascii=False), encoding="utf-8")


def _read_jsonl_lines(path: Path = STATE_JSONL_PATH) -> list[dict]:
    if not path.is_file():
        return []
    lines = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        lines.append(json.loads(raw_line))
    return lines


@pytest.fixture(autouse=True)
def isolated_state_files():
    """各テスト前後で state/tier_autoapply.jsonl・state/tier_selection.json を退避・復元した。

    tier_autoapply.py は実 `.claude/state/` を対象に書く設計のため、subprocess
    経由の hook 起動テスト（TestPromptPrefixSecretMasking）は実ファイルを汚染
    しうる。test_tier_autoapply.py の isolated_state_files と同型の退避・復元で
    副作用を隔離した（gap_check 側テストは tmp_path のみを使うため影響なし）。
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
# item1 (Med・[SR-NEW]): _warn_gap の session_id 未サニタイズ
# ---------------------------------------------------------------------------


class TestSessionIdSanitizationBeforeWarn:
    """`_warn_gap` の session_id が stop.py 慣行と同型でサニタイズ→切り詰めされることを固定した。

    session_id は tier_selection.json（外部編集可能な state ファイル）由来の
    ものを使い、SR report が指摘した脅威モデル（悪用シナリオ）を再現した。
    先頭 8 文字に ANSI エスケープ（\\x1b）・U+2028/U+2029 を計 10 文字並べ、
    その直後に "VISIBLE-ID-1234" を続けた。正しい実装（サニタイズ→[:8]切り詰め
    の順）では制御文字が全て除去された後に切り詰められるため "VISIBLE" が
    stderr に現れる。是正前の実装（サニタイズ無し、または切り詰め→サニタイズの
    逆順）だと [:8] の時点で制御文字のみが残り "VISIBLE" は一切現れないため、
    本テストは単一の理由（サニタイズ未実装）で失敗する。
    """

    def test_control_chars_and_ansi_escape_removed_before_truncation(
        self,
        gap_mod: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        jsonl_path: Path,
        db_path: Path,
        tmp_path: Path,
    ) -> None:
        """session_id の制御文字/ANSI/U+2028/U+2029 が stderr に出ず、サニタイズ後切り詰めされたことを確認した。"""
        malicious_session_id = ("\x1b" * 8) + _LS + _PS + "VISIBLE-ID-1234"
        tier_selection_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection_file(tier_selection_path, {"session_id": malicious_session_id})
        _patch_paths(
            gap_mod,
            monkeypatch,
            jsonl_path=jsonl_path,
            db_path=db_path,
            tier_selection_path=tier_selection_path,
        )
        old_ts = _prod_ts(datetime.now(timezone.utc) - timedelta(hours=1))
        # jsonl 行の session_id は生の（未サニタイズの）値と一致させる必要がある
        # （相関ロジック自体は生値で行い、サニタイズは _warn_gap の表示直前のみ）。
        _append_jsonl_row(
            jsonl_path, ts=old_ts, session_id=malicious_session_id, role_recorded="developer"
        )
        # agent_outcomes には記録なし（M=0）。真の欠落として _warn_gap が呼ばれるはず。

        fake_stderr = _capture_stderr(monkeypatch)
        gap_mod.run({})  # payload に session_id なし → tier_selection.json へ fallback

        captured = fake_stderr.getvalue()
        assert captured != "", "session_id fallback 経由の欠落検知自体が発火しなかった"
        assert "\x1b" not in captured, "ANSI エスケープ (\\x1b) が生のまま stderr に出力された"
        assert _LS not in captured, "U+2028 (LINE SEPARATOR) が生のまま stderr に出力された"
        assert _PS not in captured, "U+2029 (PARAGRAPH SEPARATOR) が生のまま stderr に出力された"
        assert "VISIBLE" in captured, (
            "サニタイズ→切り詰めの順で実装されていれば sid_short に 'VISIBLE' が含まれるはず"
            "（切り詰め→サニタイズの逆順や未サニタイズだと制御文字のみが[:8]に残り消える）"
        )


# ---------------------------------------------------------------------------
# item4 (Low・[SR-V-001]): tier_selection.json 由来 session_id の型検証なし
# ---------------------------------------------------------------------------


class TestNonStrSessionIdFromTierSelectionFailSafe:
    """`tier_selection.json` の session_id が非 str のとき None 扱いで完全に沈黙することを固定した（F-9 型ガードと対称）。

    是正前は `_run_impl` が非文字列 session_id をそのまま使い続け、jsonl 側に
    同一の非文字列 session_id を持つ行が存在すると相関が成立してしまい
    `_warn_gap` まで到達する。int/dict では `session_id[:8]` が `TypeError` を
    送出し（`_run_impl` を直接呼ぶため run() の外側 try/except に守られず
    テストの例外として顕在化する）、list では `[:8]` 自体はエラーにならない
    ため stderr に汚染された警告が出てしまう（空文字列アサーションで検出）。
    """

    @pytest.mark.parametrize(
        "bad_session_id",
        [12345, {"a": 1}, ["x", "y"]],
        ids=["int", "dict", "list"],
    )
    def test_non_str_session_id_is_treated_as_none_and_stays_silent(
        self,
        gap_mod: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        jsonl_path: Path,
        db_path: Path,
        tmp_path: Path,
        bad_session_id: object,
    ) -> None:
        """非 str session_id（int/dict/list）で `_run_impl` が例外を送出せず、stderr も出なかったことを確認した。"""
        tier_selection_path = tmp_path / "state" / "tier_selection.json"
        _write_tier_selection_file(tier_selection_path, {"session_id": bad_session_id})
        _patch_paths(
            gap_mod,
            monkeypatch,
            jsonl_path=jsonl_path,
            db_path=db_path,
            tier_selection_path=tier_selection_path,
        )
        old_ts = _prod_ts(datetime.now(timezone.utc) - timedelta(hours=1))
        # jsonl 行の session_id を同一の非文字列値にし、型検証が無いと相関が
        # 成立して _warn_gap まで到達する状況を作った。
        _append_jsonl_row(
            jsonl_path, ts=old_ts, session_id=bad_session_id, role_recorded="developer"
        )
        # agent_outcomes には記録なし（M=0）。型検証があれば session_id=None
        # として突合対象外になり沈黙するはず。

        fake_stderr = _capture_stderr(monkeypatch)
        # run() ではなく _run_impl を直接呼び、fail-safe の外側 try/except に
        # 守られない状態で例外種別を裏取りする
        # （tester/MEMORY.md「fail-safe hook の Red 単一起因検証」パターン）。
        gap_mod._run_impl({})

        assert fake_stderr.getvalue() == "", (
            "非 str session_id は None 扱いで突合対象外になり、常に沈黙するはず"
        )


# ---------------------------------------------------------------------------
# item3 (Low・[SR-K-003]): prompt_prefix の秘密情報平文保存
# ---------------------------------------------------------------------------


class TestPromptPrefixSecretMasking:
    """`tier_autoapply.py` の prompt_prefix が record_agent_outcome._mask_secrets 相当でマスクされることを固定した。"""

    @pytest.mark.parametrize(
        "prompt_with_secret,leaked_value,masked_marker",
        [
            (
                "please use api_key=sk-ABCDEF1234567890abcd for auth",
                "sk-ABCDEF1234567890abcd",
                "api_key=***",
            ),
            (
                "call with Bearer abcDEF123.token-xyz now",
                "abcDEF123.token-xyz",
                "Bearer ***",
            ),
            (
                "token=abcdef0123456789 for the session",
                "abcdef0123456789",
                "token=***",
            ),
            (
                "-----BEGIN PRIVATE KEY-----\nMIIBVQIBADAN\n-----END PRIVATE KEY-----",
                "MIIBVQIBADAN",
                "-----BEGIN PRIVATE KEY-----***-----END PRIVATE KEY-----",
            ),
        ],
        ids=["api_key", "bearer", "token", "pem"],
    )
    def test_secret_patterns_are_masked_before_persisted_to_jsonl(
        self,
        prompt_with_secret: str,
        leaked_value: str,
        masked_marker: str,
    ) -> None:
        """prompt 中の秘密情報が jsonl の prompt_prefix に平文で残らず、マスク済み文言に置換されたことを確認した。"""
        _write_tier_selection_real(tier="haiku", suggested_model="haiku", mode="thompson")
        sid = _new_session_id()
        result = _run_autoapply_hook(
            _agent_payload("developer", session_id=sid, prompt=prompt_with_secret)
        )
        assert result.returncode == 0

        lines = _read_jsonl_lines()
        assert len(lines) == 1
        prefix = lines[0]["prompt_prefix"]
        assert leaked_value not in prefix, f"秘密情報の値がマスクされず平文で残存した: {leaked_value!r}"
        assert masked_marker in prefix, f"マスク後の期待文言が見つからなかった: {masked_marker!r} (実際: {prefix!r})"


# ---------------------------------------------------------------------------
# item2 (Low・[SR-V-002]): jsonl/lock の symlink 未検証
# ---------------------------------------------------------------------------


class TestSymlinkSkip:
    """jsonl / lock パスがシンボリックリンクの場合、追記/読み取りを沈黙 skip することを固定した。

    Windows で symlink 作成に管理者権限 / 開発者モードが必要な環境では
    `Path.symlink_to` が OSError/NotImplementedError を送出するため、その場合は
    `pytest.skip` で環境起因の非該当として扱った（プロダクションコードの
    バグではないため Red/Green の判定対象外）。
    """

    def test_autoapply_append_skips_when_jsonl_path_is_symlink(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`tier_autoapply._append_applied_state` が jsonl パスの symlink 検出で追記をスキップしたことを確認した。"""
        mod = _load_autoapply_module()
        target = tmp_path / "real_target.jsonl"
        target.write_text("", encoding="utf-8")
        link_path = tmp_path / "tier_autoapply.jsonl"
        try:
            link_path.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"シンボリックリンク作成不可の環境のため非該当: {exc}")
        monkeypatch.setattr(mod, "APPLIED_STATE_PATH", str(link_path))

        row = {
            "ts": "2026-07-07T00:00:00+00:00",
            "session_id": "sess-symlink-autoapply",
            "subagent_type": "developer",
            "role_recorded": "developer",
            "model_applied": "sonnet",
            "source": "injected",
            "prompt_prefix": "",
        }
        mod._append_applied_state(row)

        assert target.read_text(encoding="utf-8") == "", (
            "symlink 経由の追記は skip されリンク先ファイルへ書き込まれないはず"
        )

    def test_autoapply_append_skips_when_lock_path_is_symlink(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """lock ファイル（`<jsonl>.lock`）が symlink のときも追記全体を沈黙 skip したことを確認した。"""
        mod = _load_autoapply_module()
        jsonl_path = tmp_path / "tier_autoapply.jsonl"
        jsonl_path.write_text("", encoding="utf-8")
        lock_target = tmp_path / "real_lock_target"
        lock_target.write_text("", encoding="utf-8")
        lock_path = tmp_path / "tier_autoapply.jsonl.lock"
        try:
            lock_path.symlink_to(lock_target)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"シンボリックリンク作成不可の環境のため非該当: {exc}")
        monkeypatch.setattr(mod, "APPLIED_STATE_PATH", str(jsonl_path))

        row = {
            "ts": "2026-07-07T00:00:00+00:00",
            "session_id": "sess-symlink-lock",
            "subagent_type": "developer",
            "role_recorded": "developer",
            "model_applied": "sonnet",
            "source": "injected",
            "prompt_prefix": "",
        }
        mod._append_applied_state(row)

        assert jsonl_path.read_text(encoding="utf-8") == "", (
            "lock パスが symlink のときは追記全体（jsonl 本体への書き込みも含む）を"
            "沈黙 skip するはず"
        )

    def test_gap_check_skips_reading_through_symlinked_jsonl(
        self,
        gap_mod: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        db_path: Path,
        absent_tier_selection_path: Path,
        tmp_path: Path,
    ) -> None:
        """`tier_gap_check` が jsonl パスの symlink を検出し、あたかも不在であるかのように沈黙したことを確認した。

        `os.path.isfile` は symlink 先が実ファイルなら True を返すため、
        symlink 検証を追加しない現行実装では symlink 経由でも通常どおり
        読み取りが成立し、実在する起動行に基づく警告が発火してしまう。
        """
        real_jsonl = tmp_path / "real_state" / "tier_autoapply.jsonl"
        old_ts = _prod_ts(datetime.now(timezone.utc) - timedelta(hours=1))
        _append_jsonl_row(
            real_jsonl, ts=old_ts, session_id="sess-symlink-gap", role_recorded="developer"
        )
        link_path = tmp_path / "state" / "tier_autoapply.jsonl"
        link_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            link_path.symlink_to(real_jsonl)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"シンボリックリンク作成不可の環境のため非該当: {exc}")

        _patch_paths(
            gap_mod,
            monkeypatch,
            jsonl_path=link_path,
            db_path=db_path,
            tier_selection_path=absent_tier_selection_path,
        )
        # agent_outcomes には記録なし（M=0）。symlink 検証が無ければ real_jsonl
        # の起動行がそのまま読まれ K'>0 で警告してしまう。

        fake_stderr = _capture_stderr(monkeypatch)
        gap_mod.run({"session_id": "sess-symlink-gap"})

        assert fake_stderr.getvalue() == "", (
            "symlink 経由の jsonl 読み取りは skip され、不在扱いで沈黙するはず"
        )


# ---------------------------------------------------------------------------
# item5 (Low・[SR-NEW]): 読み取り側のサイズ上限なし（DoS 観点）
# ---------------------------------------------------------------------------


class TestJsonlReadSizeCap:
    """jsonl 読み取りに概ね 5MB のサイズ上限があり、末尾優先で打ち切り fail-safe 継続する契約を固定した。

    plan-report fc1-tests item5 の受け入れ条件どおり、閾値の正確な実装方式は
    plan/architecture に明記が無いため、ここでは「先頭付近のみに存在する
    対象行がサイズ上限超過時に検知/解決されなくなる」という挙動差（末尾優先の
    帰結として head 側が犠牲になる under-detection）で固定した。既存の
    fail-safe 設計方針（「破損時は N 過少計上に倒れる」）と整合する。
    """

    def test_gap_check_head_only_row_beyond_size_cap_is_not_detected(
        self,
        gap_mod: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        jsonl_path: Path,
        db_path: Path,
        absent_tier_selection_path: Path,
    ) -> None:
        """5MB超のjsonl先頭のみに存在するdeveloper起動行が、末尾優先打ち切りにより検知されなかったことを確認した。"""
        _patch_paths(
            gap_mod,
            monkeypatch,
            jsonl_path=jsonl_path,
            db_path=db_path,
            tier_selection_path=absent_tier_selection_path,
        )
        old_ts = _prod_ts(datetime.now(timezone.utc) - timedelta(hours=1))
        # 先頭にターゲット行（打ち切りが無ければ真の欠落として警告されるはずの行）を書く。
        _append_jsonl_row(
            jsonl_path, ts=old_ts, session_id="sess-5mb-head", role_recorded="developer"
        )
        # 6MB超になるまで無関係セッションの filler 行を末尾側に積む。
        filler_line = json.dumps(
            {
                "ts": old_ts,
                "session_id": "sess-filler",
                "subagent_type": "developer",
                "role_recorded": "developer",
                "model_applied": "sonnet",
                "source": "injected",
                "prompt_prefix": "x" * 300,
            },
            ensure_ascii=False,
        ) + "\n"
        with jsonl_path.open("a", encoding="utf-8") as f:
            written = 0
            target_bytes = 6 * 1024 * 1024  # 5MB 上限を確実に超える
            while written < target_bytes:
                f.write(filler_line)
                written += len(filler_line.encode("utf-8"))

        # agent_outcomes には記録なし（M=0）。先頭行が処理対象なら K'>0 で警告するはず。
        fake_stderr = _capture_stderr(monkeypatch)
        gap_mod.run({"session_id": "sess-5mb-head"})

        assert fake_stderr.getvalue() == "", (
            "5MB 上限超過時は末尾優先で打ち切られ、先頭行由来の警告は出ないはず"
        )

    def test_record_agent_outcome_head_only_match_beyond_size_cap_not_resolved(
        self, tmp_path: Path
    ) -> None:
        """`record_agent_outcome._read_applied_tier` が5MB超の先頭一致行を打ち切りにより解決しなかったことを確認した。"""
        from c3 import pricing as c3_pricing

        mod = _load_record_module()
        applied_path = tmp_path / "tier_autoapply.jsonl"
        head_row = {
            "ts": "2026-01-01T00:00:00+00:00",
            "session_id": "sess-record-5mb-head",
            "subagent_type": "developer",
            "role_recorded": "developer",
            "model_applied": "haiku",
            "source": "injected",
            "prompt_prefix": "",
        }
        filler_row = {
            "ts": "2026-01-01T00:00:00+00:00",
            "session_id": "sess-record-filler",
            "subagent_type": "developer",
            "role_recorded": "developer",
            "model_applied": "sonnet",
            "source": "injected",
            "prompt_prefix": "x" * 300,
        }
        filler_line = json.dumps(filler_row, ensure_ascii=False) + "\n"
        with applied_path.open("w", encoding="utf-8") as f:
            f.write(json.dumps(head_row, ensure_ascii=False) + "\n")
            written = 0
            target_bytes = 6 * 1024 * 1024
            while written < target_bytes:
                f.write(filler_line)
                written += len(filler_line.encode("utf-8"))

        mod.APPLIED_STATE_PATH = str(applied_path)
        result = mod._read_applied_tier("sess-record-5mb-head", "developer", c3_pricing)

        assert result is None, (
            "5MB 上限超過時は先頭の一致行が打ち切られ解決されないはず（末尾優先打ち切り）"
        )


# ---------------------------------------------------------------------------
# item6 (Low・[SR-NEW]): role 集合の排他性の機械検証なし（設計上 Green の想定）
# ---------------------------------------------------------------------------


class TestLaunchLogRolesDisjointFromReviewerTypes:
    """`tier_autoapply.LAUNCH_LOG_ROLES` と `check_agent_invocation.REVIEWER_TYPES` の積集合が空であることを機械検証した。

    plan-report fc1-tests item6 の指示どおり実装変更は行わない
    （fc2-impl でも role 集合は変更しない）。両モジュールの実定数を import
    して直接比較するため、本テストは追加時点で既に Green（設計上 Green）で
    ある。将来 role 追加時にどちらかの集合が拡張され交差が発生した場合の
    回帰検知として機能する。
    """

    def test_launch_log_roles_and_reviewer_types_are_disjoint(self) -> None:
        """`LAUNCH_LOG_ROLES ∩ REVIEWER_TYPES == frozenset()` が両モジュールの実集合で成立したことを確認した。"""
        autoapply_mod = _load_autoapply_module()
        check_invocation_mod = _load_check_invocation_module()

        launch_log_roles = autoapply_mod.LAUNCH_LOG_ROLES
        reviewer_types = check_invocation_mod.REVIEWER_TYPES

        assert isinstance(launch_log_roles, frozenset)
        assert isinstance(reviewer_types, frozenset)
        assert launch_log_roles & reviewer_types == frozenset(), (
            f"LAUNCH_LOG_ROLES と REVIEWER_TYPES が重複した: "
            f"{launch_log_roles & reviewer_types}"
        )
