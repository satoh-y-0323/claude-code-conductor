#!/usr/bin/env python3
"""
Stop hook: session template creation and pattern trust score management.
Triggered at the end of each Claude Code session.
"""

import json
import sys
import os
import re
import stat
import tempfile
from datetime import date, datetime, timezone

from session_utils import SESSION_JSON_MARKER, is_worktree, create_session_template, SESSIONS_DIR, ensure_session_initialized, extract_section

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
# 取り込みガード(500)とは別レイヤの既存エントリ肥大検知線。直接 Edit 経路で
# patterns.json に載った肥大 description を毎 Stop で警告するための閾値（strict greater）。
DESCRIPTION_WARN_LENGTH = 1000
MAX_LAST_MSG = 500

# サブエージェントの MEMORY.md は、ハーネスが起動時に「先頭 200 行 / 25KB」までしか
# system prompt へ載せない（公式 sub-agents.md「Enable persistent memory」）。
# 超過分は読まれないため、過去にユーザーと合意した許容例外が injection window 外へ落ち、
# 次回起動時にその合意を知らないまま再指摘する（＝合意の実効的な巻き戻り）。
# ハーネスの truncation は「読まれる範囲」の cutoff であって「書く量」の抑制圧力ではないため、
# 超過後ではなく予算の 80% 到達時点で警告する（超過時点では既に巻き戻りが起きている）。
AGENT_MEMORY_DIR = os.path.join(_CLAUDE_DIR, 'agent-memory')
AGENT_MEMORY_LIMIT_BYTES = 25 * 1024
AGENT_MEMORY_LIMIT_LINES = 200
AGENT_MEMORY_WARN_RATIO = 0.8
# 行数カウントのための読み込み上限。サイズ自体は getsize() で正確に取れるため、
# 全体を read() する必要はない。予算の 2 倍を超えたファイルは（上限を超えた時点で
# 必ず警告条件を満たすので）行数を下限値として報告する [SR-NEW]。
AGENT_MEMORY_READ_CAP_BYTES = AGENT_MEMORY_LIMIT_BYTES * 2

# 個別メモリファイル（agent-memory/<agent>/ 配下・MEMORY.md 以外の *.md。
# topics/ 等のサブディレクトリを含め再帰走査の対象）向けの閾値。
# 索引 MEMORY.md と異なり起動時に system prompt へ自動注入されないため、
# injection 予算（25KB/200行の 80%）はそのまま適用できない。個別ファイルは
# 必要時に Read されるだけなので、制約は「1 回の Read で払うトークン・保守コスト」
# であり注入予算とは性質が違う。実測分布は 27〜50KB が正規運用範囲、200KB 超は
# 外れ値だったため、その中間の区切りとして 100KB を採用する
# （strict greater・超過のみ警告）[S3 ⑦]。
AGENT_MEMORY_FILE_LIMIT_BYTES = 100 * 1024

# 過去セッションファイルから引き継ぐ - [ ] 行のサニタイズ用パターン。
# C0/C1 制御文字 (タブ 	 と通常スペース   は保持) と U+2028 / U+2029 を除去する。
# 過去ファイルの ## 残タスク はユーザー編集領域のため信頼境界として扱う [SR-V-001]。
# raw string は \uXXXX を解釈しないため、U+2028 / U+2029 は chr() で生成して連結する。
_INHERIT_SANITIZE_RE = re.compile(
    r'[\x00-\x08\x0b-\x1f\x7f-\x9f' + chr(0x2028) + chr(0x2029) + r']'
)

