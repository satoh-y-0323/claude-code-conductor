#!/usr/bin/env python3
"""SubagentStart / SubagentStop hook: log agent execution events.

C3 開発版専用。配布版（_template/）には含めない（hatch_build.py の
EXCLUDE_PATTERNS で除外）。settings.local.json の hooks セクションから
呼ばれる。

stdin から受け取った JSON のうちホワイトリスト対象フィールドのみを
`.claude/logs/agent-runs.jsonl` に追記する。
SubagentStop 時には同 session_id + agent_id の最古未消費 Start を
検索して duration_seconds を算出する。

公式仕様で SubagentStart / SubagentStop の入力 JSON に agent_id が
含まれることが確認されたため、agent_id ベースのペアリングに移行した。

既知の制限（TOCTOU）:
  複数プロセスが同時にログファイルに書き込む場合、_find_unmatched_start が
  読み取った直後に別プロセスが同じ Start を消費する可能性がある（TOCTOU）。
  本スクリプトは C3 開発版・ローカルファイル前提のため、この制限は許容する。
  ロック機構は実装しない。
"""

import collections
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

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

# stdin の最大読み込みバイト数 (sec-M-2)
_MAX_STDIN_BYTES = 1 * 1024 * 1024  # 1 MB

# ログ走査の最大行数 (sec-M-2)
_MAX_SCAN_LINES = 10_000

# イベント名定数 (code-L-1)
_EVENT_START = "SubagentStart"
_EVENT_STOP = "SubagentStop"

# po-sqlite Phase 2-B: SubagentStop の status 値の正常終了マーカー（仕様変更時の単一窓口）。
_STATUS_SUCCESS = "success"

# po-sqlite Phase 2-B: po_status.current_step の最大文字数 [SR-V-001]。
# payload.agent_type / agent_id は任意文字列のため DB 容量保護のため切り詰める。
_MAX_CURRENT_STEP_LEN = 200

# payload のホワイトリスト対象フィールド (sec-M-1)
# subagent-metrics: total_tokens / status / token_usage / model を追加。
# Tier 自動ルーティング (tier-routing) の学習データ収集の前提となる。
# result 系（応答本文・コード断片混入リスク）は意図的に除外。
_SAFE_PAYLOAD_FIELDS = frozenset({
    'hook_event_name',
    'session_id',
    'agent_id',
    'agent_type',
    'cwd',
    'transcript_path',
    'stop_hook_active',
    'permission_mode',
    'total_tokens',
    'status',
    'token_usage',
    'model',
})

# U+2028 (LINE SEPARATOR) / U+2029 (PARAGRAPH SEPARATOR) の定数 (sec-H-1)
# ensure_ascii=False の json.dumps はこれらをエスケープしないため、
# _append_log で明示的に \\u2028 / \\u2029 へ置換する。
_U2028 = ' '
_U2029 = ' '


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def _sanitize_payload(payload: dict) -> dict:
    """ホワイトリスト対象フィールドのみを抽出して返す (sec-M-1)。

    last_assistant_message / agent_transcript_path 等の長文・任意コンテンツ系を除外し、
    デバッグに必要な cwd / transcript_path は保持する。
    """
    return {k: v for k, v in payload.items() if k in _SAFE_PAYLOAD_FIELDS}


