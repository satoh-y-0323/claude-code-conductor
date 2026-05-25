"""``c3 tier stats`` - tier-routing Tier 自動ルーティングの学習データ可視化 CLI。

tier-routing (Phase 2 完成) の効果計測用ダッシュボード。

主な機能:
- ``c3 tier stats``: 全 complexity × tier の累積（tier_bandit）と直近 outcome を表形式表示
- ``c3 tier stats --json``: 機械可読 JSON 出力
- ``c3 tier stats --recent N``: 直近 outcome の表示件数を変更（デフォルト 10）

設計判断:
- PO 廃止前の ``c3 status`` CLI パターンを踏襲（旧 ``cli_status.py`` / v2.0.0 で削除）
- データがゼロでも「収集中」と分かる表示にする
- escalation 発動回数は専用テーブルがないため今回は表示なし（将来拡張余地）
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from c3 import db as c3_db


logger = logging.getLogger(__name__)


_DEFAULT_RECENT_LIMIT = 10
_LEARNING_THRESHOLD = c3_db.LEARNING_THRESHOLD  # SSOT: db.py で一元管理（CR-M-002）
_TIERS = ("haiku", "sonnet", "opus")
_COMPLEXITIES = ("simple", "medium", "complex")


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "tier",
        help="Tier 自動ルーティング統計（tier-routing）",
    )
    sub = parser.add_subparsers(dest="tier_command", metavar="<subcommand>")
    sub.required = True

    stats = sub.add_parser(
        "stats",
        help="学習データと累積統計を表示",
    )
    stats.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="JSON 形式で出力",
    )
    stats.add_argument(
        "--recent",
        type=int,
        default=_DEFAULT_RECENT_LIMIT,
        help=f"直近 outcome の表示件数（デフォルト {_DEFAULT_RECENT_LIMIT}）",
    )
    stats.set_defaults(handler=handle_stats)


def handle_stats(args: argparse.Namespace) -> int:
    db_path = c3_db.locate_c3_db()
    if db_path is None or not db_path.exists():
        print(
            "DB が見つかりません: .claude/state/c3.db\n"
            "新セッションを開始すると SessionStart hook が自動で初期化します。",
            file=sys.stderr,
        )
        return 1

    try:
        snapshot = _collect_snapshot(db_path, recent_limit=args.recent)
    except Exception as exc:  # noqa: BLE001
        print(
            f"DB アクセスエラー: {exc}\n"
            "schema_version が古い可能性。新セッションで自動マイグレーションされます。",
            file=sys.stderr,
        )
        return 1

    if args.as_json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return 0

    _render_human(snapshot)
    return 0


def _collect_snapshot(db_path, recent_limit: int) -> dict[str, Any]:
    """DB から tier_bandit / tier_recent_outcomes / agent_cost を読み snapshot dict を返す。"""
    bandit_rows: list[dict[str, Any]] = []
    total_trials = 0

    for complexity in _COMPLEXITIES:
        params = c3_db.read_tier_params(complexity, db_path=db_path)
        for tier in _TIERS:
            alpha, beta, trials = params[tier]
            total_trials += trials
            denom = alpha + beta
            expected = alpha / denom if denom > 0 else 0.5
            bandit_rows.append({
                "complexity": complexity,
                "tier": tier,
                "alpha": alpha,
                "beta": beta,
                "trials": trials,
                "expected_success_rate": expected,
            })

    recent_outcomes: list[dict[str, Any]] = c3_db.read_recent_outcomes(
        limit=recent_limit,
        db_path=db_path,
    )

    agent_cost: list[dict[str, Any]] = c3_db.read_agent_cost_summary(db_path=db_path)

    if total_trials < _LEARNING_THRESHOLD:
        mode = "uniform"
    else:
        mode = "thompson"

    return {
        "learning_progress": {
            "trials": total_trials,
            "threshold": _LEARNING_THRESHOLD,
            "mode": mode,
        },
        "tier_bandit": bandit_rows,
        "recent_outcomes": recent_outcomes,
        "agent_cost": agent_cost,
    }


def _render_human(snapshot: dict[str, Any]) -> None:
    """人間向けの表形式で snapshot を stdout に出力する。"""
    progress = snapshot["learning_progress"]
    trials = progress["trials"]
    threshold = progress["threshold"]
    if progress["mode"] == "uniform":
        mode_label = "学習データ収集中"
    else:
        mode_label = "Thompson Sampling 動作中"

    print(f"学習データ収集状況: {trials} / {threshold} 試行（{mode_label}）")
    print()

    print("== Tier 別累積（tier_bandit） ==")
    print(f"{'complexity':<12} {'tier':<8} {'trials':>6}  {'alpha':>5}  {'beta':>5}  {'期待成功率':>10}")
    for row in snapshot["tier_bandit"]:
        print(
            f"{row['complexity']:<12} {row['tier']:<8} "
            f"{row['trials']:>6}  {row['alpha']:>5.2f}  {row['beta']:>5.2f}  "
            f"{row['expected_success_rate'] * 100:>9.2f}%"
        )
    print()

    print(f"== 直近 outcome 履歴（tier_recent_outcomes、最新 {len(snapshot['recent_outcomes'])} 件） ==")
    if not snapshot["recent_outcomes"]:
        print("（記録なし）")
    else:
        print(f"{'ts':<25} {'complexity':<12} {'tier':<8} {'outcome':<10}")
        for row in snapshot["recent_outcomes"]:
            outcome = "success" if row["success"] else "failure"
            print(f"{row['ts']:<25} {row['complexity']:<12} {row['tier']:<8} {outcome:<10}")
    print()

    print("== 学習データ記録チャネル ==")
    print("記録元: dev-workflow フェーズ E の最終承認時のみ（record_tier_outcome.py）")
    print("直接指示作業ではデータが溜まりません（設計通り）")
    print()

    print("== Agent 別コスト集計（agent_cost_runs） ==")
    agent_cost = snapshot.get("agent_cost", [])
    if not agent_cost:
        print("（コストデータ未収集）")
    else:
        print(
            f"{'agent_type':<16} {'runs':>5}  {'total_usd':>10}  "
            f"{'in_tok':>9}  {'out_tok':>9}  {'cache_r':>9}  {'cache_w':>9}"
        )
        for row in agent_cost:
            note = "  （マクロ集計・tier 学習対象外）" if row["agent_type"] == "mainline" else ""
            print(
                f"{row['agent_type']:<16} {row['runs']:>5}  "
                f"${row['total_cost_usd']:>9.4f}  "
                f"{row['input_tokens']:>9}  {row['output_tokens']:>9}  "
                f"{row['cache_read_tokens']:>9}  {row['cache_create_tokens']:>9}"
                f"{note}"
            )
    print()
    print("（注: 本リリースはデータ収集基盤のみ。cost-aware routing は v2.22.0 予定）")
