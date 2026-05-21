#!/usr/bin/env python3
"""PostToolUse hook: plan-report の YAML frontmatter 機械検査（配布対象）。

`.claude/reports/plan-report-*.md` への Write/Edit を検出し、汎用ルールに違反していれば
WARN を出す。出力は 2 経路:

- **stderr**: 人間（ターミナル表示）向け [PlannerCheck WARN] テキスト
- **stdout JSON**: `hookSpecificOutput.additionalContext` で LLM に system reminder として注入

Claude Code 公式仕様（https://code.claude.com/docs/en/hooks）に従い、PostToolUse の
exit 0 では stdout JSON のみが LLM コンテキストに伝達される。stderr は人間にしか
届かないため、両方出力して二重防衛とする。

## 検査ルール（配布対象）

- **R2**: `agent: code-reviewer` / `security-reviewer` の task の writes パスに
  タイムスタンプ（YYYYMMDD 単独、または YYYYMMDD-HHMMSS）が含まれていれば WARN。
  parallel-agents skill の成果物取り込みでタイムスタンプを動的に取得すると
  writes と実ファイル名が乖離するため、task_id ベースの固定名にする。
- **R4**: 同一 writes パスを複数 task が宣言していて、後発 task の depends_on に
  先発 task が含まれていない場合は WARN（並列実行で破壊的競合）。
- **R6**: plan-report のタスク総数 >= 3 かつ `agent: code-reviewer` /
  `agent: security-reviewer` が 0 件の場合は WARN（レビュー全削除検出）。
  2026-05-21 のルール違反 2 対策として追加。

## 開発元のみのルール

- **R3**（`src/c3/_template/` への writes 禁止）は C3 開発リポジトリ固有のため
  `.dev/hooks/_planner_check.py` に分離して開発元でのみ動作。
"""

import json
import os
import re
import sys
from pathlib import Path

# 共通ヘルパー (_hook_utils.write_debug_log) を hooks/ 経由で import するため、
# このスクリプトのディレクトリを PYTHONPATH に追加する。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hook_utils import write_debug_log  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

try:
    import yaml
except ImportError:
    # PyYAML 未インストール環境では silent exit
    sys.exit(0)


# プロジェクトルートと debug ログのパスはスクリプトファイルからの絶対パスに固定する。
# cwd 依存だと worktree コンテキストで `.claude/tmp/` が意図しない場所に作られる。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEBUG_LOG_PATH = PROJECT_ROOT / ".claude" / "tmp" / "planner_check_debug.log"

# DoS 防御の読み取り上限（hook はローカル前提だが多層防御として明示する）。
_STDIN_READ_LIMIT_BYTES = 1 * 1024 * 1024  # 1 MiB
_FILE_READ_LIMIT_BYTES = 512 * 1024        # 512 KiB


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
# YYYYMMDD（8 桁）または YYYYMMDD-HHMMSS（8 桁 + ハイフン + 6 桁）。
# 前後が英数字なら task_id の一部とみなし誤検知を回避する。
# 注意: ハイフン区切りで孤立した 8 桁数字（例: report-12345678.md）は YYYYMMDD と
# 区別できないため意図的に WARN を発火させる（test_planner_check.py
# test_8digit_standalone_in_filename_triggers_warn 参照）。
# task_id に 8 桁数字のみを使う場合は英数字混在 ID に変更すること。
_TIMESTAMP_RE = re.compile(r"(?<![A-Za-z0-9])\d{8}(?:-\d{6})?(?![A-Za-z0-9])")

# R6 検出の閾値: タスク総数がこの値以上で reviewer 0 件なら WARN を出す
_R6_TASK_THRESHOLD = 3

_REVIEWER_AGENTS = frozenset({"code-reviewer", "security-reviewer"})


