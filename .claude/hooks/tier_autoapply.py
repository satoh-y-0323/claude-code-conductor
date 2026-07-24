#!/usr/bin/env python3
"""PreToolUse Agent hook: tier-routing 機械適用（フェーズ3・T1）。

architecture-report-20260707-065043.md §3 の確定仕様を実装した本 hook は、
`matcher: Agent` の 2 本目として `check_agent_invocation.py` と同居する。
両 hook の作用 role は集合として排他（本 hook は LAUNCH_LOG_ROLES のみ・
check_agent は reviewer 系のみ）のため実行順序に依存しない（§1）。

## 動作（§3-2 ロール分岐表）

- `APPLY_ROLES = {developer, wt_developer}` かつ `model` 無指定かつ
  tier_selection.json から有効 tier を解決できたとき、`updatedInput` で
  `model:{推奨tier}` を注入した（source=injected・permissionDecision は
  T0 実測により省略形を正とした）。
- `RED_APPLY_ROLES = {tester, wt_tester}` かつ `model` 無指定かつ
  `C3_TASK_ID:` マーカーが `test-` で始まりかつ `roles.tester.tier` を解決できた
  ときのみ注入した（Red 限定・ADR-2）。confirm-/impl-/マーカー無し・roles 欠落は
  注入せず記録のみ（frontmatter の既定が実効・fail-safe）。
- 明示 `model` 指定時は素通り（source=explicit・明示尊重）。
- tier_selection 不在/破損/非文字列 tier のときは注入せず素通り
  （source=frontmatter-default・frontmatter の sonnet が実効するため）。
- `LAUNCH_LOG_ROLES = {developer, wt_developer, tester, wt_tester}` は
  注入有無に関わらず applied-state（tier_autoapply.jsonl）へ 1 行追記した。
  reviewer 系・その他 role は記録も注入もしない（exit 0 素通り）。
- 起動プロンプト先頭の `C3_TASK_ID:` マーカーを抽出し jsonl の `task_id` に
  載せた（T8・並列経路の record 突合キー）。

## 安全弁

- kill-switch: `C3_TIER_AUTOAPPLY_DISABLE=1` で注入も記録も行わず exit 0
  （旧来のソフト適用のみ動作へ完全復帰）。
- fail-safe: 不正 JSON・非 Agent・非 dict tool_input・想定外例外は全て握って
  exit 0（`check_agent_invocation.py` の流儀を踏襲）。
- ts は `agent_outcomes.ts`（record 経由・`src/c3/db.py:1046`）と同一プロファイル
  `datetime.now(timezone.utc).isoformat(timespec="seconds")` で書いた
  （§0-4(d)・§5-2 項3 の跨りソース `ts_floor` 辞書順比較の成立条件）。

## パス解決（§3-7・DC-AS-003）

`.claude/hooks/` は 1 階層遡りで `_CLAUDE_DIR` を求め、`record_agent_outcome.py`
（3 階層遡り）と同一の `.claude/state/tier_autoapply.jsonl` に解決した。
"""

from __future__ import annotations

import collections
import json
import os
import re
import sys
from datetime import datetime, timezone

try:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

# 並行 append の排他用 OS ファイルロック（プラットフォーム別）。Windows の
# append モードはプロセス間で atomic でなく、複数 subagent 同時起動時に行が
# 欠落しうるため、専用ロックファイルの OS ロックで直列化した（§3-4）。
try:
    import msvcrt  # type: ignore[import-not-found]
except ImportError:
    msvcrt = None  # type: ignore[assignment]
try:
    import fcntl  # type: ignore[import-not-found]
except ImportError:
    fcntl = None  # type: ignore[assignment]

# このファイルは .claude/hooks/ に置かれている前提。1 階層遡って .claude/ を得た:
#   hooks/ → .claude/
_HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(_HOOK_DIR)
# 1 階層遡りで `.claude` に到達することを実行時に検証した（record_agent_outcome.py
# の _CLAUDE_DIR 検証と同一思想。誤配置のサイレント破綻を防ぐ・開発時チェック）。
if not (_CLAUDE_DIR.endswith(os.sep + ".claude") or _CLAUDE_DIR.endswith("/.claude")):
    raise RuntimeError(
        f"_CLAUDE_DIR resolution broke: expected to end with '.claude' but got "
        f"{_CLAUDE_DIR!r}. Check that this file is at .claude/hooks/."
    )

