#!/usr/bin/env python3
"""PostToolUse hook: タイムスタンプ報告契約の検査（P2・配布対象）。

`.claude/reports/` 直下への Write で、以下 4 prefix のタイムスタンプ契約逸脱を warn する:
  - requirements-report-
  - architecture-report-
  - plan-report-
  - design-review-report-

判定は basename 全体のフルマッチ `^{prefix}\\d{8}-\\d{6}\\.md$` で行う
（prefix 一致と suffix 一致の独立 2 条件ではない。`plan-report-badname-extra-20260725-180252.md`
のように prefix と タイムスタンプの間へ任意文字列を挟む名前も逸脱として検出するため）。
不一致の場合は 2 経路で警告: stderr（人間向け）+ stdout JSON（LLM向け）。

## ディレクトリ判定とケース非依存化

reports ディレクトリの判定は `.claude/reports/` 直下かどうかを名前ベースで確認する。
Windows・macOS の既定（ケース非依存 FS）では `.CLAUDE` や `REPORTS` は `.claude` / `reports` と
同一実体の別名であり、名前比較は `.lower()` を使用してケース非依存で行う。
basename（prefix・タイムスタンプ・`.md`）の判定は厳密に行う（`re.ASCII` で Unicode 偽装を防止）。

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

# 共通ヘルパー (_hook_utils.sanitize_for_terminal, norm_component) を hooks/ 経由で import するため、
# このスクリプトのディレクトリを PYTHONPATH に追加する。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hook_utils import norm_component, sanitize_for_terminal  # noqa: E402

try:
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass


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

    # strict-4: 対象 prefix の確認
    strict4_prefixes = [
        'requirements-report-',
        'architecture-report-',
        'plan-report-',
        'design-review-report-',
    ]

    matched_prefix = None
    for prefix in strict4_prefixes:
        if basename.startswith(prefix):
            matched_prefix = prefix
            break
    if matched_prefix is None:
        sys.exit(0)

    # basename 全体のフルマッチ確認（`{prefix}YYYYMMDD-HHMMSS.md`）
    # re.ASCII を指定してタイムスタンプを ASCII 数字に限定し、全角数字による偽装を防止する。
    contract_pattern = re.compile(re.escape(matched_prefix) + r'\d{8}-\d{6}\.md', re.ASCII)
    if contract_pattern.fullmatch(basename):
        # 正しい形式
        sys.exit(0)

    # 違反: 警告を出す（2 経路）
    # stderr（人間向け）
    print(
        f'[ReportContract WARN] {sanitize_for_terminal(basename)} '
        f'のファイル名形式が違反しています。'
        f'期待形式: {{prefix}}YYYYMMDD-HHMMSS.md\n'
        f'採番は report-timestamp skill を使用してください。',
        file=sys.stderr
    )

    # stdout JSON（LLM向け）
    context = (
        f'ファイル名: {sanitize_for_terminal(basename)}\n'
        f'期待形式: {{prefix}}YYYYMMDD-HHMMSS.md\n'
        f'採番方法: report-timestamp skill で取得したタイムスタンプを使用してください。'
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