def _read_log_records() -> list[dict]:
    """LOG_FILE を読み込んで有効な JSONL レコードのリストを返す。

    末尾 _MAX_SCAN_LINES 行のみ走査してメモリ使用量を抑える (sec-M-2)。
    JSON パース失敗行はスキップする。
    """
    if not os.path.exists(LOG_FILE):
        return []
    records = []
    with open(LOG_FILE, 'r', encoding='utf-8') as f:
        for line in collections.deque(f, maxlen=_MAX_SCAN_LINES):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _find_unmatched_start(
    records: list[dict], session_id: str, agent_id: str
) -> dict | None:
    """同 session_id + agent_id の対応 SubagentStop が無い最古 SubagentStart を返す。

    agent_id ベースでペアリングするため、同 session 内の並列エージェントが
    互いに誤ペアリングすることはない。
    公式仕様で agent_id が SubagentStart / SubagentStop のペイロードに
    含まれることが確認されている。

    既知の制限（TOCTOU）:
      本関数は読み取り後に別プロセスが同 Start を消費する可能性があるが、
      C3 開発版・ローカルファイル前提のため許容する。
    """
    pending_starts: collections.deque[dict] = collections.deque()
    for r in records:
        p = r.get('payload', {})
        if p.get('session_id') != session_id or p.get('agent_id') != agent_id:
            continue
        event_name = p.get('hook_event_name', '')
        if event_name == _EVENT_START:
            pending_starts.append(r)
        elif event_name == _EVENT_STOP:
            if pending_starts:
                pending_starts.popleft()
    return pending_starts[0] if pending_starts else None


def _calc_duration_seconds(start_ts: str, end_ts: str) -> float | None:
    """ISO8601 文字列の差分を秒数（小数点3桁）で返す。パース失敗時は None を返す。"""
    try:
        start_dt = datetime.fromisoformat(start_ts)
        end_dt = datetime.fromisoformat(end_ts)
        return round((end_dt - start_dt).total_seconds(), 3)
    except (ValueError, TypeError):
        return None


def _append_log(record: dict) -> None:
    """record を JSONL 形式で LOG_FILE に追記する。

    ensure_ascii=False で json.dumps した後、_U2028 / _U2029 を
    \\u2028 / \\u2029 に明示置換する (sec-H-1)。json.dumps は
    ensure_ascii=False 時にこれらをエスケープしないため、
    JSONL の行区切りが壊れる恐れがある。
    O_APPEND でファイルシステムレベルの追記アトミック性を確保する (sec-L-1)。
    """
    os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)
    try:
        line = json.dumps(record, ensure_ascii=False)
        line = line.replace(_U2028, '\\u2028').replace(_U2029, '\\u2029')
        fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception as e:
        print(f'[subagent_log] ログ追記に失敗しました: {e}', file=sys.stderr)


def main() -> int:
    """stdin から JSON を読み込み、サニタイズして LOG_FILE に追記する。

    SubagentStop イベントの場合は同 session_id + agent_id の最古未消費 Start を
    検索して duration_seconds / matched_start_ts を付加する。
    stdin の IOError・JSON パースエラーを含む全例外を catch して 0 を返す。
    """
    try:
        raw = sys.stdin.read(_MAX_STDIN_BYTES + 1)
        if len(raw) > _MAX_STDIN_BYTES:
            print(
                f'[subagent_log] stdin が上限 ({_MAX_STDIN_BYTES} bytes) を超えています。'
                'record を書き込まずに終了します。',
                file=sys.stderr,
            )
            return 0
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f'[subagent_log] stdin の JSON パースに失敗しました: {e}', file=sys.stderr)
        return 0
    except Exception as e:
        print(f'[subagent_log] stdin の読み込みに失敗しました: {e}', file=sys.stderr)
        return 0

    event_name = payload.get('hook_event_name', '')
    session_id = payload.get('session_id', '')
    now_ts = _now_iso()

    record = {
        'ts': now_ts,
        'payload': _sanitize_payload(payload),
    }

    if event_name == _EVENT_STOP:
        agent_id = payload.get('agent_id', '')
        if agent_id:
            records = _read_log_records()
            start = _find_unmatched_start(records, session_id, agent_id)
            if start is not None:
                duration = _calc_duration_seconds(start.get('ts', ''), now_ts)
                if duration is not None:
                    record['duration_seconds'] = duration
                    record['matched_start_ts'] = start.get('ts')

    _append_log(record)

    return 0


if __name__ == '__main__':
    sys.exit(main())