def _sanitize(s: str) -> str:
    """ターミナルインジェクション対策: 制御文字と JSON 互換性を壊す Unicode 行区切りを除去する。

    除去対象:
      - C0/C1 制御文字 (\\x00-\\x1f) と DEL (\\x7f) — ANSI エスケープ (ESC 0x1b) を含む
      - U+2028 (Line Separator) / U+2029 (Paragraph Separator) — 一部の JS/JSON
        パーサが行区切りとして扱い、ensure_ascii=False の JSON 解析を破壊する
    """
    return re.sub("[\x00-\x1f\x7f\u2028\u2029]", "", str(s))


def _normalize(path: str) -> str:
    """Windows のバックスラッシュを `/` に統一する。"""
    return path.replace("\\", "/")


def _is_plan_report(file_path: str) -> bool:
    """basename が `plan-report-*.md` で、パスに `..` トラバーサルを含まない。

    `..` セグメントを含むパス（``../../plan-report-foo.md`` 等）は後段の
    ``open(file_path)`` で任意ファイル読み取りの経路になりうるため拒否する。
    """
    normalized = _normalize(file_path)
    if ".." in normalized.split("/"):
        return False
    basename = os.path.basename(normalized)
    return basename.startswith("plan-report-") and basename.endswith(".md")


def _extract_frontmatter(text: str) -> dict | None:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _check_r2_reviewer_timestamp(task: dict) -> list[str]:
    """reviewer の writes にタイムスタンプ入りファイル名があれば違反メッセージ。"""
    if task.get("agent") not in _REVIEWER_AGENTS:
        return []
    writes = task.get("writes")
    if not isinstance(writes, list):
        return []
    violations: list[str] = []
    for w in writes:
        if not isinstance(w, str):
            continue
        basename = os.path.basename(_normalize(w))
        if _TIMESTAMP_RE.search(basename):
            violations.append(
                f"R2: task {task.get('id')!r} ({task.get('agent')}) の writes "
                f"{w!r} にタイムスタンプ風パターンが含まれています。"
                "task_id ベースのファイル名にしてください"
            )
    return violations


def _build_ancestor_map(by_id: dict[str, dict]) -> dict[str, set[str]]:
    """各 task の depends_on 推移閉包（祖先集合）を返す。"""
    ancestors: dict[str, set[str]] = {tid: set() for tid in by_id}
    for tid in by_id:
        stack = list(by_id[tid].get("depends_on", []) or [])
        while stack:
            dep = stack.pop()
            if dep in ancestors[tid]:
                continue
            ancestors[tid].add(dep)
            dep_task = by_id.get(dep)
            if dep_task:
                stack.extend(dep_task.get("depends_on", []) or [])
    return ancestors


def _all_pairs_transitively_ordered(
    claim_ids: list[str], ancestors: dict[str, set[str]]
) -> bool:
    """claim_ids 内の全ペアが depends_on 推移閉包で順序付けされているか。"""
    for i, a in enumerate(claim_ids):
        for b in claim_ids[i + 1:]:
            if a in ancestors.get(b, set()) or b in ancestors.get(a, set()):
                continue
            return False
    return True


