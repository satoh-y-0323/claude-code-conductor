#!/usr/bin/env python3
"""PostToolUse hook: skills/ 変更通知 + デバッグ出力 / TODO / FIXME / XXX 検出.

統合された 2 つの責務:

1. skills/ 変更通知（旧 validate_skill_change.py 由来）:
   `.claude/skills/` 配下のファイルを Write / Edit したとき「実際のエージェント
   動作で確認してください」というリマインダを stdout に出す。

2. デバッグ出力検出（F-007）:
   Write / Edit 完了後に対象ファイルへ console.log / print( / TODO /
   FIXME / XXX を検出して stderr に警告する。code-review-checklist.md の
   「不要なデバッグ出力が残っていないか」項目を自動化する位置付け。

両者とも **警告のみ・ブロックしない**（exit 0）。判断は人間に委ねる方針。
"""

import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except AttributeError:
    pass


# 対象拡張子（小文字で比較）
_TARGET_EXTENSIONS = frozenset({
    '.py', '.js', '.ts', '.tsx', '.jsx', '.cs', '.go', '.rs',
})

# サイズ上限（256 KB）。これを超えるファイルは先頭のみスキャンする。
_MAX_SCAN_BYTES = 256 * 1024

# バイナリ判定用のサンプルバイト数
_BINARY_SAMPLE_BYTES = 8 * 1024

# 検出パターン
# (pattern_name, regex, applicable_extensions)
# applicable_extensions が None なら全対象拡張子に適用。
_QUALITY_PATTERNS: list[tuple[str, "re.Pattern[str]", "frozenset[str] | None"]] = [
    ('console.log', re.compile(r'console\.log\('), frozenset({'.js', '.ts', '.tsx', '.jsx'})),
    ('print', re.compile(r'^\s*print\('), frozenset({'.py'})),
    ('TODO', re.compile(r'\bTODO\b'), None),
    ('FIXME', re.compile(r'\bFIXME\b'), None),
    ('XXX', re.compile(r'\bXXX\b'), None),
]


def _scan_file(file_path: str, max_bytes: int = _MAX_SCAN_BYTES) -> list[tuple[int, str, str]]:
    """ファイルを行単位でスキャンし、ヒットしたパターンのリストを返す。

    返り値: [(line_no, pattern_name, line_excerpt), ...]
    対象外拡張子・バイナリ・存在しないファイルは空リストを返す。
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in _TARGET_EXTENSIONS:
        return []

    if not os.path.isfile(file_path):
        return []

    try:
        # バイナリ判定: 先頭 8KB に NUL バイトが含まれていればスキップ
        with open(file_path, 'rb') as f:
            sample = f.read(_BINARY_SAMPLE_BYTES)
        if b'\x00' in sample:
            return []

        # テキストとして先頭 max_bytes だけ読む（大ファイル対策）
        with open(file_path, 'rb') as f:
            raw = f.read(max_bytes)
        text = raw.decode('utf-8', errors='replace')
    except OSError:
        return []

    findings: list[tuple[int, str, str]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for pattern_name, regex, applicable_exts in _QUALITY_PATTERNS:
            if applicable_exts is not None and ext not in applicable_exts:
                continue
            if regex.search(line):
                excerpt = line.strip()
                if len(excerpt) > 80:
                    excerpt = excerpt[:77] + '...'
                findings.append((line_no, pattern_name, excerpt))
    return findings


def _check_skills_change(file_path: str) -> None:
    """`.claude/skills/` 配下を変更した場合、動作確認を促すメッセージを stdout に出す.

    旧 validate_skill_change.py 由来のロジック。skills/ 変更は実エージェントの
    挙動に直接影響するため、テストファイルだけでなく実動作の確認を促す。
    """
    normalized = file_path.replace('\\', '/')
    if '.claude/skills/' not in normalized:
        return

    skill_name = os.path.basename(file_path)
    skill_name = re.sub(r'[^\x20-\x7e　-鿿]', '', skill_name)
    print(f'[C3] .claude/skills/{skill_name} を変更しました。実際のエージェント動作で確認してください。')


def _check_quality_patterns(file_path: str) -> None:
    """ファイル内のデバッグ出力 / TODO 等を stderr に警告する."""
    findings = _scan_file(file_path)
    if not findings:
        return

    # ターミナルインジェクション対策: ファイル名表示前にサニタイズ
    safe_name = os.path.basename(file_path)
    safe_name = re.sub(r'[^\x20-\x7e　-鿿]', '', safe_name)

    for line_no, pattern_name, excerpt in findings:
        # excerpt も同様にサニタイズ
        safe_excerpt = re.sub(r'[^\x20-\x7e　-鿿]', '', excerpt)
        print(
            f'[C3 quality] {safe_name}:{line_no} {pattern_name} を検出: {safe_excerpt}',
            file=sys.stderr,
        )


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return

    if payload.get('tool_name') not in ('Write', 'Edit'):
        return

    file_path = payload.get('tool_input', {}).get('file_path', '')
    if not isinstance(file_path, str) or not file_path:
        return

    _check_skills_change(file_path)
    _check_quality_patterns(file_path)


if __name__ == '__main__':
    sys.exit(main() or 0)
