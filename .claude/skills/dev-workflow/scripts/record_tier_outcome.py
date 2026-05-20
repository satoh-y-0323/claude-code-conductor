#!/usr/bin/env python3
"""CLI: record the outcome of the previously-selected tier in tier_bandit.

tier-routing MVP: dev-workflow フェーズ E の承認/否認シグナルを受けて、
``select_tier.py`` が直近に書いた ``.claude/state/tier_selection.json`` を
読み、対応する Tier の (alpha, beta, trials) を ``tier_bandit`` テーブルで
更新する。

Usage:
    python .claude/skills/dev-workflow/scripts/record_tier_outcome.py --outcome success
    python .claude/skills/dev-workflow/scripts/record_tier_outcome.py --outcome failure

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

# このファイルは .claude/skills/dev-workflow/scripts/ に置かれている前提。
# 上位 3 階層を遡って .claude/ ディレクトリを得る:
#   scripts/ → dev-workflow/ → skills/ → .claude/
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(_SCRIPT_DIR)))
# 3 階層遡りで `.claude` に到達することを実行時に検証。
# 将来スクリプトが別階層に移動された場合のサイレント破綻を防ぐ。
#
# 注: このアサーションはセキュリティ防御ではなく、スクリプトの誤配置
# （ディレクトリ階層変更・移動忘れ等）を実行時に検出するための開発時チェック。
# 外部攻撃者がディレクトリ構造を制御できる脅威モデルは前提としていない（[SR-NEW]）。
assert _CLAUDE_DIR.endswith(os.sep + ".claude") or _CLAUDE_DIR.endswith("/.claude"), (
    f"_CLAUDE_DIR resolution broke: expected to end with '.claude' but got {_CLAUDE_DIR!r}. "
    "Check that this file is at .claude/skills/dev-workflow/scripts/."
)
TIER_SELECTION_PATH = os.path.join(_CLAUDE_DIR, "state", "tier_selection.json")
# Phase 2-C: prompt 履歴ファイル（select_tier.py が読む類似度推定の母数）。
PROMPT_HISTORY_PATH = os.path.join(_CLAUDE_DIR, "logs", "prompt-history.jsonl")


def _load_c3_db_module():
    """c3.db helper モジュールを返す。

    c3 パッケージは pip install 済みのため sys.path 操作は不要。
    """
    try:
        from c3 import db as c3_db  # type: ignore[import-not-found]
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


# prompt-history.jsonl の上限サイズ（バイト）。超過時は末尾 _PROMPT_HISTORY_TRUNCATE_LINES 行
# だけを残してローテーションする。読み込み側 (select_tier._PROMPT_HISTORY_SCAN_LINES=1000) と
# 同じオーダーで保持し、ディスク消費を抑える [SR-V-001]。
_PROMPT_HISTORY_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_PROMPT_HISTORY_TRUNCATE_LINES = 2000


def _rotate_prompt_history_if_needed() -> None:
    """prompt-history.jsonl が上限超過なら末尾 N 行を残して切り詰める。

    書き込み側のサイズ無制限成長を防ぐシンプルなローテーション。失敗時は警告のみ。
    """
    try:
        size = os.path.getsize(PROMPT_HISTORY_PATH)
    except OSError:
        return
    if size <= _PROMPT_HISTORY_MAX_BYTES:
        return
    try:
        # 末尾 N 行のみ deque で保持して上書きする（ファイル全体は走査するが I/O のみ）
        import collections as _c
        with open(PROMPT_HISTORY_PATH, "r", encoding="utf-8") as f:
            tail = list(_c.deque(f, maxlen=_PROMPT_HISTORY_TRUNCATE_LINES))
        tmp_path = PROMPT_HISTORY_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.writelines(tail)
        os.replace(tmp_path, PROMPT_HISTORY_PATH)
    except OSError as exc:
        print(
            f"[record_tier_outcome] prompt-history rotate skipped: {exc}",
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
        _rotate_prompt_history_if_needed()
        line = json.dumps(record, ensure_ascii=False)
        # JSONL 互換性: U+2028 (LINE SEPARATOR) / U+2029 (PARAGRAPH SEPARATOR) を
        # ECMAScript パーサが行区切りと解釈するため事前にエスケープする [SR-V-001]。
        # NOTE: ソースコード上は escape 表記で識谞し、実体文字を埋め込まない
        # (Cycle 3 M-01 / Cycle 4 H-01 の回帰防止。)
        _LS = chr(0x2028)  # LINE SEPARATOR
        _PS = chr(0x2029)  # PARAGRAPH SEPARATOR
        line = line.replace(_LS, "\u2028").replace(_PS, "\u2029")
        with open(PROMPT_HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as exc:
        print(
            f"[record_tier_outcome] prompt-history append skipped: {exc}",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record tier outcome for tier-routing Thompson Sampling"
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
