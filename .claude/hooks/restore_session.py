#!/usr/bin/env python3
"""
restore_session.py: SessionStart(compact) hook.
コンテキスト圧縮後に現在のセッション状態を再注入する。
"""

import os
import re
import sys

try:
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(_HOOKS_DIR)
SESSIONS_DIR = os.path.join(_CLAUDE_DIR, 'memory', 'sessions')

# 定数（architecture §3.5 / plan T4）
# GENBA = 「現在地」（現場）の音写命名。architecture §2.3 で確定した用語（CR M-01）。
# 他 Python consumer（init-session 等）が生まれたら session_utils へ移動すること（CR L-06）。
APPROACH_TAIL_LINES = 15
# GENBA_DONE: 現在地値が「完了」状態を表す定数（GENBA = 現在地 の音写）（CR M-01）。
# 他 Python consumer が生まれたら session_utils へ移動すること（CR L-06）。
GENBA_DONE = '完了'

# 現在地フィールドの読み取り用 regex（architecture §2.3）
_GENBA_RE = re.compile(r'^現在地:[ \t]*(.*)$', re.MULTILINE)

# date_str（ファイル名由来）の YYYYMMDD 形式検証用（SR L-3 / CR M-03）
# ファイル名経由で任意文字列が混入するのを防ぐ。他の regex 定数と同じモジュールレベルに配置。
_DATE_STR_RE = re.compile(r'^\d{8}$')


