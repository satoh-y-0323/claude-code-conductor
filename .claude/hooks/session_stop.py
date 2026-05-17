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
from datetime import datetime, timezone

try:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(_HOOKS_DIR)
_FLAG_PATH = os.path.join(_CLAUDE_DIR, "state", "llm_summary_agent_requested.flag")
# フラグ状態機械: 空文字列 = エージェント実行中、この値 = エージェント完了済み
_FLAG_DONE_CONTENT = "DONE"

_AGENT_INSTRUCTION = """\
直近のセッションファイルが llm_summary.md より新しいため要約が必要です。
Agent ツールを以下のパラメータで起動してください（**必ず run_in_background: true**）:

  subagent_type: "summarize-memory"
  description: "Summarize recent session memory (background)"
  run_in_background: true

ユーザーをブロックしないよう、起動後すぐに次の入力を受け付けてください。
"""


def _needs_summary(claude_dir: str) -> bool:
    """要約が必要か判定する.

    判定ロジック:
      - sessions ディレクトリ不在 / *.tmp が 1 件もない → False
      - llm_summary.md 不在 → True (初回生成)
      - max(mtime of *.tmp) > mtime(llm_summary.md) → True (新規 session あり)
      - それ以外 → False (要約済み)

    タイムスタンプは os.path.getmtime() で取得する機械的判定。
    listdir と getmtime の間にファイルが削除される TOCTOU に対応するため、
    各ファイルの getmtime を個別に try/except で囲む [CR-CC-002]。
    """
    sessions_dir = os.path.join(claude_dir, "memory", "sessions")
    if not os.path.isdir(sessions_dir):
        return False
    tmp_paths = [
        os.path.join(sessions_dir, f)
        for f in os.listdir(sessions_dir)
        if f.endswith(".tmp")
    ]
    if not tmp_paths:
        return False
    mtimes = []
    for p in tmp_paths:
        try:
            mtimes.append(os.path.getmtime(p))
        except OSError:
            continue
    if not mtimes:
        return False
    latest_session_mtime = max(mtimes)

    summary_path = os.path.join(claude_dir, "memory", "llm_summary.md")
    if not os.path.isfile(summary_path):
        return True
    return latest_session_mtime > os.path.getmtime(summary_path)


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

    Phase 3: Phase 1/2 完了後に「要約が必要か」を判定し、
    LLM 要約エージェントの起動指示を制御する。
    - flag あり                          → exit 0 + flag 削除 (実行中重複防止)
    - _needs_summary == True             → exit 2 + flag 作成 + stderr に Agent 起動指示
    - _needs_summary == False            → exit 0 (要約済み or session なし)
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
            try:
                with open(flag_path, encoding="utf-8") as flag_file:
                    flag_content = flag_file.read().strip()
            except OSError:
                # 読み取り失敗は保守的に「実行中」と解釈して重複起動を防ぐ
                flag_content = ""

            if flag_content == _FLAG_DONE_CONTENT:
                # エージェント完了済み → フラグを削除してから今回セッションの要否を判定。
                # TOCTOU 対策: os.unlink() がアトミックに成功した方だけ処理を続行する。
                # OSError（FileNotFoundError）= 別プロセスが先に削除 → 重複起動防止でスキップ。
                try:
                    os.unlink(flag_path)
                except OSError:
                    return 0
                if not _needs_summary(_CLAUDE_DIR):
                    return 0
                _create_flag(flag_path)
                print(_AGENT_INSTRUCTION, file=sys.stderr)
                return 2
            else:
                # フラグ空 = エージェント実行中 → 重複防止でスキップ
                return 0

        # 要約が必要か（session mtime vs llm_summary.md mtime 比較）
        if not _needs_summary(_CLAUDE_DIR):
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
