#!/usr/bin/env python3
"""UserPromptSubmit hook: recommend an LLM Tier (haiku/sonnet/opus) for the prompt.

F-005 MVP: タスクの複雑度を簡易ヒューリスティックで推定し、Thompson Sampling
（または学習データ収集期は uniform random）で推奨 Tier を決定して
``additionalContext`` で親 Claude に伝える。

MVP スコープ（ユーザー承認済み）:
- **やる**: 推奨 Tier を提示するのみ。agent の `model:` フロントマターは触らない
- **やらない**: 動的 model 切替（次フェーズ）、フォールバック（Haiku 失敗→Sonnet 自動昇格）

学習データ:
- 結果は ``.claude/state/tier_selection.json`` に直近 1 件のみ書き込む
- dev-workflow フェーズ E の承認/否認時に ``record_tier_outcome.py`` が
  この json を読んで ``tier_bandit`` テーブルを更新する

入力 / 出力:
- stdin: UserPromptSubmit payload（``prompt`` フィールドを参照）
- stdout: JSON object ``{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
   "additionalContext": "..."}}`` を返すと Claude Code がその context を追加注入する
- exit 0: 成功 / 失敗どちらでも 0（セッションを止めない方針）
"""

from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

try:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


# 学習データ収集期の閾値（合計試行数がこの値未満なら uniform 選択）
LEARNING_THRESHOLD = 30

# 複雑度推定のキーワード
SIMPLE_KEYWORDS = frozenset({
    "typo", "rename", "doc", "comment", "誤字", "リネーム", "コメント",
})
COMPLEX_KEYWORDS = frozenset({
    "refactor", "redesign", "migrate", "security", "concurrency",
    "リファクタ", "再設計", "移行", "セキュリティ", "並行",
})

# 結果の永続化先（dev-workflow フェーズ E の record_tier_outcome.py が読む）
_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(_HOOKS_DIR)
TIER_SELECTION_PATH = os.path.join(_CLAUDE_DIR, "state", "tier_selection.json")

TIERS: tuple[str, ...] = ("haiku", "sonnet", "opus")


def estimate_complexity(prompt: str) -> str:
    """prompt 文字列から complexity を推定する（簡易ヒューリスティック）。

    Returns:
        ``"simple"`` / ``"medium"`` / ``"complex"`` のいずれか。
    """
    text = prompt.lower()
    n = len(prompt)

    # complex の判定を先に: 800 文字以上、または明確な複雑キーワード
    if n >= 800:
        return "complex"
    if any(kw in text for kw in COMPLEX_KEYWORDS):
        return "complex"

    # simple: 200 文字未満、かつ simple キーワードを含む
    if n < 200 and any(kw in text for kw in SIMPLE_KEYWORDS):
        return "simple"

    return "medium"


def select_tier(
    params: dict[str, tuple[float, float, int]],
    *,
    rng: random.Random | None = None,
) -> tuple[str, str]:
    """Beta サンプリングまたは uniform 選択で推奨 Tier を返す。

    Args:
        params: ``read_tier_params`` の戻り値。
            ``{"haiku": (alpha, beta, trials), ...}``
        rng: テスト用に決定論的にしたい場合は ``random.Random(seed)`` を渡す。

    Returns:
        ``(tier, mode)`` のタプル。``mode`` は ``"thompson"`` / ``"uniform"`` で、
        プロンプトに「学習データ収集中」と表示するかの分岐に使う。
    """
    rng = rng or random
    total_trials = sum(p[2] for p in params.values())
    if total_trials < LEARNING_THRESHOLD:
        return rng.choice(TIERS), "uniform"

    # 純 Thompson Sampling: 各 tier の Beta(α, β) からサンプリング、最大値を選ぶ
    samples = {
        tier: rng.betavariate(p[0], p[1])
        for tier, p in params.items()
    }
    chosen = max(samples, key=lambda t: samples[t])
    return chosen, "thompson"


# Phase 2-B: 失敗率による昇格マッピング（haiku → sonnet, sonnet → opus）。
# opus からの昇格はなし（最上位）。
_ESCALATION_MAP: dict[str, str] = {
    "haiku": "sonnet",
    "sonnet": "opus",
}

# Phase 2-B: failure rate がこの値以上で escalation 判定。
ESCALATION_THRESHOLD = 0.5