TIER_SELECTION_PATH = os.path.join(_CLAUDE_DIR, "state", "tier_selection.json")
APPLIED_STATE_PATH = os.path.join(_CLAUDE_DIR, "state", "tier_autoapply.jsonl")

# §3-2 ロール分岐表の定数。
# APPLY_ROLES: 無条件 APPLY（起動フェーズを問わず tier_selection.json のトップレベル
#   tier を機械適用する）role 集合。
# RED_APPLY_ROLES: Red フェーズ（test- タスク）限定で機械適用する role 集合（ADR-2）。
#   注入条件は「role ∈ RED_APPLY_ROLES かつ C3_TASK_ID マーカーが test- 開始かつ
#   roles.tester.tier 解決可」の 3 条件全成立時のみ。D-3/D-5 等の非 Red 起動には
#   test- マーカーを付与しない運用不変則（dev-workflow SKILL）と組で NF-2 を守る。
#
# 【opus 固定不変則（ADR-6）】APPLY_ROLES / RED_APPLY_ROLES に追加できるのは
# frontmatter model: sonnet の role のみ。opus frontmatter の agent（architect /
# planner / design-critic / doc-writer / project-setup）は tier レバーを持たず
# 恒久対象外（機械検査テストで frontmatter が sonnet であることを CI 回帰網に固定）。
APPLY_ROLES = frozenset({"developer", "wt_developer"})
RED_APPLY_ROLES = frozenset({"tester", "wt_tester"})
LAUNCH_LOG_ROLES = frozenset({"developer", "wt_developer", "tester", "wt_tester"})
# wt_* → 無印への正規化（record 側 role 語彙・agent_outcomes.role と一致・§0-4）。
_ROLE_NORMALIZE = {"wt_developer": "developer", "wt_tester": "tester"}

_VALID_TIERS = ("haiku", "sonnet", "opus")

KILL_SWITCH_ENV = "C3_TIER_AUTOAPPLY_DISABLE"

# DoS 防御の stdin 読み取り上限（check_agent_invocation.py と同一）。
_STDIN_READ_LIMIT_BYTES = 1 * 1024 * 1024  # 1 MiB

# applied-state のローテーション閾値（§3-4）。
_MAX_JSONL_BYTES = 1 * 1024 * 1024  # 1 MB
_ROTATE_TAIL_LINES = 500

_PROMPT_PREFIX_MAX_CHARS = 200

# T8: 起動プロンプト先頭の C3_TASK_ID マーカー抽出用（§3-1・SR-AI-001 で \A 化）。
# 文字列先頭アンカー \A + allowlist + 上限200字で誤抽出・秘密混入を構造排除する
# （record MAX_TASK_LEN=200 と整合）。re.MULTILINE の行頭 ^ は本文孤立行・フェンス内・
# 2 行目マーカーを誤抽出するため撤廃し、マーカーは「文字列先頭 1 行目のみ」を唯一の
# 正当配置に統一する（parallel/逐次とも 1 行目マーカー・2 行目以降にガード指示・§2-3 改訂 7）。
_TASK_ID_MARKER_RE = re.compile(r'\AC3_TASK_ID:[ ]([A-Za-z0-9._\-]{1,200})[ ]*(?:\r?\n|\Z)')


def _load_pricing_module():
    """c3.pricing モジュールを返した（import 失敗時は None）。"""
    try:
        from c3 import pricing as c3_pricing  # type: ignore[import-not-found]

        return c3_pricing
    except ImportError:
        return None


# item3(SR-K-003・Low): prompt_prefix の秘密情報マスクパターン。hooks は import
# 非依存方針のため他モジュールを import せず同型を複製した。
# キー名やプレフィックスは残し、値のみを *** に置換する。PEM ブロックは
# 開始タグ + *** + 終了タグ に置換する。
# [CR-NEW1] _mask_secrets / _MASK_PATTERNS は以下の3ファイルに同型複製が存在した（パターン本体はバイト一致）。
# 語彙・パターンを変更する際は3ファイル全てを同期する（共通モジュール化は本サイクル未実施）:
#   - .claude/hooks/select_tier.py
#   - .claude/hooks/tier_autoapply.py
#   - .claude/skills/dev-workflow/scripts/record_agent_outcome.py
_MASK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r'(password=)\S+', re.IGNORECASE),
    re.compile(r'(api[_-]?key=)\S+', re.IGNORECASE),
    re.compile(r'(Bearer\s+)[\w\-\.]+', re.IGNORECASE),
    re.compile(r'(\btoken=)\S+', re.IGNORECASE),
    re.compile(r'(\bsecret=)\S+', re.IGNORECASE),
    re.compile(r'(aws_secret_access_key=)\S+', re.IGNORECASE),
    re.compile(r'(-----BEGIN [A-Z ]*PRIVATE KEY-----)[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----)'),
]


