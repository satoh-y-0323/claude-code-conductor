#!/usr/bin/env python3
"""
Stop hook: session template creation and pattern trust score management.
Triggered at the end of each Claude Code session.
"""

import json
import sys
import os

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
import re
from datetime import date, datetime, timezone

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(_HOOKS_DIR)
SESSIONS_DIR = os.path.join(_CLAUDE_DIR, 'memory', 'sessions')
PATTERNS_FILE = os.path.join(_CLAUDE_DIR, 'memory', 'patterns.json')

EXPIRY_DAYS = 30
PROMOTION_THRESHOLD = 0.8
COOLING_DAYS = 3
SESSION_JSON_MARKER = 'C3:SESSION:JSON'


def is_worktree(cwd: str) -> bool:
    git_path = os.path.join(cwd, '.git')
    return os.path.exists(git_path) and os.path.isfile(git_path)


def get_session_path(yyyymmdd: str) -> str:
    return os.path.join(SESSIONS_DIR, f'{yyyymmdd}.tmp')


def create_session_template(yyyymmdd: str) -> str:
    return (
        f"SESSION: {yyyymmdd}\n"
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
        f'  "session": "{yyyymmdd}",\n'
        f'  "patterns": [],\n'
        f'  "successes": [],\n'
        f'  "failures": [],\n'
        f'  "todos": []\n'
        f"}}\n"
        f"-->\n"
    )


def ensure_session_file(yyyymmdd: str) -> None:
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    path = get_session_path(yyyymmdd)
    # wx フラグ相当: ファイルが存在しない場合のみ作成（TOCTOU安全）
    try:
        with open(path, 'x', encoding='utf-8') as f:
            f.write(create_session_template(yyyymmdd))
        print(f'[Stop] セッションファイルを作成しました: {path}', file=sys.stderr)
    except FileExistsError:
        _update_facts_timestamp(path)


def _update_facts_timestamp(path: str) -> None:
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    now = datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S')
    updated = re.sub(r'(- 記録時刻: ).*', rf'\g<1>{now}', content)
    if updated != content:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(updated)


def extract_session_patterns(yyyymmdd: str) -> list:
    path = get_session_path(yyyymmdd)
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


def _parse_session_date(yyyymmdd: str):
    try:
        return datetime.strptime(yyyymmdd, '%Y%m%d').date()
    except ValueError:
        return date.min


def count_sessions_since(registered_yyyymmdd: str) -> int:
    if not os.path.isdir(SESSIONS_DIR):
        return 1
    registered = _parse_session_date(registered_yyyymmdd)
    count = sum(
        1 for fname in os.listdir(SESSIONS_DIR)
        if fname.endswith('.tmp') and _parse_session_date(fname[:-4]) >= registered
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


def update_patterns(yyyymmdd: str) -> None:
    new_observations = extract_session_patterns(yyyymmdd)
    data = load_patterns()
    today = date.today()

    for obs in new_observations:
        pid = obs.get('id')
        if not pid:
            continue
        description = obs.get('description', '')
        existing = next((p for p in data['patterns'] if p['id'] == pid), None)
        if existing is None:
            data['patterns'].append({
                "id": pid,
                "description": description,
                "registered_date": yyyymmdd,
                "trust_score": 0.1,
                "promotion_candidate": False,
                "observations": [{"date": yyyymmdd}],
                "last_updated": yyyymmdd,
            })
        else:
            if not any(o['date'] == yyyymmdd for o in existing['observations']):
                existing['observations'].append({"date": yyyymmdd})
                existing['last_updated'] = yyyymmdd

    active = []
    for pattern in data['patterns']:
        if pattern.get('promoted', False):
            active.append(pattern)
            continue

        registered = _parse_session_date(pattern['registered_date'])
        days_elapsed = (today - registered).days

        if days_elapsed >= EXPIRY_DAYS:
            continue

        sessions_total = count_sessions_since(pattern['registered_date'])
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
        json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        pass

    cwd = os.getcwd()
    if is_worktree(cwd):
        sys.exit(0)

    today_str = date.today().strftime('%Y%m%d')
    ensure_session_file(today_str)
    update_patterns(today_str)


if __name__ == '__main__':
    main()
