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

import collections
import difflib
import hashlib
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

# Phase 2-C: prompt 履歴は別ファイル（権限分離・軽量）。
# プライバシー対策: prefix 200 文字 + SHA256 hash のみで full prompt は保存しない。
PROMPT_HISTORY_PATH = os.path.join(_CLAUDE_DIR, "logs", "prompt-history.jsonl")

# prompt prefix の最大保存文字数
_PROMPT_PREFIX_MAX = 200

# 類似度の閾値
SIMILARITY_STRONG_THRESHOLD = 0.8  # この値以上で complexity を上書き
SIMILARITY_WEAK_THRESHOLD = 0.6    # この値以上で信頼度補強のみ

# prompt-history.jsonl の末尾から読む最大行数（パフォーマンス対策）
_PROMPT_HISTORY_SCAN_LINES = 1000

TIERS: tuple[str, ...] = ("haiku", "sonnet", "opus")


def _prompt_prefix_and_hash(prompt: str) -> tuple[str, str]:
    """prompt から (prefix, hash) を抽出する。

    Phase 2-C: prefix 200 文字 + SHA256 先頭 16 文字（プライバシー対策）。
    """
    prefix = prompt[:_PROMPT_PREFIX_MAX]
    h = hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()[:16]
    return prefix, h


def _read_prompt_history() -> list[dict]:
    """prompt-history.jsonl から末尾 N 行を読み込む。

    各行は ``{"ts", "prompt_hash", "prompt_prefix", "complexity", "tier", "outcome"}``
    の形式（record_tier_outcome.py が書き込む）。
    壊れた行はスキップ。ファイル不在時は空リスト。
    """
    if not os.path.isfile(PROMPT_HISTORY_PATH):
        return []
    records: list[dict] = []
    try:
        with open(PROMPT_HISTORY_PATH, "r", encoding="utf-8") as f:
            for line in collections.deque(f, maxlen=_PROMPT_HISTORY_SCAN_LINES):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    records.append(data)
    except OSError:
        return []
    return records


def similarity_boost(
    prompt: str,
    *,
    history: list[dict] | None = None,
) -> tuple[str | None, list[tuple[float, dict]]]:
    """prompt-history との類似度から complexity 上書き候補を返す。

    Phase 2-C: difflib.SequenceMatcher で過去プロンプトと比較する。

    Args:
        prompt: 現在の prompt 文字列。
        history: テスト用に注入可能な history。省略時は ファイルから読む。

    Returns:
        ``(strong_complexity, weak_matches)`` のタプル。

        - ``strong_complexity``: 類似度 ≥ ``SIMILARITY_STRONG_THRESHOLD`` の
          エントリが見つかれば最新の complexity（上書き候補）。なければ None。
        - ``weak_matches``: 類似度 ≥ ``SIMILARITY_WEAK_THRESHOLD`` のリスト。
          ``additionalContext`` の信頼度補強表示に使う。各要素は
          ``(ratio, history_entry)``。
    """
    if history is None:
        history = _read_prompt_history()
    if not history:
        return None, []

    # 現在 prompt の prefix で比較（完全一致を狙うのではなく、書き出しの傾向を見る）
    current_prefix = prompt[:_PROMPT_PREFIX_MAX]

    weak_matches: list[tuple[float, dict]] = []
    strong_candidates: list[tuple[str, str]] = []  # (ts, complexity)

    for entry in history:
        past_prefix = entry.get("prompt_prefix", "")
        if not isinstance(past_prefix, str) or not past_prefix:
            continue
        ratio = difflib.SequenceMatcher(None, current_prefix, past_prefix).ratio()
        if ratio >= SIMILARITY_STRONG_THRESHOLD:
            ts = entry.get("ts", "")
            complexity = entry.get("complexity")
            if isinstance(complexity, str) and complexity:
                strong_candidates.append((ts, complexity))
        elif ratio >= SIMILARITY_WEAK_THRESHOLD:
            weak_matches.append((ratio, entry))

    if not strong_candidates:
        return None, weak_matches

    # 強類似が複数あれば最新（ts が大きい）の complexity を採用
    strong_candidates.sort(key=lambda x: x[0], reverse=True)
    return strong_candidates[0][1], weak_matches


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
        # 既定の DB ヘルパーを呼ぶ（v1.11.0 以降は c3.db、それより前は
        # parallel_orchestra.c3_db、いずれも shim で透過的に解決される）。
        c3_db = _load_c3_db_module()
        if c3_db is None:
            return chosen_tier, None

        def _db_failure_rate(complexity: str, tier: str) -> tuple:
            return c3_db.read_tier_failure_rate(complexity, tier)
        failure_rate_fn = _db_failure_rate

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
    prompt_prefix: str | None = None,
    prompt_hash: str | None = None,
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
    # Phase 2-C: prompt 情報を追加（record_tier_outcome.py が
    # prompt-history.jsonl に追記する際に参照する）
    if prompt_prefix is not None:
        payload["prompt_prefix"] = prompt_prefix
    if prompt_hash is not None:
        payload["prompt_hash"] = prompt_hash
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
    complexity_source: str | None = None,
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
        suffix += f" [Phase 2-B 昇格: {escalation_reason}]"
    if complexity_source:
        suffix += f" [複雑度判定: {complexity_source}]"

    return (
        f"[F-005 Tier 推奨] 複雑度: {complexity} / 推奨 Tier: {tier}（{confidence}）。"
        f"PO 経由のサブエージェント起動時はこの推奨が claude --agents JSON で"
        f" 自動適用されます（Phase 2-A）。親 Claude の Agent ツール経由は依然"
        f" frontmatter 指定が優先されるため、コスト最適化したい場合は手動切替"
        f" してください。{suffix}"
    )


def _load_c3_db_module():
    """c3.db helper モジュールを返す。

    v1.11.0 で parallel_orchestra.c3_db から c3.db に物理移動した。
    c3 パッケージは pip install 済みのため sys.path 操作は不要。
    """
    try:
        from c3 import db as c3_db  # type: ignore[import-not-found]
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

    # Phase 2-C: 類似度推定で complexity を補強
    heuristic_complexity = estimate_complexity(prompt)
    strong_complexity, weak_matches = similarity_boost(prompt)
    if strong_complexity:
        complexity = strong_complexity
        complexity_source = f"similarity (overridden from {heuristic_complexity})"
    else:
        complexity = heuristic_complexity
        complexity_source = (
            f"heuristic (weak similar={len(weak_matches)})"
            if weak_matches else "heuristic"
        )

    prompt_prefix, prompt_hash = _prompt_prefix_and_hash(prompt)

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
        prompt_prefix=prompt_prefix,
        prompt_hash=prompt_hash,
    )

    context_text = build_additional_context(
        complexity, effective_tier, mode, params,
        escalation_reason=escalation_reason,
        complexity_source=complexity_source,
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