def _mask_secrets(text: str) -> str:
    """秘密情報パターンにマッチする値部分を *** に置換して返した。

    3ファイル同型複製（複製先は _MASK_PATTERNS 上の [CR-NEW1] コメント参照）。キー名や
    プレフィックスは残し値のみ置換する。PEM は開始タグ + *** + 終了タグ に置換する。
    """
    result = text
    for pattern in _MASK_PATTERNS:
        # group(2) があれば PEM ブロック（BEGIN...END）、なければプレフィックス系。
        result = pattern.sub(
            lambda m: m.group(1) + "***" + (m.group(2) if m.lastindex and m.lastindex >= 2 else ""),
            result,
        )
    return result


def _clean_prompt_prefix(prompt: object) -> str:
    """prompt の秘密情報をマスクし、先頭200字を制御文字除去のうえ返した（§3-3）。

    item3(SR-K-003): 制御文字除去・切り詰めより前に _mask_secrets を適用する。
    切り詰め境界をまたぐ秘密や、制御文字混入でパターン境界が崩れる前にマスクを
    確定させるため（record_agent_outcome.py の prompt-history 経路と同等の軽減）。
    """
    if not isinstance(prompt, str):
        return ""
    prompt = _mask_secrets(prompt)
    out = []
    for ch in prompt:
        code = ord(ch)
        if code < 0x20:  # C0 制御文字（\t \r \n 含む）
            continue
        if 0x7F <= code <= 0x9F:  # DEL + C1 制御文字
            continue
        if code in (0x2028, 0x2029):  # LINE / PARAGRAPH SEPARATOR
            continue
        out.append(ch)
    return "".join(out)[:_PROMPT_PREFIX_MAX_CHARS]


def _extract_task_id(prompt: object) -> str | None:
    """起動プロンプトから C3_TASK_ID マーカー（最初の 1 個）を抽出した（§3）。

    許容文字集合 [A-Za-z0-9._-]・文字列先頭アンカー \\A・上限200字で誤抽出と秘密混入を防ぐ。
    マーカーは「プロンプト文字列の先頭 1 行目」にある場合のみ抽出成立する（本文孤立行・
    フェンス内・2 行目以降は非マッチ＝None・SR-AI-001）。
    prompt_prefix の mask→truncate パイプラインとは独立（正規表現自体がサニタイザ・§3-3）。
    非文字列・不一致は None（→ jsonl は task_id: null で従来挙動・逐次経路も None）。
    """
    if not isinstance(prompt, str):
        return None
    m = _TASK_ID_MARKER_RE.search(prompt)
    if m is None:
        return None
    return m.group(1)


