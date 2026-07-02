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
- --tier 省略時、--execution=subagent は ``.claude/agents/{role}.md`` の
  frontmatter `model:` 行を自己解決し、``pricing.resolve_tier`` で正規化する
  （帰属ズレの構造的再発防止）。解決不能時は記録スキップ。
  --execution=persona は --tier 省略時、frontmatter へは fallback せず常に
  tier="unknown" 固定でイベントログのみ記録する（frontmatter は subagent の
  実使用 tier であり、persona の実行時には親モデルが効くため）。
- --tier を明示した場合は TIERS(haiku/sonnet/opus) に含まれるか検証し、
  含まれなければ警告 + 記録スキップとする。ただし --execution=persona かつ
  --tier unknown は明示的な escape 値として許容する。
- --execution=subagent のみ bandit（agent_tier_bandit）を更新する。
  --execution=persona は agent_outcomes イベントログのみ（親 Claude ペルソナは
  実行時に frontmatter の model が効かないため）。
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


# Round4 [SR-V-001]: --note 中の秘密情報マスクパターン。select_tier.py の
# _MASK_PATTERNS と同期（select_tier.py 側を変更した場合はこちらも合わせる）。
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

    select_tier.py の _mask_secrets() と同期（select_tier.py 側を変更した
    場合はこちらも合わせる）。キー名やプレフィックスは残し、値のみを置換する。
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
        help="省略時は agents/{role}.md の model: を自己解決する",
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

    selection = _read_selection()
    session_id = selection.get("session_id") if selection else None

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
        if execution == "subagent":
            c3_db.update_agent_tier_params(
                role, complexity, tier, success=success, db_path=db_path
            )
            c3_db.record_agent_outcome_event(
                role=role, complexity=complexity, tier=tier, success=success,
                gate=gate, note=note, session_id=session_id, db_path=db_path,
            )
        else:  # persona
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
