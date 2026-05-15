#!/usr/bin/env python3
"""Stop hook orchestrator: stdin 読み出し 1 回で stop + consolidate_memory を順次実行する.

settings.json の Stop hook 配列に複数本登録するのではなく、本ファイル 1 本に
集約することで:
- stdin の payload パースが 1 回で済む
- フックエントリの重複を排除（settings.json のフラット化）

責務:
  Phase 1: stop.run(payload) — セッションファイル更新 + パターン信頼度
  Phase 2: consolidate_memory.run_sync(today) — 集約・promotion 候補・archive
  Phase 3: LLM 要約エージェント起動フラグ制御（exit 2 + stderr で Claude に指示）

各フェーズは独立して try/except でラップし、片方が失敗しても他方を実行する。
失敗してもセッションは止めない（exit 0）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from datetime import date, datetime, timedelta, timezone

try:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(_HOOKS_DIR)
_FLAG_PATH = os.path.join(_CLAUDE_DIR, "state", "llm_summary_agent_requested.flag")

_AGENT_INSTRUCTION = """\
直近 7 日のセッションファイルが蓄積されています。
`summarize-memory` スキルをバックグラウンドで実行して
`.claude/memory/llm_summary.md` を更新してください。

手順:
1. `.claude/skills/summarize-memory/SKILL.md` を Read で読み込む
2. その手順に従って要約を生成し `.claude/memory/llm_summary.md` に Write する
3. 完了後 `.claude/state/llm_summary_agent_requested.flag` を削除する
"""


def _has_recent_sessions(
    sessions_dir: str, window_days: int = 7, today: date | None = None
) -> bool:
    """sessions_dir 内に window_days 日以内の YYYYMMDD.tmp ファイルがあれば True を返す.

    循環依存回避のため consolidate_memory.py に依存せず独立実装する。
    today は主にテスト注入用。省略時は date.today() を使う。
    """
    if not os.path.isdir(sessions_dir):
        return False
    reference = today if today is not None else date.today()
    cutoff = reference - timedelta(days=window_days)
    for filename in os.listdir(sessions_dir):
        if not filename.endswith(".tmp"):
            continue
        stem = filename[:-4]  # ".tmp" を除去
        try:
            file_date = datetime.strptime(stem, "%Y%m%d").date()
        except ValueError:
            continue
        if file_date >= cutoff:
            return True
    return False


def _llm_summary_recently_updated(claude_dir: str, cooldown_minutes: int = 60) -> bool:
    """llm_summary.md が cooldown_minutes 分以内に更新されていれば True を返す."""
    summary_path = os.path.join(claude_dir, "memory", "llm_summary.md")
    if not os.path.isfile(summary_path):
        return False
    mtime = datetime.fromtimestamp(os.path.getmtime(summary_path), tz=timezone.utc)
    return (datetime.now(timezone.utc) - mtime).total_seconds() < cooldown_minutes * 60


def _create_flag(flag_path: str) -> None:
    """flag_path の親ディレクトリを作成してから空ファイルを touch する."""
    os.makedirs(os.path.dirname(flag_path), exist_ok=True)
    with open(flag_path, "w", encoding="utf-8"):
        pass


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
    片方が失敗しても他方は実行する。

    Phase 3: Phase 1/2 完了後にフラグファイルを参照し、
    LLM 要約エージェントの起動指示を制御する。
    - flag なし & 直近 7 日に session あり → exit 2 + flag 作成 + stderr に起動指示
    - flag あり                            → exit 0 + flag 削除 (ループ防止)
    - flag なし & session なし             → exit 0 (何もしない)
    """
    try:
        payload = json.loads(sys.stdin.read())
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

    # Phase 2: consolidate_memory.py — 集約・promotion 候補・archive・LLM デタッチ
    try:
        consolidate_module = _load_module("consolidate_memory")
        consolidate_module.run_sync(today=today)
    except Exception as e:
        print(f"[session_stop:consolidate_memory] failed: {e}", file=sys.stderr)

    # Phase 3: LLM 要約エージェント起動フラグ制御
    try:
        flag_path = _FLAG_PATH
        if os.path.exists(flag_path):
            # フラグあり → 削除して exit 0（exit 2 ループ防止）
            os.unlink(flag_path)
            return 0

        # 直近 7 日に session ファイルがあるか確認
        sessions_dir = os.path.join(_CLAUDE_DIR, "memory", "sessions")
        if not _has_recent_sessions(sessions_dir):
            return 0

        # llm_summary.md が直近 60 分以内に更新済みならスキップ（連続発火防止）
        if _llm_summary_recently_updated(_CLAUDE_DIR):
            return 0

        # フラグ作成 + stderr に Agent 起動指示
        _create_flag(flag_path)
        print(_AGENT_INSTRUCTION, file=sys.stderr)
        return 2
    except Exception as e:
        print(f"[session_stop:flag_control] failed: {type(e).__name__}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
