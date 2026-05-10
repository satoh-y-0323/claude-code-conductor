#!/usr/bin/env python3
"""
Stop hook: session template creation and pattern trust score management.
Triggered at the end of each Claude Code session.
"""

import json
import sys
import os
import re
import tempfile
from datetime import date, datetime, timezone

from session_utils import SESSION_JSON_MARKER, is_worktree, create_session_template, SESSIONS_DIR, ensure_session_initialized

try:
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(_HOOKS_DIR)
PATTERNS_FILE = os.path.join(_CLAUDE_DIR, 'memory', 'patterns.json')

EXPIRY_DAYS = 30
PROMOTION_THRESHOLD = 0.8
COOLING_DAYS = 3
MAX_ID_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 500
MAX_LAST_MSG = 500

# _append_last_message が処理済みのパスを記録するキャッシュ（重複 read/write 防止）。
_last_message_applied_paths: set = set()


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
        # /exit による中断等でファイルが空の場合はテンプレートを書き直す（DRY: session_utils へ委譲）
        ensure_session_initialized(path, date_str)
        _update_facts_timestamp(path)


def _apply_session_updates(path: str, content: str, message: str = '') -> None:
    """タイムスタンプ更新と最終応答追記を1回のread/writeで処理する内部ヘルパー。

    Args:
        path: セッションファイルのパス
        content: 既に読み込み済みのファイル内容
        message: 追記する最終応答メッセージ（空文字の場合はタイムスタンプのみ更新）
    """
    # タイムスタンプを更新
    now = datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S')
    updated = re.sub(r'(- 記録時刻: ).*', rf'\g<1>{now}', content)

    # 最終応答を追記（メッセージがあり、まだ存在しない場合のみ）
    if message and '- 最終応答:' not in updated:
        single_line = ' '.join(message.split())
        # サロゲート文字など UTF-8 非互換文字を除去（JSON デコード時に生成される場合がある）
        single_line = single_line.encode('utf-8', errors='replace').decode('utf-8')
        truncated = single_line[:MAX_LAST_MSG]
        if len(single_line) > MAX_LAST_MSG:
            truncated += '…（省略）'
        # --> をサニタイズして <!-- C3:SESSION:JSON ... --> ブロックを保護する
        truncated = truncated.replace('-->', '-- >')

        updated = re.sub(
            r'(- 記録時刻: [^\n]*)',
            lambda m: m.group(0) + f'\n- 最終応答: {truncated}',
            updated
        )

    if updated != content:
        dir_ = os.path.dirname(path)
        # アトミック書き込み: 一時ファイルに書き込んでから os.replace() で置換する
        tmp_path = None
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix='.tmp')
            try:
                with os.fdopen(tmp_fd, 'w', encoding='utf-8') as tmp_f:
                    tmp_f.write(updated)
            except Exception:
                os.close(tmp_fd)
                raise
            os.replace(tmp_path, path)
            tmp_path = None  # os.replace が成功したので finally でのクリーンアップ不要
        finally:
            if tmp_path is not None and os.path.exists(tmp_path):
                os.unlink(tmp_path)


def _append_last_message(path: str, message: str) -> None:
    """セッションファイルに最終応答を追記し、記録時刻を更新する（1回のread/writeで処理）。"""
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    _apply_session_updates(path, content, message)
    # 処理済みパスとして記録し、_update_facts_timestamp の重複処理を防ぐ
    _last_message_applied_paths.add(os.path.abspath(path))


def _update_facts_timestamp(path: str) -> None:
    """記録時刻を更新する。_append_last_message が同じパスに対して実行済みの場合は
    タイムスタンプ更新が完了しているためスキップする（重複read/writeを防ぐ）。"""
    abs_path = os.path.abspath(path)
    if abs_path in _last_message_applied_paths:
        # _append_last_message がタイムスタンプ更新済みのためスキップ
        return
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    _apply_session_updates(path, content)


def extract_session_patterns(date_str: str) -> list:
    path = get_session_path(date_str)
    if not os.path.exists(path):
        return []
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    # --\s*> は --> と '-- >' の両方にマッチさせる。
    # append_checkpoint がサニタイズで --> を '-- >' に変換するため、両形式を許容する必要がある。
    match = re.search(rf'<!-- {SESSION_JSON_MARKER}\s*(.*?)--\s*>', content, re.DOTALL)
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


def _build_sessions_by_date(sessions_dir: str) -> set[str]:
    """Build a set of yyyymmdd strings from the sessions directory.

    Returns a set of date strings (yyyymmdd) for .tmp files found in sessions_dir.
    Builds the result once so repeated calls to os.listdir are avoided.
    """
    if not os.path.isdir(sessions_dir):
        return set()
    return {fname[:-4] for fname in os.listdir(sessions_dir) if fname.endswith('.tmp')}


def count_sessions_since(registered_date_str: str, sessions_by_date: set[str] | None = None) -> int:
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
            try:
                return json.load(f)
            except json.JSONDecodeError as e:
                print(f'[Stop] patterns.json の JSON 解析に失敗しました（空データで継続）: {e}',
                      file=sys.stderr)
                return {"patterns": []}
    return {"patterns": []}


def save_patterns(data: dict) -> None:
    patterns_dir = os.path.dirname(PATTERNS_FILE)
    os.makedirs(patterns_dir, exist_ok=True)
    # アトミック書き込み: 一時ファイルに書き込んでから os.replace() で置換する
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=patterns_dir, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as tmp_f:
                json.dump(data, tmp_f, ensure_ascii=False, indent=2)
        except Exception:
            os.close(fd)
            raise
        os.replace(tmp_path, PATTERNS_FILE)
        tmp_path = None  # os.replace が成功したので finally でのクリーンアップ不要
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            os.unlink(tmp_path)


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

    print('[Stop] セッション終了処理が完了しました', file=sys.stderr)


def run(payload: dict) -> int:
    """payload を引数で受け取る本体処理。session_stop.py orchestrator からも呼ばれる.

    Args:
        payload: Stop hook の stdin payload（dict 化済み）。

    Returns:
        常に 0（失敗してもセッションを止めない方針）。
    """
    # stop_hook_active=true は Stop hook が decision:block を返した後の 2 回目呼び出し。
    # セッション処理は初回のみ実行する。
    if payload.get('stop_hook_active'):
        return 0

    cwd = os.getcwd()
    if is_worktree(cwd):
        return 0

    today_str = date.today().strftime('%Y%m%d')
    ensure_session_file(today_str)

    last_msg = payload.get('last_assistant_message', '').strip()
    if last_msg:
        _append_last_message(get_session_path(today_str), last_msg)

    update_patterns(today_str)
    return 0


def main():
    """単独実行時のエントリポイント（後方互換）.

    stdin を 1 回読んで run() に渡す。session_stop.py orchestrator 経由では
    呼ばれない（payload は orchestrator が読む）。
    """
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        payload = {}
    sys.exit(run(payload))


if __name__ == '__main__':
    main()
