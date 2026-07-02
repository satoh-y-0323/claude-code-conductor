"""``c3 tier stats`` - tier-routing Tier 自動ルーティングの学習データ可視化 CLI。

tier-routing (Phase 2 完成) の効果計測用ダッシュボード。

主な機能:
- ``c3 tier stats``: role 別 complexity × tier の累積（agent_tier_bandit）と
  直近 outcome（agent_outcomes）を表形式表示
- ``c3 tier stats --json``: 機械可読 JSON 出力
- ``c3 tier stats --recent N``: 直近 outcome の表示件数を変更（デフォルト 10）
- ``c3 tier stats --role <role>``: 指定 role のみに絞り込む

設計判断:
- PO 廃止前の ``c3 status`` CLI パターンを踏襲（旧 ``cli_status.py`` / v2.0.0 で削除）
- データがゼロでも「収集中」と分かる表示にする
- escalation 発動回数は専用テーブルがないため今回は表示なし（将来拡張余地）
- v2.41.0 cli-tier-stats タスクで role 次元（agent_tier_bandit / agent_outcomes）
  に対応。旧フラット tier_bandit / learning_progress / tier_cost（session 合計
  USD 概算セクション）は廃止（architecture-report-20260702-214748.md §3-7）
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from c3 import db as c3_db
from c3._terminal import sanitize_terminal_text


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
    stats.add_argument(
        "--role",
        default=None,
        help="AGENT_ROLES のいずれかで絞り込む",
    )
    stats.set_defaults(handler=handle_stats)


def handle_stats(args: argparse.Namespace) -> int:
    role_filter = getattr(args, "role", None)
    if role_filter is not None and role_filter not in c3_db.AGENT_ROLES:
        print(
            f"--role の値が不正です: {role_filter!r}"
            f"（有効な値: {', '.join(c3_db.AGENT_ROLES)}）",
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
        snapshot = _collect_snapshot(db_path, recent_limit=args.recent, role_filter=role_filter)
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


def _collect_snapshot(
    db_path, recent_limit: int, role_filter: str | None = None,
) -> dict[str, Any]:
    """DB から role 別 tier_bandit / recent_outcomes / agent_cost / tier_cost_rate を読み snapshot dict を返す。

    Args:
        db_path: c3.db のパス。
        recent_limit: 直近 outcome の取得件数上限。
        role_filter: 指定時は当該 role のみに絞り込む（未指定時は全 AGENT_ROLES）。
    """
    roles: list[str] = [role_filter] if role_filter is not None else list(c3_db.AGENT_ROLES)

    tier_bandit_by_role: dict[str, dict[str, Any]] = {}
    for role in roles:
        rows: list[dict[str, Any]] = []
        total_trials = 0
        for complexity in _COMPLEXITIES:
            params = c3_db.read_agent_tier_params(role, complexity, db_path=db_path)
            for tier in _TIERS:
                alpha, beta, trials = params[tier]
                total_trials += trials
                denom = alpha + beta
                expected = alpha / denom if denom > 0 else 0.5
                rows.append({
                    "complexity": complexity,
                    "tier": tier,
                    "alpha": alpha,
                    "beta": beta,
                    "trials": trials,
                    "expected_success_rate": expected,
                })
        mode = "uniform" if total_trials < _LEARNING_THRESHOLD else "thompson"
        tier_bandit_by_role[role] = {
            "trials": total_trials,
            "threshold": _LEARNING_THRESHOLD,
            "mode": mode,
            "rows": rows,
        }

    recent_outcomes: list[dict[str, Any]] = c3_db.read_recent_agent_outcomes(
        limit=recent_limit,
        role=role_filter,
        db_path=db_path,
    )

    agent_cost: list[dict[str, Any]] = c3_db.read_agent_cost_summary(db_path=db_path)

    tier_cost_rate: list[dict[str, Any]] = c3_db.read_tier_cost_rate_summary(db_path=db_path)

    return {
        "roles": roles,
        "tier_bandit_by_role": tier_bandit_by_role,
        "recent_outcomes": recent_outcomes,
        "agent_cost": agent_cost,
        "tier_cost_rate": tier_cost_rate,
        "routing_params": {
            "cost_lambda": c3_db.resolve_cost_lambda(),
            "epsilon": c3_db.resolve_epsilon(),
            "escalation_threshold": c3_db.resolve_escalation_threshold(),
        },
    }


def _render_human(snapshot: dict[str, Any]) -> None:
    """人間向けの表形式で snapshot を stdout に出力する。"""
    print("== Tier 別累積（role 別グループ表示） ==")
    print()
    for role in snapshot["roles"]:
        group = snapshot["tier_bandit_by_role"][role]
        role_safe = sanitize_terminal_text(str(role))
        print(f"[{role_safe}]")
        if group["trials"] == 0:
            print("収集中")
            print()
            continue

        mode_label = "一様ルーティング中" if group["mode"] == "uniform" else "Thompson Sampling 動作中"
        print(f"学習データ収集状況: {group['trials']} / {group['threshold']} 試行（{mode_label}）")
        print(
            f"{'complexity':<12} {'tier':<8} {'trials':>6}  {'alpha':>5}  {'beta':>5}  "
            f"{'期待成功率':>10}"
        )
        for row in group["rows"]:
            complexity_safe = sanitize_terminal_text(str(row["complexity"]))
            tier_safe = sanitize_terminal_text(str(row["tier"]))
            print(
                f"{complexity_safe:<12} {tier_safe:<8} "
                f"{row['trials']:>6}  {row['alpha']:>5.2f}  {row['beta']:>5.2f}  "
                f"{row['expected_success_rate'] * 100:>9.2f}%"
            )
        print()

    print(f"== 直近 outcome 履歴（最新 {len(snapshot['recent_outcomes'])} 件） ==")
    if not snapshot["recent_outcomes"]:
        print("（記録なし）")
    else:
        print(
            f"{'ts':<25} {'role':<12} {'complexity':<12} {'tier':<8} "
            f"{'gate':<10} {'outcome':<10}"
        )
        for row in snapshot["recent_outcomes"]:
            outcome = "success" if row["success"] else "failure"
            role_safe = sanitize_terminal_text(str(row["role"]))
            complexity_safe = sanitize_terminal_text(str(row["complexity"]))
            tier_safe = sanitize_terminal_text(str(row["tier"]))
            gate_safe = sanitize_terminal_text(str(row["gate"])) if row["gate"] else ""
            print(
                f"{row['ts']:<25} {role_safe:<12} {complexity_safe:<12} {tier_safe:<8} "
                f"{gate_safe:<10} {outcome:<10}"
            )
    print()

    print("== 学習データ記録チャネル ==")
    print("記録元: dev-workflow の各フェーズ承認ゲート・並列タスク単位（record_agent_outcome.py）")
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
            agent_type_safe = sanitize_terminal_text(str(row["agent_type"]))
            note = "  （マクロ集計・tier 学習対象外）" if agent_type_safe == "mainline" else ""
            print(
                f"{agent_type_safe:<16} {row['runs']:>5}  "
                f"${row['total_cost_usd']:>9.4f}  "
                f"{row['input_tokens']:>9}  {row['output_tokens']:>9}  "
                f"{row['cache_read_tokens']:>9}  {row['cache_create_tokens']:>9}"
                f"{note}"
            )
    print()

    print("== Tier 別 USD/MTok レート（model 一致・tie-break が使用） ==")
    tier_cost_rate = snapshot.get("tier_cost_rate", [])
    if not tier_cost_rate:
        print("（rate データ未収集）")
    else:
        print(f"{'complexity':<12} {'tier':<8} {'sessions':>8}  {'rate_usd_per_mtok':>18}")
        for row in tier_cost_rate:
            complexity_safe = sanitize_terminal_text(str(row["complexity"]))
            tier_safe = sanitize_terminal_text(str(row["tier"]))
            print(
                f"{complexity_safe:<12} {tier_safe:<8} "
                f"{row['sessions']:>8}  "
                f"{row['rate_usd_per_mtok']:>18.4f}"
            )
    print()

    print("== routing パラメータ（環境変数で調整可） ==")
    rp = snapshot.get("routing_params", {})
    cost_lambda = rp.get("cost_lambda")
    epsilon = rp.get("epsilon", c3_db.EPSILON_TIEBREAK)
    escalation_threshold = rp.get("escalation_threshold", c3_db.ESCALATION_THRESHOLD_DEFAULT)
    if cost_lambda is None:
        print("λ (C3_TIER_COST_LAMBDA): 未設定 → v2.25.0 互換（ε tie-break のみ）")
    elif cost_lambda == 0.0:
        print("λ: 0.0（cost 無視・純 Thompson）")
    else:
        print(f"λ: {cost_lambda}（全 tier weighting 有効）")
    print(f"ε (C3_TIER_EPSILON): {epsilon}")
    print(f"escalation threshold (C3_ESCALATION_THRESHOLD): {escalation_threshold}")
    print()
