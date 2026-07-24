"""C3 tier-routing パラメータ解決（env override + SSOT 定数）。

``db.py``（SQLite read/write helpers）から分離した tier-routing の tunable 群。
``LEARNING_THRESHOLD`` / ``EPSILON_TIEBREAK`` / ``COST_LAMBDA_*`` /
``ESCALATION_THRESHOLD_DEFAULT`` の SSOT 定数と、それらを環境変数で上書き解決する
``resolve_*`` を提供する。DB I/O には依存しない（純粋な env パース）。

後方互換のため、これらは ``c3.db`` からも re-export される。cli_tier.py /
select_tier.py は従来どおり ``c3.db`` 経由でも、本モジュール直接でも参照できる。
"""

from __future__ import annotations

import math
import os
import sys
from typing import cast

# tier-routing: 学習データ収集期の閾値（合計試行数がこの値未満なら uniform 選択）。
# SSOT: cli_tier.py / select_tier.py はここから参照する（CR-M-002）。
LEARNING_THRESHOLD = 30
# cost-aware tie-break の拮抗判定閾値。Beta サンプルは 0〜1 スケールで、
# 成功率 5pt（=0.05）以内を拮抗とみなす。本定数が SSOT。
# 過大にすると成功率を犠牲にするリスク、過小にすると無発動になる。
# C3_TIER_EPSILON 環境変数で上書き可（v2.25.0）。
EPSILON_TIEBREAK = 0.05
# cost-weighted Thompson の重み係数 λ の既定値。
# None = v2.25.0 互換モード（ε tie-break を維持し全 tier weighting を発動しない）。
# C3_TIER_COST_LAMBDA 環境変数で上書き可（v2.26.0）。
# λ>0 で全 tier の score=sample-λ*cost_norm weighting が発動、λ=0 明示で cost 無視（純 Thompson）。
# 本定数が SSOT。
COST_LAMBDA_DEFAULT = None
# failure rate がこの値以上で 1 段上位 tier へ escalation する閾値。
# C3_ESCALATION_THRESHOLD 環境変数で上書き可（v2.26.0）。
# 本定数が SSOT（select_tier.py はここから参照）。
ESCALATION_THRESHOLD_DEFAULT = 0.5

# cost-weighted Thompson の λ 有効範囲（v2.27.0: 上限を 1.0→5.0 に拡張）。
# cost を成功率より強く効かせる余地を確保するため上限を 5.0 に設定。
# select_tier.py の _resolve_cost_lambda はここを SSOT として参照する。
COST_LAMBDA_MIN = 0.0
COST_LAMBDA_MAX = 5.0

# tier-routing 学習シグナルの記録対象 role（v2.41.0 db-foundation）。
# agent_tier_bandit / agent_outcomes の role 列で許容される値の SSOT。
# record_agent_outcome.py の --role 検証・cli_tier.py の role 別表示グルーピングが参照する。
AGENT_ROLES: tuple[str, ...] = ("interviewer", "architect", "planner", "developer", "tester")

# bandit 集計・escalation failure_rate でカウントする客観 gate の SSOT（フェーズ2.5・ADR-25-1）。
# D-2.5=実装完了 / D-3・D-5=テスト合否（「動く実装」の直接測定） / D-2.5-stuck=自力完走不能。
# E-1/E-2（レビュー指摘由来）は success/failure とも対称に除外する（含めない）。
# db.py の read_agent_tier_params / read_agent_failure_rate が read-side でのみ参照する
# （record 側は全 gate を無条件記録するため参照不要）。
BANDIT_GATES: tuple[str, ...] = ("D-2.5", "D-3", "D-5", "D-2.5-stuck")

# role 別 bandit gate 集合（tester Red 限定 tier-routing 拡張・ADR-1）。
# 既定は BANDIT_GATES（developer 等の「動く実装」を測る D 系 gate）。
# tester のみ Red 成果物の生存を測る "D-1" に限定し、D-3/D-5 の tester 記録は
# 集計対象から外す（Red セルの意味論を gate=D-1 だけで完結させる・FR-3）。
# read_agent_tier_params / read_agent_failure_rate が read-side でのみ参照する
# （record 側は全 gate を無条件記録するため参照不要）。
# **"wt_tester" キーは置かない（統合方式・ADR-1 / architecture §2-1）**:
# parallel 経路の wt_tester 記録は既存規約どおり --role tester へ正規化され tester
# セルへ自動合流するため、bandit_gates_for_role("wt_tester") が呼ばれる経路自体が
# 存在しない（AGENT_ROLES にも wt_* は含まれない）。
BANDIT_GATES_BY_ROLE: dict[str, tuple[str, ...]] = {
    "tester": ("D-1",),
}


def bandit_gates_for_role(role: str) -> tuple[str, ...]:
    """role の bandit 集計対象 gate 集合を返す（read-side フィルタの SSOT）。

    ``BANDIT_GATES_BY_ROLE`` に role の明示エントリがあればそれを返し、無ければ
    既定の ``BANDIT_GATES`` を返す。未知 role も既定集合で fail-safe に集計する
    （新 role 追加時に集計が空になって静かに壊れることを避ける）。

    Args:
        role: '_db_params.AGENT_ROLES' のいずれか（未知値も可・既定集合に落ちる）。

    Returns:
        当該 role の bandit gate タプル。tester なら ``("D-1",)``、それ以外は
        ``BANDIT_GATES``。
    """
    return BANDIT_GATES_BY_ROLE.get(role, BANDIT_GATES)


# escalation failure_rate 計算の時間窓デフォルト（日数・フェーズ2.5・ADR-25-2）。
# C3_FAILURE_WINDOW_DAYS 環境変数で上書き可。妥当域は半開区間 (0, 3650]（0 拒否・上限 10 年）。
# 本定数が SSOT（read_agent_failure_rate がここから解決する）。
FAILURE_WINDOW_DAYS_DEFAULT = 14.0

