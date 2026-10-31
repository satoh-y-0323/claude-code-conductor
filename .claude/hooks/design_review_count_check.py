#!/usr/bin/env python3
"""PostToolUse hook: design-review-report のサマリ件数と本文の見出しの数の照合（配布対象）。

`.claude/reports/` 直下の ``design-review-report-*.md`` への Write / Edit を検出し、
ディスク上の本文を読み直して、サマリ表の重要度別の件数と本文の指摘見出しの数が
食い違っていれば警告する（警告のみ・ブロックしない）。

レポートの形式の正本は ``.claude/skills/dev-workflow/references/design-critic-rubric.md``
の「design-review-report 出力形式」節である。本 hook は形式をここに写さず、
数え方の規則は ``check_counts`` の実装とそのテスト
（``tests/hooks/test_design_review_count_check.py``）が定める。

## 出力（食い違いのときだけ）

- **stderr**: 人間（ターミナル表示）向けの警告
- **stdout JSON**: ``hookSpecificOutput.additionalContext`` で LLM へ注入する

PostToolUse の exit 0 では stdout JSON のみが LLM コンテキストへ届き、stderr は人間にしか
届かないため両方へ出す（planner_check.py と同じ二経路）。

## fail 方針

警告の hook のため異常時は fail-open（exit 0・沈黙）。対象は次のとおり:

- 壊れた stdin・Write / Edit 以外・``file_path`` の欠落や不正
- 対象外のパス（判定は ``norm_component`` で正規化した basename と
  ``.claude/reports/`` 直下の形で行う）
- 開けない・UTF-8 として読めないファイル
- 上限（512 KiB・バイト）を超えるファイル（途中で切った本文で判定すると末尾の見出しを
  取りこぼして誤警告するため、判定しない）

サマリ表の数値セルが整数に変換できない（例: 桁数が多すぎる）場合は、その行を
「読めない」扱いにして照合を続ける（例外は送出しない。行が欠けるため食い違いとして警告する）。
"""

import json
import os
import re
import sys
from pathlib import Path

# 共通ヘルパーを hooks/ 経由で import するため、このスクリプトのディレクトリを
# PYTHONPATH に追加する。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hook_utils import (  # noqa: E402
    is_reports_direct_child,
    norm_component,
    sanitize_for_terminal,
    write_debug_log,
)

try:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


# プロジェクトルートと debug ログのパスはスクリプトファイルからの絶対パスに固定する
# （planner_check.py と同じ。cwd 依存だと worktree で意図しない場所に作られる）。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEBUG_LOG_PATH = PROJECT_ROOT / ".claude" / "tmp" / "design_review_count_check_debug.log"

# DoS 防御の読み取り上限（planner_check.py と同値）。
_STDIN_READ_LIMIT_CHARS = 1 * 1024 * 1024  # 1M chars
_FILE_READ_LIMIT_BYTES = 512 * 1024        # 512 KiB

_REPORT_PREFIX = "design-review-report-"
_REPORT_SUFFIX = ".md"

SEVERITIES = ("High", "Medium", "Low")
TOTAL = "合計"
_TABLE_LABELS = SEVERITIES + (TOTAL,)

# 行頭（先頭空白可）の ``` か ~~~ でフェンスを開閉する。閉じるのは開いたのと同じ文字の
# フェンスのみ（Markdown の解釈に合わせる。``` の中の ~~~ では閉じない）。
# コードフェンスの行（CommonMark のフェンスの規則）。開きは 3 文字以上の ` か ~ で、
# ` のフェンスの info string には ` を含まない。閉じは開きと同じ文字・開き以上の長さで、
# 後ろは空白だけ（info string 付きの行や短い行は閉じず、フェンスの中身になる）。
# 字下げはリストの中のフェンスを拾うため制限しない（4 文字以上の字下げをコードブロックとして
# 扱う規則は、リストの入れ子を追わない本 hook では採らない）。
_FENCE_RE = re.compile(r"^\s*(`{3,}(?=[^`]*$)|~{3,})(.*)$")

# 見出しレベル 3 の `[DC-<ID>]` 直後の括弧（全角・半角）の先頭の重要度（太字可）。
_HEADING_RE = re.compile(
    r"^###[ \t]+\[DC-[^\]\s]+\][ \t]*[（(][ \t]*(?:\*\*)?(High|Medium|Low)(?![A-Za-z])"
)

# サマリ表のヘッダ: 1 番目のセルが「重要度」・2 番目のセルが「件数」で始まる。
_TABLE_HEADER_RE = re.compile(r"^\s*\|\s*重要度\s*\|\s*件数[^|]*\|")

_NUMBER_RE = re.compile(r"\d+", re.ASCII)


def _unfenced_lines(text: str) -> list[str]:
    """コードフェンスの外の行だけを返す。閉じていないフェンスは末尾まで続く。"""
    out: list[str] = []
    open_fence: str | None = None
    for line in text.splitlines():
        m = _FENCE_RE.match(line)
        if open_fence is None:
            if m:
                open_fence = m.group(1)
                continue
            out.append(line)
        elif (
            m
            and m.group(1)[0] == open_fence[0]
            and len(m.group(1)) >= len(open_fence)
            and not m.group(2).strip()
        ):
            open_fence = None
    return out


def _count_headings(lines: list[str]) -> dict:
    counts = {sev: 0 for sev in SEVERITIES}
    for line in lines:
        m = _HEADING_RE.match(line)
        if m:
            counts[m.group(1)] += 1
    return counts


