#!/usr/bin/env python3
"""Inject past review decisions as hints into a review report.

review-hint: レビュー判断ヒント機能。code-reviewer / security-reviewer が
生成したレビューレポート（`.claude/reports/code-review-report-*.md` /
`security-review-report-*.md`）に、過去の許容例外・対応履歴を後付けで追記する。

特徴:
- レビュアー本体の挙動には介入しない（レポート生成後の決定論的な後処理）
- レポート末尾に「## 過去判断ヒント」セクションを追加するだけ
- 元レポート本文は変更しない
- レビュアー間の重複指摘（同一 checklist_id を両方が指摘）も併せて検出してフラグ化

入力:
- 引数 1: code-review-report のパス
- 引数 2 (任意): security-review-report のパス（指定時は重複指摘フラグ判定）

出力:
- 指定された各レポートの末尾に「## 過去判断ヒント」セクションを追記
- exit code: 0 (失敗してもセッションを止めない方針)
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


# レポート末尾に追記する見出し（既存追記との衝突回避用に固定文字列で識別）
HINT_HEADING = "## 過去判断ヒント"

# 6 ヶ月超は「要再評価」フラグを付ける
DEFAULT_REEVAL_DAYS = 30 * 6

# checklist_id を抽出する正規表現（[CR-XX-NNN] / [SR-XX-NNN]、連番は 3 桁以上）。
# 短すぎる連番（[CR-Q-1] 等）は誤抽出を防ぐため対象外とする。
CHECKLIST_ID_RE = re.compile(r"\[((?:CR|SR)-[A-Z]+-\d{3,})\]")


def extract_checklist_ids(report_text: str) -> list[str]:
    """レポートから checklist_id（[CR-XX-NNN] / [SR-XX-NNN]）を抽出する。

    重複は除去するが、出現順を保つ。
    """
    seen: dict[str, None] = {}
    for m in CHECKLIST_ID_RE.finditer(report_text):
        seen.setdefault(m.group(1), None)
    return list(seen.keys())


def _is_old(decided_at_iso: str, days: int = DEFAULT_REEVAL_DAYS) -> bool:
    """decided_at が `days` 日以上古ければ True。"""
    try:
        decided = datetime.fromisoformat(decided_at_iso)
    except ValueError:
        return False
    if decided.tzinfo is None:
        decided = decided.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return (now - decided) > timedelta(days=days)


def _sanitize_md(s: str) -> str:
    """マークダウン構造を崩しうる文字（改行・# / ``` ）を空白に置換する。

    対象文字:
        - ``\\r`` / ``\\n``: 行区切り
        - ``#``: 見出し記号
        - `` ` ``: コードブロック・インラインコード境界
        - ``\\u2028`` (LINE SEPARATOR) / ``\\u2029`` (PARAGRAPH SEPARATOR):
          Python の ``str.splitlines()`` および ECMAScript JSON.parse が行区切りとして扱う Unicode 文字。
          Markdown レンダラー上の実害は軽微だが、record_tier_outcome.py の
          U+2028/U+2029 エスケープとの一貫性のため対象に含める。
        - ``\\x85`` (NEXT LINE / NEL):
          Python の ``str.splitlines()`` が行区切りとして扱う Unicode 文字。
          splitlines() 互換性のため対象に含める。

    DB 由来フィールドをレポート Markdown に埋め込む際の防御。[SR-NEW] / [CR-Q-001]
    """
    # NOTE: ソースコード上は escape 表記で記述し、実体文字を埋め込まない。
    # raw string を使わず通常文字列にすることで \u2028 / \u2029 / \x85 を Python が Unicode に解決する。
    # コードポイント昇順: \r(0D) < \n(0A) < \x85(85) < # < ` < \u2028 < \u2029
    return re.sub("[\r\n\x85#`\u2028\u2029]", " ", str(s))


