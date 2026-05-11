#!/usr/bin/env python3
"""CLI: record a review decision in c3.db.review_decisions.

F-001: dev-workflow フェーズ E でユーザーが「対応 / 許容 / 保留」を選んだ
判断を SQLite に記録するための薄い CLI ラッパー。実装本体は
``c3.db.insert_review_decision``。

Usage:
    python .claude/hooks/record_review_decision.py \
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
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record a review decision in c3.db")
    parser.add_argument("--checklist-id", required=True,
                        help="例: CR-Q-001 / SR-K-002")
    parser.add_argument("--finding", required=True,
                        help="指摘本文（後の参照用）")
    parser.add_argument("--decision", required=True,
                        choices=["fixed", "accepted", "deferred"],
                        help="判断")
    parser.add_argument("--reason", default=None,
                        help="許容/保留時の理由")
    parser.add_argument("--context", default=None,
                        help="ファイル名・コミット等の補助情報")
    parser.add_argument("--reviewer", required=True,
                        choices=["code-reviewer", "security-reviewer"],
                        help="どちらの reviewer の指摘か")

    args = parser.parse_args(argv)

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