def maybe_escalate(
    complexity: str,
    chosen_tier: str,
    *,
    failure_rate_fn=None,
) -> tuple[str, str | None]:
    """Phase 2-B: failure rate が高ければ 1 段昇格する。

    Args:
        complexity: ``simple`` / ``medium`` / ``complex``。
        chosen_tier: select_tier が選んだ tier。
        failure_rate_fn: テスト用に注入可能な
            ``(complexity, tier) -> (rate_or_None, sample_count)``。
            省略時は :func:`c3_db.read_tier_failure_rate` を使う。

    Returns:
        ``(effective_tier, escalation_reason)``。
        昇格しない場合は ``escalation_reason`` が None。
        opus はそれ以上昇格できないので常に元の tier を返す。
    """
    if chosen_tier not in _ESCALATION_MAP:
        return chosen_tier, None

    if failure_rate_fn is None:
        # 既定の DB ヘルパーを呼ぶ（C3 開発版なら parallel_orchestra.c3_db、
        # 配布版でも同モジュールが import 可能）。
        c3_db = _load_c3_db_module()
        if c3_db is None:
            return chosen_tier, None

        def failure_rate_fn(complexity: str, tier: str):  # type: ignore[no-redef]
            return c3_db.read_tier_failure_rate(complexity, tier)

    rate, samples = failure_rate_fn(complexity, chosen_tier)
    if rate is None or rate < ESCALATION_THRESHOLD:
        return chosen_tier, None

    escalated = _ESCALATION_MAP[chosen_tier]
    reason = (
        f"{chosen_tier}_failure_rate={rate:.2f} "
        f"({samples} 試行) → {escalated} に昇格"
    )
    return escalated, reason


def write_tier_selection(
    complexity: str,
    tier: str,
    mode: str,
    *,
    escalated: bool = False,
    escalation_reason: str | None = None,
) -> None:
    """直近の選択結果を ``tier_selection.json`` に書く。

    record_tier_outcome.py がこの json を読んで α/β を更新する。
    既存ファイルは上書きされる（最新 1 件のみ保持）。

    F-005 Phase 2-A: ``suggested_model`` も併せて書く。runner.py がこれを読んで
    PO 経由のサブエージェント起動時に ``claude --agents`` で動的に上書きする。
    tier 名と model の短縮名は同一とする。

    F-005 Phase 2-B: ``escalated`` / ``escalation_reason`` を任意で含める。
    failure rate に基づく昇格が起きた場合のみ True / 文字列が入る。
    """
    os.makedirs(os.path.dirname(TIER_SELECTION_PATH), exist_ok=True)
    payload: dict[str, object] = {
        "complexity": complexity,
        "tier": tier,
        "mode": mode,
        # Phase 2-A: tier はそのまま claude --agents の model 短縮名として使える
        "suggested_model": tier,
    }
    if escalated:
        payload["escalated"] = True
        if escalation_reason:
            payload["escalation_reason"] = escalation_reason
    try:
        with open(TIER_SELECTION_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except OSError as exc:
        print(
            f"[select_tier] failed to write {TIER_SELECTION_PATH}: {exc}",
            file=sys.stderr,
        )


def build_additional_context(
    complexity: str, tier: str, mode: str,
    params: dict[str, tuple[float, float, int]],
    *,
    escalation_reason: str | None = None,
) -> str:
    """親 Claude に追加注入する文字列を組み立てる。"""
    trials = sum(p[2] for p in params.values())
    if mode == "uniform":
        confidence = f"学習データ収集中（合計 {trials}/{LEARNING_THRESHOLD} 試行）"
    else:
        tier_trials = params[tier][2]
        confidence = f"信頼度 trials={tier_trials}"

    suffix = ""
    if escalation_reason:
        suffix = f" [Phase 2-B 昇格: {escalation_reason}]"

    return (
        f"[F-005 Tier 推奨] 複雑度: {complexity} / 推奨 Tier: {tier}（{confidence}）。"
        f"PO 経由のサブエージェント起動時はこの推奨が claude --agents JSON で"
        f" 自動適用されます（Phase 2-A）。親 Claude の Agent ツール経由は依然"
        f" frontmatter 指定が優先されるため、コスト最適化したい場合は手動切替"
        f" してください。{suffix}"
    )


def _load_c3_db_module():
    """parallel_orchestra.c3_db を import 可能にして返す。"""
    here = Path(__file__).resolve()
    src = here.parents[2] / "src"
    if src.is_dir():
        src_str = str(src)
        if src_str not in sys.path:
            sys.path.insert(0, src_str)
    try:
        from parallel_orchestra import c3_db  # type: ignore[import-not-found]
        return c3_db
    except ImportError as exc:
        print(f"[select_tier] c3_db import failed: {exc}", file=sys.stderr)
        return None


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return 0

    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str) or not prompt.strip():
        return 0

    complexity = estimate_complexity(prompt)

    c3_db = _load_c3_db_module()
    if c3_db is None:
        # DB ヘルパーが無い環境でも uniform 選択で推奨を返す
        params = {t: (1.0, 1.0, 0) for t in TIERS}
    else:
        params = c3_db.read_tier_params(complexity)

    tier, mode = select_tier(params)

    # Phase 2-B: failure rate に基づく escalation
    effective_tier, escalation_reason = maybe_escalate(complexity, tier)
    escalated = effective_tier != tier

    write_tier_selection(
        complexity, effective_tier, mode,
        escalated=escalated, escalation_reason=escalation_reason,
    )

    context_text = build_additional_context(
        complexity, effective_tier, mode, params,
        escalation_reason=escalation_reason,
    )
    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context_text,
        }
    }
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
