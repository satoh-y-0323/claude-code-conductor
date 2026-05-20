#!/usr/bin/env python3
"""Stop hook orchestrator: stdin 読み出し 1 回で stop + consolidate_memory を順次実行する.

settings.json の Stop hook 配列に複数本登録するのではなく、本ファイル 1 本に
集約することで:
- stdin の payload パースが 1 回で済む
- フックエントリの重複を排除（settings.json のフラット化）

責務:
  Phase 1: stop.run(payload) — セッションファイル更新 + パターン信頼度
  Phase 2: consolidate_memory.run_sync(today) — 集約・promotion 候補・archive

各フェーズは独立して try/except でラップし、片方が失敗しても他方を実行する。
失敗してもセッションは止めない（exit 0）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from datetime import datetime, timezone

try:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

# Stop hook の stdin payload に対する上限（1 MB）[SR-V-001]
MAX_STDIN_BYTES = 1 * 1024 * 1024

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_module(name: str) -> types.ModuleType:
    """同階層の hook ファイルをモジュールとして動的にロードする.

    sys.path 操作を避けるため importlib.util を使用する（既存 consolidate_memory
    の `_load_session_utils()` と同じ方針）。
    """
    path = os.path.join(_HOOKS_DIR, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"hook モジュールが見つかりません: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


def main() -> int:
    """Stop hook エントリポイント.

    stdin を 1 回読んで stop.run / consolidate_memory.run_sync を順に呼ぶ。
    片方が失敗しても他方は実行する。常に exit 0 を返す。

    [SR-V-001] stdin payload の上限は MAX_STDIN_BYTES (1 MB)。
    超過時は stderr に警告を出力して return 0 で早期リターンする（セッションは止めない）。
    """
    try:
        raw = sys.stdin.read()
        if len(raw) > MAX_STDIN_BYTES:
            print(
                f"[session_stop] stdin payload exceeds {MAX_STDIN_BYTES} bytes; aborting",
                file=sys.stderr,
            )
            return 0
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        payload = {}

    # 全体で同じ "today" を共有する（決定論性確保）
    today = datetime.now(timezone.utc)

    # Phase 1: stop.py — セッションファイル更新 + パターン信頼度
    try:
        stop_module = _load_module("stop")
        stop_module.run(payload)
    except Exception as e:
        print(f"[session_stop:stop] failed: {e}", file=sys.stderr)

    # Phase 2: consolidate_memory.py — 集約・promotion 候補・archive
    try:
        consolidate_module = _load_module("consolidate_memory")
        consolidate_module.run_sync(today=today)
    except Exception as e:
        print(f"[session_stop:consolidate_memory] failed: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
