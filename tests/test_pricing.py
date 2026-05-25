"""tests/test_pricing.py

v2.21.0 pricing.py の単体テスト（8 件）。
純関数のみのため tmp_path 不要。

テスト内容:
  P1-P3: resolve_tier の部分一致（opus / sonnet / haiku）
  P4:    resolve_tier で未知モデルは None
  P5:    compute_cost_usd: Opus 4.7（現行）の手計算 USD 一致
  P6:    compute_cost_usd: Opus 4.1（旧世代）が $15 系で計算される（世代振り分け回帰テスト）
  P7:    compute_cost_usd: cache_read と cache_create がそれぞれ独立単価で計上される
  P8:    compute_cost_usd: Sonnet / Haiku 現行の手計算一致
  P9:    compute_cost_usd: 不明モデルは (0.0, False) で非例外
  P10:   known_models が単価表キーを全て含む
"""

from __future__ import annotations

import pytest

from c3.pricing import compute_cost_usd, known_models, resolve_tier


class TestResolveTier:
    """P1〜P4: resolve_tier のテスト。"""

    def test_opus_model_returns_opus(self):
        """P1: 'opus' を含むモデルは 'opus' を返す。"""
        assert resolve_tier("claude-opus-4-7-20260101") == "opus"

    def test_sonnet_model_returns_sonnet(self):
        """P2: 'sonnet' を含むモデルは 'sonnet' を返す。"""
        assert resolve_tier("claude-sonnet-4-6") == "sonnet"

    def test_haiku_model_returns_haiku(self):
        """P3: 'haiku' を含むモデルは 'haiku' を返す。"""
        assert resolve_tier("claude-haiku-4-5-20251001") == "haiku"

    def test_unknown_model_returns_none(self):
        """P4: 'gpt-4' 等の未知モデルは None を返す。"""
        assert resolve_tier("gpt-4") is None
        assert resolve_tier("gemini-pro") is None
        assert resolve_tier("") is None


class TestComputeCostUsd:
    """P5〜P9: compute_cost_usd のテスト。"""

    def test_opus_current_generation_uses_5_dollar_pricing(self):
        """P5: Opus 4.7（現行）は input $5.0/MTok で計算される。

        手計算: input 1_000_000 tokens → $5.0
        """
        cost, known = compute_cost_usd(
            model="claude-opus-4-7-20260101",
            input_tokens=1_000_000,
            output_tokens=0,
            cache_read_tokens=0,
            cache_create_tokens=0,
        )
        assert known is True
        assert cost == pytest.approx(5.0, rel=1e-6)

    def test_opus_old_generation_uses_15_dollar_pricing(self):
        """P6: Opus 4.1（旧世代）は input $15.0/MTok で計算される（世代振り分け回帰テスト）。

        手計算: input 1_000_000 tokens → $15.0
        """
        cost, known = compute_cost_usd(
            model="claude-opus-4-1-20250101",
            input_tokens=1_000_000,
            output_tokens=0,
            cache_read_tokens=0,
            cache_create_tokens=0,
        )
        assert known is True
        assert cost == pytest.approx(15.0, rel=1e-6)

    def test_cache_tokens_use_independent_pricing(self):
        """P7: cache_read と cache_create がそれぞれ独立した単価で計上される。

        Opus 4.5（現行）の単価: cache_read=$0.50/MTok, cache_write=$6.25/MTok
        手計算:
          cache_read  1_000_000 → $0.50
          cache_create 1_000_000 → $6.25
          合計 → $6.75
        """
        cost, known = compute_cost_usd(
            model="claude-opus-4-5-20250101",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=1_000_000,
            cache_create_tokens=1_000_000,
        )
        assert known is True
        assert cost == pytest.approx(6.75, rel=1e-6)

    def test_sonnet_and_haiku_current_generation(self):
        """P8: Sonnet / Haiku 現行の手計算一致。

        Sonnet 4.6: input $3.0/MTok
          → 1_000_000 input tokens → $3.0
        Haiku 4.5: input $1.0/MTok
          → 1_000_000 input tokens → $1.0
        """
        sonnet_cost, sonnet_known = compute_cost_usd(
            model="claude-sonnet-4-6-20260101",
            input_tokens=1_000_000,
            output_tokens=0,
            cache_read_tokens=0,
            cache_create_tokens=0,
        )
        assert sonnet_known is True
        assert sonnet_cost == pytest.approx(3.0, rel=1e-6)

        haiku_cost, haiku_known = compute_cost_usd(
            model="claude-haiku-4-5-20251001",
            input_tokens=1_000_000,
            output_tokens=0,
            cache_read_tokens=0,
            cache_create_tokens=0,
        )
        assert haiku_known is True
        assert haiku_cost == pytest.approx(1.0, rel=1e-6)

    def test_unknown_model_returns_zero_and_false(self):
        """P9: 不明モデル 'foo-bar' は (0.0, False) を返し、例外を投げない。"""
        cost, known = compute_cost_usd(
            model="foo-bar",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=0,
            cache_create_tokens=0,
        )
        assert cost == 0.0
        assert known is False