def _load_session_utils():
    """session_utils モジュールを動的にロードして返す（同階層）。"""
    import importlib.util

    util_path = os.path.join(_HOOKS_DIR, "session_utils.py")
    spec = importlib.util.spec_from_file_location("session_utils", util_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"session_utils が見つかりません: {util_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


def find_latest_session() -> str | None:
    if not os.path.isdir(SESSIONS_DIR):
        return None
    files = [f for f in os.listdir(SESSIONS_DIR) if f.endswith('.tmp')]
    if not files:
        return None
    return os.path.join(SESSIONS_DIR, max(files))


def extract_section(content: str, heading: str) -> str:
    """``session_utils.extract_section`` への薄いラッパー（後方互換用）。

    過去にこのモジュール直下にあった ``extract_section`` を呼び出すテスト・スクリプトとの
    互換維持のため、モジュールレベルで公開する。実体は :mod:`session_utils` 側にある。
    """
    return _load_session_utils().extract_section(content, heading)


def extract_genba(content: str) -> str:
    """セッション本文から「現在地:」行の値を抽出する（architecture §2.3）。

    行正規表現 ``^現在地:[ \\t]*(.*)$`` (MULTILINE) でマッチし、
    トリム済みの値を返す。行が存在しない場合は空文字列（後方互換）。

    Args:
        content: セッションファイル全体のテキスト。

    Returns:
        現在地の値（trim 済み）、または空文字列（行なし・値なし）。
    """
    m = _GENBA_RE.search(content)
    return m.group(1).strip() if m else ''


def _tail(text: str, n: int) -> str:
    """テキストの末尾 n 行を返す（architecture §3.5）。

    行数が n 以下の場合はテキストをそのまま返す（切り詰めない）。

    Args:
        text: 対象テキスト。
        n: 末尾から取得する行数。

    Returns:
        末尾 n 行（n 以下の場合は全体）。

    Note:
        n=0 のとき ``lines[-0:]`` は ``lines[0:]``（全体）と等価なため、
        全行を返す（切り詰めない）。Python の ``-0 == 0`` による反直感挙動（CR M-04）。
        呼び出し元は ``APPROACH_TAIL_LINES=15``（固定定数）のため実害はないが、
        仕様として明記・固定する。
    """
    lines = text.splitlines()
    return '\n'.join(lines[-n:]) if len(lines) > n else text  # nul-boundary: allow(末尾 N 行を人間可読テキストへ戻す。呼び出し元は表示のため再 splitlines するだけ)


def _sanitize_genba(value: str) -> str:
    """現在地の値を出力に埋め込む前にサニタイズする（architecture §10 / SR 観点）。

    ``session_utils.sanitize_value`` への薄いラッパー。
    サニタイズ範囲を ``stop.py::_INHERIT_SANITIZE_RE`` と同等以上に保つため、
    共通関数に委譲する（SR M-1 / CR M-02）:
    - 改行文字（\\n / \\r）を除去する
    - C0 制御文字・DEL（\\x7f）・C1 制御文字（\\x80-\\x9f）・U+2028/U+2029 を除去する
    - ``-->`` を ``-- >`` に置換して HTML コメントブロックの破壊を防ぐ
    - タブ（\\t）は保持する（SR L-1・session_utils.sanitize_value 参照）
    """
    return _load_session_utils().sanitize_value(value)


def main():
    path = find_latest_session()
    if not path or not os.path.exists(path):
        sys.exit(0)

    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    session_utils = _load_session_utils()
    _extract_section = session_utils.extract_section
    _sanitize = session_utils.sanitize_value

    date_str = os.path.basename(path).replace('.tmp', '')

    # date_str の YYYYMMDD 形式検証（SR L-3）:
    # ファイル名経由で任意文字列が date_str に混入するのを防ぐ。
    # 8桁数字以外のファイル名はヘッダへの注入を避けるため exit 0 でスキップする。
    if not _DATE_STR_RE.match(date_str):
        sys.exit(0)

    # 現在地フィールドを読み取る（architecture §3.2 step2）
    genba = extract_genba(content)
    genba_in_progress = genba != '' and genba != GENBA_DONE

    todos = _extract_section(content, '残タスク')
    successes = _extract_section(content, 'うまくいったアプローチ')
    failures = _extract_section(content, '試みたが失敗したアプローチ')

    # - [ ] 行のみにフィルタ（architecture §3.4）
    # フィルタ判定は元行の lstrip() で行い（- [ ] プレフィックス保持）、
    # 出力時に sanitize_value でサニタイズする（SR M-2）。
    pending_todos = [
        _sanitize(line) for line in todos.splitlines()
        if line.lstrip().startswith('- [ ]')
    ]

    # early-exit 判定（architecture §3.2 step4）
    if not genba_in_progress and not pending_todos and not successes and not failures:
        sys.exit(0)

    lines = []

    # ①ワークフロー復帰指示（現在地が進行中のときのみ・出力冒頭）
    if genba_in_progress:
        safe_genba = _sanitize_genba(genba)
        lines.append(
            f'⚠️ dev-workflow 進行中（現在地: {safe_genba}）。\n'
            f'残作業に直接着手せず、対応 skill（develop / review-phase / start）経由で再開し、\n'
            f'各エージェント出力後の Approval Flow を守ること。'
        )

    # ②ヘッダ
    lines.append(f'[C3 セッション復元: {date_str} / 圧縮後リマインダー]')

    # ③残タスク（- [ ] 行のみ・0件ならセクション省略）
    if pending_todos:
        lines.append('\n## 残タスク')
        lines.append('\n'.join(pending_todos))  # nul-boundary: allow(stdout へ出す復元メッセージの残タスク段落。表示専用)

    # ④うまくいったアプローチ（末尾 N 行に切り詰め・行単位でサニタイズ）（SR M-2）
    if successes:
        lines.append('\n## うまくいったアプローチ')
        tail_text = _tail(successes, APPROACH_TAIL_LINES)
        sanitized_lines = [_sanitize(line) for line in tail_text.splitlines()]
        lines.append('\n'.join(sanitized_lines))  # nul-boundary: allow(stdout へ出す復元メッセージの成功アプローチ段落。表示専用)

    # ⑤試みたが失敗したアプローチ（末尾 N 行に切り詰め・行単位でサニタイズ）（SR M-2）
    if failures:
        lines.append('\n## 試みたが失敗したアプローチ')
        tail_text = _tail(failures, APPROACH_TAIL_LINES)
        sanitized_lines = [_sanitize(line) for line in tail_text.splitlines()]
        lines.append('\n'.join(sanitized_lines))  # nul-boundary: allow(stdout へ出す復元メッセージの失敗アプローチ段落。表示専用)

    print('\n'.join(lines))  # nul-boundary: allow(stdout へ出力する復元メッセージ全体。読み手は Claude Code の表示でリポジトリ内に split 側がない)


if __name__ == '__main__':
    main()