def build_hint_section(
    decisions_by_id: dict[str, list[dict]],
    *,
    duplicate_ids: set[str] | None = None,
    reeval_days: int = DEFAULT_REEVAL_DAYS,
) -> str:
    """ヒントセクションの Markdown を生成する。

    Args:
        decisions_by_id: ``{checklist_id: [decision_row, ...]}``。
            decision_row は :func:`c3_db.fetch_review_decisions` の戻り値要素。
        duplicate_ids: 両 reviewer が指摘した checklist_id の集合。
        reeval_days: ``[要再評価]`` ラベルを付ける閾値（日数）。

    Returns:
        ``HINT_HEADING`` で始まる Markdown 文字列。
        ヒントが無い場合は空文字列を返す（呼び出し側で追記をスキップする）。
    """
    duplicate_ids = duplicate_ids or set()
    has_decisions = any(rows for rows in decisions_by_id.values())
    if not has_decisions and not duplicate_ids:
        return ""

    lines: list[str] = [HINT_HEADING, ""]

    for checklist_id, rows in decisions_by_id.items():
        if not rows:
            continue
        # サマリ
        total = len(rows)
        fixed = sum(1 for r in rows if r.get("decision") == "fixed")
        accepted = sum(1 for r in rows if r.get("decision") == "accepted")
        deferred = sum(1 for r in rows if r.get("decision") == "deferred")
        lines.append(f"### [{checklist_id}]")
        summary_parts = []
        if fixed:
            summary_parts.append(f"対応 {fixed}")
        if accepted:
            summary_parts.append(f"許容 {accepted}")
        if deferred:
            summary_parts.append(f"保留 {deferred}")
        lines.append(f"- 過去 {total} 件: " + " / ".join(summary_parts))

        # 直近の判断
        latest = rows[0]
        latest_decided = latest.get("decided_at", "")
        flag = " [要再評価]" if _is_old(latest_decided, reeval_days) else ""
        latest_decision = latest.get("decision", "?")
        latest_reason = latest.get("reason") or "(理由未記載)"
        latest_decided_safe = _sanitize_md(latest_decided)
        latest_decision_safe = _sanitize_md(latest_decision)
        latest_reason_safe = _sanitize_md(latest_reason)
        lines.append(
            f"- 直近: {latest_decided_safe}（{latest_decision_safe}、"
            f"理由: {latest_reason_safe}）{flag}"
        )
        lines.append("")

    if duplicate_ids:
        lines.append("### ⚠ 重複指摘フラグ")
        for cid in sorted(duplicate_ids):
            lines.append(f"- `{cid}` は code-reviewer と security-reviewer の両方で指摘されています")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def append_hints_to_report(
    report_path: Path,
    hint_section: str,
) -> bool:
    """レポートの末尾にヒントセクションを追記する。

    既に同一見出しが存在する場合は二重追記を避けて False を返す。
    成功時 True。
    """
    if not hint_section:
        return False
    try:
        text = report_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[review_hint_inject] failed to read {report_path}: {exc}", file=sys.stderr)
        return False

    if HINT_HEADING in text:
        # 二重追記回避
        return False

    new_text = text.rstrip() + "\n\n" + hint_section
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=report_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_text)
        os.replace(tmp_path, report_path)
    except OSError as exc:
        print(f"[review_hint_inject] failed to write {report_path}: {exc}", file=sys.stderr)
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        return False
    return True


def collect_decisions_for_report(report_text: str) -> dict[str, list[dict]]:
    """レポート内の checklist_id を全て抽出し、各 ID の過去判断を取得する。"""
    try:
        from c3 import db as c3_db  # noqa: PLC0415
    except ImportError as exc:
        print(f"[review_hint_inject] c3.db import failed: {exc}", file=sys.stderr)
        return {}

    ids = extract_checklist_ids(report_text)
    decisions: dict[str, list[dict]] = {}
    for cid in ids:
        decisions[cid] = c3_db.fetch_review_decisions(cid)
    return decisions


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(
            "usage: review_hint_inject.py <code-review-report> [<security-review-report>]",
            file=sys.stderr,
        )
        return 0  # セッションを止めない方針

    report_paths: list[Path] = []
    for a in argv:
        p = Path(a)
        if not p.is_file():
            print(f"[review_hint_inject] not a file (skipped): {p}", file=sys.stderr)
            continue
        report_paths.append(p)

    if not report_paths:
        return 0

    # 各レポートから ID を集める（重複指摘判定用）
    ids_by_report: list[tuple[Path, list[str], str]] = []
    all_id_sets: list[set[str]] = []
    for p in report_paths:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        ids = extract_checklist_ids(text)
        ids_by_report.append((p, ids, text))
        all_id_sets.append(set(ids))

    # 重複指摘: 2 つ以上のレポートで現れる ID
    duplicate_ids: set[str] = set()
    if len(all_id_sets) >= 2:
        intersection = all_id_sets[0]
        for s in all_id_sets[1:]:
            intersection = intersection & s
        duplicate_ids = intersection

    # 各レポートにヒント追記
    for p, ids, text in ids_by_report:
        decisions = collect_decisions_for_report(text)
        # このレポートで該当する重複指摘のみ表示
        report_dups = duplicate_ids & set(ids)
        hint = build_hint_section(decisions, duplicate_ids=report_dups)
        if hint:
            append_hints_to_report(p, hint)

    return 0


if __name__ == "__main__":
    sys.exit(main())
