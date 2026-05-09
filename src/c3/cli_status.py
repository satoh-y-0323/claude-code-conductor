"""``c3 status`` - PO ダッシュボード CLI (F-003 Phase 2)。

`po-status` skill の DuckDB ATTACH 経由は初回 5〜10 秒の遅延があるため、
SQLite 直接参照で <1 秒の即時応答を提供する CLI 版。

主な機能:
- 引数なしで最新 session の active worktree 一覧を表形式表示
- ``--watch`` でリアルタイム再描画
- ``--json`` で機械可読出力
- ``--state failed --verbose`` で失敗 worktree の error_message 全文表示
- stale 検出（heartbeat 90 秒超の running をハイライト）
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from c3._terminal import sanitize_terminal_text, strip_ansi, supports_color
from parallel_orchestra import c3_db


logger = logging.getLogger(__name__)


_VALID_STATES = ["starting", "running", "completed", "failed", "waiting"]
_DEFAULT_INTERVAL = 30
_DEFAULT_STALE_THRESHOLD = 90
_DEFAULT_LIMIT = 50
_DEFAULT_ALL_SESSIONS_LIMIT = 5
_MIN_INTERVAL = 1  # Lower bound to avoid busy loop / divide-by-zero
_MIN_STALE_THRESHOLD = 1  # Lower bound to avoid false-stale on every row
_BUSY_TIMEOUT_MS = 5000
_ERROR_PREVIEW_LEN = 80
_ERROR_FULL_LEN = 500
_CURRENT_STEP_TRUNCATE = 40
_WORKTREE_TAIL_LEN = 30  # ASCII-character count; multi-byte CJK is naturally truncated by the same width
_SESSION_PREFIX_LEN = 8


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "status",
        help="Show PO worktree status from .claude/state/c3.db",
    )
    parser.add_argument("--session", help="Filter by session_id")
    parser.add_argument(
        "--all",
        action="store_true",
        help=f"Show recent {_DEFAULT_ALL_SESSIONS_LIMIT} sessions instead of just the latest",
    )
    parser.add_argument(
        "--state",
        choices=_VALID_STATES,
        help="Filter by state",
    )
    parser.add_argument(
        "--worktree",
        help="Glob pattern to filter worktree_id (e.g. 'po/*-task-*')",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Auto-refresh display (default interval 30s)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=_DEFAULT_INTERVAL,
        help=f"--watch refresh interval in seconds (default: {_DEFAULT_INTERVAL})",
    )
    parser.add_argument(
        "--stale-threshold",
        type=int,
        default=_DEFAULT_STALE_THRESHOLD,
        help=f"Mark running rows stale if heartbeat older than N seconds (default: {_DEFAULT_STALE_THRESHOLD})",
    )
    parser.add_argument(
        "--no-stale",
        action="store_true",
        help="Disable stale detection",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_LIMIT,
        help=f"Max rows per session (default: {_DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output rows as JSON for machine consumption",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help=f"Show full error_message ({_ERROR_FULL_LEN} chars) instead of preview ({_ERROR_PREVIEW_LEN})",
    )
    parser.set_defaults(handler=handle)


def handle(args: argparse.Namespace) -> int:
    # Clamp lower bounds so users cannot accidentally cause busy loop / mass false-stale.
    if args.interval < _MIN_INTERVAL:
        args.interval = _MIN_INTERVAL
    if args.stale_threshold < _MIN_STALE_THRESHOLD:
        args.stale_threshold = _MIN_STALE_THRESHOLD
    if args.watch:
        return _run_watch_loop(args)
    return _render_once(args)


def _run_watch_loop(args: argparse.Namespace) -> int:
    """--watch モード: ANSI 画面クリア + 描画 + sleep のループ。"""
    try:
        while True:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
            _render_once(args)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()
        return 0


def _render_once(args: argparse.Namespace) -> int:
    db_path = c3_db.locate_c3_db()
    if db_path is None or not Path(db_path).exists():
        print("no po_status records found (c3.db not initialized)")
        return 0

    sessions_to_fetch = _resolve_sessions(args, db_path)
    if not sessions_to_fetch:
        if args.json:
            print(json.dumps([], ensure_ascii=False, indent=2))
        else:
            print("no po_status records found")
        return 0

    rows: list[dict] = []
    for sid in sessions_to_fetch:
        rows.extend(c3_db.fetch_po_status(session_id=sid, db_path=db_path, limit=args.limit))

    rows = _apply_filters(rows, state=args.state, worktree_glob=args.worktree)
    rows = _annotate_stale(rows, threshold_sec=args.stale_threshold, enabled=not args.no_stale)
    rows = _attach_error_messages(rows, db_path=db_path, verbose=args.verbose)

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(_render_table(rows, color=supports_color(), verbose=args.verbose))

    return 0


def _resolve_sessions(args: argparse.Namespace, db_path: Path) -> list[str]:
    """args に応じて表示対象の session_id リストを返す。"""
    if args.session:
        return [args.session]
    if args.all:
        return _list_recent_sessions(db_path, limit=_DEFAULT_ALL_SESSIONS_LIMIT)
    latest = _get_latest_session_id(db_path)
    return [latest] if latest else []


def _get_latest_session_id(db_path: Path) -> str | None:
    """po_status から最新 heartbeat の session_id を 1 つ取得。

    --watch 中の heartbeat スレッド書き込みと衝突しないよう
    busy_timeout を設定する（既知パターン: F-002 Phase 2-B）。
    """
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            row = conn.execute(
                "SELECT session_id FROM po_status "
                "GROUP BY session_id "
                "ORDER BY MAX(last_heartbeat) DESC "
                "LIMIT 1"
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to get latest session_id: %s", exc)
        return None


def _list_recent_sessions(db_path: Path, *, limit: int) -> list[str]:
    """po_status から直近 N session の session_id を取得（最新順）。

    --watch 中の heartbeat スレッド書き込みと衝突しないよう
    busy_timeout を設定する（既知パターン: F-002 Phase 2-B）。
    """
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            rows = conn.execute(
                "SELECT session_id FROM po_status "
                "GROUP BY session_id "
                "ORDER BY MAX(last_heartbeat) DESC "
                "LIMIT ?",
                (limit,),
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to list recent sessions: %s", exc)
        return []


def _apply_filters(
    rows: list[dict],
    *,
    state: str | None,
    worktree_glob: str | None,
) -> list[dict]:
    if state:
        rows = [r for r in rows if r.get("state") == state]
    if worktree_glob:
        rows = [r for r in rows if fnmatch.fnmatch(r.get("worktree_id", ""), worktree_glob)]
    return rows


def _annotate_stale(rows: list[dict], *, threshold_sec: int, enabled: bool) -> list[dict]:
    """running 行のうち heartbeat が threshold 超のものに stale=True を付与。"""
    if not enabled:
        for r in rows:
            r["stale"] = False
        return rows
    now = datetime.now(timezone.utc)
    for r in rows:
        if r.get("state") != "running":
            r["stale"] = False
            continue
        ts = _parse_iso(r.get("last_heartbeat"))
        if ts is None:
            r["stale"] = False
        else:
            r["stale"] = (now - ts).total_seconds() > threshold_sec
    return rows


def _attach_error_messages(
    rows: list[dict],
    *,
    db_path: Path,
    verbose: bool,
) -> list[dict]:
    """failed 行のみに po_results.error_message を結合する。

    非 failed 行には ``error_message`` キーを付けない。
    JSON 出力でキーの有無により failed/非 failed を判別できる責務分離。
    呼び出し側は ``r.get("error_message", "")`` で安全に参照する。
    """
    failed_session_ids = {r["session_id"] for r in rows if r.get("state") == "failed"}
    if not failed_session_ids:
        return rows

    error_map: dict[tuple[str, str], str] = {}
    for sid in failed_session_ids:
        results = c3_db.fetch_po_results(session_id=sid, db_path=db_path, status="failure")
        for res in results:
            key = (res["session_id"], res["worktree_id"])
            error_map[key] = res.get("error_message") or ""

    truncate_len = _ERROR_FULL_LEN if verbose else _ERROR_PREVIEW_LEN
    for r in rows:
        if r.get("state") != "failed":
            continue
        msg = error_map.get((r["session_id"], r["worktree_id"]), "")
        if msg and len(msg) > truncate_len:
            msg = msg[:truncate_len] + "…"
        r["error_message"] = msg
    return rows


def _render_table(rows: list[dict], *, color: bool, verbose: bool) -> str:
    if not rows:
        return "no rows match the given filters"

    headers = ["session", "worktree", "state", "current_step", "progress", "heartbeat"]
    has_error = any(r.get("error_message") for r in rows)
    if has_error:
        headers.append("error")

    table_rows: list[list[str]] = []
    for r in rows:
        sess = (r.get("session_id", "") or "")[:_SESSION_PREFIX_LEN]
        # ASCII 文字数ベースの末尾抽出（CJK 等のマルチバイトはそのまま truncate される）
        wt = r.get("worktree_id", "") or ""
        if len(wt) > _WORKTREE_TAIL_LEN:
            wt = "…" + wt[-(_WORKTREE_TAIL_LEN - 1):]
        state_label = r.get("state", "?") or "?"
        is_stale = bool(r.get("stale"))
        if is_stale:
            state_label = "[STALE]"
        state_colored = _colorize_state(state_label, r.get("state"), is_stale, color=color)

        # current_step / error_message は信頼できない可能性があるテキストとして扱い、
        # ANSI / 制御文字インジェクションを防ぐためサニタイズしてから truncate する。
        step = sanitize_terminal_text(r.get("current_step") or "")
        if len(step) > _CURRENT_STEP_TRUNCATE:
            step = step[:_CURRENT_STEP_TRUNCATE - 1] + "…"

        progress = r.get("progress_pct")
        progress_str = f"{progress}%" if progress is not None else "--"

        hb = _relative_time(r.get("last_heartbeat") or "")

        row_data = [sess, wt, state_colored, step, progress_str, hb]
        if has_error:
            err = sanitize_terminal_text(r.get("error_message", "") or "")
            row_data.append(err)
        table_rows.append(row_data)

    return _format_table(headers, table_rows)


def _colorize_state(label: str, raw_state: str | None, stale: bool, *, color: bool) -> str:
    if not color:
        return label
    code = "\033[0m"
    if stale:
        code = "\033[33m"  # yellow
    elif raw_state == "completed":
        code = "\033[32m"  # green
    elif raw_state == "failed":
        code = "\033[31m"  # red
    elif raw_state == "running":
        code = "\033[36m"  # cyan
    elif raw_state in ("starting", "waiting"):
        code = "\033[90m"  # bright black
    return f"{code}{label}\033[0m"


def _format_table(headers: list[str], rows: list[list[str]]) -> str:
    """シンプルな空白パディング表（ANSI 'm' コード長を無視して幅計算）。"""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            visible = strip_ansi(cell)
            if len(visible) > widths[i]:
                widths[i] = len(visible)

    def fmt_row(cells: list[str]) -> str:
        parts = []
        for i, cell in enumerate(cells):
            visible_len = len(strip_ansi(cell))
            pad = " " * (widths[i] - visible_len)
            parts.append(cell + pad)
        return "  ".join(parts).rstrip()

    lines = [fmt_row(headers)]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append(fmt_row(row))
    return "\n".join(lines)


def _relative_time(iso: str) -> str:
    dt = _parse_iso(iso)
    if dt is None:
        return "?"
    now = datetime.now(timezone.utc)
    sec = (now - dt).total_seconds()
    if sec < 0:
        return "future?"
    if sec < 60:
        return f"{int(sec)}s ago"
    if sec < 3600:
        return f"{int(sec / 60)}m ago"
    return f"{int(sec / 3600)}h ago"


def _parse_iso(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