class TestKnownModels:
    """P10: known_models のテスト。"""

    def test_known_models_contains_all_pricing_keys(self):
        """P10: known_models が単価表キーを全て含む。"""
        models = known_models()
        expected_keys = {"opus-4-5", "sonnet-4-5", "haiku-4-5", "opus-4-1", "haiku-3-5"}
        assert expected_keys.issubset(set(models)), (
            f"不足しているキー: {expected_keys - set(models)}"
        )


class TestOpusGenerationRegression:
    """P11〜P14: CR-H-001 回帰テスト — Opus 世代振り分けの列挙マッチ検証。

    確定仕様（plan-report Round2 planner 決定）:
      - _CURRENT_OPUS_RE = re.compile(r"opus-4-[5-9]") にマッチ → 現行 $5 系
      - それ以外の opus → 旧世代 $15 系
    """

    def test_initial_opus4_no_minor_is_old_generation(self):
        """P11: 初代 Opus 4（"claude-opus-4-YYYYMMDD"、マイナー番号なし）は旧世代 $15 系。

        手計算: input 1_000_000 tokens → $15.0
        """
        cost, known = compute_cost_usd(
            model="claude-opus-4-20250514",
            input_tokens=1_000_000,
            output_tokens=0,
            cache_read_tokens=0,
            cache_create_tokens=0,
        )
        assert known is True
        assert cost == pytest.approx(15.0, rel=1e-6), (
            f"初代 Opus 4 が旧世代 ($15) に分類されなかった: cost={cost}"
        )

    def test_opus_4_1_is_old_generation(self):
        """P12: claude-opus-4-1-... は旧世代 $15 系。

        手計算: input 1_000_000 tokens → $15.0
        """
        cost, known = compute_cost_usd(
            model="claude-opus-4-1-20250805",
            input_tokens=1_000_000,
            output_tokens=0,
            cache_read_tokens=0,
            cache_create_tokens=0,
        )
        assert known is True
        assert cost == pytest.approx(15.0, rel=1e-6), (
            f"Opus 4.1 が旧世代 ($15) に分類されなかった: cost={cost}"
        )

    def test_opus_4_5_is_current_generation(self):
        """P13: claude-opus-4-5-... は現行 $5 系。

        手計算: input 1_000_000 tokens → $5.0
        """
        cost, known = compute_cost_usd(
            model="claude-opus-4-5-20260101",
            input_tokens=1_000_000,
            output_tokens=0,
            cache_read_tokens=0,
            cache_create_tokens=0,
        )
        assert known is True
        assert cost == pytest.approx(5.0, rel=1e-6), (
            f"Opus 4.5 が現行 ($5) に分類されなかった: cost={cost}"
        )

    def test_opus_4_7_is_current_generation(self):
        """P14: claude-opus-4-7-... は現行 $5 系。

        手計算: input 1_000_000 tokens → $5.0
        """
        cost, known = compute_cost_usd(
            model="claude-opus-4-7-20260601",
            input_tokens=1_000_000,
            output_tokens=0,
            cache_read_tokens=0,
            cache_create_tokens=0,
        )
        assert known is True
        assert cost == pytest.approx(5.0, rel=1e-6), (
            f"Opus 4.7 が現行 ($5) に分類されなかった: cost={cost}"
        )
