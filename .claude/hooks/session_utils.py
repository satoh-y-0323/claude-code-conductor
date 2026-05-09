#!/usr/bin/env python3
"""Shared utilities for session management hooks (stop.py, pre_compact.py)."""

import os
import re
from datetime import datetime, timezone

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(_HOOKS_DIR)
SESSIONS_DIR = os.path.join(_CLAUDE_DIR, 'memory', 'sessions')

SESSION_JSON_MARKER = 'C3:SESSION:JSON'


def is_worktree(cwd: str) -> bool:
    git_path = os.path.join(cwd, '.git')
    return os.path.exists(git_path) and os.path.isfile(git_path)


def create_session_template(date_str: str) -> str:
    return (
        f"SESSION: {date_str}\n"
        f"TASK_TYPE: \n"
        f"AGENT: \n"
        f"DURATION: \n"
        f"\n"
        f"## うまくいったアプローチ\n"
        f"\n"
        f"## 試みたが失敗したアプローチ\n"
        f"\n"
        f"## 残タスク\n"
        f"\n"
        f"## 事実ログ（自動生成 / stop.py）\n"
        f"- 記録時刻: \n"
        f"\n"
        f"<!-- {SESSION_JSON_MARKER}\n"
        f"{{\n"
        f'  "session": "{date_str}",\n'
        f'  "patterns": [],\n'
        f'  "successes": [],\n'
        f'  "failures": [],\n'
        f'  "todos": []\n'
        f"}}\n"
        f"-- >\n"
    )


def ensure_session_initialized(path: str, date_str: str) -> None:
    """空のセッションファイルをテンプレートで再初期化する共有ヘルパー。

    FileExistsError ブランチで使用: ファイルが空の場合のみテンプレートを書き直す。
    stop.py::ensure_session_file と append_checkpoint の両方から呼ばれる。
    """
    # 単一プロセス前提: getsize と open('w') の間の TOCTOU は許容範囲（並列実行なし）
    if os.path.getsize(path) == 0:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(create_session_template(date_str))


def extract_section(content: str, heading: str) -> str:
    """セッションファイル本文から ``## {heading}`` セクションの内容を抽出する。

    F-004（MemoryConsolidation）と restore_session.py で共通利用される。
    ``## {heading}\\n`` から次の ``\\n## `` または ``\\n<!--`` または末尾までを返す。
    見つからない場合は空文字列を返す。

    Args:
        content: セッションファイル全体のテキスト。
        heading: 抽出したいセクションの見出し（``## `` の後の文字列）。
            例: ``"うまくいったアプローチ"``。

    Returns:
        セクション本文（前後の空白除去済み）、または空文字列。

    Notes:
        restore_session.py には独自実装の同名関数があり、後方互換性のため
        当面そのまま残す。新規コード（consolidate_memory.py 等）は本関数を使う。
    """
    pattern = rf'## {re.escape(heading)}\n(.*?)(?=\n## |\n<!--|\Z)'
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        return ''
    return match.group(1).strip()


def append_checkpoint(session_file: str, label: str, summary: str) -> None:
    """Append a checkpoint block to the session file.

    Used by wave-execution (milestone snapshots) and pre_compact.py
    (compaction markers). Checkpoint blocks are append-only — they record
    the state at a point in time and never overwrite earlier entries.

    Args:
        session_file: Absolute path to the session file (.tmp).
        label: Short identifier shown in the heading
            (e.g. "Wave 2 success", "PreCompact: manual").
        summary: Multi-line Markdown body describing the state.
    """
    os.makedirs(os.path.dirname(session_file), exist_ok=True)

    date_str = os.path.splitext(os.path.basename(session_file))[0]
    try:
        with open(session_file, 'x', encoding='utf-8') as f:
            f.write(create_session_template(date_str))
    except FileExistsError:
        ensure_session_initialized(session_file, date_str)

    ts = datetime.now(timezone.utc).isoformat()
    # --> を '-- >' に置換して <!-- C3:SESSION:JSON ... --> ブロックの破壊を防ぐ
    body = summary.strip().replace('-->', '-- >')
    # label のサニタイズ（ターミナルインジェクション対策 + HTML コメントブロック保護）:
    # - \x00-\x08, \x0b-\x0c, \x0e-\x1f: 制御文字を除去（\n=\x0a と \t=\x09 は保持）
    # - --> を '-- >' に置換して <!-- C3:SESSION:JSON ... --> ブロックの破壊を防ぐ
    sanitized_label = re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f]', '', label)
    sanitized_label = sanitized_label.replace('-->', '-- >')
    block = (
        f"\n"
        f"## [Checkpoint: {sanitized_label} - {ts}]\n"
        f"{body}\n"
    )

    with open(session_file, 'a', encoding='utf-8') as f:
        f.write(block)
