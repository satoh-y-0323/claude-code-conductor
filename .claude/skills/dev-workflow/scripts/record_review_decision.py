#!/usr/bin/env python3
"""CLI: record a review decision in c3.db.review_decisions.

review-hint: dev-workflow フェーズ E でユーザーが「対応 / 許容 / 保留」を選んだ
判断を SQLite に記録するための薄い CLI ラッパー。実装本体は
``c3.db.insert_review_decision``。

Usage:
    python .claude/skills/dev-workflow/scripts/record_review_decision.py \
        --checklist-id CR-Q-001 \
        --finding "関数が長い" \
        --decision accepted \
        --reason "既存スタイルを尊重するため" \
        --reviewer code-reviewer

Exit code:
  0: 記録成功 / DB が無く何もしなかった（呼び出し元を止めない方針）
  2: 引数不正（argparse のエラー時）
"""

from __future__ import annotations

import argparse
import re
import sys


# checklist_id 形式検証用の正規表現（[CR-XX-NNN] / [SR-XX-NNN]、連番 3 桁以上）[SR-V-001]。
# review_hint_inject.py の CHECKLIST_ID_RE と整合（[ ] なし）。
CHECKLIST_ID_PATTERN = re.compile(r"^(CR|SR)-[A-Z]+-\d{3,}$")


# DB 肥大化防止のためのフィールド長上限 [SR-V-001]。
# 文字数 / バイト数の両方を上限として切り詰める（呼び出し元を止めない方針）。
# サロゲートペア・絵文字等で UTF-8 バイト長が文字長を大きく超えるケースを防ぐ。
MAX_FINDING_LEN = 2000        # 文字数上限
MAX_REASON_LEN = 2000
MAX_CONTEXT_LEN = 1000
MAX_FIELD_BYTES = 8 * 1024    # 全フィールド共通バイト数上限（8 KB）


def _truncate(value: str | None, limit: int, name: str) -> str | None:
    """value が文字数 limit 超または UTF-8 バイト数 MAX_FIELD_BYTES 超なら切り詰めて警告を出す。

    None / 空文字列はそのまま返す。文字数で切ったあともバイト数を再確認し、
    両条件で安全になるまで切り詰める（BMP 外文字対応）。

    NOTE: 現在の定数（MAX_FINDING_LEN=2000・MAX_FIELD_BYTES=8192）では、
    BMP 外の 4 バイト UTF-8 文字 2000 個でも 8000 バイトしか消費しないため、
    文字数で切った後の while ループは実質的に発火しない（防御的残置）。
    将来 MAX_*_LEN を引き上げる場合に備えて while を残してある。
    定数を変更する際は両条件の整合（MAX_LEN * 4 > MAX_FIELD_BYTES）を必ず確認すること。

    パフォーマンス: while ループ最悪計算量は O(N^2)（N=文字数）であり、入力長が
    数十 MB のオーダーで遅くなる。ローカル CLI 前提・上記の通り通常入力では
    そもそも while に入らないため許容。本来高速化するなら bytes 単位で 2 分探索する。
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
            f"[record_review_decision] --{name} truncated to {len(value)} chars / "
            f"{byte_len} bytes",
            file=sys.stderr,
        )
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record a review decision in c3.db")
    parser.add_argument("--checklist-id", required=True,
                        help="例: CR-Q-001 / SR-K-002")
    parser.add_argument("--finding", required=True,
                        help=f"指摘本文（後の参照用、最大 {MAX_FINDING_LEN} 文字で切り詰め）")
    parser.add_argument("--decision", required=True,
                        choices=["fixed", "accepted", "deferred"],
                        help="判断")
    parser.add_argument("--reason", default=None,
                        help=f"許容/保留時の理由（最大 {MAX_REASON_LEN} 文字で切り詰め）")
    parser.add_argument("--context", default=None,
                        help=f"ファイル名・コミット等の補助情報（最大 {MAX_CONTEXT_LEN} 文字で切り詰め）")
    parser.add_argument("--reviewer", required=True,
                        choices=["code-reviewer", "security-reviewer"],
                        help="どちらの reviewer の指摘か")

    args = parser.parse_args(argv)

    # checklist-id 形式検証（不正な値は DB に蓄積させず skip。CR-NEW / SR-NEW は対象外として除外）
    # [SR-V-001] 不正な ID が DB に入ると review-hint 照合が空振りするため insert を中止する
    if args.checklist_id not in ("CR-NEW", "SR-NEW") and not CHECKLIST_ID_PATTERN.match(args.checklist_id):
        print(
            f"[record_review_decision] --checklist-id format invalid (skipped): {args.checklist_id!r} "
            f"(expected pattern: CR-XX-NNN or SR-XX-NNN)",
            file=sys.stderr,
        )
        return 0

    # 長さ上限を適用（DB 肥大化防止）
    args.finding = _truncate(args.finding, MAX_FINDING_LEN, "finding")
    args.reason = _truncate(args.reason, MAX_REASON_LEN, "reason")
    args.context = _truncate(args.context, MAX_CONTEXT_LEN, "context")

    try:
        from c3.db import insert_review_decision
    except ImportError as exc:
        print(f"[record_review_decision] c3.db import failed: {exc}",
              file=sys.stderr)
        return 0  # 呼び出し元を止めない

    ok = insert_review_decision(
        checklist_id=args.checklist_id,
        finding_text=args.finding,
        decision=args.decision,
        reason=args.reason,
        context_summary=args.context,
        reviewer=args.reviewer,
    )
    if not ok:
        # 失敗は警告のみ（DB 不在等で C3 利用先で頻繁に起きうる）
        print(
            f"[record_review_decision] not recorded "
            f"(DB unavailable?): {args.checklist_id}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
