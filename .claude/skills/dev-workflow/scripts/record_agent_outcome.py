#!/usr/bin/env python3
"""CLI: record the outcome of an agent execution for tier-routing learning.

tier-routing 学習シグナル再設計（architecture-report-20260702-214748.md §3-4・
ADR-2〜ADR-7）に基づく後継スクリプト。旧 record_tier_outcome.py を置き換える。

Usage:
    python .claude/skills/dev-workflow/scripts/record_agent_outcome.py \\
        --role developer --outcome success --gate D-2.5 \\
        --execution subagent --complexity medium

設計のポイント（ADR-2/ADR-6/ADR-7 準拠。Round 1 修正反映）:
- --role/--outcome/--gate/--execution/--complexity は必須だが、argparse の
  ``required=True``（``SystemExit(2)``）ではなく ``required=False`` + main() 内
  チェックで stderr 警告 + ``return 0``（記録スキップ）とする
  （「全エラー exit 0 流儀」を貫くため。呼び出し元の dev-workflow を止めない）。
- --tier 省略時の tier 解決優先順位（architecture-report-20260707-065043.md
  §4・フェーズ3。旧 §3-1・ADR-AS-1〜AS-4 を踏襲しつつ applied-state を優先2
  として新規挿入し、tier_selection.json を優先3へ降格した）:

    | 優先 | ソース                                            | 対象                          |
    |----|--------------------------------------------------|-------------------------------|
    | 1  | --tier（明示・TIERS 検証）                        | 全 role（並列/worktree 経路は常用申告） |
    | 2  | applied-state（tier_autoapply.jsonl・session_id 一致の最新行） | _SOFT_APPLY_ROLES（developer） |
    | 3  | tier_selection.json の tier→suggested_model       | 同上・優先2 不成立時（フォールバック A 互換） |
    | 4  | agents/{role}.md frontmatter model:               | developer 以外、および 2・3 が不成立の developer |
    | 5  | 解決不能                                          | 記録スキップ（stderr 警告 + exit 0） |

  --execution=subagent かつ role が _SOFT_APPLY_ROLES に含まれる場合（現状
  developer のみ）、--tier 省略時はまず applied-state を読む（優先2）。
  フェーズ3で PreToolUse hook（tier_autoapply.py）が Agent 起動時の実適用
  tier を session_id・role_recorded 付きで tier_autoapply.jsonl に追記する
  ため、record はそれを session_id 一致の最新行で読む（＝適用者=記録 SSOT）。
  applied-state が不在／session_id 不一致／kill-switch で行が無い等で優先2 が
  不成立のときは、優先3 として tier_selection.json（select_tier.py が
  UserPromptSubmit で書く推奨/実効 tier の SSOT）の `tier`（無ければ
  `suggested_model`）を ``pricing.resolve_tier`` で正規化し採用する
  （フォールバック A 互換のため温存・削除しない）。tier_selection.json も
  無い・値が不正（正規化不能）・role が対象外（tester 等）のときは優先 4
  （frontmatter 自己解決）へ fallback する。
  逸脱時の是正エスケープハッチ（ADR-AS-2）: 親が起動時に指定した model: が
  推奨 Tier と異なる場合は必ず --tier に実際の tier を付す運用を dev-workflow
  SKILL.md 側が担う（本スクリプトは優先 1 で受けるのみでコード変更は無い）。
  並列（worktree isolation）経路は wt_developer→developer の記録を親 Claude
  が main リポジトリで実行し、tier_selection.json を読まず常に --tier を
  明示する（ADR-AS-4）。そのため本スクリプトは並列経路向けの追加コードを
  持たず、優先 1（--tier 明示）を共用するだけで足りる。
  --execution=persona は --tier 省略時、frontmatter へも tier_selection.json
  へも fallback せず常に tier="unknown" 固定でイベントログのみ記録する
  （frontmatter/tier_selection は subagent の実使用 tier であり、persona の
  実行時には親モデルが効くため）。
- --tier を明示した場合は TIERS(haiku/sonnet/opus) に含まれるか検証し、
  含まれなければ警告 + 記録スキップとする。ただし --execution=persona かつ
  --tier unknown は明示的な escape 値として許容する。
- 両 execution モードとも agent_outcomes イベントログに記録する。bandit params は
  読み取り時に agent_outcomes から導出計算される（agent_tier_bandit 削除・
  ADR-25-4）。--execution=persona は親 Claude ペルソナ実行のため frontmatter
  model が効かず tier="unknown" で記録される。
- dedupe: 同一 (session_id, gate, role, outcome, task) が直近 5 分以内に
  記録済みなら 2 回目は skip。--task は任意引数で、省略時同士は従来通り
  (session_id, gate, role, outcome) のみで判定する（後方互換）。task の
  NULL（--task なし）と非 NULL（--task あり）は別物として扱う。
  session_id が無い場合は dedupe しない（ADR-6: 保守的に記録）。
- --gate E-2 の記録は成否問わず prompt-history.jsonl に 1 行追記する。
- --final は tier_selection.json の削除のみを担う（prompt-history 追記とは分離）。
- DB 不在 / SQL エラー時は警告のみで exit 0（呼び出し元を止めない）。
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

try:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

# このファイルは .claude/skills/dev-workflow/scripts/ に置かれている前提。
# 上位 3 階層を遡って .claude/ ディレクトリを得る:
#   scripts/ → dev-workflow/ → skills/ → .claude/
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(_SCRIPT_DIR)))
# 3 階層遡りで `.claude` に到達することを実行時に検証。
# 将来スクリプトが別階層に移動された場合のサイレント破綻を防ぐ。
#
# 注: これはセキュリティ防御ではなく、スクリプトの誤配置（ディレクトリ階層変更・
# 移動忘れ等）を実行時に検出するための開発時チェック。外部攻撃者がディレクトリ
# 構造を制御できる脅威モデルは前提としていない（[SR-NEW]、record_tier_outcome.py
# から踏襲）。`assert` は `python -O` で無効化されるため、RuntimeError で明示的に
# 投げる。
if not (_CLAUDE_DIR.endswith(os.sep + ".claude") or _CLAUDE_DIR.endswith("/.claude")):
    raise RuntimeError(
        f"_CLAUDE_DIR resolution broke: expected to end with '.claude' but got {_CLAUDE_DIR!r}. "
        "Check that this file is at .claude/skills/dev-workflow/scripts/."
    )

TIER_SELECTION_PATH = os.path.join(_CLAUDE_DIR, "state", "tier_selection.json")
# フェーズ3（T3）: PreToolUse hook（tier_autoapply.py）が Agent 起動時の実適用
# tier を 1 行 JSONL で追記する applied-state。tier_autoapply.py と同一の
# _CLAUDE_DIR 機構（あちらは hooks/ からの 1 階層遡り・こちらは scripts/ からの
# 3 階層遡り）で同一絶対パスに解決する（DC-AS-003）。
APPLIED_STATE_PATH = os.path.join(_CLAUDE_DIR, "state", "tier_autoapply.jsonl")
PROMPT_HISTORY_PATH = os.path.join(_CLAUDE_DIR, "logs", "prompt-history.jsonl")
# ADR-2 DC-AS-002/003: --tier 省略時に自己解決する frontmatter の探索先。
AGENTS_DIR = os.path.join(_CLAUDE_DIR, "agents")

_VALID_OUTCOMES = ("success", "failure")
_VALID_EXECUTIONS = ("persona", "subagent")
_VALID_COMPLEXITIES = ("simple", "medium", "complex")
# Round1 CR-低: --tier override 検証用の TIERS 語彙（select_tier.py の TIERS /
# db.py の _TIER_BANDIT_TIERS と同じ語彙。SSOT はそちら側にあるが、依存を
# 増やさないためこのスクリプト内でも同じ値を保持する）。
_VALID_TIERS = ("haiku", "sonnet", "opus")
# architecture-report-20260703-081149.md §3-1 / ADR-AS-1: tier_selection.json
# を tier 解決の SSOT として読む role の一覧（ソフト適用対象）。将来 role を
# 追加する場合はこのタプルを拡張するだけでよい（SSOT）。tester 等はここに
# 含めず frontmatter 自己解決のまま（role gating）。
_SOFT_APPLY_ROLES = ("developer",)
# Round1 CR-NEW: dedupe キーへ --task を組み込むため、agent_outcomes への
# task 列追加（migration）はせず note フィールドの先頭にマーカーを埋め込んで
# 表現する。
_TASK_NOTE_PREFIX = "[task:"

# ADR-2 DC-AS-003: model: 行は単一行形式のみ許容。想定外形式は解決不能扱い。
_MODEL_LINE_RE = re.compile(r'^model:\s*["\']?([\w][\w.-]*)["\']?\s*$', re.MULTILINE)

# dedupe 判定ウィンドウ（ADR-6）。
_DEDUPE_WINDOW = timedelta(minutes=5)

# prompt-history.jsonl の上限サイズ（バイト）。超過時は末尾 N 行だけ残してローテーション
# する（record_tier_outcome.py から移植・[SR-V-001]）。
_PROMPT_HISTORY_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_PROMPT_HISTORY_TRUNCATE_LINES = 2000

# Round4 [SR-V-001]/[SR-K-003]: --note/--gate/--task の長さ上限。DB 肥大化・
# 秘密情報の大量書き込みを防ぐ。record_review_decision.py の MAX_FINDING_LEN /
# MAX_FIELD_BYTES と同期（この定数を変更する場合は record_review_decision.py
# 側も合わせて確認すること）。
MAX_NOTE_LEN = 2000          # 文字数上限
MAX_GATE_LEN = 200
MAX_TASK_LEN = 200
MAX_FIELD_BYTES = 8 * 1024   # 全フィールド共通バイト数上限（8 KB）


def _truncate(value: str | None, limit: int, name: str) -> str | None:
    """value が文字数 limit 超または UTF-8 バイト数 MAX_FIELD_BYTES 超なら切り詰めて警告を出す。

    record_review_decision.py の _truncate() と同じロジック（同期コメント）。
    文字数で切ったあともバイト数を再確認し、両条件で安全になるまで切り詰める
    （BMP 外文字対応）。None / 空文字列はそのまま返す。
    """
    if not value:
        return value
    truncated = False
    if len(value) > limit:
        value = value[:limit]
        truncated = True
    byte_len = len(value.encode("utf-8"))
    while byte_len > MAX_FIELD_BYTES:
        value = value[: max(1, len(value) - 1)]
        byte_len = len(value.encode("utf-8"))
        truncated = True
    if truncated:
        print(
            f"[record_agent_outcome] --{name} truncated to {len(value)} chars / "
            f"{byte_len} bytes",
            file=sys.stderr,
        )
    return value


# Round4 [SR-V-001]: --note 中の秘密情報マスクパターン。
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
    """秘密情報パターンにマッチする値部分を *** に置換して返す。

    3ファイル同型複製（複製先は _MASK_PATTERNS 上の [CR-NEW1] コメント参照）。
    キー名やプレフィックスは残し、値のみを置換する。
    PEM ブロックは開始タグ + *** + 終了タグ に置換する。
    """
    result = text
    for pattern in _MASK_PATTERNS:
        # group(2) があれば PEM ブロック (BEGIN...END)、なければプレフィックス系
        result = pattern.sub(
            lambda m: m.group(1) + "***" + (m.group(2) if m.lastindex and m.lastindex >= 2 else ""),
            result,
        )
    return result


def _load_c3_db_module():
    """c3.db helper モジュールを返す（import 失敗時は None）。"""
    try:
        from c3 import db as c3_db  # type: ignore[import-not-found]
        return c3_db
    except ImportError as exc:
        print(f"[record_agent_outcome] c3_db import failed: {exc}", file=sys.stderr)
        return None


def _load_pricing_module():
    """c3.pricing モジュールを返す（import 失敗時は None）。"""
    try:
        from c3 import pricing as c3_pricing  # type: ignore[import-not-found]
        return c3_pricing
    except ImportError as exc:
        print(f"[record_agent_outcome] c3_pricing import failed: {exc}", file=sys.stderr)
        return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record agent outcome for tier-routing learning (ADR-2/6/7)"
    )
    # ADR-2: 必須 5 引数は required=False にし、main() 内チェックで
    # stderr 警告 + exit 0（記録スキップ）にする（argparse SystemExit(2) を避ける）。
    parser.add_argument("--role", default=None, help="AGENT_ROLES のいずれか（必須）")
    parser.add_argument(
        "--outcome", default=None, choices=None, help="success|failure（必須）"
    )
    parser.add_argument("--gate", default=None, help="ゲート ID（自由 TEXT・必須）")
    parser.add_argument(
        "--execution", default=None, help="persona|subagent（必須）"
    )
    parser.add_argument(
        "--complexity", default=None, help="simple|medium|complex（必須）"
    )
    parser.add_argument(
        "--tier", default=None,
        help="省略時は developer は tier_selection.json（soft-apply・逸脱時は "
        "frontmatter fallback）、それ以外は agents/{role}.md の model: を"
        "自己解決する",
    )
    parser.add_argument("--note", default=None, help="帰属理由等の自由記述")
    parser.add_argument(
        "--task", default=None,
        help="タスク識別子（任意）。dedupe キー (session_id, gate, role, "
        "outcome, task) に含める。省略時は従来通り task 抜きで判定する",
    )
    parser.add_argument(
        "--final", action="store_true",
        help="tier_selection.json を削除する（E-2 完了時のみ付与）",
    )
    return parser


def _validate_args(args: argparse.Namespace, c3_db) -> str | None:
    """必須引数の欠落・不正値を検証する。

    Returns:
        警告メッセージ（問題あり）、または None（問題なし）。
    """
    if not args.role:
        return "--role is required"
    agent_roles = getattr(c3_db, "AGENT_ROLES", ()) if c3_db is not None else ()
    if args.role not in agent_roles:
        return f"--role {args.role!r} is not a valid role (expected one of {agent_roles})"
    if not args.outcome:
        return "--outcome is required"
    if args.outcome not in _VALID_OUTCOMES:
        return f"--outcome {args.outcome!r} is invalid (expected success|failure)"
    if not args.gate:
        return "--gate is required"
    if not args.execution:
        return "--execution is required"
    if args.execution not in _VALID_EXECUTIONS:
        return f"--execution {args.execution!r} is invalid (expected persona|subagent)"
    # DC-AM-005: --complexity 省略時に tier_selection.json への fallback は
    # 実装しない。ここで必須チェックすることで、以降 selection を読む前に
    # 確実に記録スキップする。
    if not args.complexity:
        return "--complexity is required (no fallback to tier_selection.json)"
    if args.complexity not in _VALID_COMPLEXITIES:
        return f"--complexity {args.complexity!r} is invalid (expected simple|medium|complex)"
    return None


def _resolve_tier_from_frontmatter(role: str, c3_pricing) -> str | None:
    """AGENTS_DIR/{role}.md の frontmatter `model:` 行から tier を解決する。

    --tier 省略時、--execution=subagent の経路でのみ呼ばれる（Round1: persona は
    --tier 省略時に frontmatter へ fallback せず常に "unknown" 固定とするため、
    この関数は呼ばれない）。単一行正規表現でパースし、pricing.resolve_tier で
    TIERS 語彙へ正規化する。

    Returns:
        解決できた tier 文字列、または解決不能なら None。
    """
    agent_file = os.path.join(AGENTS_DIR, f"{role}.md")
    if not os.path.isfile(agent_file):
        return None
    try:
        with open(agent_file, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None

    match = _MODEL_LINE_RE.search(content)
    if not match:
        return None
    if c3_pricing is None:
        return None
    return c3_pricing.resolve_tier(match.group(1))


def _read_selection() -> dict | None:
    """TIER_SELECTION_PATH から選択情報を読む。無い/壊れていれば None。"""
    if not TIER_SELECTION_PATH or not os.path.isfile(TIER_SELECTION_PATH):
        return None
    try:
        with open(TIER_SELECTION_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"[record_agent_outcome] failed to read tier_selection: {exc}",
            file=sys.stderr,
        )
        return None
    if not isinstance(data, dict):
        return None
    return data


# item5(SR-NEW・Low): applied-state 読み取り側のサイズ上限（DoS 抑止）。
# tier_autoapply.py::_rotate_if_needed（1MB・追記側のみ発火）とは別レイヤの
# 読み取り防御で、超過時は末尾優先（tail-priority）で最大 5MB のみ走査する
# （tier_gap_check.py::_iter_capped_lines と同型の自己完結実装）。tier_autoapply.jsonl
# は末尾ほど新しく _read_applied_tier は最新行を採用するため、先頭を犠牲にしても
# 直近の適用 tier を優先解決できる。打ち切りは fail-safe（解決不能=None）に倒れる。
_APPLIED_STATE_MAX_READ_BYTES = 5 * 1024 * 1024


def _iter_applied_state_capped_lines(path: str):
    """applied-state jsonl を末尾優先で最大 5MB だけ読み、行文字列を yield した。

    上限超過時は末尾へ seek し、seek 後の最初の（途中で切れた）部分行を捨ててから
    完全行のみを返す（tail-priority）。上限以下なら先頭から全行を返す。

    [CR-NEW2] 同型の自己完結複製が
    tier_gap_check.py::_iter_capped_lines（定数 _MAX_READ_BYTES）にも存在した。
    一方を変更する際は他方も同期する。
    """
    with open(path, "rb") as f:
        try:
            f.seek(0, os.SEEK_END)
            size = f.tell()
        except OSError:
            size = 0
        if size > _APPLIED_STATE_MAX_READ_BYTES:
            f.seek(size - _APPLIED_STATE_MAX_READ_BYTES)
            f.readline()  # 途中で切れた先頭部分行を捨てる
        else:
            f.seek(0)
        for raw in f:
            yield raw.decode("utf-8", errors="replace")


def _read_applied_tier(session_id: str | None, role: str, c3_pricing) -> str | None:
    """applied-state（tier_autoapply.jsonl）から実適用 tier を解決する（優先2・§4-2）。

    フェーズ3で PreToolUse hook（tier_autoapply.py）が Agent 起動時の実適用値を
    session_id・role_recorded 付きで JSONL 追記する。record はこれを session_id
    一致で読むことで「適用者=記録 SSOT」を成立させる（tier_selection.json の
    推奨値を読む優先3 より優先する）。

    突合規則（_read_selection() と同じ防御思想）:
    - APPLIED_STATE_PATH 不在なら None。
    - session_id が None なら None（突合不能・§0-4 の NULL 制約。session_id
      NULL の行同士を突き合わせない）。
    - 行単位で json.loads し、壊れ行は skip（try/except continue）。
    - session_id 一致 かつ role_recorded == role の行を収集し、最新
      （末尾側／ts 最大）の model_applied を採用する。
    - model_applied 非文字列（int/list/dict 等）は skip（resolve_tier の
      内部 .lower() による AttributeError 防止。soft-apply 側 L566-574 と同理由）。
      source=frontmatter-default（model_applied=null）の行は tier を持たない
      ため自然に skip される。
    - resolve_tier で正規化し _VALID_TIERS に含まれれば返す。

    Returns:
        解決できた正規化 tier 文字列、または解決不能なら None。
    """
    if session_id is None or c3_pricing is None:
        return None
    # item2(SR-V-002): symlink 経由の読み取りは不在扱いで skip（tier_gap_check.py /
    # tier_autoapply.py の islink 検証と同型。os.path.isfile は symlink 先が実ファイル
    # なら True を返すため islink を併せて検証する）。
    if (
        not APPLIED_STATE_PATH
        or not os.path.isfile(APPLIED_STATE_PATH)
        or os.path.islink(APPLIED_STATE_PATH)
    ):
        return None
    latest_model: str | None = None
    latest_ts: str | None = None
    try:
        # item5(SR-NEW): 末尾優先で最大 5MB のみ走査する（_iter_applied_state_capped_lines）。
        for line in _iter_applied_state_capped_lines(APPLIED_STATE_PATH):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            if row.get("session_id") != session_id:
                continue
            if row.get("role_recorded") != role:
                continue
            model_applied = row.get("model_applied")
            if not isinstance(model_applied, str):
                continue
            ts = row.get("ts")
            ts_key = ts if isinstance(ts, str) else ""
            # 最新（ts 最大）を採用。ts 同値/欠落時は後勝ち（末尾側）で
            # 上書きするため >= で比較する。ts プロファイルは
            # tier_autoapply.py と同一 UTC 秒精度 ISO8601（辞書順＝時系列）。
            if latest_ts is None or ts_key >= latest_ts:
                latest_ts = ts_key
                latest_model = model_applied
    except OSError as exc:
        print(
            f"[record_agent_outcome] failed to read applied-state: {exc}",
            file=sys.stderr,
        )
        return None
    if latest_model is None:
        return None
    resolved = c3_pricing.resolve_tier(latest_model)
    if resolved in _VALID_TIERS:
        return resolved
    return None


def _delete_selection() -> None:
    try:
        if TIER_SELECTION_PATH and os.path.isfile(TIER_SELECTION_PATH):
            os.remove(TIER_SELECTION_PATH)
    except OSError as exc:
        print(
            f"[record_agent_outcome] failed to delete tier_selection: {exc}",
            file=sys.stderr,
        )


def _task_note_marker(task: str | None) -> str | None:
    """--task を note フィールドへ埋め込むためのマーカー文字列を返す。

    Round1 CR-NEW（dedupe 粒度不足）対応: agent_outcomes への task 列追加
    （migration）は避け、task 識別子を note フィールドの先頭に
    ``[task:<id>]`` 形式で埋め込む。dedupe 判定はこのマーカーを LIKE 検索
    することで (session_id, gate, role, success, task) 相当のキー粒度を実現する。
    task が None（--task 省略）なら None を返す。
    """
    if task is None:
        return None
    return f"{_TASK_NOTE_PREFIX}{task}]"


def _escape_like_pattern(value: str) -> str:
    """LIKE パターン中のワイルドカード文字（% / _）と ESCAPE 文字自身（\\）を
    リテラルとしてエスケープする。

    Round2 CR（code-review-report-20260703-021609.md [対応予定] Medium）対応:
    task_id に % / _ を含むと無関係な task の note に誤マッチしていた。
    置換順序は必ず「\\ → % → _」の順。逆順にするとエスケープで挿入した
    \\ が再エスケープされ二重エスケープになる。
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _compose_note(note: str | None, task: str | None) -> str | None:
    """DB へ書き込む note を組み立てる（task マーカー + 元の --note）。"""
    marker = _task_note_marker(task)
    if marker is None:
        return note
    return marker if not note else f"{marker} {note}"