def _split_cells(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip() for cell in body.split("|")]


def _strip_bold(cell: str) -> str:
    if len(cell) >= 4 and cell.startswith("**") and cell.endswith("**"):
        return cell[2:-2].strip()
    return cell


def _read_table(lines: list[str]) -> dict | None:
    """最初のサマリ表の High/Medium/Low/合計 行の 2 番目のセルを読む。表が無ければ None。"""
    start = None
    for i, line in enumerate(lines):
        if _TABLE_HEADER_RE.match(line):
            start = i
            break
    if start is None:
        return None

    values: dict = {}
    for line in lines[start + 1:]:
        if not line.strip().startswith("|"):
            break  # 表の終わり
        cells = _split_cells(line)
        if len(cells) < 2:
            continue
        label = _strip_bold(cells[0])
        if label not in _TABLE_LABELS or label in values:
            continue
        value = _strip_bold(cells[1])
        if _NUMBER_RE.fullmatch(value):
            try:
                values[label] = int(value)
            except ValueError:
                # 桁数が整数文字列変換の上限（sys.get_int_max_str_digits）を超える等。
                # 読めない行として扱う（例外で hook を落とさない）
                pass
        # 数値でない行は無いものとして扱う
    return values


def check_counts(text: str) -> tuple[bool, dict, dict | None]:
    """サマリ表の件数と本文の見出しの数を照合する。

    戻り値は ``(一致か, 見出しの数, 表の値 | None)``。見出しの数は常に High/Medium/Low の
    3 キーを持つ。表の値は読めた行だけを持つ（欠けた行はキーが無い）。
    """
    lines = _unfenced_lines(text)
    headings = _count_headings(lines)
    table = _read_table(lines)

    if table is None:
        return sum(headings.values()) == 0, headings, None

    if any(label not in table for label in _TABLE_LABELS):
        return False, headings, table
    ok = all(table[sev] == headings[sev] for sev in SEVERITIES) and table[TOTAL] == sum(
        headings.values()
    )
    return ok, headings, table


def _format_table(table: dict | None) -> str:
    if table is None:
        return "サマリ表なし"
    parts = [
        f"{label} {table[label]}" if label in table else f"{label} 読めない"
        for label in _TABLE_LABELS
    ]
    return " / ".join(parts)  # nul-boundary: allow(LLM・人間へ出す表示用の一行。行集合として再パースしない)


def _format_headings(headings: dict) -> str:
    parts = [f"{sev} {headings[sev]}" for sev in SEVERITIES]
    parts.append(f"{TOTAL} {sum(headings.values())}")
    return " / ".join(parts)  # nul-boundary: allow(LLM・人間へ出す表示用の一行。行集合として再パースしない)


def _is_target(path: Path) -> bool:
    # ケース非依存 FS の別名・末尾ドット/スペースは同じ実体を指すため、
    # report_contract_check と同じく norm_component で正規化した basename で判定する
    name = norm_component(path.name)
    return (
        name.startswith(_REPORT_PREFIX)
        and name.endswith(_REPORT_SUFFIX)
        and is_reports_direct_child(path)
    )


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read(_STDIN_READ_LIMIT_CHARS))
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)
    if not isinstance(payload, dict):
        sys.exit(0)

    if payload.get("tool_name") not in ("Write", "Edit"):
        sys.exit(0)

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        sys.exit(0)
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path or "\x00" in file_path:
        sys.exit(0)

    path = Path(file_path)
    if not _is_target(path):
        sys.exit(0)

    # 本文はディスクから読み直す（Edit の payload は差分のみで、Write の content も
    # 書き込み後の実体と一致する保証がないため）。
    # 上限はバイトで測る。上限 + 1 バイトまで読み、超えていれば判定しない。
    try:
        with open(file_path, "rb") as fh:
            data = fh.read(_FILE_READ_LIMIT_BYTES + 1)
    except (OSError, ValueError):
        sys.exit(0)
    if len(data) > _FILE_READ_LIMIT_BYTES:
        sys.exit(0)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        sys.exit(0)

    ok, headings, table = check_counts(text)
    basename = path.name
    if ok:
        write_debug_log(DEBUG_LOG_PATH, f"{basename} PASS")
        sys.exit(0)

    write_debug_log(DEBUG_LOG_PATH, f"{basename} WARN")

    safe_name = sanitize_for_terminal(basename)
    table_line = _format_table(table)
    headings_line = _format_headings(headings)

    print(
        f"[DesignReviewCount WARN] {safe_name}: サマリ表の件数と本文の指摘見出しの数が"
        "食い違っています。\n"
        f"  サマリ表: {table_line}\n"
        f"  本文の見出し: {headings_line}",
        file=sys.stderr,
    )

    additional_context = sanitize_for_terminal(
        f"[DesignReviewCount WARN] {safe_name} のサマリ表の件数と本文の指摘見出し"
        "（### [DC-...]（重要度…））の数が食い違っています。"
    ) + "\n" + (
        f"- サマリ表の値: {table_line}\n"
        f"- 本文の見出しの数: {headings_line}\n"
        "このレポートを書いた本人なら、サマリ表を本文の見出しの数に合わせて Edit で直すこと"
        "（自分が付けた見出しの重要度の方が誤記なら、その重要度表記に限って見出しを直す）。"
        "書いた本人でない（裁定する親など）なら、サマリ表を書き換えず、"
        "件数は本文の見出しから数えて使うこと。"
    )
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": additional_context,
        }
    }
    print(json.dumps(output, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
