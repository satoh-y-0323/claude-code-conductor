"""scripts/audit_review_decisions.py

`review_decisions` の未判定な指摘（accepted / deferred）を棚卸しし、
判定結果（resolution）を書き戻すための配布元専用ツール。

使い方:
  python scripts/audit_review_decisions.py list [--db PATH] [--limit N]
  python scripts/audit_review_decisions.py resolve --id N --resolution {resolved|open|unverifiable}
                                                   [--note TEXT] [--commit SHA] [--force] [--db PATH]
  python scripts/audit_review_decisions.py summary [--db PATH]

配布対象外（scripts/ は wheel / sdist に含まれない）。migration 007 の適用が前提。

設計判断（architecture-report-20260804-202004.md）:
  - ADR-3: 配布物（db.py / record_review_decision.py）は変更せず、書き込みは本スクリプトが直接行う。
           ただし DB 探索は `locate_c3_db` を再利用する（SSOT）。
  - ADR-3: exit code は例外ではなく `main(argv)` の戻り値で返す（プロセス境界の seam）。
  - ADR-5: 未判定の抽出条件は
           `resolution IS NULL AND decision IN ('accepted','deferred') AND id <= 1232`（SSOT）。
  - ADR-8(a): 接続時に journal_mode=WAL と busy_timeout を設定する（定数は c3.db から import）。
  - ADR-8(b): resolved / unverifiable は note 必須、open は note 任意。commit は全タイプで必須。
  - ADR-8(c): 「書けなかったのに成功に見える」経路を作らない（異常系は戻り値 2）。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from c3.db import BUSY_TIMEOUT_MS, locate_c3_db

# stdout/stderr reconfigure for Windows CI compatibility (cp1252 environment)
# stdin は未使用なため reconfigure 不要（本スクリプトは stdin を読まない）
try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError, OSError):
    pass

try:
    sys.stderr.reconfigure(encoding='utf-8')
except (AttributeError, ValueError, OSError):
    pass


# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

# このスクリプトは scripts/ に置かれる
_REPO_ROOT = Path(__file__).resolve().parent.parent

# 抽出条件（ADR-5 が SSOT）
_FROZEN_MAX_ID = 1232
_TARGET_DECISIONS = ("accepted", "deferred")

# 判定語彙（ADR-2）
_VALID_RESOLUTIONS = ("resolved", "open", "unverifiable")
# note が必須の判定値（ADR-8(b)）。open のみ note 任意。
_NOTE_REQUIRED_RESOLUTIONS = ("resolved", "unverifiable")

_DEFAULT_LIMIT = 10

# list が出力するレコードのキー順（ADR-4）
_LIST_COLUMNS = (
    "id",
    "checklist_id",
    "finding_text",
    "decision",
    "reason",
    "context_summary",
    "decided_at",
    "reviewer",
    "severity",
)


# ---------------------------------------------------------------------------
# DB 接続
# ---------------------------------------------------------------------------

def _resolve_db_path(db_arg: str | None) -> Path | None:
    """--db が指定されていればそれを最優先し、省略時のみ locate_c3_db にフォールバックする。"""
    if db_arg:
        return Path(db_arg)
    return locate_c3_db()


def _connect(db_arg: str | None) -> sqlite3.Connection | None:
    """DB へ接続する。DB が見つからない場合は stderr へ出力して None を返す。"""
    db_path = _resolve_db_path(db_arg)
    if db_path is None:
        print(
            "エラー: c3.db が見つかりません。--db でパスを明示してください。",
            file=sys.stderr,
        )
        return None
    if not db_path.is_file():
        print(f"エラー: DB ファイルが存在しません: {db_path}", file=sys.stderr)
        return None

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


def _report_operational_error(exc: sqlite3.OperationalError) -> int:
    """OperationalError を stderr へ報告して戻り値 2 を返す。

    resolution 列が無い場合は migration 007 未適用である旨を明示する。
    """
    message = str(exc)
    lowered = message.lower()
    if "no such column" in lowered or "has no column named" in lowered:
        print(
            f"エラー: migration 007 が未適用の可能性があります（{message}）。"
            " SessionStart hook もしくは apply_pending_migrations で適用してください。",
            file=sys.stderr,
        )
    else:
        print(f"エラー: DB 操作に失敗しました: {message}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# git 連携
# ---------------------------------------------------------------------------

def _run_git(args: list[str]) -> str | None:
    """リポジトリルートで git を実行し stdout を返す。失敗時は None を返す。"""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=_REPO_ROOT,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None
    return result.stdout


def _count_commits_since(decided_at: str) -> int | None:
    """decided_at 以降のコミット件数を返す。git が失敗した場合は None を返す。"""
    if not decided_at:
        return None
    out = _run_git(["log", f"--since={decided_at}", "--oneline"])
    if out is None:
        return None
    return len([line for line in out.splitlines() if line.strip()])


def _head_commit() -> str | None:
    """現在の HEAD の SHA を返す。git が失敗した場合は None を返す。"""
    out = _run_git(["rev-parse", "HEAD"])
    if out is None:
        return None
    return out.strip() or None


# ---------------------------------------------------------------------------
# サブコマンド
# ---------------------------------------------------------------------------

def _cmd_list(args: argparse.Namespace) -> int:
    conn = _connect(args.db)
    if conn is None:
        return 2

    try:
        sql = (
            f"SELECT {', '.join(_LIST_COLUMNS)} FROM review_decisions"
            " WHERE resolution IS NULL"
            f" AND decision IN ({', '.join('?' * len(_TARGET_DECISIONS))})"
            " AND id <= ?"
            " ORDER BY id"
        )
        params: list[object] = [*_TARGET_DECISIONS, _FROZEN_MAX_ID]
        if args.limit != 0:
            sql += " LIMIT ?"
            params.append(args.limit)
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            return _report_operational_error(exc)
    finally:
        conn.close()

    if not rows:
        # 「対象なし」は stderr へ出す。stdout は JSON Lines 専用に保ち、
        # 非 JSON 行が混ざって消費側のパースが壊れることを避ける（戻り値は 0）。
        print("対象の未判定レコードはありません。", file=sys.stderr)
        return 0

    # decided_at ごとのコミット件数はコストが高いので同一値をキャッシュする
    commits_cache: dict[str, int | None] = {}
    for row in rows:
        record = dict(zip(_LIST_COLUMNS, row))
        decided_at = record["decided_at"]
        if decided_at not in commits_cache:
            commits_cache[decided_at] = _count_commits_since(decided_at)
        record["commits_since"] = commits_cache[decided_at]
        print(json.dumps(record, ensure_ascii=False))

    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    # 値域検証（argparse の choices は使わず戻り値 2 で返す）
    if args.resolution not in _VALID_RESOLUTIONS:
        print(
            f"エラー: --resolution の値が不正です: {args.resolution!r}"
            f"（許容値: {', '.join(_VALID_RESOLUTIONS)}）",
            file=sys.stderr,
        )
        return 2

    # note の必須判定（ADR-8(b)）
    if args.resolution in _NOTE_REQUIRED_RESOLUTIONS and not args.note:
        print(
            f"エラー: --resolution {args.resolution} には --note が必須です"
            "（監査可能性のため判定の根拠を残す）。",
            file=sys.stderr,
        )
        return 2

    # commit は全タイプで必須。省略時は HEAD を自動取得し、失敗したら明示指定を促す。
    commit = args.commit
    if not commit:
        commit = _head_commit()
        if commit is None:
            print(
                "エラー: git rev-parse HEAD に失敗しました。--commit で SHA を明示してください。",
                file=sys.stderr,
            )
            return 2

    conn = _connect(args.db)
    if conn is None:
        return 2

    try:
        try:
            row = conn.execute(
                "SELECT resolution FROM review_decisions WHERE id = ?",
                (args.id,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            return _report_operational_error(exc)

        if row is None:
            print(f"エラー: id={args.id} のレコードが存在しません。", file=sys.stderr)
            return 2

        if row[0] is not None and not args.force:
            print(
                f"エラー: id={args.id} は既に resolution={row[0]!r} で判定済みです。"
                " 再判定する場合は --force を付けてください。",
                file=sys.stderr,
            )
            return 2

        try:
            conn.execute(
                "UPDATE review_decisions"
                " SET resolution = ?, resolution_note = ?, resolution_commit = ?"
                " WHERE id = ?",
                (args.resolution, args.note, commit, args.id),
            )
            conn.commit()
        except sqlite3.OperationalError as exc:
            return _report_operational_error(exc)
    finally:
        conn.close()

    print(f"id={args.id} を resolution={args.resolution} で記録しました（commit={commit}）。")
    return 0


def _cmd_summary(args: argparse.Namespace) -> int:
    conn = _connect(args.db)
    if conn is None:
        return 2

    try:
        sql = (
            "SELECT resolution, severity, reviewer, COUNT(*) FROM review_decisions"
            f" WHERE decision IN ({', '.join('?' * len(_TARGET_DECISIONS))})"
            " AND id <= ?"
            " GROUP BY resolution, severity, reviewer"
            " ORDER BY resolution IS NULL DESC, resolution, severity, reviewer"
        )
        params: list[object] = [*_TARGET_DECISIONS, _FROZEN_MAX_ID]
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            return _report_operational_error(exc)
    finally:
        conn.close()

    print(f"resolution x severity x reviewer 件数（id <= {_FROZEN_MAX_ID} / "
          f"decision IN {_TARGET_DECISIONS}）")
    total = 0
    for resolution, severity, reviewer, count in rows:
        print(
            f"  {resolution or 'NULL(未判定)'}\t{severity or 'NULL'}\t{reviewer or 'NULL'}\t{count}"
        )
        total += count
    print(f"  合計: {total} 件")
    return 0


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="review_decisions の未判定指摘を棚卸しし、判定結果を書き戻す",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def _add_db_option(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--db",
            default=None,
            help="c3.db のパス（省略時は locate_c3_db で探索する）",
        )

    p_list = subparsers.add_parser("list", help="未判定レコードを JSON Lines で出力する")
    _add_db_option(p_list)
    p_list.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_LIMIT,
        help=f"出力件数の上限（既定 {_DEFAULT_LIMIT}・0 で全件）",
    )
    p_list.set_defaults(func=_cmd_list)

    p_resolve = subparsers.add_parser("resolve", help="1 件に判定結果を書き戻す")
    _add_db_option(p_resolve)
    p_resolve.add_argument("--id", type=int, required=True, help="対象の review_decisions.id")
    p_resolve.add_argument(
        "--resolution",
        required=True,
        help=f"判定結果（{'|'.join(_VALID_RESOLUTIONS)}）",
    )
    p_resolve.add_argument("--note", default=None, help="判定の根拠（resolved / unverifiable では必須）")
    p_resolve.add_argument("--commit", default=None, help="判定時点の SHA（省略時は git rev-parse HEAD）")
    p_resolve.add_argument("--force", action="store_true", help="判定済みの行を上書きする")
    p_resolve.set_defaults(func=_cmd_resolve)

    p_summary = subparsers.add_parser(
        "summary", help="resolution x severity x reviewer の件数を集計する"
    )
    _add_db_option(p_summary)
    p_summary.set_defaults(func=_cmd_summary)

    return parser


def main(argv: list[str] | None = None) -> int:
    """スクリプトのエントリポイント。

    Args:
        argv: コマンドライン引数（None なら sys.argv[1:]）。

    Returns:
        終了コード: 0 = 成功 / 2 = 検証エラー・異常系。
        exit code は例外ではなく戻り値で返す（ADR-3 のプロセス境界 seam）。
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse の使い方エラー / --help も戻り値へ寄せ、例外を境界外へ漏らさない
        return int(exc.code or 0)

    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
