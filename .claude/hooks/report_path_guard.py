#!/usr/bin/env python3
"""PreToolUse hook: レポートの書き先パスを守るガード（P4・配布対象）。

Write ツールで作成されるレポートファイル（対象 9 prefix）について、
書き先が `.claude/reports/` 直下であること・既存レポートを上書きしないこと・
strict-4 prefix がタイムスタンプ命名であることを検査し、逸脱を exit 2 でブロックする。

## 発火条件

`tool_name` == "Write" かつ、basename を `_hook_utils.norm_component()`
（lower + 末尾ドット/スペース除去）で正規化した値が対象 9 prefix のいずれかで
始まる場合のみ。非対象 basename・Write 以外は沈黙（exit 0・stderr 空）。

## 判定入力の一本化

target = `file_path` が絶対ならそのまま、相対なら `os.getcwd()` と結合したパス。
第 1 層・第 2 層とも同じ target を判定する（判定対象が経路によってブレないようにする）。

## 検査 1: 封じ込め（二層）

- 第 1 層（名前判定）: target の親が `reports`・祖父が `.claude`
  （`norm_component` 正規化で比較。ケース非依存 FS の別名・末尾ドットを吸収する）
- 第 2 層（実体照合）: `realpath(target)` の親ディレクトリが許可 root と
  `os.path.samefile` で一致すること

許可 root は 2 つ:
  - root A = `realpath(os.getcwd())/.claude/reports`（一次）
  - root B = `realpath($CLAUDE_PROJECT_DIR)/.claude/reports`（env が非空のときのみ）

env 未設定・空なら root A のみで判定する（縮退。root B が無いことを理由に block しない）。
両層 AND で許可し、不成立・両 root 不在・親不在は block（fail-closed）。

### 免責（脅威モデル外）

許可 root ディレクトリ自体（`.claude/reports` そのもの）が外部へのリンクに
差し替えられているケースは、expected（root A/B）と resolved（target の親）が
同一実体になるため block しない。root そのものを差し替えられる権限を持つ相手は
本 hook の防御対象ではなく、ここで block すると正規運用（reports を別ボリュームへ
逃がす構成）を壊すだけになる。

## 判定の分割（正規化 basename と元 basename）

report_contract_check.py と統一したポリシー: **prefix 一致判定は
`norm_component()` 正規化後の basename・形式（timestamp フルマッチ）判定は
元の basename** に対して行う。これにより大文字混じり prefix（`Plan-Report-`）は
prefix 一致として検出しつつ、`.MD` のような大文字混じり拡張子は timestamp 形式として
誤許可しない（ケース非依存 FS では正規名と同一実体になりうるため、この場合は
strict-4 なら検査 3 で block・非 strict-4 なら通常の新規作成として許可される）。

## 検査 2: 上書き block

元の basename が timestamp 形式（prefix + `\\d{8}-\\d{6}\\.md` の `re.ASCII`
フルマッチ）かつ書き込み先実体が既存 → block。task_id 形式（非 timestamp）の
既存上書きは許可する（同一 task の再実行で更新する正規経路のため）。

## 検査 3: strict-4 形式 block

strict-4 prefix（requirements / architecture / plan / design-review）に一致する
（正規化 basename が prefix で始まる）ファイルの、元の basename が timestamp 形式に
フルマッチしない場合は新規作成でも block。CR / SR / test / debug 系は
task_id 命名が正規のため非適用。

## fail 方針

- 壊れた stdin JSON / payload 非オブジェクト / キー欠落 / 空 file_path は fail-open
  （対象かどうかすら判定できない入力で他の Write を巻き込まない）
- 対象と判定した後のパス判定で例外を誘発する入力・NUL 混入は fail-closed（block）

## bypass

環境変数 `C3_REPORT_GUARD_DISABLE=1` で全検査をスキップする。
"""

import json
import os
import re
import sys
from pathlib import Path

# 共通ヘルパー (_hook_utils.norm_component, sanitize_for_terminal, STRICT4_PREFIXES,
# timestamp_pattern) を hooks/ 経由で import するため、このスクリプトのディレクトリを
# PYTHONPATH に追加する。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hook_utils import (  # noqa: E402
    norm_component,
    sanitize_for_terminal,
    STRICT4_PREFIXES,
    timestamp_pattern,
)

try:
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass

# 対象 9 prefix（正規化済み basename に対して startswith で判定する）
# strict-4 の先頭 4 件は _hook_utils.STRICT4_PREFIXES から導出し、二重管理を避ける。
TARGET_PREFIXES = STRICT4_PREFIXES + (
    'code-review-report-',
    'security-review-report-',
    'test-report-',
    'debug-analysis-',
    'debug-needed-',
)

REPORTS_DIR_NAME = 'reports'
CLAUDE_DIR_NAME = '.claude'
PROJECT_DIR_ENV = 'CLAUDE_PROJECT_DIR'
DISABLE_ENV = 'C3_REPORT_GUARD_DISABLE'

NUL = '\x00'


def _match_prefix(norm_basename):
    """正規化済み basename が対象 9 prefix のどれで始まるかを返す（無ければ None）。"""
    for prefix in TARGET_PREFIXES:
        if norm_basename.startswith(prefix):
            return prefix
    return None