def _is_duplicate(
    db_path,
    *,
    session_id: str,
    gate: str,
    role: str,
    success: bool,
    task: str | None = None,
    busy_timeout_ms: int = 5000,
) -> bool:
    """ADR-6 + Round1 CR-NEW: 同一 (session_id, gate, role, success, task) が
    直近 5 分以内にあるか。

    task が None（--task 省略）の場合は note に task マーカーが付いていない
    行のみを対象に従来通り判定する（後方互換）。task 指定時は同じマーカーを
    持つ行のみを対象にする。NULL（task 無し）と非 NULL（task 指定）は別物
    として扱う。

    DB 不在・SQL エラー時は「重複なし」として扱う（保守的に記録する）。
    """
    if db_path is None:
        return False
    sql = (
        "SELECT ts FROM agent_outcomes "
        "WHERE session_id = ? AND gate = ? AND role = ? AND success = ? "
    )
    params: list = [session_id, gate, role, 1 if success else 0]
    if task is None:
        sql += "AND (note IS NULL OR note NOT LIKE ? ESCAPE '\\') "
        params.append(f"{_TASK_NOTE_PREFIX}%")
    else:
        sql += "AND note LIKE ? ESCAPE '\\' "
        params.append(f"{_escape_like_pattern(_task_note_marker(task))}%")
    sql += "ORDER BY ts DESC LIMIT 1"
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            rows = conn.execute(sql, tuple(params)).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return False
    if not rows:
        return False
    try:
        last_ts = datetime.fromisoformat(rows[0][0])
    except (ValueError, TypeError):
        return False
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last_ts) <= _DEDUPE_WINDOW


