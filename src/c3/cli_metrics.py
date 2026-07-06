"""``c3 metrics`` - P4 効果の総括メトリクス CLI。

事前検出実績（レビュー指摘の fixed/accepted 判断記録）・差し戻しの傾向と帰属・
手戻りコスト概況を read-only で集計して表示する。

主な機能:
- ``c3 metrics``: 3 セクション構成の人間向け出力
- ``c3 metrics --json``: 機械可読 JSON 出力
- ``c3 metrics --since YYYY-MM-DD``: 全セクション共通の下限日付フィルタ
- ``c3 metrics --months N``: 差し戻し傾向（月次）の表示バケット数上限（既定 12）
- ``c3 metrics --examples N``: 事前検出実例の表示件数上限（既定 5）

設計判断（plan-report-20260706-221212.md T4 / architecture-report-20260706-213701.md
§2-4 に準拠）:
- headline の全カウント（fixed_medium_plus・critical/high/medium 内訳・
  fixed_unknown）は ``db.read_review_decision_matrix`` の fixed×severity
  バケットから本モジュールで導出する単一算出源とし、``db.fetch_prevented_findings``
  （実例専用・LIMIT あり）の行数は件数集計に使わない（DC-AM-001）。
- ``role_distribution`` は ``db.read_rework_role_distribution`` の平坦リストを
  review / development / other の 3 分類に本モジュールで振り分ける（DC-AM-003）。
- ``rework.trend`` はヘルパー層（db.py）でゼロ埋め済みのリストを素通しする
  （追加のゼロ埋めはしない・DC-AM-002）。
- ``rework_cost.note`` / ``fix_cycles.note`` はヘルパー層が生成したクリーン
  文言を新規に組み立てず素通しで出力する（DC-AM-001 round 4/5・ADR-006-15）。
- ``data_available`` はトップレベル単一フラグでなく section（prevented_detection /
  rework / rework_cost）別に分離する（DC-GP-003）。「収集中」表示は
  ``prevented_detection.data_available=false`` のときのみ [1] に適用し、
  [2]/[3] は独立に表示する（データが無くてもゼロ値・注記は表示する）。
- 入力検証（``--since`` 書式 / ``--months``・``--examples`` の非負整数）は
  DB アクセス前に stderr + exit 1 とする（記録系のフェイルセーフとは非対称・
  cli_tier.py の DB 不在処理と同じ様式を踏襲）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any

from c3 import db as c3_db
from c3._terminal import sanitize_terminal_text

# --months の上限（DoS 耐性・SR-NEW item5）。10 年分あれば実用上十分で、巨大値による
# 暦月リスト構築・月次出力ループの O(N) 消費を DB アクセス前に頭打ちにする。
MAX_MONTHS = 120


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "metrics",
        help="P4 効果の総括メトリクス（c3 metrics）",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="JSON 形式で出力",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="YYYY-MM-DD 以降の記録のみ集計（全セクション共通フィルタ）",
    )
    parser.add_argument(
        "--months",
        type=int,
        default=12,
        help="差し戻し傾向（月次）の表示バケット数上限（デフォルト 12）",
    )
    parser.add_argument(
        "--examples",
        type=int,
        default=5,
        help="事前検出実例の表示件数上限（デフォルト 5）",
    )
    parser.set_defaults(handler=handle_metrics)


def handle_metrics(args: argparse.Namespace) -> int:
    since = getattr(args, "since", None)
    if since is not None:
        try:
            datetime.strptime(since, "%Y-%m-%d")
        except ValueError:
            print(
                f"--since の書式が不正です: {since!r}（期待: YYYY-MM-DD）",
                file=sys.stderr,
            )
            return 1

    months = getattr(args, "months", 12)
    if months < 1:
        print(
            f"--months は 1 以上の整数を指定してください: {months!r}",
            file=sys.stderr,
        )
        return 1
    if months > MAX_MONTHS:
        print(
            f"--months は {MAX_MONTHS} 以下の整数を指定してください: {months!r}",
            file=sys.stderr,
        )
        return 1

    examples = getattr(args, "examples", 5)
    if examples < 1:
        print(
            f"--examples は 1 以上の整数を指定してください: {examples!r}",
            file=sys.stderr,
        )
        return 1

    db_path = c3_db.locate_c3_db()
    if db_path is None or not db_path.exists():
        print(
            "DB が見つかりません: .claude/state/c3.db\n"
            "新セッションを開始すると SessionStart hook が自動で初期化します。",
            file=sys.stderr,
        )
        return 1

    try:
        snapshot = _collect_snapshot(
            db_path, since=since, months=months, examples=examples,
        )
    except Exception as exc:  # noqa: BLE001
        # 例外メッセージ本文（sqlite の "no such column: ..." 等・DB 内部スキーマ名や
        # パスを含みうる）は出力せず型名のみに統一した（db.py 全体の type(exc).__name__
        # 規律・SR-R-001 / db_sr_r001_complete パターンに整合）。
        print(
            f"DB アクセスエラー: {type(exc).__name__}\n"
            "schema_version が古い可能性。新セッションで自動マイグレーションされます。",
            file=sys.stderr,
        )
        return 1

    if getattr(args, "as_json", False):
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return 0

    _render_human(snapshot)
    return 0


def _sanitize_tree(value: Any) -> Any:
    """DB 由来の文字列を再帰的にサニタイズする（dict/list を辿り str 値のみ変換）。

    JSON 構造（キー名・ネスト・非文字列値の型）は維持し、str 値だけ
    ``sanitize_terminal_text`` を通す。item2（``--json`` 経路）と item6
    （人間向け出力の全 DB 由来フィールド）を単一算出源に集約し、「DB から
    読んだ文字列は snapshot 構築時に必ずサニタイズを通す」不変条件を満たす
    （SR-INJ-003・モジュール全体掃除）。数値・真偽値・None は素通しする。
    """
    if isinstance(value, str):
        return sanitize_terminal_text(value)
    if isinstance(value, dict):
        return {k: _sanitize_tree(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_tree(v) for v in value]
    return value


def _collect_snapshot(
    db_path, since: str | None, months: int, examples: int,
) -> dict[str, Any]:
    """DB から metrics ヘルパー 6 本を読み snapshot dict を返す。"""
    matrix = c3_db.read_review_decision_matrix(db_path, since=since)
    example_rows = c3_db.fetch_prevented_findings(
        db_path, limit=examples, since=since,
    )
    trend = c3_db.read_rework_trend(db_path, months=months, since=since)
    role_rows = c3_db.read_rework_role_distribution(db_path, since=since)
    fix_cycles = c3_db.read_session_fix_cycles(db_path, since=since)
    rework_cost = c3_db.read_rework_session_cost(db_path, since=since)

    headline = _derive_headline(matrix)
    role_distribution = _split_role_distribution(role_rows)

    prevented_detection = {
        "data_available": bool(matrix),
        "headline": headline,
        "matrix": matrix,
        "examples": example_rows,
    }

    # trend はヘルパー層でゼロ埋め済みのため bool(trend) では実データの有無を判定できず、
    # rework_count > 0 で実データ有無を判定する（item5）。architecture §2-4 の字面との乖離は
    # ゼロ埋めの実装に基づく補正判定（code-review で指摘・根拠コメント追加）。
    rework_data_available = (
        bool(role_rows)
        or any(row["rework_count"] > 0 for row in trend)
        or fix_cycles.get("total_sessions", 0) > 0
    )
    rework = {
        "data_available": rework_data_available,
        "trend": trend,
        "fix_cycles": fix_cycles,
        "role_distribution": role_distribution,
    }

    rework_cost_out = dict(rework_cost)
    # has_cost_rows で判定: agent_cost_runs に紐づく行が 1 件でも存在するか（item1）。
    # 合計金額が 0 でも行が存在すればデータありと判定（0 円行のみのケースも含む）。
    rework_cost_out["data_available"] = rework_cost.get("has_cost_rows", False)

    # DB 由来の文字列フィールドを snapshot 構築時に一律サニタイズする（item2/item6）。
    # headline / role_distribution は上でサニタイズ前の raw な matrix / role_rows から
    # 導出済み（件数・gate 分類のロジックは従来どおり）だが、表示・JSON 出力に載る
    # 文字列値はここで全て制御文字を除去する。JSON 構造・数値は _sanitize_tree が保持する。
    prevented_detection = _sanitize_tree(prevented_detection)
    rework = _sanitize_tree(rework)
    rework_cost_out = _sanitize_tree(rework_cost_out)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "since": since,
        "months": months,
        "examples": examples,
        "prevented_detection": prevented_detection,
        "rework": rework,
        "rework_cost": rework_cost_out,
    }


def _derive_headline(matrix: list[dict]) -> dict[str, int]:
    """matrix の fixed×severity バケットから headline 全カウントを導出する。

    単一算出源（DC-AM-001）。``fetch_prevented_findings`` の行数は使わない。
    """
    fixed_by_severity: dict[str, int] = {}
    for row in matrix:
        if row["decision"] == "fixed":
            severity = row["severity"]
            fixed_by_severity[severity] = fixed_by_severity.get(severity, 0) + row["count"]

    # 相互参照: .claude/skills/dev-workflow/scripts/record_review_decision.py:_SEVERITY_VOCAB /
    # src/c3/db.py:fetch_prevented_findings の IN リテラル（item2）。
    # 語彙変更時は 3 箇所同期が必要・import 共有は実行コンテキスト分離のため不可。
    critical = fixed_by_severity.get("critical", 0)
    high = fixed_by_severity.get("high", 0)
    medium = fixed_by_severity.get("medium", 0)
    return {
        "fixed_medium_plus": critical + high + medium,
        "critical": critical,
        "high": high,
        "medium": medium,
        "fixed_unknown": fixed_by_severity.get("unknown", 0),
    }


def _split_role_distribution(role_rows: list[dict]) -> dict[str, list[dict]]:
    """role_rows を review / development / other の 3 分類に振り分ける（DC-AM-003）。"""
    review: list[dict] = []
    development: list[dict] = []
    other: list[dict] = []
    for row in role_rows:
        gate = row["gate"]
        if gate in c3_db.METRICS_REVIEW_GATES:
            review.append(row)
        elif gate in c3_db.METRICS_DEV_GATES:
            development.append(row)
        else:
            other.append(row)
    return {"review": review, "development": development, "other": other}


def _render_human(snapshot: dict[str, Any]) -> None:
    """人間向けの 3 セクション構成で snapshot を stdout に出力する。"""
    print("== C3 効果メトリクス ==")
    since_label = snapshot["since"] or "指定なし"
    print(
        f"since={since_label} months={snapshot['months']} examples={snapshot['examples']}"
    )
    print()

    pd = snapshot["prevented_detection"]
    print("[1] 事前検出実績（出荷前に捕捉したレビュー指摘）")
    if not pd["data_available"]:
        print(
            "    収集中（forward-only）。判断記録が蓄積され次第、"
            "事前検出実績が表示されます。"
        )
    else:
        headline = pd["headline"]
        print(
            f"    ヘッドライン: fixed かつ Medium 以上 {headline['fixed_medium_plus']} 件"
            f"（critical {headline['critical']} / high {headline['high']} / "
            f"medium {headline['medium']}）"
        )
        print(
            f"                  ＋ severity 未記録の fixed {headline['fixed_unknown']} 件"
            "（unknown・下限値注記）"
        )
        print("    reviewer x severity x decision マトリクス（severity 未記録は unknown）:")
        # snapshot の文字列フィールドは _collect_snapshot で一律サニタイズ済みのため、
        # ここでの再サニタイズは行わない（DB 由来文字列の単一算出源は snapshot 層・item6）。
        for row in pd["matrix"]:
            print(
                f"      {str(row['reviewer']):<20} {row['severity']:<10} "
                f"{row['decision']:<10} {row['count']:>4}"
            )
        print("    直近の実例:")
        if not pd["examples"]:
            print("      （実例なし）")
        else:
            for ex in pd["examples"]:
                finding_display = str(ex["finding_text"])[:60]
                print(
                    f"      [{ex['severity']}] {ex['reviewer']} {ex['checklist_id']} "
                    f"{finding_display} ({ex['decided_at']})"
                )
    print()

    rw = snapshot["rework"]
    print("[2] 差し戻しの傾向と帰属")
    print("    月次:")
    for row in rw["trend"]:
        print(
            f"      {row['month']}  差し戻し {row['rework_count']:>3} 件  "
            f"セッション {row['session_count']:>3}  "
            f"1セッションあたり {row['per_session']:.2f}"
        )
    fix_cycles = rw["fix_cycles"]
    dist = fix_cycles["distribution"]
    print(
        f"    fix-cycle 近似分布: 0回 {dist['0']} / 1回 {dist['1']} / 2回+ {dist['2plus']}"
        f"（平均 {fix_cycles['mean']:.2f}・最大 {fix_cycles['max']}）"
    )
    print(f"      ※ {fix_cycles['note']}")
    role_dist = rw["role_distribution"]
    print("    帰属role分布:")
    print(
        f"      レビュー差し戻し[E-1/E-2/C-3]: "
        f"{sum(r['count'] for r in role_dist['review'])} 件"
    )
    print(
        f"      開発内[D-3/D-5]: {sum(r['count'] for r in role_dist['development'])} 件"
    )
    print(f"      other: {sum(r['count'] for r in role_dist['other'])} 件")
    print()

    rc = snapshot["rework_cost"]
    print("[3] 手戻りコスト概況（session 粒度近似）")
    print(
        f"    差し戻しありセッション合計: ${rc['rework_total_usd']:.4f} / "
        f"全体 ${rc['overall_total_usd']:.4f}（比率 {rc['overall_ratio']:.4f}）"
    )
    print(f"      ※ {rc['note']}")
    print()