def _check_r4_writes_conflicts(tasks: list) -> list[str]:
    """同一 writes パスを宣言する複数 task が depends_on で順序付けされていなければ警告。"""
    by_id: dict[str, dict] = {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        tid = task.get("id")
        if isinstance(tid, str) and tid:
            by_id[tid] = task

    claims: dict[str, list[str]] = {}
    for tid, task in by_id.items():
        writes = task.get("writes")
        if not isinstance(writes, list):
            continue
        for w in writes:
            if not isinstance(w, str):
                continue
            claims.setdefault(_normalize(w), []).append(tid)

    ancestors = _build_ancestor_map(by_id)

    violations: list[str] = []
    for path, claim_ids in claims.items():
        if len(claim_ids) < 2:
            continue
        if _all_pairs_transitively_ordered(claim_ids, ancestors):
            continue
        # claim_ids は task id の生 join のため、特殊文字を repr で逃がして安全に出力する
        joined = ", ".join(repr(cid) for cid in sorted(claim_ids))
        violations.append(
            f"R4: writes {path!r} を複数 task ({joined}) "
            "が宣言していますが depends_on で順序付けされていません。"
            "並列実行で破壊的競合が起きる可能性があります"
        )
    return violations


def _check_r6_reviewer_absence(tasks: list) -> list[str]:
    """plan-report 内のレビュータスクが完全消失している場合の警告。

    検出条件: タスク総数 >= _R6_TASK_THRESHOLD AND reviewer 系 agent が 0 件。
    閾値が 3 なのは、小規模な単発タスク（ドキュメント修正など）の合理的な省略を巻き込まないため。
    """
    if not isinstance(tasks, list) or len(tasks) < _R6_TASK_THRESHOLD:
        return []
    reviewer_tasks = [
        t for t in tasks
        if isinstance(t, dict) and t.get("agent") in _REVIEWER_AGENTS
    ]
    if reviewer_tasks:
        return []
    return [
        f"R6: plan-report のタスク総数 {len(tasks)} 件に対して "
        "code-reviewer / security-reviewer タスクが 0 件です。"
        "レビュータスクを意図的に省略する場合はユーザー承認を取ってください"
    ]


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read(_STDIN_READ_LIMIT_BYTES))
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    if payload.get("tool_name") not in ("Write", "Edit"):
        sys.exit(0)

    file_path = payload.get("tool_input", {}).get("file_path", "")
    if not isinstance(file_path, str) or not file_path:
        sys.exit(0)

    if not _is_plan_report(file_path):
        sys.exit(0)

    basename = os.path.basename(_normalize(file_path))

    try:
        with open(file_path, encoding="utf-8") as fh:
            text = fh.read(_FILE_READ_LIMIT_BYTES)
    except (OSError, UnicodeDecodeError):
        sys.exit(0)

    frontmatter = _extract_frontmatter(text)
    if frontmatter is None:
        sys.exit(0)

    tasks = frontmatter.get("tasks")
    if not isinstance(tasks, list):
        sys.exit(0)

    warnings: list[str] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        warnings.extend(_check_r2_reviewer_timestamp(task))

    warnings.extend(_check_r4_writes_conflicts(tasks))
    warnings.extend(_check_r6_reviewer_absence(tasks))

    if warnings:
        # 検出されたルール名（R2/R4/R6）を抽出してデバッグログに記録
        detected_rules = sorted({m.split(":")[0] for m in warnings if ":" in m})
        write_debug_log(DEBUG_LOG_PATH, f"{basename} WARN {','.join(detected_rules)}")
        # 経路1: stderr に人間向けメッセージ（ターミナル表示用）
        print("[PlannerCheck WARN] plan-report の検査で違反を検出しました:",
              file=sys.stderr)
        sanitized_warnings = [_sanitize(msg) for msg in warnings]
        for msg in sanitized_warnings:
            print(f"  - {msg}", file=sys.stderr)

        # 経路2: stdout JSON で LLM コンテキストに system reminder として注入
        # Claude Code が hookSpecificOutput.additionalContext を読み取り、
        # LLM のコンテキストウィンドウに挿入する（公式仕様）
        additional_context = (
            "[PlannerCheck WARN] plan-report の検査で違反を検出しました:\n"
            + "\n".join(f"  - {msg}" for msg in sanitized_warnings)
            + "\n\n"
            + "ルールの詳細は .claude/rules/plan-design-guidelines.md を参照。"
            + "意図的に許容する場合はユーザーに確認を取ること。"
        )
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": additional_context,
            }
        }
        print(json.dumps(output, ensure_ascii=False))
    else:
        write_debug_log(DEBUG_LOG_PATH, f"{basename} PASS")

    sys.exit(0)


if __name__ == "__main__":
    main()