# stderr へ表示する外部由来の値（agent-memory のディレクトリ名・patterns.json の id 等）の
# 無害化用。_INHERIT_SANITIZE_RE はセッションファイルの「行単位」処理向けで LF が来ない前提の
# ため \x0a を残すが、表示経路では改行が入ると「1 件 1 行」の出力契約が崩れ、後続テキストを
# 独立した警告行に見せかけられる [SR-V-001]。表示用は C0 全域（TAB 含む）・C1 に加え、
# 双方向制御文字とゼロ幅文字（表示偽装）も除去する。
_DISPLAY_SANITIZE_RE = re.compile(
    r'[\x00-\x1f\x7f-\x9f'
    + chr(0x200b) + '-' + chr(0x200f)
    + chr(0x2028) + chr(0x2029)
    + chr(0x202a) + '-' + chr(0x202e)
    + chr(0x2066) + '-' + chr(0x2069)
    + chr(0xfeff)
    + r']'
)

# _append_last_message が処理済みのパスを記録するキャッシュ（重複 read/write 防止）。
_last_message_applied_paths: set = set()


def get_session_path(date_str: str) -> str:
    return os.path.join(SESSIONS_DIR, f'{date_str}.tmp')


def _inherit_backlog_from_latest_session(
    new_path: str, today_str: str, sessions_dir: str | None = None
) -> None:
    """新規セッションファイルに、直近過去セッションの未完了バックログを引き継ぐ。

    過去ファイルの ``## 残タスク`` セクションから ``- [ ]`` 行のみ抽出し、
    新規ファイルの ``## 残タスク`` セクションに追記する。``- [x]`` 行は対象外。
    マルチライン継続行（インデントされた `- [ ]` 以外の継続行）は引き継がない。

    本ヘルパーは `ensure_session_file` の新規作成パスからのみ呼ばれる。既存当日
    ファイルが存在する場合は呼ばれないため、ユーザー編集を上書きする危険はない。

    過去ファイルから引き継ぐ行は `_INHERIT_SANITIZE_RE` で制御文字・ANSI エスケープ・
    U+2028/U+2029 を除去してから書き込むため、過去ファイルの改ざんによる端末
    インジェクションは構造的に防御される [SR-V-001]。

    Args:
        new_path: 新規作成された当日セッションファイルのパス。
        today_str: 今日の日付（YYYYMMDD）。これ未満の日付の .tmp が対象。
        sessions_dir: SESSIONS_DIR を上書きしたい場合のテスト用引数。
            None の場合はモジュールグローバル SESSIONS_DIR を使う。
    """
    _sessions_dir = sessions_dir if sessions_dir is not None else SESSIONS_DIR
    if not os.path.isdir(_sessions_dir):
        return

    # 今日より前で最大の日付（= 直近過去セッション）を選ぶ
    past_dates = [
        fname[:-4] for fname in os.listdir(_sessions_dir)
        if fname.endswith('.tmp') and fname[:-4] < today_str
    ]
    if not past_dates:
        return
    latest_past_path = os.path.join(_sessions_dir, f'{max(past_dates)}.tmp')

    try:
        # universal newlines（デフォルト）で読み込む。攻撃により \r 単体が混入しても
        # この時点で \n に変換されるため、\r 単体は構造的に防御される。
        # ただし Python の str.splitlines() は \n に加え \v / \f / \x1c / \x1d / \x1e /
        # \x85 / U+2028 / U+2029 でも分割する。これらは _INHERIT_SANITIZE_RE で
        # 事前に除去してから splitlines に渡す必要がある。
        with open(latest_past_path, 'r', encoding='utf-8') as f:
            past_content = f.read()
    except OSError:
        return

    # U+2028/U+2029 等の行区切り文字を事前にサニタイズしてから splitlines するため、
    # 過去ファイルに混入した行区切り文字によって意図しない行分割が発生しない。
    backlog_section = extract_section(past_content, '残タスク')
    sanitized_section = _INHERIT_SANITIZE_RE.sub('', backlog_section)
    pending_tasks = [
        line for line in sanitized_section.splitlines()
        if line.lstrip().startswith('- [ ]')
    ]
    if not pending_tasks:
        return

    try:
        with open(new_path, 'r', encoding='utf-8') as f:
            new_content = f.read()
    except OSError:
        return

    inheritance_block = '\n'.join(pending_tasks) + '\n'  # nul-boundary: allow(セッション .tmp の残タスクセクションへ書く行。利用先に旧形式が既に存在する永続形式で後方互換が要る)
    updated = new_content.replace(
        '## 残タスク\n',
        f'## 残タスク\n{inheritance_block}',
        1,
    )

    if updated == new_content:
        return

    # アトミック書き込み: _apply_session_updates と同じ tempfile + os.replace パターン。
    # suffix='.writing' は SESSIONS_DIR 内の `.tmp` 一覧フィルタに引っかからないよう
    # 構造的に隔離する [SR-NEW L-1]。
    dir_ = os.path.dirname(new_path)
    tmp_path = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix='.writing')
        try:
            with os.fdopen(tmp_fd, 'w', encoding='utf-8') as tmp_f:
                tmp_f.write(updated)
        except Exception:
            os.close(tmp_fd)
            raise
        os.replace(tmp_path, new_path)
        tmp_path = None
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def ensure_session_file(date_str: str) -> None:
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    path = get_session_path(date_str)
    # wx フラグ相当: ファイルが存在しない場合のみ作成（TOCTOU安全）
    try:
        with open(path, 'x', encoding='utf-8') as f:
            f.write(create_session_template(date_str))
        # 新規作成時のみ、直近過去セッションから未完了タスクを引き継ぐ。
        # 既存ファイルがある場合 (FileExistsError ブランチ) はユーザー編集を尊重し
        # 引き継ぎを発動しない。sessions_dir を明示渡しにすることで、引き継ぎ関数内
        # の SESSIONS_DIR 直参照を排除し、テスト時のグローバル差し替え依存を減らす。
        _inherit_backlog_from_latest_session(path, date_str, sessions_dir=SESSIONS_DIR)
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

    # 最終応答を最新メッセージに更新する（既存があれば上書き、なければ追記）。
    # 同一 stop hook 呼び出し内の冪等性は session_stop.py が stop.run を 1 回だけ呼ぶ
    # + stop_hook_active 早期 return (run() 内) で担保されているため、過去セッションの
    # 古い応答は積極的に上書きしてよい。古い `not in updated` ガードは「最初の応答が
    # 一日中残る」問題を引き起こしていた。
    if message:
        single_line = ' '.join(message.split())  # nul-boundary: allow(最終応答を単一行へ平坦化する空白正規化。そもそも行集合ではない)
        # サロゲート文字など UTF-8 非互換文字を除去（JSON デコード時に生成される場合がある）
        single_line = single_line.encode('utf-8', errors='replace').decode('utf-8')
        truncated = single_line[:MAX_LAST_MSG]
        if len(single_line) > MAX_LAST_MSG:
            truncated += '…（省略）'
        # --> をサニタイズして <!-- C3:SESSION:JSON ... --> ブロックを保護する
        truncated = truncated.replace('-->', '-- >')

        # 置換文字列に truncated（LLM 出力由来）を直接埋め込むと \1 等が後方参照として
        # 解釈される。両分岐とも lambda で構造的に防御する [SR-V-001 Info-1]。
        if '- 最終応答:' in updated:
            replacement = f'- 最終応答: {truncated}'
            updated = re.sub(
                r'- 最終応答: [^\n]*',
                lambda _: replacement,
                updated,
                count=1,
            )
        else:
            updated = re.sub(
                r'(- 記録時刻: [^\n]*)',
                lambda m: m.group(0) + f'\n- 最終応答: {truncated}',
                updated,
                count=1,
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
    """Parse a yyyymmdd date string and return a date object, or None if invalid.

    Returns None (rather than a sentinel like date.min) so callers can explicitly
    filter out unparseable entries instead of silently treating them as very old.
    """
    try:
        return datetime.strptime(date_str, '%Y%m%d').date()
    except ValueError:
        return None


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
    # If registered_date_str itself is unparseable, we cannot determine a baseline;
    # fall back to counting all sessions rather than silently returning a wrong value.
    if registered is None:
        return max(len(sessions_by_date), 1)
    count = sum(
        1 for d in sessions_by_date
        # Skip session entries whose date string is malformed (parsed as None).
        if (parsed := _parse_session_date(d)) is not None and parsed >= registered
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


def _warn_oversized_descriptions(patterns: list) -> None:
    """既存エントリの description が DESCRIPTION_WARN_LENGTH を超えていたら stderr 警告する。

    削除・切り詰めは行わない（警告のみ・read-only）。1 エントリ 1 行で出力する。
    本関数は例外を出さない防御的実装だが、呼び出し側でも try/except で二重に保護する。
    """
    for p in patterns:
        if not isinstance(p, dict):
            continue
        desc = p.get('description', '')
        if not isinstance(desc, str):
            continue
        if len(desc) > DESCRIPTION_WARN_LENGTH:
            pid = p.get('id', '(unknown)')
            # [SR-V-001] pid は直接 Edit 経路の値で未検証のため、出力前に
            # 制御文字/ANSI 等を除去し MAX_ID_LENGTH で切り詰めて無害化する。
            if not isinstance(pid, str):
                pid = str(pid)
            pid = _DISPLAY_SANITIZE_RE.sub('', pid)[:MAX_ID_LENGTH]
            print(
                f'[Stop] patterns.json エントリの description が肥大しています '
                f'(id={pid}, {len(desc)}字 > {DESCRIPTION_WARN_LENGTH}字)。'
                f'内容を見直すか、日次評価が原因の場合は .dev/changelog-evals.md へ移してください',
                file=sys.stderr,
            )


def _sanitize_path_element(value) -> str:
    """agent 名・相対サブパスの各セグメント・ファイル名を、連結前に個別サニタイズする。

    連結後の文字列全体へ長さ上限をかけると、長い要素の隣に来た別要素が丸ごと
    消えてどのファイルの警告か分からなくなる（要素境界が壊れる）。要素ごとに
    独立してサニタイズ・切り詰めることで、対象ファイルの一意特定を保つ
    [S3 ⑦ 契約(4)]。
    """
    if not isinstance(value, str):
        value = str(value)
    return _DISPLAY_SANITIZE_RE.sub('', value)[:MAX_ID_LENGTH]


def _is_link_like(path: str) -> bool:
    """symlink / junction など「別の場所を指す」エントリかどうかを判定する。

    os.path.islink() は POSIX symlink 用の reparse tag しか見ないため、Windows の
    junction (IO_REPARSE_TAG_MOUNT_POINT) を検出できない。junction はディレクトリ
    として列挙されるとそのまま再帰走査に「辿られて」しまう [S3 ⑦ tester 実測]。
    reparse tag の種類を問わず FILE_ATTRIBUTE_REPARSE_POINT 属性の有無で判定する
    ことで、symlink・junction の両方、および将来追加されうる他の reparse tag も
    一律に除外する。
    """
    try:
        if os.path.islink(path):
            return True
        st = os.lstat(path)
    except OSError:
        return False
    attrs = getattr(st, 'st_file_attributes', 0)
    return bool(attrs & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def _iter_relative_md_files(agent_dir: str, _rel_parts: list | None = None):
    """agent_dir 配下の *.md を再帰的に列挙する（symlink / junction は辿らない）。

    yield するのは agent_dir からの相対パス要素のリスト（例: ["topics", "foo.md"]）。
    走査エラー（権限不足等）はそのディレクトリ配下をスキップして継続する
    （fail-open。1 ディレクトリの読み取りエラーで走査全体を止めない）。
    """
    rel_parts = _rel_parts or []
    dir_path = os.path.join(agent_dir, *rel_parts) if rel_parts else agent_dir
    try:
        entries = sorted(os.listdir(dir_path))
    except OSError:
        return
    for entry in entries:
        full = os.path.join(dir_path, entry)
        if _is_link_like(full):
            continue
        try:
            is_dir = os.path.isdir(full)
        except OSError:
            continue
        new_rel = rel_parts + [entry]
        if is_dir:
            yield from _iter_relative_md_files(agent_dir, new_rel)
        elif entry.lower().endswith('.md'):
            yield new_rel


def _warn_index_file(agent_dir: str, safe_agent: str, warn_bytes: int, warn_lines: int) -> None:
    """<agent_dir>/MEMORY.md（索引）が injection 予算の 80% に達していたら警告する。

    直下 1 階層の MEMORY.md を直接パスで判定する（_iter_relative_md_files 経由の
    os.listdir を追加で呼ばない）。listdir を丸ごと差し替える表示サニタイズ系の
    既存テストは「top-level の os.listdir が 1 回だけ呼ばれる」前提のため、ここで
    再度 listdir すると意図しない偽陰性を生む [S3 ⑦ 実装ノート]。

    MEMORY.md 自体が symlink / junction の場合はスキップする [CR-NEW]。
    直接パスで開く経路は `_iter_relative_md_files` のリンク除外を通らないため、
    ここで検査しないと「symlink / junction は辿らない」契約 [S3 ⑦ 契約(1)] に
    索引経路だけ穴が残る（リンク先の任意ファイルのサイズ・行数が警告に出る）。
    """
    path = os.path.join(agent_dir, 'MEMORY.md')
    if _is_link_like(path):
        return
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            head = f.read(AGENT_MEMORY_READ_CAP_BYTES)
    except OSError:
        # MEMORY.md 未生成のディレクトリ（wt_developer 等）はスキップする
        return

    # 末尾改行は行を増やさない（全 agent-memory ファイルが末尾改行ありのため、
    # 単純な count+1 だと通常経路で常に 1 行多く報告してしまう）。
    lines = head.count(b'\n')
    if head and not head.endswith(b'\n'):
        lines += 1
    truncated = size > len(head)

    if size <= warn_bytes and lines <= warn_lines:
        return

    line_label = f'{lines}行以上' if truncated else f'{lines}行'
    print(
        f'[Stop] agent-memory/{safe_agent}/MEMORY.md が injection 予算に接近しています '
        f'({size}B / {AGENT_MEMORY_LIMIT_BYTES}B, {line_label} / {AGENT_MEMORY_LIMIT_LINES}行)。'
        f'超えた分は起動時に system prompt へ載らず、蓄積した知見が読まれなくなります。'
        f'価値の低いエントリから削って予算内に戻してください',
        file=sys.stderr,
    )


def _warn_individual_file(agent_dir: str, rel_parts: list, safe_agent: str) -> None:
    """個別メモリファイル（索引 MEMORY.md 以外の *.md）が 100KB を超えていたら警告する。

    索引と異なり system prompt へ自動注入されないため、行数しきい値や injection
    予算の文言は使わない。制約は「Read のたびに払うコスト・保守コスト」。
    """
    path = os.path.join(agent_dir, *rel_parts)
    try:
        size = os.path.getsize(path)
    except OSError:
        # 読めないファイル 1 件で走査全体を止めない（fail-open）
        return

    if size <= AGENT_MEMORY_FILE_LIMIT_BYTES:
        return

    safe_parts = [_sanitize_path_element(p) for p in rel_parts]
    # nul-boundary: allow(警告文に埋め込む相対パス表示のみ・人間可読出力であり機械パース対象ではない)
    rel_display = '/'.join(safe_parts)
    print(
        f'[Stop] agent-memory/{safe_agent}/{rel_display} が個別メモリファイルとして'
        f'肥大しています ({size}B > {AGENT_MEMORY_FILE_LIMIT_BYTES}B)。'
        f'Read のたびに読み込みコストがかかり保守コストも増えるため、'
        f'内容を分割するか整理してください',
        file=sys.stderr,
    )


def _warn_oversized_agent_memory() -> None:
    """agent-memory/*/ 配下の *.md を 2 種類のしきい値で走査し stderr 警告する。

    - <agent>/MEMORY.md（索引。直下 1 階層のみ）: injection 予算 25KB/200行の 80% 到達
    - それ以外の *.md（個別メモリファイル。サブディレクトリ含め再帰走査）:
      AGENT_MEMORY_FILE_LIMIT_BYTES (100KB) 超過のみ

    直下以外の場所にある "MEMORY.md"（例: topics/MEMORY.md）は契約上の想定外だが、
    索引として扱うと agent 直下と衝突するため、個別メモリファイル扱いとする
    [設計コメント: 契約に明記なし・「索引は直下 1 階層のみ」と解釈して実装]。

    削除・切り詰めは行わない（警告のみ・read-only）。1 ファイル 1 行で出力する。
    patterns.json 側の `_warn_oversized_descriptions` と同じ「警告のみ」方針を取る。
    symlink / junction は辿らない [S3 ⑦ 契約(1)]。
    本関数は例外を出さない防御的実装だが、呼び出し側でも try/except で二重に保護する。
    """
    warn_bytes = int(AGENT_MEMORY_LIMIT_BYTES * AGENT_MEMORY_WARN_RATIO)
    warn_lines = int(AGENT_MEMORY_LIMIT_LINES * AGENT_MEMORY_WARN_RATIO)
    try:
        agent_names = sorted(os.listdir(AGENT_MEMORY_DIR))
    except OSError:
        # 利用先の初回起動時などディレクトリ自体が無い場合は何もしない
        return

    for agent_name in agent_names:
        agent_dir = os.path.join(AGENT_MEMORY_DIR, agent_name)
        if _is_link_like(agent_dir):
            continue
        try:
            if not os.path.isdir(agent_dir):
                # トップ階層の .gitkeep 等（ディレクトリでないエントリ）はスキップ
                continue
        except OSError:
            continue

        # ディレクトリ名はファイルシステム由来で未検証のため、出力前に制御文字・
        # 表示偽装文字を除去し MAX_ID_LENGTH で切り詰めて無害化する。
        safe_agent = _sanitize_path_element(agent_name)

        _warn_index_file(agent_dir, safe_agent, warn_bytes, warn_lines)

        for rel_parts in _iter_relative_md_files(agent_dir):
            if rel_parts == ['MEMORY.md']:
                continue  # 索引は _warn_index_file が処理済み。二重報告しない。
            _warn_individual_file(agent_dir, rel_parts, safe_agent)


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
        # isinstance ガード: 非 dict エントリは検索対象外として無視し、
        # 該当 id なし扱いで新規パターンとして追加される（削除はしない）[CR-E-001][SR-V-001]。
        existing = next((p for p in data['patterns'] if isinstance(p, dict) and p.get('id') == pid), None)
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
        if not isinstance(pattern, dict):
            # 型が dict でないエントリは trust/expiry 計算不能。
            # registered_date parse 不能時と同様に保持して継続（クラッシュ回避）[SR-V-001]。
            active.append(pattern)
            continue
        if pattern.get('promoted', False):
            active.append(pattern)
            continue

        registered = _parse_session_date(pattern['registered_date'])
        if registered is None:
            # registered_date が parse 不能ならパターンを保持して継続（クラッシュ回避）
            active.append(pattern)
            continue
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
    # 肥大検知ガード（警告のみ・read-only）。誤動作しても Stop を止めないよう
    # 防御的実装に加えて呼び出し側でも try/except で二重に握る（belt-and-suspenders）。
    try:
        _warn_oversized_descriptions(active)
    except Exception:
        pass
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

    # agent-memory の肥大検知（警告のみ）。誤動作しても Stop を止めない。
    try:
        _warn_oversized_agent_memory()
    except Exception:
        pass

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
