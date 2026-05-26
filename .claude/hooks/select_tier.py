#!/usr/bin/env python3
"""UserPromptSubmit hook: recommend an LLM Tier (haiku/sonnet/opus) for the prompt.

tier-routing MVP: タスクの複雑度を簡易ヒューリスティックで推定し、Thompson Sampling
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
import math
import os
import random
import re
import sys
from typing import NamedTuple
from pathlib import Path

try:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


# 学習データ収集期の閾値（合計試行数がこの値未満なら uniform 選択）。
# SSOT: c3.db.LEARNING_THRESHOLD から取得し、import 失敗時はフォールバック値 30 を使う（CR-M-002）。
try:
    from c3 import db as _c3_db_const  # type: ignore[import-not-found]
    LEARNING_THRESHOLD: int = _c3_db_const.LEARNING_THRESHOLD
    EPSILON: float = _c3_db_const.EPSILON_TIEBREAK
    ESCALATION_THRESHOLD: float = _c3_db_const.ESCALATION_THRESHOLD_DEFAULT
    COST_LAMBDA_DEFAULT: float | None = _c3_db_const.COST_LAMBDA_DEFAULT
    COST_LAMBDA_MIN: float = _c3_db_const.COST_LAMBDA_MIN
    COST_LAMBDA_MAX: float = _c3_db_const.COST_LAMBDA_MAX
except ImportError:
    LEARNING_THRESHOLD = 30
    EPSILON = 0.05
    ESCALATION_THRESHOLD = 0.5
    COST_LAMBDA_DEFAULT = None
    COST_LAMBDA_MIN = 0.0
    COST_LAMBDA_MAX = 5.0

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

# prompt 保存前のマスク処理: pre_tool.py の _SECRET_PATTERNS と同等のパターン。
# 検出した値を *** に置換してから保存することで二次漏洩を防ぐ。
# NOTE: ここで値をマスクすることで類似度推定の精度も若干下がる可能性があるが、
#       セキュリティ優先として許容する（設計書に記載なし: SR-V-001 対応と判断）。
_MASK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r'(password=)\S+', re.IGNORECASE),
    re.compile(r'(api[_-]?key=)\S+', re.IGNORECASE),
    re.compile(r'(Bearer\s+)[\w\-\.]+', re.IGNORECASE),
    re.compile(r'(\btoken=)\S+', re.IGNORECASE),
    re.compile(r'(\bsecret=)\S+', re.IGNORECASE),
    re.compile(r'(aws_secret_access_key=)\S+', re.IGNORECASE),
    re.compile(r'(-----BEGIN [A-Z ]*PRIVATE KEY-----)[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----)'),
]


def _mask_secrets(text: str) -> str:
    """秘密情報パターンにマッチする値部分を *** に置換して返す。

    キー名やプレフィックスは残し、値のみを置換することで
    「何が含まれていたか」は伝わらないようにする。
    PEM ブロックは開始タグ + *** + 終了タグ に置換する。
    """
    result = text
    for pattern in _MASK_PATTERNS:
        # group(2) があれば PEM ブロック (BEGIN...END)、なければプレフィックス系
        result = pattern.sub(
            lambda m: m.group(1) + "***" + (m.group(2) if m.lastindex and m.lastindex >= 2 else ""),
            result,
        )
    return result

# prompt-history.jsonl の末尾から読む最大行数（パフォーマンス対策）
_PROMPT_HISTORY_SCAN_LINES = 1000

TIERS: tuple[str, ...] = ("haiku", "sonnet", "opus")

# cost-aware tie-break の拮抗判定閾値（v2.23.0・SSOT は db.EPSILON_TIEBREAK）。
# Beta サンプルは 0〜1 スケール。成功率 5pt 以内＝実質同等とみなす拮抗判定閾値。
# 過大は成功率犠牲リスク、過小は無発動。C3_TIER_EPSILON env で上書き可（v2.25.0）。


class SelectionResult(NamedTuple):
    """select_tier_detailed の戻り値（NamedTuple = immutable）。

    tier: 選択された tier 名。
    mode: "uniform" または "thompson"。
    cost_tiebreak: Thompson 分岐で cost tie-break が発動した場合 True。
    contenders: 拮抗判定に入った tier のタプル（observability/デバッグ用）。
        frozen 安全のため list ではなく tuple を使用。
    cost_weighted: λ>0 全 tier weighting が適用された場合 True（v2.26.0）。
        lam=None（env 未設定）または cost_map=None の場合は False のまま。
        uniform 分岐では常に False（探索保護・不可侵）。
    """

    tier: str
    mode: str
    cost_tiebreak: bool = False
    contenders: tuple[str, ...] = ()
    cost_weighted: bool = False


def _cost_tiebreak(
    samples: dict[str, float],
    cost_map: dict[str, float] | None,
    *,
    epsilon: float = EPSILON,
    lam: float | None = None,
) -> tuple[str, bool, tuple[str, ...]]:
    """Thompson サンプルから cost を考慮して最適な tier を返す（3 経路）。

    lam（λ）の値によって 3 つの経路に分岐する:
      - 経路 0（cost_map is None or lam == 0）: cost を見ない。argmax(sample) を返す。
        lam=None を渡しても cost_map が None なら経路 0。lam=0.0 の明示オプトも経路 0。
      - 経路 1（lam is None・既定）: v2.25.0 の ε-gated min-max 最安（後方互換）。
        contenders ≤ 1 は argmax、複数 contenders は min-max 正規化コスト最安を選ぶ。
        lam=None（env 未設定センチネル）がデフォルト→ v2.25.0 挙動と完全一致。
      - 経路 2（lam > 0）: 全 tier weighting。
        score[t] = sample[t] - lam * cost_norm[t] で全 tier を比較し最大を選ぶ。
        cost_norm は全 tier の min-max 正規化（最安→0・最高→1）。

    Args:
        samples: {tier: beta_sample} の dict（Thompson Sampling 結果）。
        cost_map: {tier: cost} の dict。None なら cost を見ず従来挙動（経路 0）。
            cost は実測 rate_usd_per_mtok または静的参照単価（ハイブリッド）。
            v2.24.0 で rate 化（USD/MTok）により実測・静的とも同次元で整合済み。
            ``cost_map`` は None、または samples の全 tier キーを含む dict を渡すこと。
            partial dict を渡すと ``cost_map[t]`` で KeyError が発生する。
            ``select_tier_detailed`` 経由では呼び出し側（main）が全 TIERS 分を
            構築して保証する。
        epsilon: 拮抗判定の閾値（デフォルト EPSILON=0.05）。経路 0/1 で使用。
            経路 2 では contenders 算出にのみ使用（選択自体は全 tier score 比較）。
        lam: cost weighting 係数（λ）。None=センチネル（経路 1・後方互換）、
            0.0=cost 無視明示（経路 0）、0 < lam <= COST_LAMBDA_MAX=全 tier weighting（経路 2）。
            デフォルト None で既存 2 引数呼び出しの挙動・シグネチャを完全不変にする。

    Returns:
        (chosen, did_tiebreak, contenders) のタプル。
        - chosen: 選択された tier 名。
        - did_tiebreak: cost が選択に影響した場合 True。
            経路 1: contenders 内 min-max で安い方を選んだ場合 True（全 tier コスト同値時は False）。
            経路 2: 全 tier weighting で argmax(sample) と異なる選択になった場合 True。
        - contenders: ε 拮抗判定に入った tier のタプル（observability 用）。
    """
    max_sample = max(samples.values())
    contenders = [t for t in samples if max_sample - samples[t] <= epsilon]

    # 経路 0: cost を見ない（cost_map なし or λ=0 明示）。
    # None == 0 は Python では False のため lam == 0 は float 0.0 のみ真（意図通り）。
    if cost_map is None or lam == 0:
        chosen = max(samples, key=lambda t: samples[t])
        return chosen, False, tuple(contenders)

    # 経路 1（lam=None・既定）: v2.25.0 の ε-gated min-max 最安（現行ロジック完全踏襲）。
    if lam is None:
        if len(contenders) <= 1:
            chosen = max(samples, key=lambda t: samples[t])
            return chosen, False, tuple(contenders)
        # 拮抗群内で min-max 正規化コストを計算し最安 tier を選ぶ
        costs = {t: cost_map[t] for t in contenders}
        lo, hi = min(costs.values()), max(costs.values())
        if hi == lo:
            # [CR-Q-001] 全 tier コスト同値: cost は選択に無関与。
            # argmax(sample) を返し did_tiebreak=False（observability 精緻化・v2.27.0 で精緻化済み）。
            chosen = max(samples, key=lambda t: samples[t])
            return chosen, False, tuple(contenders)
        norm = {t: (costs[t] - lo) / (hi - lo) for t in contenders}
        chosen = min(contenders, key=lambda t: (norm[t], -samples[t]))
        return chosen, True, tuple(contenders)

    # 経路 2（lam > 0）: 全 tier weighting。
    # cost_norm は全 tier で min-max 正規化（最安→0・最高→1）。
    costs = {t: cost_map[t] for t in samples}
    lo, hi = min(costs.values()), max(costs.values())
    norm = {t: ((costs[t] - lo) / (hi - lo) if hi > lo else 0.0) for t in samples}
    score = {t: samples[t] - lam * norm[t] for t in samples}
    # 同点は sample 大優先（決定論）
    chosen = max(samples, key=lambda t: (score[t], samples[t]))
    pure_max = max(samples, key=lambda t: samples[t])
    cost_tiebreak = chosen != pure_max
    return chosen, cost_tiebreak, tuple(contenders)


def _prompt_prefix_and_hash(prompt: str) -> tuple[str, str]:
    """prompt から (prefix, hash) を抽出する。

    Phase 2-C: prefix 200 文字 + SHA256 先頭 16 文字（プライバシー対策）。
    SR-V-001: prefix に含まれる秘密情報パターンは *** にマスクしてから保存する。
    hash はマスク前の原文から計算する（一意性を保つため）。
    """
    prefix = _mask_secrets(prompt[:_PROMPT_PREFIX_MAX])
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


def select_tier_detailed(
    params: dict[str, tuple[float, float, int]],
    *,
    rng: random.Random | None = None,
    cost_map: dict[str, float] | None = None,
    epsilon: float | None = None,
    lam: float | None = None,
) -> SelectionResult:
    """Beta サンプリングまたは uniform 選択で推奨 Tier を SelectionResult で返す。

    Args:
        params: ``read_tier_params`` の戻り値。
            ``{"haiku": (alpha, beta, trials), ...}``
        rng: テスト用に決定論的にしたい場合は ``random.Random(seed)`` を渡す。
        cost_map: {tier: cost} の dict、または None。
            None（cost を見ない＝従来 Thompson）または「params の全 tier キーを
            含む完全な dict」のいずれか。partial dict は渡されない前提
            （呼び出し側が全 TIERS 分を構築して保証する）。
            uniform 分岐では cost_map の有無に関わらず完全無視する（探索保護）。
        epsilon: 拮抗判定閾値。None なら module 定数 EPSILON を使う（C3_TIER_EPSILON で上書き可）。
        lam: cost weighting 係数（λ）。None=センチネル（v2.25.0 ε-gated 後方互換）、
            0.0=cost 無視明示、0<lam<=COST_LAMBDA_MAX=全 tier weighting 発動（C3_TIER_COST_LAMBDA で上書き可）。
            None（デフォルト）では env 未設定時と完全一致する（後方互換の核心）。
            uniform 分岐では lam の値に関わらず完全無視する（探索保護・不可侵）。

    Returns:
        SelectionResult（tier, mode, cost_tiebreak, contenders, cost_weighted）。
        mode は ``"thompson"`` / ``"uniform"``。
        cost_weighted は lam>0 かつ cost_map が有効な場合のみ True。
    """
    rng = rng or random
    total_trials = sum(p[2] for p in params.values())
    if total_trials < LEARNING_THRESHOLD:
        # uniform: cost/λ を完全無視・従来挙動完全維持（不可侵）
        return SelectionResult(rng.choice(TIERS), "uniform", False, ())

    # Thompson Sampling: rng の消費順序を従来 select_tier と完全一致させる
    samples = {
        tier: rng.betavariate(p[0], p[1])
        for tier, p in params.items()
    }
    eff_epsilon = epsilon if epsilon is not None else EPSILON
    chosen, did_tiebreak, contenders = _cost_tiebreak(samples, cost_map, epsilon=eff_epsilon, lam=lam)
    cost_weighted = (cost_map is not None and lam is not None and lam > 0)
    return SelectionResult(chosen, "thompson", did_tiebreak, contenders, cost_weighted)


def select_tier(
    params: dict[str, tuple[float, float, int]],
    *,
    rng: random.Random | None = None,
    cost_map: dict[str, float] | None = None,
    epsilon: float | None = None,
    lam: float | None = None,
) -> tuple[str, str]:
    """Beta サンプリングまたは uniform 選択で推奨 Tier を返す。

    Args:
        params: ``read_tier_params`` の戻り値。
            ``{"haiku": (alpha, beta, trials), ...}``
        rng: テスト用に決定論的にしたい場合は ``random.Random(seed)`` を渡す。
        cost_map: {tier: cost} の dict、または None。
            None なら cost を見ず従来の Thompson Sampling と完全一致。
            uniform 分岐では cost_map の有無に関わらず完全無視する。
            詳細は :func:`select_tier_detailed` を参照。
        epsilon: 拮抗判定閾値。None なら module 定数 EPSILON を使う。
        lam: cost weighting 係数（λ）。select_tier_detailed に委譲する。
            None=センチネル（v2.25.0 後方互換）、0.0=cost 無視、0<lam<=1=全 tier weighting。

    Returns:
        ``(tier, mode)`` のタプル。``mode`` は ``"thompson"`` / ``"uniform"`` で、
        プロンプトに「学習データ収集中」と表示するかの分岐に使う。
        戻り値型は v2.22.0 以前と完全に不変。
    """
    result = select_tier_detailed(params, rng=rng, cost_map=cost_map, epsilon=epsilon, lam=lam)
    return result.tier, result.mode


# Phase 2-B: 失敗率による昇格マッピング（haiku → sonnet, sonnet → opus）。
# opus からの昇格はなし（最上位）。
_ESCALATION_MAP: dict[str, str] = {
    "haiku": "sonnet",
    "sonnet": "opus",
}

# Phase 2-B: failure rate がこの値以上で escalation 判定。
# SSOT: db.ESCALATION_THRESHOLD_DEFAULT 由来（import 部で取得）。C3_ESCALATION_THRESHOLD env で上書き可（v2.26.0）。


def _db_failure_rate(complexity: str, tier: str) -> tuple:
    """DB から failure rate を読み取るデフォルト実装。

    c3_db のインポートに失敗した場合は ``(None, 0)`` を返す。
    """
    c3_db = _load_c3_db_module()
    if c3_db is None:
        return None, 0
    return c3_db.read_tier_failure_rate(complexity, tier)


def maybe_escalate(
    complexity: str,
    chosen_tier: str,
    *,
    failure_rate_fn=None,
    threshold: float | None = None,
) -> tuple[str, str | None]:
    """Phase 2-B: failure rate が高ければ 1 段昇格する。

    Args:
        complexity: ``simple`` / ``medium`` / ``complex``。
        chosen_tier: select_tier が選んだ tier。
        failure_rate_fn: テスト用に注入可能な
            ``(complexity, tier) -> (rate_or_None, sample_count)``。
            省略時は :func:`_db_failure_rate` を使う。
        threshold: escalation 閾値（failure rate がこの値以上で昇格）。
            None のとき module 定数 ``ESCALATION_THRESHOLD`` を使う。
            ``main()`` は ``_resolve_escalation_threshold()`` で解決した値を渡す。

    Returns:
        ``(effective_tier, escalation_reason)``。
        昇格しない場合は ``escalation_reason`` が None。
        opus はそれ以上昇格できないので常に元の tier を返す。
    """
    if chosen_tier not in _ESCALATION_MAP:
        return chosen_tier, None

    effective_fn = failure_rate_fn or _db_failure_rate
    rate, samples = effective_fn(complexity, chosen_tier)
    eff_threshold = threshold if threshold is not None else ESCALATION_THRESHOLD
    if rate is None or rate < eff_threshold:
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
    session_id: str | None = None,
    cost_tiebreak: bool = False,
    cost_weighted: bool = False,
    cost_lambda: float | None = None,
) -> None:
    """直近の選択結果を ``tier_selection.json`` に書く。

    record_tier_outcome.py がこの json を読んで α/β を更新する。
    既存ファイルは上書きされる（最新 1 件のみ保持）。

    ``suggested_model`` を併せて書く。tier 名と model の短縮名は同一とする。
    （PO 廃止前は runner.py が読んで ``claude --agents`` 用に使っていたが、v2.0.0 以降は
    記録目的のみ。将来再利用する余地のために維持する。）

    ``escalated`` / ``escalation_reason`` を任意で含める。
    failure rate に基づく昇格が起きた場合のみ True / 文字列が入る。

    ``session_id`` を任意で含める。UserPromptSubmit payload の session UUID。
    None のときは tier_selection.json のキー自体を省略する（後方互換）。

    ``cost_tiebreak`` を任意で含める（v2.23.0）。
    Thompson Sampling の拮抗群内で cost tie-break が発動した場合のみ True。
    False のときはキー自体を省略する（escalated/session_id と同パターン）。

    ``cost_weighted`` を任意で含める（v2.26.0）。
    λ>0 の全 tier weighting が適用された場合のみ True。
    False のときはキー自体を省略する（cost_tiebreak と同パターン）。

    ``cost_lambda`` を任意で含める（v2.26.0）。
    ``_resolve_cost_lambda()`` で解決した λ 値（None 以外のとき出力）。
    None のときはキー自体を省略する（env 未設定時の後方互換）。
    """
    os.makedirs(os.path.dirname(TIER_SELECTION_PATH), exist_ok=True)
    payload: dict[str, object] = {
        "complexity": complexity,
        "tier": tier,
        "mode": mode,
        # tier はそのまま claude --agents の model 短縮名として使える
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
    if session_id is not None:
        payload["session_id"] = session_id
    if cost_tiebreak:
        payload["cost_tiebreak"] = True
    if cost_weighted:
        payload["cost_weighted"] = True
    if cost_lambda is not None:
        payload["cost_lambda"] = cost_lambda
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
    cost_tiebreak: bool = False,
    cost_weighted: bool = False,
) -> str:
    """親 Claude に追加注入する文字列を組み立てる。

    ``cost_tiebreak`` が True のとき、suffix に cost-aware 発動を示す文言を追加する（v2.23.0）。
    False のときは不変（既存文言と完全一致）。

    ``cost_weighted`` が True のとき、cost-weighted 文言を suffix に追加する（v2.26.0）。
    True のときは cost_tiebreak の文言より優先される（λ>0 全 tier weighting を明示）。
    False のときは cost_tiebreak による既存文言のみ（v2.25.0 以前と完全一致）。
    """
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
    if cost_weighted:
        suffix += " [cost-weighted: 成功率とコストを加重して選択]"
    elif cost_tiebreak:
        suffix += " [cost-aware: 成功率拮抗のため低コスト Tier を選択]"

    return (
        f"[tier-routing 推奨] 複雑度: {complexity} / 推奨 Tier: {tier}（{confidence}）。"
        f" 親 Claude の Agent ツール経由ではエージェント定義の frontmatter 指定が"
        f" 優先されるため、コスト最適化したい場合は手動切替してください。{suffix}"
    )


def _load_c3_db_module():
    """c3.db helper モジュールを返す。

    c3 パッケージは pip install 済みのため sys.path 操作は不要。
    """
    try:
        from c3 import db as c3_db  # type: ignore[import-not-found]
        return c3_db
    except ImportError as exc:
        print(f"[select_tier] c3_db import failed: {exc}", file=sys.stderr)
        return None


def _resolve_epsilon() -> float:
    """``C3_TIER_EPSILON`` を安全に解決する。

    不正値（非数値 / 0 以下 / 1 超 / NaN）は受け付けず、stderr 警告 + デフォルト（EPSILON）に戻す。
    未設定 / 空文字は無警告でデフォルトを返す（[SR-V-001]）。
    """
    raw = os.environ.get("C3_TIER_EPSILON")
    if raw is None or raw == "":
        return EPSILON
    try:
        x = float(raw)
    except ValueError:
        print(
            f"[select_tier:epsilon] invalid C3_TIER_EPSILON={raw!r}, "
            f"using default {EPSILON}",
            file=sys.stderr,
        )
        return EPSILON
    if math.isnan(x):
        print(
            f"[select_tier:epsilon] C3_TIER_EPSILON={raw!r} is NaN, "
            f"using default {EPSILON}",
            file=sys.stderr,
        )
        return EPSILON
    if x <= 0 or x > 1:
        print(
            f"[select_tier:epsilon] C3_TIER_EPSILON={x} out of range (0, 1], "
            f"using default {EPSILON}",
            file=sys.stderr,
        )
        return EPSILON
    return x


def _resolve_escalation_threshold() -> float:
    """``C3_ESCALATION_THRESHOLD`` を安全に解決する。

    不正値（非数値 / 0 以下 / 1 超 / NaN）は受け付けず、stderr 警告 + デフォルト（ESCALATION_THRESHOLD）に戻す。
    未設定 / 空文字は無警告でデフォルトを返す。
    妥当域: 0 < x <= 1（_resolve_epsilon と同じ範囲）。区間表記: (0, 1]（x=0 拒否のため半開区間）。
    """
    raw = os.environ.get("C3_ESCALATION_THRESHOLD")
    if raw is None or raw == "":
        return ESCALATION_THRESHOLD
    try:
        x = float(raw)
    except ValueError:
        print(
            f"[select_tier:escalation] invalid C3_ESCALATION_THRESHOLD={raw!r}, "
            f"using default {ESCALATION_THRESHOLD}",
            file=sys.stderr,
        )
        return ESCALATION_THRESHOLD
    if math.isnan(x):
        print(
            f"[select_tier:escalation] C3_ESCALATION_THRESHOLD={raw!r} is NaN, "
            f"using default {ESCALATION_THRESHOLD}",
            file=sys.stderr,
        )
        return ESCALATION_THRESHOLD
    if x <= 0 or x > 1:
        print(
            f"[select_tier:escalation] C3_ESCALATION_THRESHOLD={x!r} out of range (0, 1], "
            f"using default {ESCALATION_THRESHOLD}",
            file=sys.stderr,
        )
        return ESCALATION_THRESHOLD
    return x


def _resolve_cost_lambda() -> float | None:
    """``C3_TIER_COST_LAMBDA`` を安全に解決する。

    不正値（非数値 / 0 未満 / COST_LAMBDA_MAX 超 / NaN）は受け付けず、stderr 警告 + デフォルト（COST_LAMBDA_DEFAULT）に戻す。
    未設定 / 空文字は無警告でデフォルト（None）を返す。
    妥当域: 0 <= x <= COST_LAMBDA_MAX（x == 0 は許容＝cost 無視の明示オプト・_resolve_epsilon と異なり下限を含む）。区間表記: [0, COST_LAMBDA_MAX]（x=0 許容のため閉区間）。
    戻り値が None の場合は v2.25.0 互換の ε tie-break 経路を維持する（センチネル）。
    """
    raw = os.environ.get("C3_TIER_COST_LAMBDA")
    if raw is None or raw == "":
        return COST_LAMBDA_DEFAULT
    try:
        x = float(raw)
    except ValueError:
        print(
            f"[select_tier:cost_lambda] invalid C3_TIER_COST_LAMBDA={raw!r}, "
            f"using default {COST_LAMBDA_DEFAULT}",
            file=sys.stderr,
        )
        return COST_LAMBDA_DEFAULT
    if math.isnan(x):
        print(
            f"[select_tier:cost_lambda] C3_TIER_COST_LAMBDA={raw!r} is NaN, "
            f"using default {COST_LAMBDA_DEFAULT}",
            file=sys.stderr,
        )
        return COST_LAMBDA_DEFAULT
    if x < COST_LAMBDA_MIN or x > COST_LAMBDA_MAX:
        print(
            f"[select_tier:cost_lambda] C3_TIER_COST_LAMBDA={x!r} out of range [0, {COST_LAMBDA_MAX}], "
            f"using default {COST_LAMBDA_DEFAULT}",
            file=sys.stderr,
        )
        return COST_LAMBDA_DEFAULT
    return x


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return 0

    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str) or not prompt.strip():
        return 0

    session_id = payload.get("session_id")

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

    # v2.24.0: cost_map をハイブリッド解決（実測 rate USD/MTok を主に、欠損 tier は静的単価で補完）。
    # rate 化により measured(実測)と tier_reference_cost(静的)が同次元（USD/MTok）になり単位整合済み。
    # c3_db が None または pricing import 失敗時は cost_map=None で従来 Thompson にデグレード。
    # cost_map=None の場合、select_tier_detailed/_cost_tiebreak は従来 Thompson 挙動と完全一致（引数定義参照）。
    cost_map = None
    if c3_db is not None:
        try:
            from c3 import pricing  # type: ignore[import-not-found]
            measured = c3_db.read_tier_cost_rate_for_complexity(complexity)  # {tier: rate USD/MTok} 実測>0 のみ
            cost_map = {}
            for tier_name in TIERS:
                if tier_name in measured and measured[tier_name] > 0:
                    cost_map[tier_name] = measured[tier_name]
                else:
                    # tier_reference_cost は未知 tier に 0.0 を返すが、TIERS と _TIER_REFERENCE_KEY は
                    # 同期前提のため現行 3 tier（haiku/sonnet/opus）では 0.0 混入は起きない。
                    cost_map[tier_name] = pricing.tier_reference_cost(tier_name)  # 静的 fallback
        except ImportError:
            cost_map = None

    eps = _resolve_epsilon()
    lam = _resolve_cost_lambda()
    result = select_tier_detailed(params, cost_map=cost_map, epsilon=eps, lam=lam)
    tier, mode = result.tier, result.mode
    cost_tiebreak = result.cost_tiebreak

    # Phase 2-B: failure rate に基づく escalation
    esc_thr = _resolve_escalation_threshold()
    effective_tier, escalation_reason = maybe_escalate(complexity, tier, threshold=esc_thr)
    escalated = effective_tier != tier

    write_tier_selection(
        complexity, effective_tier, mode,
        escalated=escalated, escalation_reason=escalation_reason,
        prompt_prefix=prompt_prefix,
        prompt_hash=prompt_hash,
        session_id=session_id,
        cost_tiebreak=cost_tiebreak,
        cost_weighted=result.cost_weighted,
        cost_lambda=lam,
    )

    context_text = build_additional_context(
        complexity, effective_tier, mode, params,
        escalation_reason=escalation_reason,
        complexity_source=complexity_source,
        cost_tiebreak=cost_tiebreak,
        cost_weighted=result.cost_weighted,
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
