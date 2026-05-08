#!/usr/bin/env python3
"""SubagentStart / SubagentStop hook: log agent execution events.

C3 開発版専用。配布版（_template/）には含めない（hatch_build.py の
EXCLUDE_PATTERNS で除外）。settings.local.json の hooks セクションから
呼ばれる。

入力 JSON 全体を `.claude/logs/agent-runs.jsonl` に追記する。
SubagentStop 時には同 session_id + agent_id の最古未消費 Start を
検索して duration_seconds を算出する。

公式仕様で SubagentStart / SubagentStop の入力 JSON に agent_id が
含まれることが確認されたため、agent_id ベースのペアリングに移行した。
"""

import json
import os
import sys
from datetime import datetime, timezone

try:
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(_HOOKS_DIR)
LOG_DIR = os.path.join(_CLAUDE_DIR, 'logs')
LOG_FILE = os.path.join(LOG_DIR, 'agent-runs.jsonl')


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def _read_log_records() -> list:
    if not os.path.exists(LOG_FILE):
        return []
    out = []
    with open(LOG_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _find_unmatched_start(records: list, session_id: str, agent_id: str):
    """同 session_id + agent_id の対応 SubagentStop が無い最古 SubagentStart を返す。

    agent_id ベースでペアリングするため、同 session 内の並列エージェントが
    互いに誤ペアリングすることはない。
    公式仕様で agent_id が SubagentStart / SubagentStop のペイロードに
    含まれることが確認されている。
    """
    pending_starts = []
    for r in records:
        p = r.get('payload', {})
        if p.get('session_id') != session_id or p.get('agent_id') != agent_id:
            continue
        event_name = p.get('hook_event_name', '')
        if event_name == 'SubagentStart':
            pending_starts.append(r)
        elif event_name == 'SubagentStop':
            if pending_starts:
                pending_starts.pop(0)
    return pending_starts[0] if pending_starts else None


def _calc_duration_seconds(start_ts: str, end_ts: str):
    try:
        start_dt = datetime.fromisoformat(start_ts)
        end_dt = datetime.fromisoformat(end_ts)
        return round((end_dt - start_dt).total_seconds(), 3)
    except (ValueError, TypeError):
        return None


def _append_log(record: dict) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        payload = {}

    event_name = payload.get('hook_event_name', '')
    session_id = payload.get('session_id', '')
    now_ts = _now_iso()

    record = {
        'ts': now_ts,
        'payload': payload,
    }

    if event_name == 'SubagentStop':
        agent_id = payload.get('agent_id', '')
        if agent_id:
            records = _read_log_records()
            start = _find_unmatched_start(records, session_id, agent_id)
            if start is not None:
                duration = _calc_duration_seconds(start.get('ts', ''), now_ts)
                if duration is not None:
                    record['duration_seconds'] = duration
                    record['matched_start_ts'] = start.get('ts')

    try:
        _append_log(record)
    except OSError as e:
        print(f'[subagent_log] ログ追記に失敗しました: {e}', file=sys.stderr)
        return 0

    return 0


if __name__ == '__main__':
    sys.exit(main())
