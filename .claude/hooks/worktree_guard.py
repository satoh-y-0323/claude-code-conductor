#!/usr/bin/env python3
"""PreToolUse hook: worktree boundary guardrail.

PO_WORKTREE_GUARD=1 が設定されている場合のみ動作する。
worktree 内で実装タスクを実行するワークフロー（parallel-agents skill が
isolation:"worktree" 付きで起動する agent など）が事前にこの env を設定して有効化する。
Write / Edit ツールの対象パスが CWD（worktree ルート）外であればブロックする。

NOTE [SR-V-002]: env 未設定時にガードが無効化されるリスクは parallel-agents/SKILL.md
で `PO_WORKTREE_GUARD=1` 設定を必須化することで運用上対処する。CWD parts チェック
単独で自動有効化する設計変更は次回 major bump で検討予定。
"""

import json
import os
import sys
import unicodedata
from pathlib import Path

# 共通ヘルパー (_hook_utils.sanitize_for_terminal) を hooks/ 経由で import するため、
# このスクリプトのディレクトリを PYTHONPATH に追加する。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hook_utils import sanitize_for_terminal  # noqa: E402

try:
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass

# worktree パスの識別に使うコンポーネント名。
# `.claude/worktrees/agent-<id>/` という構造を前提とし、
# "worktrees" の直前のコンポーネントが ".claude" であることをパス分割で検査する。
# os.sep を末尾に補完する理由: `.claude/worktrees/agent-test/` のような
# パスを split(os.sep) すると末尾の空文字列が含まれるが、
# インデックス検索には影響しないため補完不要。
# ただし startswith(cwd + os.sep) による境界チェックでは os.sep が必須（例:
# `/foo/bar` が `/foo/baz` の prefix と誤判定されるのを防ぐ）。
_WORKTREES_PARENT = ".claude"
_WORKTREES_COMPONENT = "worktrees"


def decide_containment(resolved, cwd, same_entity=os.path.samefile, path_exists=os.path.exists):
    """対象パスが cwd 配下かどうかを二層 AND で判定する純関数（S3 ①）。

    戻り値はブロック判定そのもの（True = block / False = allow）。

    第 1 層: 対象パスの解決（realpath）後、比較直前に両辺を NFC 正規化した文字列の
             prefix 判定。表現差（NFC/NFD）だけの不一致を吸収する。
    第 2 層: ファイルシステム実体による cwd 配下確認。対象の実在する最深祖先から
             根方向へ祖先鎖を辿り、いずれかの祖先（最深祖先自身を含む）が cwd と
             samefile であれば配下と判定する（対象自身が cwd と同一の場合も、
             対象自身が「実在する最深祖先」になるため自然に許可される）。

    許可は第 1 層・第 2 層の両方に合格した場合のみ（AND）。片方のみの合格では
    許可しない。

    例外（OSError / ValueError）はこの関数の内側で捕捉し、fail-closed として
    True（block）を返す。捕捉が囲む範囲は対象パスの解決（realpath）と両層の判定の
    みに限定する。

    TOCTOU 免責 [SR-NEW-3]: realpath / path_exists / same_entity の各呼び出しの
    間に対象パスやその祖先が差し替えられる（判定後・実書き込み前にリンクへ
    すり替えられる等）競合は原理的に残る。本 hook は agent の書き込み先が
    worktree 外へ逸脱する事故を防ぐガードであり、悪意ある同時操作に対する
    セキュリティ境界ではないため、この競合は許容する（patterns_guard.py の
    フラグ TTL 判定と同じ免責）。射程外の経路（Bash / NotebookEdit 経由の
    書き込み）が元々ガードされないことと合わせ、本 hook 単独を防御の最終線に
    しない前提で運用する。
    """
    try:
        resolved = os.path.realpath(resolved)

        # 実測補正: Windows / Python 3.11.9 では os.path.realpath() は NUL 混入
        # パスでも例外を出さず NUL を保持したまま成功して返る。例外捕捉だけでは
        # 不足するため、解決後パスに \x00 が含まれる場合を明示的に block する。
        if "\x00" in resolved or "\x00" in cwd:
            return True

        # 第 1 層: 正規化文字列（realpath 後 NFC）の prefix 判定
        norm_resolved = unicodedata.normalize("NFC", resolved)
        norm_cwd = unicodedata.normalize("NFC", cwd)
        if norm_resolved != norm_cwd and not norm_resolved.startswith(norm_cwd + os.sep):
            return True

        # 第 2 層: ファイルシステム実体による cwd 配下確認。
        # 対象の実在する最深祖先を求める（対象自身が実在すればそれが最深祖先）。
        target = Path(resolved)
        deepest_existing = None
        for candidate in (target, *target.parents):
            if path_exists(str(candidate)):
                deepest_existing = candidate
                break
        if deepest_existing is None:
            return True

        # 最深祖先から根方向へ祖先鎖を辿り、いずれかが cwd と samefile なら配下。
        for ancestor in (deepest_existing, *deepest_existing.parents):
            if same_entity(str(ancestor), cwd):
                return False
        return True
    except (OSError, ValueError):
        return True


def main():
    if os.environ.get('PO_WORKTREE_GUARD') != '1':
        sys.exit(0)

    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    tool_name = payload.get('tool_name', '')
    if tool_name not in ('Write', 'Edit'):
        sys.exit(0)

    file_path = payload.get('tool_input', {}).get('file_path', '')
    if not file_path:
        sys.exit(0)

    cwd = os.path.realpath(os.getcwd())

    # [SR-V-001] CWD がパスコンポーネント分割で ".claude/worktrees/..." の
    # 構造を持つことを検証する。
    # str.split(os.sep) でパス要素に分解し、"worktrees" の直前コンポーネントが
    # ".claude" であることを確認する。
    # これにより、".claude" 自体が symlink で別名解決される場合でも
    # os.path.realpath() 後のパスで正しく検証できる
    # （文字列部分一致 (_WORKTREES_MARKER in cwd) よりも誤検知が少ない）。
    parts = cwd.split(os.sep)
    try:
        wt_idx = parts.index(_WORKTREES_COMPONENT)
        if wt_idx == 0 or parts[wt_idx - 1] != _WORKTREES_PARENT:
            sys.exit(0)
    except ValueError:
        sys.exit(0)

    target = file_path if os.path.isabs(file_path) else os.path.join(cwd, file_path)

    if decide_containment(target, cwd):
        # stderr 表示専用の best-effort 解決（判定には使わない・失敗しても target を出す）
        try:
            display_resolved = os.path.realpath(target)
        except (OSError, ValueError):
            display_resolved = target
        print(
            f'[WorktreeGuard BLOCK] worktree 外へのファイル操作をブロックしました。\n'
            f'  対象パス: {sanitize_for_terminal(file_path)}\n'
            f'  解決パス: {sanitize_for_terminal(display_resolved)}\n'
            f'  許可範囲: {sanitize_for_terminal(cwd)}',
            file=sys.stderr
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == '__main__':
    main()
