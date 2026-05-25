"""Claude API モデルの USD/MTok 単価からトークンコストを計算する純関数モジュール。

出典: https://platform.claude.com/docs/en/about-claude/pricing
取得日: 2026-05-25

単価は改定されうるため、メンテ時はこの URL を再確認すること。
（本モジュールの _PRICING dict と docstring の日付を合わせて更新する）

公開 API:
  - resolve_tier(model) -> str | None
  - compute_cost_usd(...) -> tuple[float, bool]
  - known_models() -> tuple[str, ...]

設計判断（plan-report T1 §2):
  Opus は世代で単価が約 3 倍異なる（4.1/4 = $15 系、4.5/4.6/4.7 = $5 系）ため、
  単純な "opus" 部分一致では取り違える。
  「現行世代を列挙マッチ → それ以外の opus は旧世代」の 2 段構成で実装する。
  具体的には: model に "opus-4-[5-9]" にマッチする場合は現行 ($5 系)、
  それ以外の opus（"claude-opus-4-YYYYMMDD" 形式の初代 Opus 4 / 4.1 等）は旧世代 ($15 系)。
  将来 Opus メジャー更新（5.x 等）が出たら本判定と _PRICING の見直しが必要。
  haiku は "haiku-3" を含む場合は旧世代。
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# 単価表 (USD/MTok)  取得日: 2026-05-25
# キー = pricing tier 識別子（resolve_pricing_key() が返す文字列）
# 値 = (input, output, cache_5m_write, cache_read)
# ---------------------------------------------------------------------------
# 現行世代（C3 が実際に使うモデル）
# 旧世代（過去ログ互換・現行 C3 では通常出現しない）

# 現行 Opus 世代（$5 系）: 4.5 / 4.6 / 4.7（将来 4.8/4.9 も同単価想定）
# それ以外の opus（初代 Opus 4 "claude-opus-4-YYYYMMDD" / 4.1 等）は旧世代 $15 系とする。
_CURRENT_OPUS_RE = re.compile(r"opus-4-[5-9]")

_PRICING: dict[str, tuple[float, float, float, float]] = {
    "opus-4-5":   (5.0,   25.0,  6.25, 0.50),   # Opus 4.5 / 4.6 / 4.7 共通
    "sonnet-4-5": (3.0,   15.0,  3.75, 0.30),   # Sonnet 4.5 / 4.6 共通
    "haiku-4-5":  (1.0,    5.0,  1.25, 0.10),   # Haiku 4.5
    "opus-4-1":   (15.0,  75.0, 18.75, 1.50),   # Opus 4.1 / 4（旧世代）
    "haiku-3-5":  (0.80,   4.0,  1.00, 0.08),   # Haiku 3.5（旧世代）
}


def resolve_tier(model: str) -> str | None:
    """model 文字列から tier 名を返す（集計・表示用グルーピング）。

    Args:
        model: message.model 文字列（例: "claude-opus-4-7-20260101"）

    Returns:
        "opus" / "sonnet" / "haiku" のいずれか。
        いずれも含まなければ None。

    Note:
        単価の世代振り分けには使わない。世代判定は _resolve_pricing_key() が担当する。
    """
    lower = model.lower()
    if "opus" in lower:
        return "opus"
    if "sonnet" in lower:
        return "sonnet"
    if "haiku" in lower:
        return "haiku"
    return None


def _resolve_pricing_key(model: str) -> str | None:
    """model 文字列を _PRICING キーに解決する（内部関数）。

    2 段構成で世代を振り分ける:
    1. 具体パターン優先マッチ: Opus 旧世代 / Haiku 旧世代
    2. tier 部分一致 fallback: 現行世代

    Args:
        model: message.model 文字列

    Returns:
        _PRICING のキー文字列。既知モデルでなければ None。
    """
    lower = model.lower()

    # ---- Opus: 世代判定が必要 ----
    if "opus" in lower:
        # 現行世代（$5 系: 4.5/4.6/4.7）を列挙マッチ
        # それ以外（初代 Opus 4 "claude-opus-4-YYYYMMDD" / 4.1 等）は旧世代 ($15 系)
        if _CURRENT_OPUS_RE.search(lower):
            return "opus-4-5"   # 現行 $5 系
        return "opus-4-1"       # 旧世代 $15 系（4.0/4.1/初代 Opus 4 等）

    # ---- Haiku: 世代判定が必要 ----
    if "haiku" in lower:
        # "haiku-3" を含めば旧世代 ($0.80 系)
        if "haiku-3" in lower:
            return "haiku-3-5"
        # それ以外（haiku-4-5 等）は現行
        return "haiku-4-5"

    # ---- Sonnet: 世代で単価不変 ($3 系) ----
    if "sonnet" in lower:
        return "sonnet-4-5"

    return None


def compute_cost_usd(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_create_tokens: int,
) -> tuple[float, bool]:
    """トークン数から USD コストを計算する純関数。

    Args:
        model: message.model 文字列（例: "claude-opus-4-7-20260101"）
        input_tokens: 入力トークン数
        output_tokens: 出力トークン数
        cache_read_tokens: キャッシュ読み込みトークン数
        cache_create_tokens: キャッシュ書き込みトークン数（5 分キャッシュ単価を採用）

    Returns:
        (cost_usd, known) のタプル。
        - cost_usd: 計算した USD コスト（不明モデルは 0.0）
        - known: 単価が解決できた場合 True、不明モデルは False

    Note:
        不明モデルは (0.0, False) を返す。例外も warning も出さない。
        換算式: tokens / 1_000_000 * unit_price_per_mtok
    """
    key = _resolve_pricing_key(model)
    if key is None:
        return (0.0, False)

    inp_price, out_price, cache_write_price, cache_read_price = _PRICING[key]

    cost = (
        input_tokens        / 1_000_000 * inp_price
        + output_tokens     / 1_000_000 * out_price
        + cache_read_tokens / 1_000_000 * cache_read_price
        + cache_create_tokens / 1_000_000 * cache_write_price
    )
    return (cost, True)


def known_models() -> tuple[str, ...]:
    """単価表のキー一覧を返す（テスト・デバッグ用）。

    Returns:
        _PRICING のキー文字列のタプル（順序は定義順）。
    """
    return tuple(_PRICING.keys())
