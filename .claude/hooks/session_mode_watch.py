#!/usr/bin/env python3
"""PostToolUse hook: session.tmp のモード行挿入・plan= 差し替えを監視（P3・配布対象）。

Edit ツールで `sessions/*.tmp` ファイルを検出し、`^モード: 自律` / `^モード: HITL` 行の
挿入、複数行にわたる plan= 差し替え、実効モード行の状態遷移を警告する。
削除（HITL 復帰）・cycles= 更新は沈黙。

## 対象パス判定とケース非依存化

ファイルパスが `sessions/` を含み `.tmp` で終わるかを判定する。
Windows・macOS の既定（ケース非依存 FS）では `Sessions` や `.TMP` は `sessions` / `.tmp` と
同一実体の別名であり、判定はメンバシップ検査・拡張子比較ともに `.lower()` でケース非依存で行う。
否定形の構造自体は変わらない（各成分に `.lower()` を適用するのみ）。

## 判定の SSOT（state transition table） — 複数モード行への拡張

実効モード行は「最初の `^モード: ` 行」（HITL 行を含む。init-session/autonomous-mode が
`grep -m1 '^モード: '` で読む意味論）。警告判定は以下 3 条件の OR:
  1. (a) 挿入: old に `^モード: 自律` 行が 0 本、new に 1 本以上
  2. (b) 新出値: new の有効 plan= 値集合に old の集合に無い値がある
  3. (c) 実効行遷移: 各側の実効行（最初の `^モード: ` 行。HITL/行なしは「行なし」に写像）の
     ペアに単一行の状態遷移表を適用し、表が警告と定める遷移に該当

検知種別は (c) が警告なら表の種別を採用、それ以外は (a) / (b) の順。

| old実効 | new実効 | 動作 |
|---|---|---|
| 行なし | 行あり(E/N) | 挿入として警告 |
| 行あり | 行なし | 沈黙（削除・HITL 復帰） |
| E | E(同値) | 沈黙（cycles= 更新等） |
| E | E(異値) | 差し替えとして警告 |
| N | E | 差し替えとして警告（無効→有効昇格） |
| E | N | 沈黙（有効→無効降格） |
| N | N | 沈黙 |
| 行なし | 行なし | 沈黙 |

## 出力契約

2 経路: stderr（人間向け）+ stdout JSON（LLM向け）。
exit 0 でも stdout JSON が LLM コンテキストへ注入される。
"""

import json
import os
import re
import sys
from pathlib import Path

# 共通ヘルパー (_hook_utils.sanitize_for_terminal, norm_component) を hooks/ 経由で import するため、
# このスクリプトのディレクトリを PYTHONPATH に追加する。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hook_utils import norm_component, sanitize_for_terminal  # noqa: E402

try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass


def _find_mode_lines(text: str) -> list[str]:
    """テキストから `^モード: 自律` にマッチする行を全て探す（複数行対応）。"""
    lines = text.split('\n')
    result = []
    for line in lines:
        if re.match(r'^モード: 自律', line):
            result.append(line)
    return result


def _find_effective_mode_line(text: str) -> str | None:
    """
    テキストから実効モード行を取得する。

    実効モード行は「最初の `^モード: ` 行」（HITL を含む）。
    init-session/autonomous-mode SKILL.md の `grep -m1 '^モード: '` に対応。
    モード行なしなら None を返す。
    """
    lines = text.split('\n')
    for line in lines:
        if re.match(r'^モード: ', line):
            return line
    return None


def _extract_plan_value(mode_line: str) -> tuple[str, bool]:
    """
    モード行から plan= の値を抽出する。

    Returns:
        (value, is_extractable): value は抽出値、is_extractable は抽出成功の可否
          - 正規行（E）: value は plan= 値、is_extractable = True
          - plan= 欠落（N）: value = "", is_extractable = False
          - unclosed quote（N）: value = "", is_extractable = False
    """
    # plan= を探す
    match = re.search(r'plan=', mode_line)
    if not match:
        # plan= 欠落（N）
        return "", False

    rest = mode_line[match.end():]

    # plan= 直後が引用符の場合
    if rest.startswith('"'):
        # 次の引用符を探す
        close_quote_idx = rest.find('"', 1)
        if close_quote_idx == -1:
            # unclosed quote（N）
            return "", False
        # 引用符に囲まれた値（E）
        value = rest[1:close_quote_idx]
        return value, True

    # 引用符なしの場合: \s+cycles= または行末まで
    # SSOT（mode_line.py:89）: \s+cycles= の終端
    cycles_match = re.search(r'\s+cycles=', rest)
    if cycles_match:
        # 値は rest の開始から cycles= 前の空白前まで（strip）
        value = rest[:cycles_match.start()].strip()
    else:
        # 行末まで（strip）
        value = rest.strip()

    return value, True


