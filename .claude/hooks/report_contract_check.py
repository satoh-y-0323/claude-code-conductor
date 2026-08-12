#!/usr/bin/env python3
"""PostToolUse hook: タイムスタンプ報告契約の検査（P2・配布対象）。

`.claude/reports/` 直下への Write で、以下 2 レジームのファイル名契約逸脱を warn する。

**strict-4（タイムスタンプ形式のみ許容）** — 以下 4 prefix:
  - requirements-report-
  - architecture-report-
  - plan-report-
  - design-review-report-

判定は basename 全体のフルマッチ `^{prefix}\\d{8}-\\d{6}\\.md$` で行う
（prefix 一致と suffix 一致の独立 2 条件ではない。`plan-report-badname-extra-20260725-180252.md`
のように prefix と タイムスタンプの間へ任意文字列を挟む名前も逸脱として検出するため）。

**loose-3（タイムスタンプ形式 または task_id 形式のどちらかを許容＝緩検査）** — 以下 3 prefix:
  - code-review-report-
  - security-review-report-
  - test-report-

CR / SR / test レポートは「タイムスタンプ採番」と「task_id 採番（並列 wave のファイル名衝突
回避）」の 2 レジームを持つため、どちらか一方を強制しない。以下 2 形式のいずれかに一致すれば
沈黙し、どちらにも一致しなければ警告する:
  - タイムスタンプ形式: `^{prefix}\\d{8}-\\d{6}\\.md$`
  - task_id 形式: `^{prefix}[英数 . _ -]{1,200}\\.md$`

`debug-analysis-` / `debug-needed-` はどちらのレジームにも属さず警告対象外。
不一致の場合は 2 経路で警告: stderr（人間向け）+ stdout JSON（LLM向け）。

## ディレクトリ判定とケース非依存化

reports ディレクトリの判定は `.claude/reports/` 直下かどうかを名前ベースで確認する。
Windows・macOS の既定（ケース非依存 FS）では `.CLAUDE` や `REPORTS` は `.claude` / `reports` と
同一実体の別名であり、名前比較は `.lower()` を使用してケース非依存で行う。
basename の判定は 2 レジームとも厳密に行う（`re.IGNORECASE` 不使用・`re.ASCII` で Unicode 偽装を
防止）。strict-4 は prefix・タイムスタンプ・`.md` の厳密フルマッチ、loose-3 は
タイムスタンプ形式 または task_id 形式（許容文字は ASCII 英数と `.` `_` `-` のみ）の
どちらかへの厳密フルマッチであり、緩いのは「許容形式が 2 つある」点のみで
各形式の照合自体は厳密である。

## 出力契約（PostToolUse warn の標準形）

exit 0 でも stdout JSON が LLM コンテキストへ additionalContext として注入される。
stderr は人間にのみ届く。詳細は Claude Code 公式仕様を参照:
  https://code.claude.com/docs/en/hooks
"""

import json
import os
import re
import sys
from pathlib import Path

# 共通ヘルパー (_hook_utils.sanitize_for_terminal, norm_component, STRICT4_PREFIXES,
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

# strict-4: STRICT4_PREFIXES は _hook_utils から import。
# loose-3: タイムスタンプ形式 または task_id 形式のどちらかを許容する prefix。
# 注: タイムスタンプ形式（YYYYMMDD-HHMMSS）は task_id 形式の真部分集合であり、
# 判定としては task_id 形式 1 本で足りる（2 形式併記は利用者向けの説明のため）。
LOOSE3_PREFIXES = (
    'code-review-report-',
    'security-review-report-',
    'test-report-',
)

# task_id の許容文字集合と長さ上限（英数と `.` `_` `-` のみ 1〜200 文字）。
TASK_ID_MAX_LEN = 200


def task_id_pattern(prefix):
    """`{prefix}{task_id}.md` のフルマッチ用パターンを返す。

    task_id は ASCII 英数と `.` `_` `-` のみ 1〜TASK_ID_MAX_LEN 文字。
    文字集合をリテラルで ASCII に限定したうえで、`re.ASCII` も明示して意図を固定する。
    """
    return re.compile(
        re.escape(prefix) + r'[A-Za-z0-9._-]{1,' + str(TASK_ID_MAX_LEN) + r'}\.md',
        re.ASCII,
    )


