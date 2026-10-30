#!/usr/bin/env python3
"""PreToolUse hook: worktree boundary guardrail.

作動条件: hook 入力 JSON の `agent_type` が WT_AGENT_TYPES（wt_developer /
wt_tester / wt_systematic-debugger）に含まれること。Claude Code は subagent 内の
tool 呼び出しの hook 入力に `agent_type` を載せるため、parallel-agents skill が
isolation:"worktree" 付きで起動する wt_* agent だけがガード対象になる。
旧作動条件の環境変数 PO_WORKTREE_GUARD は hook のプロセスに届く経路が無く、
ガードが一度も作動していなかったため廃止した（現在は読まない）。

境界: hook プロセスの cwd（os.getcwd()）を含む worktree のルート。cwd のパス要素から
最も内側の `.claude` / `worktrees` / `<名前>` までをルートとする（入れ子は内側が
ルート）。入力 JSON の cwd は使わない。対象 agent で cwd からルートを導けない場合は
block する（fail-closed）。cwd の取得・正規化が OSError / ValueError になった場合も
block する。対象パスの判定は decide_containment（二層 AND）に委ねる。

入力の型の異常: 対象 agent で tool_input が dict でない・file_path が文字列でない場合は
block する。stdin が不正な JSON・dict でない JSON の場合は、agent_type を読めず対象
agent を判定できないため通過（exit 0）する。

射程: Write / Edit ツールのみ。Bash / NotebookEdit 経由の書き込みは射程外
（本 hook 単独を防御の最終線にしない）。

無効化: 環境変数 C3_WORKTREE_GUARD_DISABLE=1。hook のプロセスに届くのは settings の
`env`（settings.json / settings.local.json）経由で設定した値であり、Bash ツール内の
`export` は hook のプロセスに届かない。
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

# worktree ルートの識別に使うパス要素名（derive_worktree_root が使う）。
# cwd を os.sep で分割し、`.claude` の直後に `worktrees` が続く位置のうち最も内側
# （cwd に最も近い側）を探し、その次の要素 `<名前>` までをルートとする
# （`.claude/worktrees/<名前>` が入れ子になっている場合は内側がルート）。
_WORKTREES_PARENT = ".claude"
_WORKTREES_COMPONENT = "worktrees"

# ガード対象の agent_type（.claude/agents/wt_*.md の name: と一致させる）。
WT_AGENT_TYPES = frozenset({"wt_developer", "wt_tester", "wt_systematic-debugger"})


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


def derive_worktree_root(cwd):
    """cwd のパス要素から最も内側の `.claude/worktrees/<名前>` のルートを返す。

    導けない場合（`worktrees` の直前が `.claude` でない・`<名前>` の要素が無い等）は
    None を返す。
    """
    parts = cwd.split(os.sep)
    for idx in range(len(parts) - 2, 0, -1):
        if parts[idx] == _WORKTREES_COMPONENT and parts[idx - 1] == _WORKTREES_PARENT:
            depth = len(parts) - (idx + 2)  # ルート（<名前>）から cwd までの段数
            return str(Path(cwd).parents[depth - 1]) if depth > 0 else cwd
    return None


def _block(file_path, resolved, allowed):
    print(
        f'[WorktreeGuard BLOCK] worktree 外へのファイル操作をブロックしました。\n'
        f'  対象パス: {sanitize_for_terminal(file_path)}\n'
        f'  解決パス: {sanitize_for_terminal(resolved)}\n'
        f'  許可範囲: {sanitize_for_terminal(allowed)}',
        file=sys.stderr
    )
    sys.exit(2)


def main():
    if os.environ.get('C3_WORKTREE_GUARD_DISABLE') == '1':
        sys.exit(0)

    # 不正な JSON・dict でない JSON は exit 0（対象外）とする。理由: これらの入力からは
    # agent_type を読めず対象 agent かどうかを判定できない。
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)
    if not isinstance(payload, dict):
        sys.exit(0)

    # agent_type が文字列でなければ対象外（list / dict はハッシュ不能で集合の所属判定が
    # TypeError になるため、型を先に確かめる）。
    agent_type = payload.get('agent_type')
    if not isinstance(agent_type, str) or agent_type not in WT_AGENT_TYPES:
        sys.exit(0)

    tool_name = payload.get('tool_name', '')
    if tool_name not in ('Write', 'Edit'):
        sys.exit(0)

    # 対象 agent で入力の型が想定外のときは書き込み先を判定できないため block する
    # （fail-closed）。tool_input キーが無い場合は従来どおり空の dict とみなす。
    tool_input = payload.get('tool_input', {})
    if not isinstance(tool_input, dict):
        _block(repr(tool_input), '(tool_input が dict ではありません)', '(判定不能)')
    file_path = tool_input.get('file_path', '')
    if not isinstance(file_path, str):
        _block(repr(file_path), '(file_path が文字列ではありません)', '(判定不能)')
    if not file_path:
        sys.exit(0)

    # cwd の取得・正規化が失敗したら境界を導けないため block する（fail-closed）。
    try:
        cwd = os.path.realpath(os.getcwd())
    except (OSError, ValueError):
        _block(file_path, file_path, '(cwd を取得できません)')
    root = derive_worktree_root(cwd)
    if root is None:
        # 対象 agent なのに境界を導けない: fail-closed
        _block(file_path, file_path, '(worktree ルートを cwd から導出できません)')

    target = file_path if os.path.isabs(file_path) else os.path.join(cwd, file_path)

    if decide_containment(target, root):
        # stderr 表示専用の best-effort 解決（判定には使わない・失敗しても target を出す）
        try:
            display_resolved = os.path.realpath(target)
        except (OSError, ValueError):
            display_resolved = target
        _block(file_path, display_resolved, root)

    sys.exit(0)


if __name__ == '__main__':
    main()
