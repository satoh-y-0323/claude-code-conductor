#!/usr/bin/env python3
"""CLI: emit a PO heartbeat to c3.db.po_status from a worktree-internal Claude.

F-002 Phase 2-B: PO の worktree 内で動く子 Claude プロセスから、自身の
進捗を ``.claude/state/c3.db`` の ``po_status`` テーブルに直接 UPSERT する
ための薄い CLI ラッパー。実装本体は
``parallel_orchestra.c3_db.upsert_po_status``。

session_id / worktree_id は環境変数 ``C3_PO_SESSION_ID`` /
``C3_PO_WORKTREE_ID`` から取得する（runner.py が subprocess 起動時に
注入）。DB パスは ``C3_PO_DB_PATH`` を優先し、なければ
``locate_c3_db()`` の親遡り探索に任せる。

Usage:
    python .claude/hooks/po_heartbeat.py --state running --step "Wave 2" --progress 50

Exit code:
  0: 記録成功 / 環境変数欠落 / DB 不在 / その他のフェイルセーフ
     （呼び出し元を絶対に止めない方針）
  2: 引数不正（argparse のエラー時）
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Emit a PO heartbeat to c3.db.po_status",
    )
    parser.add_argument(
        "--state",
        required=True,
        choices=["starting", "running", "completed", "failed"],
        help="UPSERT する状態",
    )
    parser.add_argument(
        "--step",
        default=None,
        help="現在のサブステップ（例: 'Wave 2 - tester'）",
    )
    parser.add_argument(
        "--progress",
        type=int,
        default=None,
        help="進捗率 0-100（不明なら省略）",
    )
    args = parser.parse_args(argv)

    session_id = os.environ.get("C3_PO_SESSION_ID")
    worktree_id = os.environ.get("C3_PO_WORKTREE_ID")
    if not session_id or not worktree_id:
        # 親 Claude が env を注入していない（PO 経由でない単独実行など）。
        # フェイルセーフで何もせず exit 0。
        print(
            "[po_heartbeat] C3_PO_SESSION_ID / C3_PO_WORKTREE_ID not set; "
            "skipping heartbeat",
            file=sys.stderr,
        )
        return 0

    try:
        from c3.db import upsert_po_status
    except ImportError as exc:
        print(f"[po_heartbeat] c3.db import failed: {exc}", file=sys.stderr)
        return 0  # 呼び出し元を止めない

    ok = upsert_po_status(
        session_id=session_id,
        worktree_id=worktree_id,
        state=args.state,
        current_step=args.step,
        progress_pct=args.progress,
    )
    if not ok:
        # DB 不在 or 書き込み失敗。失敗は警告のみで exit 0。
        print(
            f"[po_heartbeat] not recorded (DB unavailable?): "
            f"session={session_id} worktree={worktree_id} state={args.state}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