def emit_warning(basename, expected_format, stderr_hint, context_hint):
    """契約逸脱を 2 経路（stderr = 人間向け / stdout JSON = LLM 向け）で警告する。

    basename は外部由来のため、両経路とも sanitize_for_terminal() を通してから出力する。
    strict-4 / loose-3 で期待形式・採番案内の文面のみが変わり、出力の骨格は共通。
    """
    safe_basename = sanitize_for_terminal(basename)

    # stderr（人間向け）
    print(
        f'[ReportContract WARN] {safe_basename} '
        f'のファイル名形式が違反しています。'
        f'期待形式: {expected_format}\n'
        f'{stderr_hint}',
        file=sys.stderr
    )

    # stdout JSON（LLM向け）
    context = (
        f'ファイル名: {safe_basename}\n'
        f'期待形式: {expected_format}\n'
        f'採番方法: {context_hint}'
    )
    output = {
        'hookSpecificOutput': {
            'hookEventName': 'PostToolUse',
            'additionalContext': context
        }
    }
    print(json.dumps(output), file=sys.stdout)


def main():
    # JSON payload の読み込み（fail-open）
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    # tool_name の確認（Write 以外は対象外＝新規命名は Write でのみ発生）
    tool_name = payload.get('tool_name', '')
    if tool_name != 'Write':
        sys.exit(0)

    # file_path の抽出（キー欠落は fail-open）
    file_path = payload.get('tool_input', {}).get('file_path', '')
    if not file_path:
        sys.exit(0)

    # `.claude/reports/` 直下かどうかの判定
    path_obj = Path(file_path)
    basename = path_obj.name
    parent = path_obj.parent

    # 構造判定: 親が `reports` かつ祖父が `.claude`（= `.claude/reports/` 直下）。
    # 上流に同名の `reports/` があっても誤アンカーしない（resolve は使わない）。
    # ケース非依存 FS（Windows・macOS）では `.CLAUDE` や `REPORTS` は `.claude` / `reports` と
    # 同一実体の別名のため、比較は norm_component() で正規化してから行う。
    # Windows の末尾ドット・スペースも norm_component() が除去する。
    # 例: `/foo/.claude/reports/plan-report-20260725-180252.md` → 対象
    # 例: `/foo/.claude/reports/archive/plan-report-final.md` → 対象外（直下でない）
    if norm_component(parent.name) != 'reports' or norm_component(parent.parent.name) != '.claude':
        sys.exit(0)

    # basename を正規化（ケース非依存化・末尾ドット/スペース除去）してから prefix 判定。
    # report_path_guard.py と同じポリシーで統一。
    # **重要**: prefix 判定は norm_basename で行うが、形式マッチング（拡張子検証含む）は
    # 元の basename に対して行う。これにより大文字混じり prefix（Test-Report-）は検出
    # しつつ、`.MD` 拡張子のような大文字混じりは厳密に reject できる。
    norm_basename = norm_component(basename)

    # strict-4: 対象 prefix の確認 → basename 全体のフルマッチ確認
    # （`{prefix}YYYYMMDD-HHMMSS.md` のみ許容）
    for prefix in STRICT4_PREFIXES:
        if not norm_basename.startswith(prefix):
            continue
        # 形式マッチングは元の basename に対して行う（`.md` 拡張子の厳密化）
        if timestamp_pattern(prefix).fullmatch(basename):
            # 正しい形式
            sys.exit(0)
        emit_warning(
            basename,
            '{prefix}YYYYMMDD-HHMMSS.md',
            '採番は report-timestamp skill を使用してください。',
            'report-timestamp skill で取得したタイムスタンプを使用してください。',
        )
        sys.exit(0)

    # loose-3: 対象 prefix の確認 → 2 形式のいずれかへのフルマッチ確認
    # （`{prefix}YYYYMMDD-HHMMSS.md` または `{prefix}{task_id}.md`）
    for prefix in LOOSE3_PREFIXES:
        if not norm_basename.startswith(prefix):
            continue
        # 形式マッチングは元の basename に対して行う（`.md` 拡張子の厳密化）
        if (timestamp_pattern(prefix).fullmatch(basename)
                or task_id_pattern(prefix).fullmatch(basename)):
            # どちらかの正しい形式
            sys.exit(0)
        emit_warning(
            basename,
            '{prefix}YYYYMMDD-HHMMSS.md または {prefix}{task_id}.md'
            f'（task_id は英数と . _ - のみ 1〜{TASK_ID_MAX_LEN} 文字）',
            'タイムスタンプ採番は report-timestamp skill を、'
            'task_id 採番は plan-report の task_id を使用してください。',
            'タイムスタンプ形式なら report-timestamp skill で取得した値を、'
            'task_id 形式なら plan-report の task_id を使用してください。',
        )
        sys.exit(0)

    # どちらのレジームにも属さない prefix（debug 系・自由域）は対象外
    sys.exit(0)


if __name__ == '__main__':
    main()