def _rotate_prompt_history_if_needed() -> None:
    """prompt-history.jsonl が上限超過なら末尾 N 行を残して切り詰める。

    record_tier_outcome.py から移植（書き込み側のサイズ無制限成長を防ぐ）。
    失敗時は警告のみ。
    """
    try:
        size = os.path.getsize(PROMPT_HISTORY_PATH)
    except OSError:
        return
    if size <= _PROMPT_HISTORY_MAX_BYTES:
        return
    try:
        with open(PROMPT_HISTORY_PATH, "r", encoding="utf-8") as f:
            tail = list(collections.deque(f, maxlen=_PROMPT_HISTORY_TRUNCATE_LINES))
        tmp_path = PROMPT_HISTORY_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.writelines(tail)
        os.replace(tmp_path, PROMPT_HISTORY_PATH)
    except OSError as exc:
        print(
            f"[record_agent_outcome] prompt-history rotate skipped: {exc}",
            file=sys.stderr,
        )


def _append_prompt_history(
    selection: dict, *, complexity: str, tier: str, success: bool
) -> None:
    """DC-GP-005: --gate E-2 の記録時に成否問わず 1 行追記する。

    selection に prompt_prefix / prompt_hash が含まれていなければスキップ
    （古い tier_selection.json との後方互換。record_tier_outcome.py と同じ規則）。
    書き込み失敗は警告のみで握り潰す（呼び出し元を止めない）。
    """
    prompt_prefix = selection.get("prompt_prefix")
    prompt_hash = selection.get("prompt_hash")
    if not isinstance(prompt_prefix, str) or not isinstance(prompt_hash, str):
        return
    record = {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "prompt_hash": prompt_hash,
        "prompt_prefix": prompt_prefix,
        "complexity": complexity,
        "tier": tier,
        "outcome": "success" if success else "failure",
    }
    try:
        os.makedirs(os.path.dirname(PROMPT_HISTORY_PATH), exist_ok=True)
        _rotate_prompt_history_if_needed()
        line = json.dumps(record, ensure_ascii=False)
        # JSONL 互換性: U+2028 (LINE SEPARATOR) / U+2029 (PARAGRAPH SEPARATOR) を
        # ECMAScript パーサが行区切りと解釈するため事前にエスケープする [SR-V-001]。
        # NOTE: ソースコード上は escape 表記で識別し、実体文字を埋め込まない
        # (record_tier_outcome.py Cycle 3 M-01 / Cycle 4 H-01 の回帰防止。)
        _LS = chr(0x2028)  # LINE SEPARATOR
        _PS = chr(0x2029)  # PARAGRAPH SEPARATOR
        # JSON string values must contain the 6-char ASCII escape sequence
        # (backslash + 'u2028' / backslash + 'u2029'), NOT the raw U+2028/U+2029
        # character, because some JS/JSONL consumers treat those raw code points
        # as line terminators. Built via chr(0x5c) concatenation (not a literal
        # backslash-u escape in source) to avoid any tooling from re-interpreting
        # the escape sequence as the actual separator character.
        _ls_escape = '\\' + "u2028"
        _ps_escape = '\\' + "u2029"
        line = line.replace(_LS, _ls_escape).replace(_PS, _ps_escape)
        with open(PROMPT_HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as exc:
        print(
            f"[record_agent_outcome] prompt-history append skipped: {exc}",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    c3_db = _load_c3_db_module()

    warning = _validate_args(args, c3_db)
    if warning is not None:
        print(f"[record_agent_outcome] {warning} (recording skipped)", file=sys.stderr)
        return 0

    role = args.role
    outcome = args.outcome
    success = outcome == "success"
    execution = args.execution
    complexity = args.complexity

    # Round4 [SR-V-001]/[SR-K-003]: 長さ上限の適用。
    # --gate/--task は dedupe クエリ構築（[task:<id>] マーカー生成含む）より
    # 前に切り詰める。切り詰め後の値で dedupe・保存の両方を一貫させるため
    # （切り詰め前の値でマーカーを作ると保存済みマーカーと食い違う）。
    gate = _truncate(args.gate, MAX_GATE_LEN, "gate")
    task = _truncate(args.task, MAX_TASK_LEN, "task")

    busy_timeout_ms = getattr(c3_db, "BUSY_TIMEOUT_MS", 5000) if c3_db is not None else 5000

    c3_pricing = _load_pricing_module()
    tier_override = args.tier

    # ADR-AS-1: selection（tier_selection.json）の読み取りは tier 解決より前に
    # 1 度だけ行う（session_id 用読み取りと共用・二重 open 回避）。
    selection = _read_selection()

    # §0-3/§4-2 項1: session_id は tier 解決（優先2 の applied-state 突合）で
    # 使うため、tier 解決より前に hoist する（selection を再利用・二重 open なし）。
    # session_id は tier_selection.json 由来のため、tier_selection 不在
    # （--final 削除後含む）／session_id 欠落時は None（§0-4(b)）。この None は
    # applied-state 突合を不能にし、DB 記録時も NULL となる。
    session_id = selection.get("session_id") if selection else None

    if tier_override is not None:
        if tier_override in _VALID_TIERS:
            tier = tier_override
        elif execution == "persona" and tier_override == "unknown":
            # Round1 CR-低: persona 用の明示的 escape 値として許容する。
            tier = "unknown"
        else:
            print(
                f"[record_agent_outcome] --tier {tier_override!r} is not a "
                f"valid tier (expected one of {_VALID_TIERS}); recording skipped.",
                file=sys.stderr,
            )
            return 0
    elif execution == "persona":
        # Round1 親Claude検出: persona は --tier 省略時、frontmatter が解決
        # 可能でも "unknown" 固定とする（frontmatter は subagent の実使用 tier
        # であり、persona の実行時には親モデルが効くため fallback すると
        # DC-AS-001 の誤帰属がイベントログに再発するため）。
        tier = "unknown"
    else:
        tier = None
        # §4-2 優先 2（新規）: applied-state（適用者=記録 SSOT）。フェーズ3で
        # PreToolUse hook（tier_autoapply.py）が Agent 起動時の実適用値を
        # session_id 付きで JSONL 記録するため、record はそれを session_id
        # 一致で読む。role が soft-apply 対象のときのみ突合する（tester 等は
        # _SOFT_APPLY_ROLES 外なので applied-state を読まない・role gating）。
        if role in _SOFT_APPLY_ROLES:
            tier = _read_applied_tier(session_id, role, c3_pricing)
        # 優先 3（降格・旧優先2）: role が soft-apply 対象（_SOFT_APPLY_ROLES）なら
        # tier_selection.json の tier（無ければ suggested_model）を
        # resolve_tier で正規化し、_VALID_TIERS に含まれれば採用する。
        # 優先2（applied-state）が不成立のとき（applied-state 不在／session_id
        # 不一致／kill-switch で jsonl に行が無い等）のフォールバック A 互換。
        # 逐次経路（worktree なし）のみが対象。並列（worktree）経路は親が
        # --tier を明示するため優先 1 で解決され、ここには来ない（ADR-AS-4）。
        if tier is None and role in _SOFT_APPLY_ROLES and selection is not None:
            soft_apply_raw = selection.get("tier") or selection.get("suggested_model")
            # 非文字列ガード: resolve_tier() は内部で無条件に model.lower() を
            # 呼ぶため、tier_selection.json の破損・レース等で tier/
            # suggested_model が非文字列（int/list/dict 等）だと AttributeError
            # が未捕捉で伝播し、全エラー exit 0 不変を破る。_read_selection() が
            # dict 型を弾くのと同じ防御思想でフィールド型も検証し、非文字列は
            # soft-apply を採らず優先3（frontmatter fallback）へ落とす。
            if (
                isinstance(soft_apply_raw, str)
                and c3_pricing is not None
            ):
                resolved = c3_pricing.resolve_tier(soft_apply_raw)
                if resolved in _VALID_TIERS:
                    tier = resolved
        if tier is None:
            # 優先 4: frontmatter 自己解決（非対象 role、または優先2・3 が
            # 不成立だった developer の fallback）。
            tier = _resolve_tier_from_frontmatter(role, c3_pricing)
        if tier is None:
            print(
                f"[record_agent_outcome] tier unresolved for role={role!r} "
                f"(agents/{role}.md missing or model: unparseable/unknown). "
                "recording skipped (subagent requires a real tier).",
                file=sys.stderr,
            )
            return 0

    try:
        db_path = c3_db.locate_c3_db() if c3_db is not None else None
    except Exception as exc:  # noqa: BLE001
        print(
            f"[record_agent_outcome] locate_c3_db failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        db_path = None

    if session_id is not None:
        try:
            duplicate = _is_duplicate(
                db_path, session_id=session_id, gate=gate, role=role,
                success=success, task=task, busy_timeout_ms=busy_timeout_ms,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"[record_agent_outcome] dedupe check failed: {type(exc).__name__}",
                file=sys.stderr,
            )
            duplicate = False
        if duplicate:
            print(
                "[record_agent_outcome] duplicate outcome within 5 minutes "
                f"(session_id={session_id!r}, gate={gate!r}, role={role!r}); skipping.",
                file=sys.stderr,
            )
            return 0

    if c3_db is None:
        print("[record_agent_outcome] c3.db unavailable; recording skipped.", file=sys.stderr)
        return 0

    # Round4 [SR-V-001]/[SR-K-003]: note 本文のみ mask → truncate の順で処理する
    # （[task:<id>] マーカーは _compose_note() で mask/truncate 後に合成するため
    # 対象外のまま保全される）。順序を逆にすると PEM 等の複数行パターンが
    # truncate で分断されマスク漏れになる。
    note_body = _mask_secrets(args.note) if args.note else args.note
    note_body = _truncate(note_body, MAX_NOTE_LEN, "note")
    note = _compose_note(note_body, task)

    try:
        c3_db.record_agent_outcome_event(
            role=role, complexity=complexity, tier=tier, success=success,
            gate=gate, note=note, session_id=session_id, db_path=db_path,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[record_agent_outcome] recording failed: {type(exc).__name__}",
            file=sys.stderr,
        )

    # DC-GP-005: --gate E-2 は成否問わず prompt-history.jsonl に追記する。
    if gate == "E-2" and selection is not None:
        _append_prompt_history(selection, complexity=complexity, tier=tier, success=success)

    if args.final:
        _delete_selection()

    return 0


if __name__ == "__main__":
    sys.exit(main())
