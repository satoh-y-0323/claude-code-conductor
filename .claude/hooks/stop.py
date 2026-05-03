#!/usr/bin/env python3
"""
Stop hook: session template creation and pattern trust score management.
Triggered at the end of each Claude Code session.
"""

import json
import sys
import os
import re
from datetime import date, datetime, timezone

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(_HOOKS_DIR)
PATTERNS_FILE = os.path.join(_CLAUDE_DIR, 'memory', 'patterns.json')

from session_utils import SESSION_JSON_MARKER, is_worktree, create_session_template, SESSIONS_DIR

EXPIRY_DAYS = 30
PROMOTION_THRESHOLD = 0.8
COOLING_DAYS = 3
MAX_ID_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 500
MAX_LAST_MSG = 500


def get_session_path(date_str: str) -> str:
    return os.path.join(SESSIONS_DIR, f'{date_str}.tmp')


def ensure_session_file(date_str: str) -> None:
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    path = get_session_path(date_str)
    # wx フラグ相当: ファイルが存在しない場合のみ作成（TOCTOU安全）
    try:
        with open(path, 'x', encoding='utf-8') as f:
            f.write(create_session_template(date_str))
        print(f'[Stop] セッションファイルを作成しました: {path}', file=sys.stderr)
    except FileExistsError:
        _update_facts_timestamp(path)


def _append_last_message(path: str, message: str) -> None:
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    if '- 最終応答:' in content:
        return

    single_line = ' '.join(message.split())
    truncated = single_line[:MAX_LAST_MSG]
    if len(single_line) > MAX_LAST_MSG:
        truncated += '…（省略）'

    updated = re.sub(
        r'(- 記録時刻: [^\n]*)',
        lambda m: m.group(0) + f'\n- 最終応答: {truncated}',
        content
    )
    if updated != content:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(updated)


def _update_facts_timestamp(path: str) -> None:
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    now = datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S')
    updated = re.sub(r'(- 記録時刻: ).*', rf'\g<1>{now}', content)
    if updated != content:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(updated)


def extract_session_patterns(date_str: str) -> list:
    path = get_session_path(date_str)
    if not os.path.exists(path):
        return []
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    match = re.search(rf'<!-- {SESSION_JSON_MARKER}\s*(.*?)-->', content, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(1).strip())
        return data.get('patterns', [])
    except json.JSONDecodeError:
        return []


def _parse_session_date(date_str: str):
    try:
        return datetime.strptime(date_str, '%Y%m%d').date()
    except ValueError:
        return date.min


def _build_sessions_by_date(sessions_dir: str) -> dict:
    """Build a mapping of date string -> session count from sessions directory.

    Returns a dict mapping each yyyymmdd string found in sessions_dir to 1,
    enabling O(1) lookup without repeated os.listdir calls.
    """
    if not os.path.isdir(sessions_dir):
        return {}
    result = {}
    for fname in os.listdir(sessions_dir):
        if fname.endswith('.tmp'):
            result[fname[:-4]] = True
    return result


def count_sessions_since(registered_date_str: str, sessions_by_date: dict | None = None) -> int:
    if sessions_by_date is None:
        if not os.path.isdir(SESSIONS_DIR):
            return 1
        sessions_by_date = _build_sessions_by_date(SESSIONS_DIR)
    registered = _parse_session_date(registered_date_str)
    count = sum(
        1 for d in sessions_by_date
        if _parse_session_date(d) >= registered
    )
    return max(count, 1)


def load_patterns() -> dict:
    if os.path.exists(PATTERNS_FILE):
        with open(PATTERNS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"patterns": []}


def save_patterns(data: dict) -> None:
    os.makedirs(os.path.dirname(PATTERNS_FILE), exist_ok=True)
    with open(PATTERNS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def update_patterns(date_str: str) -> None:
    new_observations = extract_session_patterns(date_str)
    data = load_patterns()
    today = date.today()

    for obs in new_observations:
        pid = obs.get('id')
        if not pid or len(pid) > MAX_ID_LENGTH:
            continue
        description = obs.get('description', '')
        if len(description) > MAX_DESCRIPTION_LENGTH:
            continue
        existing = next((p for p in data['patterns'] if p['id'] == pid), None)
        if existing is None:
            data['patterns'].append({
                "id": pid,
                "description": description,
                "registered_date": date_str,
                "trust_score": 0.1,
                "promotion_candidate": False,
                "observations": [{"date": date_str}],
                "last_updated": date_str,
            })
        else:
            if not any(o['date'] == date_str for o in existing['observations']):
                existing['observations'].append({"date": date_str})
                existing['last_updated'] = date_str

    # Cache os.listdir result once before the loop to avoid O(N×M) calls
    sessions_by_date = _build_sessions_by_date(SESSIONS_DIR)

    active = []
    for pattern in data['patterns']:
        if pattern.get('promoted', False):
            active.append(pattern)
            continue

        registered = _parse_session_date(pattern['registered_date'])
        days_elapsed = (today - registered).days

        if days_elapsed >= EXPIRY_DAYS:
            continue

        sessions_total = count_sessions_since(pattern['registered_date'], sessions_by_date)
        obs_count = len(pattern['observations'])
        trust = round(min(1.0, max(0.1, obs_count / sessions_total)), 2)

        pattern['trust_score'] = trust
        pattern['promotion_candidate'] = (
            days_elapsed >= COOLING_DAYS and trust >= PROMOTION_THRESHOLD
        )
        active.append(pattern)

    data['patterns'] = active
    save_patterns(data)

    print(f'[Stop] セッション終了処理が完了しました', file=sys.stderr)


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        payload = {}

    # stop_hook_active=true は Stop hook が decision:block を返した後の 2 回目呼び出し。
    # セッション処理は初回のみ実行する。
    if payload.get('stop_hook_active'):
        sys.exit(0)

    cwd = os.getcwd()
    if is_worktree(cwd):
        sys.exit(0)

    today_str = date.today().strftime('%Y%m%d')
    ensure_session_file(today_str)

    last_msg = payload.get('last_assistant_message', '').strip()
    if last_msg:
        _append_last_message(get_session_path(today_str), last_msg)

    update_patterns(today_str)


if __name__ == '__main__':
    main()