def main():
    # JSON payload の読み込み（fail-open）
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    # tool_name の確認（Edit のみ・Write は対象外）
    tool_name = payload.get('tool_name', '')
    if tool_name != 'Edit':
        sys.exit(0)

    # file_path の抽出（キー欠落は fail-open）
    file_path = payload.get('tool_input', {}).get('file_path', '')
    if not file_path:
        sys.exit(0)

    # `sessions/*.tmp` パスかどうかの判定
    path_obj = Path(file_path)
    parts = path_obj.parts
    basename = path_obj.name

    # sessions を含むかつ .tmp で終わるかの確認（ケース非依存）
    # ケース非依存 FS では Sessions や .TMP も対象に含める。
    # パス成分は norm_component() で正規化してから比較（末尾ドット・スペース除去も含む）。
    # basename の拡張子判定も norm_component() でファイル名全体を正規化してから endswith() を適用する。
    if not any(norm_component(p) == 'sessions' for p in parts) or not norm_component(basename).endswith('.tmp'):
        sys.exit(0)

    # old_string / new_string の抽出（キー欠落は fail-open）
    tool_input = payload.get('tool_input', {})
    old_string = tool_input.get('old_string', '')
    new_string = tool_input.get('new_string', '')

    # モード行の検出 — F9 では全行走査
    old_mode_lines = _find_mode_lines(old_string)  # 自律行のみ
    new_mode_lines = _find_mode_lines(new_string)  # 自律行のみ
    old_effective_line = _find_effective_mode_line(old_string)  # 最初のモード: 行（HITL含む）
    new_effective_line = _find_effective_mode_line(new_string)  # 最初のモード: 行

    # 3 条件を全て評価してから優先度で判定する（F10 修正）
    # 優先順位: (c) 実効行遷移 > (a) 挿入 > (b) 新出値
    should_warn_c = False
    warn_kind_c = ''
    should_warn_a = False
    should_warn_b = False

    # (c) 実効行遷移: 実効行ペアに単一行の状態遷移表を適用
    # 実効行の状態を求める
    old_eff_state = None
    if old_effective_line is not None:
        if re.match(r'^モード: 自律', old_effective_line):
            # 自律行: E/N を判定
            old_eff_state = 'E' if _extract_plan_value(old_effective_line)[1] else 'N'
        # else: HITL 行は「行なし」に写像

    new_eff_state = None
    if new_effective_line is not None:
        if re.match(r'^モード: 自律', new_effective_line):
            # 自律行: E/N を判定
            new_eff_state = 'E' if _extract_plan_value(new_effective_line)[1] else 'N'
        # else: HITL 行は「行なし」に写像

    # 状態遷移表に基づいて判定
    if old_eff_state is None and new_eff_state is not None:
        # 行なし → E/N: 挿入として警告
        should_warn_c = True
        warn_kind_c = '挿入'
    elif old_eff_state == 'E' and new_eff_state == 'E':
        # E → E: 値が異なれば警告
        old_value, _ = _extract_plan_value(old_effective_line)
        new_value, _ = _extract_plan_value(new_effective_line)
        if old_value != new_value:
            should_warn_c = True
            warn_kind_c = '差し替え'
    elif old_eff_state == 'N' and new_eff_state == 'E':
        # N → E: 無効→有効昇格は警告
        should_warn_c = True
        warn_kind_c = '差し替え'

    # (a) 挿入: old に自律行が 0 本、new に 1 本以上
    if len(old_mode_lines) == 0 and len(new_mode_lines) >= 1:
        should_warn_a = True

    # (b) 新出値: new の有効 plan= 値集合に old に無い値
    if len(new_mode_lines) > 0:
        # old 側の有効値集合を収集
        old_values = set()
        for line in old_mode_lines:
            value, is_extractable = _extract_plan_value(line)
            if is_extractable:
                old_values.add(value)

        # new 側の有効値集合を収集
        new_values = set()
        for line in new_mode_lines:
            value, is_extractable = _extract_plan_value(line)
            if is_extractable:
                new_values.add(value)

        # new に old に無い値があるか
        if new_values - old_values:
            should_warn_b = True

    # 優先度で判定: (c) > (a) > (b)
    should_warn = False
    warn_kind = ''
    if should_warn_c:
        should_warn = True
        warn_kind = warn_kind_c
    elif should_warn_a:
        should_warn = True
        warn_kind = '挿入'
    elif should_warn_b:
        should_warn = True
        warn_kind = '差し替え'

    if not should_warn:
        # 沈黙
        sys.exit(0)

    # 警告を出す（2 経路）
    # stderr（人間向け）
    print(
        f'[SessionModeWatch WARN] {sanitize_for_terminal(basename)} '
        f'のモード行に {warn_kind} が検出されました。',
        file=sys.stderr
    )

    # stdout JSON（LLM向け）
    context = (
        f'ファイル: {sanitize_for_terminal(basename)}\n'
        f'検知種別: {warn_kind}\n'
        f'確認: 委任プラン承認直後の挿入か、承認済みプランへの変更かご確認ください。'
    )
    output = {
        'hookSpecificOutput': {
            'hookEventName': 'PostToolUse',
            'additionalContext': context
        }
    }
    print(json.dumps(output), file=sys.stdout)

    sys.exit(0)


if __name__ == '__main__':
    main()