def _is_timestamp_name(basename, prefix):
    """`{prefix}YYYYMMDD-HHMMSS.md` のフルマッチか。

    _hook_utils.timestamp_pattern() へ委譲。**元の（正規化前の）basename** に対して
    判定する（report_contract_check.py と統一。prefix 一致は正規化後 basename で行い、
    形式判定は元の basename で行う分割により、`.MD` 等の大文字混じり拡張子を
    timestamp 形式として誤許可しない）。
    """
    return timestamp_pattern(prefix).fullmatch(basename) is not None


def _allowed_roots():
    """許可 root（root A / root B）の候補パスを返す。

    root B は `CLAUDE_PROJECT_DIR` が非空のときのみ加える。存在確認はここでは行わず、
    照合側（samefile）に委ねる。
    """
    roots = [
        os.path.join(
            os.path.realpath(os.getcwd()), CLAUDE_DIR_NAME, REPORTS_DIR_NAME
        )
    ]
    project_dir = os.environ.get(PROJECT_DIR_ENV, '')
    if project_dir:
        roots.append(
            os.path.join(
                os.path.realpath(project_dir), CLAUDE_DIR_NAME, REPORTS_DIR_NAME
            )
        )
    return roots


def _is_contained(target):
    """target が許可 root 直下に封じ込められているか（二層 AND）。"""
    # 第 1 層: 名前判定（`.claude/reports/` 直下の形をしているか）。
    # `..` を含む脱出パスは親の名前が `..` になるためここで落ちる。
    parent = target.parent
    if norm_component(parent.name) != REPORTS_DIR_NAME:
        return False
    if norm_component(parent.parent.name) != CLAUDE_DIR_NAME:
        return False

    # 第 2 層: 実体照合（リンク偽装で名前だけ揃えた経路をここで落とす）。
    resolved_parent = os.path.dirname(os.path.realpath(str(target)))
    if NUL in resolved_parent:
        return False
    for root in _allowed_roots():
        try:
            if os.path.samefile(resolved_parent, root):
                return True
        except OSError:
            # root 不在・親不在は「一致しない」として扱う（fail-closed）。
            continue
    return False


def _block(kind_line, guidance):
    """違反種別ごとのメッセージを stderr へ出して exit 2 する。"""
    print(
        f'[ReportPathGuard BLOCK] {kind_line}\n'
        f'{guidance}\n'
        f'恒久 bypass: {DISABLE_ENV}=1 環境変数を設定してください',
        file=sys.stderr,
    )
    sys.exit(2)


def _block_containment(display_name):
    _block(
        f'封じ込め違反: {display_name} の書き先が許可された reports ディレクトリの外です。',
        'レポートは .claude/reports/ 直下に作成してください。\n'
        '書き先パスをその形へ修正し、cwd がプロジェクトルート外なら'
        'プロジェクトルートへ cd してから再実行してください。',
    )


def _block_overwrite(display_name):
    _block(
        f'上書き違反: {display_name} は既存のタイムスタンプ形式レポートです。',
        '既存レポートの上書きは禁止です。'
        'report-timestamp skill で新しいタイムスタンプを再採番し、'
        '別名のファイルとして作成してください。',
    )


def _block_format(display_name, prefix):
    _block(
        f'形式違反: {display_name} は {prefix}YYYYMMDD-HHMMSS.md の形式ではありません。',
        'report-timestamp skill でタイムスタンプを再採番し、'
        'その値をファイル名に使用してください。',
    )


def main():
    # bypass: 恒久 disable 環境変数の確認（`1` 以外の値では無効化しない）
    if os.environ.get(DISABLE_ENV) == '1':
        sys.exit(0)

    # JSON payload の読み込み（fail-open）
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)
    if not isinstance(payload, dict):
        sys.exit(0)

    # tool_name の確認（Write 以外は対象外）
    if payload.get('tool_name') != 'Write':
        sys.exit(0)

    # file_path の抽出（キー欠落・空文字は fail-open）
    tool_input = payload.get('tool_input')
    if not isinstance(tool_input, dict):
        sys.exit(0)
    file_path = tool_input.get('file_path')
    if not isinstance(file_path, str) or not file_path:
        sys.exit(0)

    # 発火条件: 正規化 basename が対象 9 prefix で始まるか。
    # ここまでは純粋な文字列操作であり、対象外なら一切副作用を出さずに抜ける。
    raw_basename = os.path.basename(file_path)
    norm_basename = norm_component(raw_basename)
    prefix = _match_prefix(norm_basename)
    if prefix is None:
        sys.exit(0)

    display_name = sanitize_for_terminal(raw_basename)

    # ここから先はパス判定。例外を誘発する入力は fail-closed で block する。
    try:
        if os.path.isabs(file_path):
            target = Path(file_path)
        else:
            target = Path(os.getcwd()) / file_path

        # NUL 混入は realpath / samefile の挙動が環境依存になるため明示 block
        if NUL in str(target):
            _block_containment(display_name)

        # 検査 1: 封じ込め
        if not _is_contained(target):
            _block_containment(display_name)

        is_timestamp = _is_timestamp_name(raw_basename, prefix)

        # 検査 2: 上書き block（timestamp 形式の既存のみ）
        if is_timestamp and os.path.exists(str(target)):
            _block_overwrite(display_name)

        # 検査 3: strict-4 形式 block
        if prefix in STRICT4_PREFIXES and not is_timestamp:
            _block_format(display_name, prefix)
    except (OSError, ValueError):
        _block_containment(display_name)

    sys.exit(0)


if __name__ == '__main__':
    main()