def _read_selection() -> dict | None:
    """tier_selection.json を読んだ（無い/壊れていれば None）。"""
    if not os.path.isfile(TIER_SELECTION_PATH):
        return None
    try:
        with open(TIER_SELECTION_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _resolve_selection_tier(c3_pricing) -> str | None:
    """tier_selection.json の tier（無ければ suggested_model）を正規化 tier に解決した。

    非文字列・破損・不在・正規化不能は None を返した（→ 注入せず frontmatter-default）。
    """
    if c3_pricing is None:
        return None
    selection = _read_selection()
    if selection is None:
        return None
    raw = selection.get("tier") or selection.get("suggested_model")
    if not isinstance(raw, str):
        return None
    resolved = c3_pricing.resolve_tier(raw)
    if resolved in _VALID_TIERS:
        return resolved
    return None


def _resolve_roles_tier(c3_pricing, role: str) -> str | None:
    """tier_selection.json の ``roles.<role>.tier`` を正規化 tier に解決した（ADR-3）。

    Red 限定注入の tier 源。トップレベル ``tier`` ではなく additive な
    ``roles`` キー配下を読む。``roles`` 欠落・``roles.<role>`` 破損・非文字列 tier・
    正規化不能は全て None を返し、呼び出し側は注入せず frontmatter-default に落ちる
    （fail-safe が安全側に一致）。
    """
    if c3_pricing is None:
        return None
    selection = _read_selection()
    if selection is None:
        return None
    roles = selection.get("roles")
    if not isinstance(roles, dict):
        return None
    entry = roles.get(role)
    if not isinstance(entry, dict):
        return None
    raw = entry.get("tier")
    if not isinstance(raw, str):
        return None
    resolved = c3_pricing.resolve_tier(raw)
    if resolved in _VALID_TIERS:
        return resolved
    return None


def _rotate_if_needed(path: str) -> None:
    """applied-state が 1MB を超えていたら末尾500行へ切り詰めた（§3-4・失敗時は無視）。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size <= _MAX_JSONL_BYTES:
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            tail = list(collections.deque(f, maxlen=_ROTATE_TAIL_LINES))
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.writelines(tail)
        os.replace(tmp_path, path)
    except OSError:
        pass


def _os_lock(lock_f) -> None:
    """ロックファイルのバイト0に排他ロックを取得した（取得不能時は例外を送出）。"""
    if msvcrt is not None:
        lock_f.seek(0)
        msvcrt.locking(lock_f.fileno(), msvcrt.LK_LOCK, 1)
    elif fcntl is not None:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)


def _os_unlock(lock_f) -> None:
    """_os_lock で取得したロックを解放した（失敗時は無視）。"""
    try:
        if msvcrt is not None:
            lock_f.seek(0)
            msvcrt.locking(lock_f.fileno(), msvcrt.LK_UNLCK, 1)
        elif fcntl is not None:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _append_applied_state(row: dict) -> None:
    """applied-state（jsonl）へ 1 行を単一 write で追記した（§3-4・追記前にローテーション）。

    専用ロックファイル（`<jsonl>.lock`）の OS ロックで並行 append を直列化した。
    ロックはハンドルクローズ／プロセス終了時に OS が自動解放するため、
    ロックファイルが残っても後続の追記をブロックしない（stale-lock デッドロック回避）。

    ロックファイル（`<jsonl>.lock`）の永続化について（CR F-6）:
    このファイルは削除せず `.claude/state/*` の gitignore exclude パターンで
    配布対象外にしている（_rotate_if_needed 同様の設計）。stale-lock 回避のため
    OS ハンドル解放に委ね、明示的な削除を行わない。
    """
    line = json.dumps(row, ensure_ascii=False)
    lock_path = APPLIED_STATE_PATH + ".lock"
    # item2(SR-V-002・Low): jsonl 本体・ロックファイルのいずれかが symlink の場合は
    # 追記全体を沈黙 skip（リンク先への誘導書き込みを防ぐ・fail-safe）。open で
    # follow する前に検証する。Windows は O_NOFOLLOW 非対応のため islink で分岐する
    # （POSIX 慣行に合わせた可搬な検証）。tier_gap_check.py 読み取り側と同型。
    # [CR-NEW3] islink 検証→open 間の TOCTOU レースは構造的に残存するが既知・脅威モデル外
    # （Windows O_NOFOLLOW 非対応による設計選択。stale-lock TOCTOU と同種の回避不能パターン）。
    if os.path.islink(APPLIED_STATE_PATH) or os.path.islink(lock_path):
        return
    try:
        os.makedirs(os.path.dirname(APPLIED_STATE_PATH), exist_ok=True)
    except OSError as exc:
        print(f"[tier_autoapply] mkdir failed: {type(exc).__name__}", file=sys.stderr)
        return

    lock_f = None
    try:
        lock_f = open(lock_path, "a+", encoding="utf-8")
    except OSError:
        lock_f = None

    locked = False
    try:
        if lock_f is not None:
            try:
                _os_lock(lock_f)
                locked = True
            except OSError:
                locked = False  # ロック不能でもベストエフォートで追記する。
        _rotate_if_needed(APPLIED_STATE_PATH)
        with open(APPLIED_STATE_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as exc:
        print(f"[tier_autoapply] append failed: {type(exc).__name__}", file=sys.stderr)
    finally:
        if lock_f is not None:
            if locked:
                _os_unlock(lock_f)
            lock_f.close()


def main() -> None:
    # kill-switch: 注入も記録も行わず旧来動作へ完全復帰した（§3-5）。
    # 無駄な I/O（stdin 読み取り・JSON パース）を避けるため kill-switch は stdin 読み取り前に判定する（CR F-5）。
    if os.environ.get(KILL_SWITCH_ENV) == "1":
        sys.exit(0)

    try:
        payload = json.loads(sys.stdin.read(_STDIN_READ_LIMIT_BYTES))
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)
    if not isinstance(payload, dict):
        sys.exit(0)

    if payload.get("tool_name") != "Agent":
        sys.exit(0)

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        sys.exit(0)

    subagent_type = tool_input.get("subagent_type", "")
    if not isinstance(subagent_type, str):
        subagent_type = ""

    # LAUNCH_LOG_ROLES 以外（reviewer 系・その他）は記録も注入もせず素通りした。
    if subagent_type not in LAUNCH_LOG_ROLES:
        sys.exit(0)

    role_recorded = _ROLE_NORMALIZE.get(subagent_type, subagent_type)
    session_id = payload.get("session_id")
    if not isinstance(session_id, str):
        session_id = None

    model_val = tool_input.get("model")
    has_explicit_model = isinstance(model_val, str) and model_val.strip() != ""

    # task_id は注入判定（RED_APPLY_ROLES の test- プレフィックス条件）と row 記録の
    # 双方で使うため先に抽出する（§3-1・ADR-2）。
    prompt = tool_input.get("prompt")
    task_id = _extract_task_id(prompt)

    inject = False
    if has_explicit_model:
        # 明示尊重: 注入せず素通りし explicit で記録した（RED_APPLY_ROLES でも不変）。
        source = "explicit"
        model_applied: str | None = model_val
    elif subagent_type in APPLY_ROLES:
        c3_pricing = _load_pricing_module()
        tier = _resolve_selection_tier(c3_pricing)
        if tier is not None:
            inject = True
            source = "injected"
            model_applied = tier
        else:
            # tier_selection 不在/破損/非文字列: frontmatter の既定が実効するため記録のみ。
            source = "frontmatter-default"
            model_applied = None
    elif subagent_type in RED_APPLY_ROLES:
        # tester/wt_tester: Red 限定注入（ADR-2）。C3_TASK_ID マーカーが test- 開始
        # かつ roles.tester.tier が解決可のときのみ注入。confirm-/impl-/マーカー無し・
        # roles 欠落/破損は注入せず記録のみ（frontmatter の既定が実効・fail-safe 一致）。
        if isinstance(task_id, str) and task_id.startswith("test-"):
            c3_pricing = _load_pricing_module()
            tier = _resolve_roles_tier(c3_pricing, role_recorded)
            if tier is not None:
                inject = True
                source = "injected"
                model_applied = tier
            else:
                source = "frontmatter-default"
                model_applied = None
        else:
            source = "frontmatter-default"
            model_applied = None
    else:
        # LAUNCH_LOG_ROLES のうち上記いずれにも該当しない role（現状なし・防御）。
        source = "frontmatter-default"
        model_applied = None

    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_id": session_id,
        "subagent_type": subagent_type,
        "role_recorded": role_recorded,
        "model_applied": model_applied,
        "source": source,
        "prompt_prefix": _clean_prompt_prefix(prompt),
        # SR-K-003 前提: task_id は _TASK_ID_MARKER_RE の allowlist [A-Za-z0-9._-] を
        # 通過済みのため秘密混入が構造的に不可能で _mask_secrets を適用していない。
        # 許容文字集合が緩和される場合（記号・空白等を許す等）は、prompt_prefix と同様に
        # _mask_secrets 適用が必要になる（マスク非対称の申し送り）。
        "task_id": task_id,
    }
    _append_applied_state(row)

    if inject:
        updated_input = {**tool_input, "model": model_applied}
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": updated_input,
            }
        }
        print(json.dumps(output, ensure_ascii=False))

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — fail-safe: 想定外例外も握って exit 0。
        print(f"[tier_autoapply] unexpected error: {type(exc).__name__}", file=sys.stderr)
        sys.exit(0)
