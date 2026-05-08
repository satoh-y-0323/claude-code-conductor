#!/usr/bin/env python3
"""CLI: record the outcome of the previously-selected tier in tier_bandit.

F-005 MVP: dev-workflow フェーズ E の承認/否認シグナルを受けて、
``select_tier.py`` が直近に書いた ``.claude/state/tier_selection.json`` を
読み、対応する Tier の (alpha, beta, trials) を ``tier_bandit`` テーブルで
更新する。

Usage:
    python .claude/hooks/record_tier_outcome.py --outcome success
    python .claude/hooks/record_tier_outcome.py --outcome failure

設計のポイント:
- 1 引数のみ（``--outcome``）にして dev-workflow から呼びやすくする。
- 記録対象が無い（``tier_selection.json`` が無い）場合は何もせず exit 0。
- 記録後は json を削除し、同じ選択が二重カウントされないようにする。
- DB 不在 / SQL エラー時は警告のみで exit 0（呼び出し元を止めない）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(_HOOKS_DIR)
TIER_SELECTION_PATH = os.path.join(_CLAUDE_DIR, "state", "tier_selection.json")
# Phase 2-C: prompt 履歴ファイル（select_tier.py が読む類似度推定の母数）。
PROMPT_HISTORY_PATH = os.path.join(_CLAUDE_DIR, "logs", "prompt-history.jsonl")


def _load_c3_db_module():
    here = Path(__file__).resolve()
    src = here.parents[2] / "src"
    if src.is_dir():
        src_str = str(src)
        if src_str not in sys.path:
            sys.path.insert(0, src_str)
    try:
        from parallel_orchestra import c3_db  # type: ignore[import-not-found]
        return c3_db
    except ImportError as exc:
        print(
            f"[record_tier_outcome] c3_db import failed: {exc}",
            file=sys.stderr,
        )
        return None


def _read_tier_selection() -> dict | None:
    if not os.path.isfile(TIER_SELECTION_PATH):
        return None
    try:
        with open(TIER_SELECTION_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"[record_tier_outcome] failed to read tier_selection: {exc}",
            file=sys.stderr,
        )
        return None
    if not isinstance(data, dict):
        return None
    if "complexity" not in data or "tier" not in data:
        return None
    return data


def _delete_tier_selection() -> None:
    """記録済みの選択を削除（二重カウント防止）。"""
    try:
        if os.path.isfile(TIER_SELECTION_PATH):
            os.remove(TIER_SELECTION_PATH)
    except OSError as exc:
        print(
            f"[record_tier_outcome] failed to delete tier_selection: {exc}",
            file=sys.stderr,
        )


def _append_prompt_history(selection: dict, success: bool) -> None:
    """Phase 2-C: prompt-history.jsonl に 1 行追記する。

    selection に prompt_prefix / prompt_hash が含まれていなければスキップ
    （古い tier_selection.json との後方互換）。
    書き込み失敗は警告のみで握り潰す（呼び出し元の dev-workflow を止めない）。
    """
    prompt_prefix = selection.get("prompt_prefix")
    prompt_hash = selection.get("prompt_hash")
    if not isinstance(prompt_prefix, str) or not isinstance(prompt_hash, str):
        return
    record = {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "prompt_hash": prompt_hash,
        "prompt_prefix": prompt_prefix,
        "complexity": selection.get("complexity"),
        "tier": selection.get("tier"),
        "outcome": "success" if success else "failure",
    }
    try:
        os.makedirs(os.path.dirname(PROMPT_HISTORY_PATH), exist_ok=True)
        with open(PROMPT_HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(
            f"[record_tier_outcome] prompt-history append skipped: {exc}",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record tier outcome for F-005 Thompson Sampling"
    )
    parser.add_argument(
        "--outcome",
        required=True,
        choices=["success", "failure"],
        help="承認 → success / 否認 → failure",
    )
    args = parser.parse_args(argv)

    selection = _read_tier_selection()
    if selection is None:
        # tier_selection.json が無い = 直近の UserPromptSubmit で hook が動いて
        # いないケース。何もせず正常終了（呼び出し元の dev-workflow を止めない）
        return 0

    c3_db = _load_c3_db_module()
    if c3_db is None:
        return 0

    success = (args.outcome == "success")
    ok = c3_db.update_tier_params(
        selection["complexity"],
        selection["tier"],
        success=success,
    )
    # Phase 2-B: tier_recent_outcomes にも 1 件記録（escalation 判定の母数）。
    # tier_bandit 更新が失敗してもこちらは試みる（DB が一時的に詰まる程度なら
    # 片方だけ通る可能性もある）。
    try:
        c3_db.record_tier_recent_outcome(
            complexity=selection["complexity"],
            tier=selection["tier"],
            success=success,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[record_tier_outcome] tier_recent_outcomes record skipped: {exc}",
            file=sys.stderr,
        )

    # Phase 2-C: prompt-history.jsonl にも 1 行追記。
    _append_prompt_history(selection, success)

    if ok:
        _delete_tier_selection()
    else:
        print(
            "[record_tier_outcome] update_tier_params returned False "
            "(DB unavailable?). selection json is kept for retry.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