# metrics（`c3 metrics`）用 gate 定数の SSOT（P4 効果の総括メトリクス・ADR-006-5）。
# BANDIT_GATES とは完全に独立した定義であり、値を再利用・コピーしない。
# db.py の metrics ヘルパー群（read_review_decision_matrix 等）は agent_outcomes /
# agent_cost_runs を read-only 消費するのみで、bandit 学習シグナル（BANDIT_GATES）
# および E-1/E-2 の bandit 除外分岐には一切干渉しない（絶対制約・成功条件5）。
METRICS_REVIEW_GATES: tuple[str, ...] = ("E-1", "E-2", "C-3")   # レビュー差し戻し（傾向・コスト対象）
METRICS_DEV_GATES: tuple[str, ...] = ("D-3", "D-5")             # 開発内リトライ（役割分布で別掲）


def _resolve_float_env(
    env_key: str,
    default: float | None,
    *,
    min_val: float,
    max_val: float,
    min_inclusive: bool,
    log_prefix: str,
) -> float | None:
    """env 変数を float として安全に解決する共通ヘルパー（resolve_* の SSOT 実体）。

    挙動（resolve_cost_lambda / resolve_epsilon / resolve_escalation_threshold で共通）:
    - 未設定 / 空文字 → 無警告で ``default`` を返す。
    - 非数値 / NaN / 範囲外 → stderr に env 名入りの警告を出し ``default`` に戻す。
    - 妥当域: 上限は常に閉区間 ``<= max_val``。下限は ``min_inclusive`` で開閉を切替
      （True なら ``>= min_val`` の閉区間、False なら ``> min_val`` の半開区間）。
    """
    raw = os.environ.get(env_key)
    if raw is None or raw == "":
        return default
    bracket = (
        f"[{min_val}, {max_val}]" if min_inclusive else f"({min_val}, {max_val}]"
    )
    try:
        x = float(raw)
    except ValueError:
        print(
            f"{log_prefix} invalid {env_key}={raw!r}, using default {default}",
            file=sys.stderr,
        )
        return default
    if math.isnan(x):
        print(
            f"{log_prefix} {env_key}={raw!r} is NaN, using default {default}",
            file=sys.stderr,
        )
        return default
    low_ok = x >= min_val if min_inclusive else x > min_val
    if not low_ok or x > max_val:
        print(
            f"{log_prefix} {env_key}={x!r} out of range {bracket}, "
            f"using default {default}",
            file=sys.stderr,
        )
        return default
    return x


def resolve_cost_lambda() -> float | None:
    """``C3_TIER_COST_LAMBDA`` を安全に解決する（cli_tier 用 SSOT）。

    妥当域: [COST_LAMBDA_MIN, COST_LAMBDA_MAX]（x=0 許容の閉区間）。
    戻り値が None の場合は v2.25.0 互換の ε tie-break 経路を維持する（センチネル）。
    詳細な共通挙動は :func:`_resolve_float_env` を参照。
    """
    return _resolve_float_env(
        "C3_TIER_COST_LAMBDA",
        COST_LAMBDA_DEFAULT,
        min_val=COST_LAMBDA_MIN,
        max_val=COST_LAMBDA_MAX,
        min_inclusive=True,
        log_prefix="[c3:cost_lambda]",
    )


def resolve_epsilon() -> float:
    """``C3_TIER_EPSILON`` を安全に解決する（cli_tier 用 SSOT）。

    妥当域: (0, 1]（x=0 拒否の半開区間）。default が float のため戻り値は常に float。
    詳細な共通挙動は :func:`_resolve_float_env` を参照。
    """
    value = _resolve_float_env(
        "C3_TIER_EPSILON",
        EPSILON_TIEBREAK,
        min_val=0.0,
        max_val=1.0,
        min_inclusive=False,
        log_prefix="[c3:epsilon]",
    )
    # default=EPSILON_TIEBREAK のため None になり得ない（戻り値型を float に絞る）
    return cast(float, value)


def resolve_escalation_threshold() -> float:
    """``C3_ESCALATION_THRESHOLD`` を安全に解決する（cli_tier 用 SSOT）。

    妥当域: (0, 1]（x=0 拒否の半開区間）。default が float のため戻り値は常に float。
    詳細な共通挙動は :func:`_resolve_float_env` を参照。
    """
    value = _resolve_float_env(
        "C3_ESCALATION_THRESHOLD",
        ESCALATION_THRESHOLD_DEFAULT,
        min_val=0.0,
        max_val=1.0,
        min_inclusive=False,
        log_prefix="[c3:escalation]",
    )
    # default=ESCALATION_THRESHOLD_DEFAULT のため None になり得ない（戻り値型を float に絞る）
    return cast(float, value)


def resolve_failure_window_days() -> float:
    """``C3_FAILURE_WINDOW_DAYS`` を安全に解決する（read_agent_failure_rate 用 SSOT）。

    妥当域: (0, 3650]（x=0 拒否の半開区間・上限 10 年で過大窓を弾く）。
    default が float のため戻り値は常に float。
    詳細な共通挙動は :func:`_resolve_float_env` を参照。
    """
    value = _resolve_float_env(
        "C3_FAILURE_WINDOW_DAYS",
        FAILURE_WINDOW_DAYS_DEFAULT,
        min_val=0.0,
        max_val=3650.0,
        min_inclusive=False,
        log_prefix="[c3:failure_window]",
    )
    # default=FAILURE_WINDOW_DAYS_DEFAULT のため None になり得ない（戻り値型を float に絞る）
    return cast(float, value)
