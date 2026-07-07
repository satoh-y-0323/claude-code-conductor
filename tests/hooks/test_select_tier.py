"""Tests for .claude/hooks/select_tier.py and c3_db tier_bandit helpers.

tier-routing MVP: Tier 自動ルーティングの検証。

テストケース:
 c3_db ヘルパー（read_tier_params / update_tier_params）:
  1. 行が無い場合は (1.0, 1.0, 0) で初期化される
  2. update_tier_params(success=True) で alpha+=1、trials+=1
  3. update_tier_params(success=False) で beta+=1、trials+=1
  4. 同じ (complexity, tier) で複数回呼ぶと累積される
  5. DB 不在時: read は initial defaults、update は False

 estimate_complexity:
  6. simple キーワード + 短文 → simple
  7. complex キーワード → complex
  8. 800 文字以上 → complex
  9. それ以外 → medium

 select_tier:
 10. trials < 30 で uniform 選択（mode="uniform"）
 11. trials >= 30 で Beta サンプリング（mode="thompson"）
 12. 決定論的サンプリング（rng シード固定）

 build_additional_context / write_tier_selection:
 13. additionalContext に複雑度・tier・信頼度が含まれる
 14. tier_selection.json に書き込まれる

 main (E2E):
 15. UserPromptSubmit payload を流すと additionalContext が stdout に出る
 16. 不正 JSON でも crash しない
"""

from __future__ import annotations

import importlib.util
import io
import json
import random
import sqlite3
import sys
import types
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "select_tier.py"
INIT_HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "session_start.py"


def _create_c3_db(db_path: Path) -> None:
    from c3.migrate import apply_pending_migrations
    apply_pending_migrations(db_path)


def _load_hook_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("select_tier_t", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


# ---------------------------------------------------------------------------
# c3_db ヘルパー
#
# [v2.41.0 select-tier-hook] TestC3DbTierBandit（旧 read_tier_params /
# update_tier_params の実 DB 蓄積前提テスト）は db-shims-and-cost タスクで
# 両関数が deprecated no-op シムに置き換わったため削除した。
# 等価カバレッジは role 次元付き新 API のテストとして
# tests/test_db.py::TestReadAgentTierParams / TestUpdateAgentTierParams が
# 引き継いでいる（O1/O2 群）。シム自体の初期値/no-op 挙動は
# tests/test_db.py::TestDeprecatedShimBehavior が担保する。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# estimate_complexity
# ---------------------------------------------------------------------------


class TestEstimateComplexity:

    @pytest.mark.parametrize("prompt,expected", [
        ("typo を修正してください", "simple"),
        ("rename foo to bar", "simple"),
        ("doc の更新", "simple"),
    ])
    def test_simple(self, prompt: str, expected: str) -> None:
        mod = _load_hook_module()
        assert mod.estimate_complexity(prompt) == expected

    @pytest.mark.parametrize("prompt", [
        "refactor this module",
        "セキュリティの観点で見直してください",
        "concurrency を考慮した実装を",
    ])
    def test_complex_by_keyword(self, prompt: str) -> None:
        mod = _load_hook_module()
        assert mod.estimate_complexity(prompt) == "complex"

    def test_complex_by_length(self) -> None:
        mod = _load_hook_module()
        long_prompt = "あ" * 900  # 900 文字
        assert mod.estimate_complexity(long_prompt) == "complex"

    @pytest.mark.parametrize("prompt", [
        "新しい機能を追加してください",
        "テストを書いて",
        "x = 1",  # 短すぎてもキーワードが無ければ medium
    ])
    def test_medium(self, prompt: str) -> None:
        mod = _load_hook_module()
        assert mod.estimate_complexity(prompt) == "medium"


# ---------------------------------------------------------------------------
# select_tier
# ---------------------------------------------------------------------------


class TestSelectTier:

    def test_uniform_when_low_trials(self) -> None:
        mod = _load_hook_module()
        params = {
            "haiku": (5.0, 1.0, 4),
            "sonnet": (3.0, 1.0, 2),
            "opus": (2.0, 1.0, 1),
        }
        # 合計 trials=7 < 30
        rng = random.Random(42)
        tier, mode = mod.select_tier(params, rng=rng)
        assert mode == "uniform"
        assert tier in ("haiku", "sonnet", "opus")

    def test_thompson_when_enough_trials(self) -> None:
        mod = _load_hook_module()
        # haiku が圧倒的に成功している（α=20, β=1）状況
        params = {
            "haiku": (20.0, 1.0, 21),
            "sonnet": (5.0, 5.0, 10),
            "opus": (2.0, 8.0, 10),
        }
        # 合計 trials=41 >= 30、サンプリングは確率的だがほぼ haiku
        rng = random.Random(42)
        tier, mode = mod.select_tier(params, rng=rng)
        assert mode == "thompson"

    def test_deterministic_with_seed(self) -> None:
        mod = _load_hook_module()
        params = {
            "haiku": (10.0, 5.0, 14),
            "sonnet": (10.0, 5.0, 14),
            "opus": (10.0, 5.0, 14),
        }
        # 同じ seed なら同じ結果
        rng1 = random.Random(123)
        rng2 = random.Random(123)
        tier1, _ = mod.select_tier(params, rng=rng1)
        tier2, _ = mod.select_tier(params, rng=rng2)
        assert tier1 == tier2


# ---------------------------------------------------------------------------
# build_additional_context / write_tier_selection
# ---------------------------------------------------------------------------


class TestContextAndStateFile:

    def test_additional_context_contains_info(self) -> None:
        mod = _load_hook_module()
        params = {
            "haiku": (10.0, 5.0, 14),
            "sonnet": (10.0, 5.0, 14),
            "opus": (10.0, 5.0, 14),
        }
        text = mod.build_additional_context("medium", "sonnet", "thompson", params)
        assert "medium" in text
        assert "sonnet" in text
        assert "trials" in text or "信頼度" in text

    def test_uniform_mode_shows_collection_phase(self) -> None:
        mod = _load_hook_module()
        params = {
            "haiku": (1.0, 1.0, 0),
            "sonnet": (1.0, 1.0, 0),
            "opus": (1.0, 1.0, 0),
        }
        text = mod.build_additional_context("simple", "haiku", "uniform", params)
        assert "学習データ収集中" in text

    def test_write_tier_selection(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = _load_hook_module()
        target = tmp_path / "tier_selection.json"
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(target))

        mod.write_tier_selection("complex", "opus", "thompson")
        assert target.is_file()
        data = json.loads(target.read_text(encoding="utf-8"))
        # tier-routing Phase 2-A: suggested_model フィールドが tier と同じ値で追加されている
        assert data == {
            "complexity": "complex",
            "tier": "opus",
            "mode": "thompson",
            "suggested_model": "opus",
        }


# ---------------------------------------------------------------------------
# [tier-routing 機械適用（フェーズ3）] build_additional_context の文言
#
# フェーズ3で PreToolUse hook（tier_autoapply）が推奨 Tier を model: へ機械適用する
# ようになったため、additionalContext は「親が model: を明示指定して適用せよ」という
# 指示形（フェーズ2ソフト適用）から「hook が自動適用する・推奨と異なる Tier を
# 使いたい時のみ model: を明示指定する（明示は尊重される）」へ移行した。
# developer 基準の推奨表示・fork 除外・role gating は維持した。
# architecture-report-20260707-065043.md §6（本実装分岐）。
# ---------------------------------------------------------------------------


class TestPhase0BuildAdditionalContextWording:
    """build_additional_context の機械適用文言テスト。"""

    def _params(self) -> dict:
        return {
            "haiku": (10.0, 5.0, 14),
            "sonnet": (10.0, 5.0, 14),
            "opus": (10.0, 5.0, 14),
        }

    def test_old_incorrect_wording_removed(self) -> None:
        """旧文言「frontmatter 指定が優先される」（事実誤り）が含まれなかった。"""
        mod = _load_hook_module()
        text = mod.build_additional_context("medium", "sonnet", "thompson", self._params())
        assert "frontmatter 指定が優先される" not in text
        assert "frontmatter 指定" not in text

    def test_new_wording_mentions_developer_baseline(self) -> None:
        """新文言に「developer 基準」が含まれた（ADR-3: 推奨は developer セル固定）。"""
        mod = _load_hook_module()
        text = mod.build_additional_context("medium", "sonnet", "thompson", self._params())
        assert "developer 基準" in text

    def test_new_wording_explains_override_via_explicit_model(self) -> None:
        """新文言に「推奨と異なる Tier を使いたい場合のみ model: を明示指定する・
        明示指定は尊重され hook に上書きされない（fork は対象外）」相当の説明が含まれていた。"""
        mod = _load_hook_module()
        text = mod.build_additional_context("medium", "sonnet", "thompson", self._params())
        assert "model:" in text
        assert "明示指定" in text
        assert "尊重" in text
        assert "fork" in text

    def test_new_wording_mentions_hook_autoapply(self) -> None:
        """機械適用（tier_autoapply hook が推奨 Tier を model: へ自動適用する）が
        文言に反映されていた（フェーズ3・本実装分岐）。"""
        mod = _load_hook_module()
        text = mod.build_additional_context("medium", "sonnet", "thompson", self._params())
        assert "自動適用" in text
        assert "tier_autoapply" in text


# ---------------------------------------------------------------------------
# [tier-routing 機械適用（フェーズ3）] build_additional_context 機械適用文言
# （architecture-report-20260707-065043.md §6 本実装分岐・
# plan-report-20260707-065732.md T5 反映）
#
# フェーズ2 のソフト適用（親が model: を明示指定して適用する指示形）を、
# フェーズ3 の機械適用（PreToolUse hook tier_autoapply が推奨 Tier を model: へ
# 自動注入する）へ移行した。additionalContext は「hook が自動適用する・推奨と
# 異なる Tier を使いたい時のみ model: を明示指定する（明示は尊重される）」旨へ
# 書き換えた。fork 除外・role gating（developer/wt_developer 限定）・推奨 Tier の
# 表示は維持した。
# ---------------------------------------------------------------------------


class TestSoftApplyDirectiveWording:
    """機械適用 additionalContext 文言（自動適用・対象明記）のアサーション。"""

    def _params(self) -> dict:
        return {
            "haiku": (10.0, 5.0, 14),
            "sonnet": (10.0, 5.0, 14),
            "opus": (10.0, 5.0, 14),
        }

    def _uniform_params(self) -> dict:
        return {
            "haiku": (1.0, 1.0, 0),
            "sonnet": (1.0, 1.0, 0),
            "opus": (1.0, 1.0, 0),
        }

    def test_old_manual_opt_in_phrase_removed(self) -> None:
        """旧文言「コスト最適化したい場合は手動指定してください」（手動判断に
        委ねる弱い提示）は除去されていた。"""
        mod = _load_hook_module()
        text = mod.build_additional_context("medium", "haiku", "thompson", self._params())
        assert "コスト最適化したい場合は手動指定してください" not in text

    def test_hook_autoapply_directive_present(self) -> None:
        """hook（tier_autoapply）が推奨 Tier を model: へ自動適用する旨が
        明記されていた（フェーズ3・機械適用）。"""
        mod = _load_hook_module()
        text = mod.build_additional_context("medium", "haiku", "thompson", self._params())
        assert "自動適用" in text
        assert "tier_autoapply" in text

    def test_recommended_tier_shown_in_display(self) -> None:
        """推奨 Tier: {tier} の形で実効 tier（引数の tier。main() からは
        effective_tier が渡される）が推奨表示に含まれていた。"""
        mod = _load_hook_module()
        text = mod.build_additional_context("medium", "haiku", "thompson", self._params())
        assert "推奨 Tier: haiku" in text

        text_opus = mod.build_additional_context("complex", "opus", "thompson", self._params())
        assert "推奨 Tier: opus" in text_opus

    def test_developer_and_wt_developer_both_named(self) -> None:
        """developer と wt_developer の両方が適用対象として明記されていた。"""
        mod = _load_hook_module()
        text = mod.build_additional_context("medium", "haiku", "thompson", self._params())
        assert "developer" in text
        assert "wt_developer" in text

    def test_fork_excluded_from_application(self) -> None:
        """fork は model 上書き不可のため対象外であることが明記されていた。"""
        mod = _load_hook_module()
        text = mod.build_additional_context("medium", "haiku", "thompson", self._params())
        assert "fork" in text
        # fork 除外と role gating（developer/wt_developer 限定）の両方で
        # 「対象外」という語が使われるため最低 2 箇所出現する。
        assert text.count("対象外") >= 2

    def test_applies_during_uniform_period_too(self) -> None:
        """学習データ収集中（uniform モード）でも常に自動適用する旨が明記され
        ていた（mode に関わらず機械適用テキストが出た）。"""
        mod = _load_hook_module()
        text_thompson = mod.build_additional_context(
            "medium", "haiku", "thompson", self._params(),
        )
        text_uniform = mod.build_additional_context(
            "simple", "haiku", "uniform", self._uniform_params(),
        )
        for text in (text_thompson, text_uniform):
            assert "自動適用" in text
            assert "推奨 Tier: haiku" in text
        assert "常に適用" in text_uniform

    def test_tester_and_persona_roles_noted_as_out_of_scope(self) -> None:
        """tester 等の他 role、および親 Claude ペルソナで動かす role
        （architect / planner を含む）は tier レバーが無いため対象外である
        ことが明記されること。"""
        mod = _load_hook_module()
        text = mod.build_additional_context("medium", "haiku", "thompson", self._params())
        assert "tester" in text
        assert "architect" in text
        assert "planner" in text


# ---------------------------------------------------------------------------
# [v2.41.0 select-tier-hook] データ源切替
#
# main() は developer 固定で read_agent_tier_params("developer", complexity) を
# 呼ぶ（旧 read_tier_params(complexity) ではない）。_db_failure_rate は
# read_agent_failure_rate("developer", complexity, tier) を呼ぶ
# （旧 read_tier_failure_rate(complexity, tier) ではない）。architecture-report §3-5。
# ---------------------------------------------------------------------------


class TestDataSourceSwitchToAgentTierParams:
    """main() が read_agent_tier_params("developer", complexity) を使うことの検証。"""

    def test_main_calls_read_agent_tier_params_with_developer_role(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """main() は c3_db.read_agent_tier_params("developer", complexity) を呼ぶ。

        mock は read_agent_tier_params のみ持ち、旧 read_tier_params は持たない。
        旧実装のままなら AttributeError が発生し Red になる。
        """
        mod = _load_hook_module()
        monkeypatch.setattr(
            mod, "TIER_SELECTION_PATH", str(tmp_path / "tier_selection.json"),
        )

        calls: list[tuple] = []

        def _spy_read_agent_tier_params(role, complexity, **kw):
            calls.append((role, complexity))
            return {t: (1.0, 1.0, 0) for t in mod.TIERS}

        mock_c3_db = types.SimpleNamespace(
            read_agent_tier_params=_spy_read_agent_tier_params,
            read_tier_cost_rate_for_complexity=lambda complexity, **kw: {},
            read_agent_failure_rate=lambda role, complexity, tier, **kw: (None, 0),
        )
        monkeypatch.setattr(mod, "_load_c3_db_module", lambda: mock_c3_db)

        payload = {"prompt": "新しい機能を追加してください"}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

        rc = mod.main()
        assert rc == 0
        assert len(calls) == 1, "read_agent_tier_params が呼ばれていない（旧 API のままの可能性）"
        role, complexity = calls[0]
        assert role == "developer"
        assert complexity == "medium"

    def test_main_works_without_legacy_read_tier_params_attribute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """mock に旧 read_tier_params 属性が無くても main() が crash せず完了する。

        main() が旧属性を参照していないことの証跡（旧実装のままなら
        AttributeError で本テストが Red になる）。
        """
        mod = _load_hook_module()
        monkeypatch.setattr(
            mod, "TIER_SELECTION_PATH", str(tmp_path / "tier_selection.json"),
        )

        params = {t: (1.0, 1.0, 0) for t in mod.TIERS}
        mock_c3_db = types.SimpleNamespace(
            read_agent_tier_params=lambda role, complexity, **kw: params,
            read_tier_cost_rate_for_complexity=lambda complexity, **kw: {},
            read_agent_failure_rate=lambda role, complexity, tier, **kw: (None, 0),
        )
        monkeypatch.setattr(mod, "_load_c3_db_module", lambda: mock_c3_db)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "テスト"})))

        rc = mod.main()
        assert rc == 0


class TestEscalationDataSourceSwitch:
    """_db_failure_rate が read_agent_failure_rate("developer", complexity, tier) を使うことの検証。"""

    def test_db_failure_rate_calls_read_agent_failure_rate_with_developer_role(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_db_failure_rate は c3_db.read_agent_failure_rate("developer", complexity, tier) を呼ぶ。

        mock は read_agent_failure_rate のみ持ち、旧 read_tier_failure_rate は持たない。
        旧実装のままなら AttributeError が発生し Red になる。
        """
        mod = _load_hook_module()
        calls: list[tuple] = []

        def _spy_read_agent_failure_rate(role, complexity, tier, **kw):
            calls.append((role, complexity, tier))
            return (0.7, 10)

        mock_c3_db = types.SimpleNamespace(
            read_agent_failure_rate=_spy_read_agent_failure_rate,
        )
        monkeypatch.setattr(mod, "_load_c3_db_module", lambda: mock_c3_db)

        rate, samples = mod._db_failure_rate("medium", "haiku")
        assert rate == 0.7
        assert samples == 10
        assert len(calls) == 1, "read_agent_failure_rate が呼ばれていない（旧 API のままの可能性）"
        role, complexity, tier = calls[0]
        assert role == "developer"
        assert complexity == "medium"
        assert tier == "haiku"

    def test_db_failure_rate_works_without_legacy_attribute(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """mock に旧 read_tier_failure_rate 属性が無くても _db_failure_rate が動く。"""
        mod = _load_hook_module()
        mock_c3_db = types.SimpleNamespace(
            read_agent_failure_rate=lambda role, complexity, tier, **kw: (None, 0),
        )
        monkeypatch.setattr(mod, "_load_c3_db_module", lambda: mock_c3_db)
        rate, samples = mod._db_failure_rate("simple", "sonnet")
        assert rate is None
        assert samples == 0

    def test_db_failure_rate_returns_c3_db_none_tuple_when_module_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """c3_db インポート失敗時は (None, 0) を返す（後方互換・不変）。"""
        mod = _load_hook_module()
        monkeypatch.setattr(mod, "_load_c3_db_module", lambda: None)
        rate, samples = mod._db_failure_rate("medium", "haiku")
        assert rate is None
        assert samples == 0


# ---------------------------------------------------------------------------
# [v2.41.0 select-tier-hook] 不変性回帰テスト
#
# LEARNING_THRESHOLD の SSOT・値は本タスクで変更しない（意味の再定義のみ・
# architecture-report ADR-3）。
# ---------------------------------------------------------------------------


class TestLearningThresholdUnchanged:
    """LEARNING_THRESHOLD の SSOT・値が本タスクで不変であることの回帰テスト。"""

    def test_learning_threshold_matches_db_ssot(self) -> None:
        mod = _load_hook_module()
        from c3 import db as c3_db
        assert mod.LEARNING_THRESHOLD == c3_db.LEARNING_THRESHOLD

    def test_learning_threshold_value_is_30(self) -> None:
        mod = _load_hook_module()
        assert mod.LEARNING_THRESHOLD == 30


# ---------------------------------------------------------------------------
# main (E2E)
# ---------------------------------------------------------------------------


class TestMainE2E:

    def test_main_outputs_additional_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        mod = _load_hook_module()
        # tier_selection の出力先を tmp に
        monkeypatch.setattr(
            mod, "TIER_SELECTION_PATH",
            str(tmp_path / "tier_selection.json"),
        )

        payload = {"prompt": "新しい機能を追加してください"}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

        rc = mod.main()
        assert rc == 0

        captured = capsys.readouterr()
        assert captured.out  # 何か出ている
        out_data = json.loads(captured.out)
        assert "hookSpecificOutput" in out_data
        assert "additionalContext" in out_data["hookSpecificOutput"]
        # tier_selection.json も書かれているはず
        assert (tmp_path / "tier_selection.json").is_file()

    def test_main_invalid_json_returns_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _load_hook_module()
        monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
        rc = mod.main()
        assert rc == 0

    def test_main_empty_prompt_returns_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _load_hook_module()
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": ""})))
        rc = mod.main()
        assert rc == 0


# ---------------------------------------------------------------------------
# _mask_secrets / _prompt_prefix_and_hash (SR-V-001)
# ---------------------------------------------------------------------------


class TestMaskSecrets:
    """秘密情報マスク処理の検証。"""

    @pytest.fixture
    def mod(self):
        return _load_hook_module()

    @pytest.mark.parametrize("input_text,expected_masked", [
        # password=
        ("password=MyS3cr3t!", "password=***"),
        ("PASSWORD=abc123", "PASSWORD=***"),
        # api_key= / api-key=
        ("api_key=sk-1234567890abcdef", "api_key=***"),
        ("api-key=sk-abcdef", "api-key=***"),
        # Bearer トークン
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def", "Authorization: Bearer ***"),
        # token=
        ("token=ghp_xxxxxxxxxxxxx", "token=***"),
        # secret=
        ("secret=topsecret", "secret=***"),
        # aws_secret_access_key=
        ("aws_secret_access_key=wJalrXUtnFEMI/K7MDENG", "aws_secret_access_key=***"),
    ])
    def test_mask_replaces_value(self, mod, input_text: str, expected_masked: str) -> None:
        result = mod._mask_secrets(input_text)
        assert result == expected_masked

    def test_mask_preserves_non_secret_text(self, mod) -> None:
        text = "通常のプロンプトです。password とは関係ありません。"
        result = mod._mask_secrets(text)
        # "password" 単体（= が続かない）はマスクされない
        assert result == text

    def test_mask_pem_private_key(self, mod) -> None:
        text = "-----BEGIN RSA PRIVATE KEY-----\nABCDEF\n-----END RSA PRIVATE KEY-----"
        result = mod._mask_secrets(text)
        assert "ABCDEF" not in result
        assert "***" in result

    def test_mask_multiple_patterns(self, mod) -> None:
        text = "api_key=sk-abc password=pass123"
        result = mod._mask_secrets(text)
        assert "sk-abc" not in result
        assert "pass123" not in result
        assert "api_key=***" in result
        assert "password=***" in result

    def test_mask_keeps_key_names(self, mod) -> None:
        """キー名（api_key= 等）は残り、値のみ *** になる。"""
        text = "api_key=secretvalue"
        result = mod._mask_secrets(text)
        assert result.startswith("api_key=")
        assert "secretvalue" not in result

    def test_prompt_prefix_and_hash_masks_prefix(self, mod) -> None:
        """_prompt_prefix_and_hash は prefix に含まれる秘密情報をマスクする。"""
        prompt = "api_key=sk-super-secret このタスクをやってください"
        prefix, h = mod._prompt_prefix_and_hash(prompt)
        assert "sk-super-secret" not in prefix
        assert "api_key=***" in prefix
        # hash はマスク前の原文から計算されるため固定値と一致する
        import hashlib
        expected_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        assert h == expected_hash

    def test_prompt_prefix_and_hash_no_false_positive(self, mod) -> None:
        """秘密情報を含まない通常プロンプトはマスクされない。"""
        prompt = "新しい機能を追加してください。セキュリティを考慮した実装で。"
        prefix, _ = mod._prompt_prefix_and_hash(prompt)
        assert prefix == prompt[:200]

    def test_main_masked_prefix_in_tier_selection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """main を通じて tier_selection.json に書かれる prompt_prefix がマスクされている。"""
        mod = _load_hook_module()
        monkeypatch.setattr(
            mod, "TIER_SELECTION_PATH",
            str(tmp_path / "tier_selection.json"),
        )
        prompt = "token=ghp_12345678abcdef この実装をリファクタリングしてください"
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": prompt})))

        rc = mod.main()
        assert rc == 0

        data = json.loads((tmp_path / "tier_selection.json").read_text(encoding="utf-8"))
        assert "ghp_12345678abcdef" not in data.get("prompt_prefix", "")
        assert "token=***" in data.get("prompt_prefix", "")


# ---------------------------------------------------------------------------
# SR-K-003 (fix-cycle-4 FD1): _prompt_prefix_and_hash のマスク順序 Red テスト
# ---------------------------------------------------------------------------


class TestSelectTierMaskOrder:
    """SR-K-003: _prompt_prefix_and_hash のマスク順序（切り詰め→マスク vs マスク→切り詰め）を検証した。

    [Red] 追加当時の実装は `_mask_secrets(prompt[:_PROMPT_PREFIX_MAX])`（切り詰め→マスク）の順だった。
    PEM ブロックのように開始/終了タグの両方が対象文字列内に存在しないとマッチしない非貪欲
    パターンでは、終了タグが `_PROMPT_PREFIX_MAX`（200字）より後方にある場合、切り詰め後の
    文字列に終了タグが存在せず PEM パターンが不成立となり、鍵本体（base64 データ）が無修正の
    まま prefix に残存していた（security-review-report-20260707-122227.md SR-K-003・実証済み）。
    """

    @pytest.fixture
    def mod(self):
        return _load_hook_module()

    def test_prompt_prefix_and_hash_masks_pem_with_late_end_tag(self, mod) -> None:
        """終了タグが200字より後方にある PEM ブロックでも鍵本体がマスクされることを要求した（Red）。

        終了タグは 349 文字目付近（200字境界より後方）に位置するよう構築した。
        追加当時の実装（切り詰め→マスク）ではこのケースで鍵本体が prefix に残存していたため、
        このテストは Red（失敗）だった。
        """
        secret_body = "SECRETBASE64DATA"
        prompt = (
            "-----BEGIN PRIVATE KEY-----\n"
            + secret_body * 20
            + "\n-----END PRIVATE KEY-----\nrest of prompt"
        )
        # 終了タグが 200 字境界より後方にあることを前提として確認した
        end_tag_pos = prompt.index("-----END PRIVATE KEY-----")
        assert end_tag_pos > mod._PROMPT_PREFIX_MAX

        prefix, _ = mod._prompt_prefix_and_hash(prompt)

        assert secret_body not in prefix


# ---------------------------------------------------------------------------
# T3: session_id 記録（AC-3 / AC-9）
# ---------------------------------------------------------------------------


class TestSessionIdRecording:
    """write_tier_selection / main の session_id 記録を検証した。"""

    def test_write_tier_selection_with_session_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """session_id が渡されると tier_selection.json に session_id キーが入る。"""
        mod = _load_hook_module()
        target = tmp_path / "tier_selection.json"
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(target))

        mod.write_tier_selection("medium", "sonnet", "thompson", session_id="sess-abc123")
        data = json.loads(target.read_text(encoding="utf-8"))
        assert "session_id" in data
        assert data["session_id"] == "sess-abc123"

    def test_write_tier_selection_without_session_id_no_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """session_id が渡されない（None）ときは tier_selection.json に session_id キーが入らない。

        既存の dict 完全一致テスト（test_write_tier_selection）と同形の確認。
        """
        mod = _load_hook_module()
        target = tmp_path / "tier_selection.json"
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(target))

        mod.write_tier_selection("complex", "opus", "thompson")
        data = json.loads(target.read_text(encoding="utf-8"))
        # session_id キーが存在しないことを確認
        assert "session_id" not in data
        # 既存テストと同一の期待 dict と一致する
        assert data == {
            "complexity": "complex",
            "tier": "opus",
            "mode": "thompson",
            "suggested_model": "opus",
        }

    def test_main_records_session_id_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """payload に session_id があるとき tier_selection.json に session_id が入る。"""
        mod = _load_hook_module()
        monkeypatch.setattr(
            mod, "TIER_SELECTION_PATH",
            str(tmp_path / "tier_selection.json"),
        )
        payload = {"prompt": "新しい機能を追加してください", "session_id": "sess-xyz-999"}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

        rc = mod.main()
        assert rc == 0

        data = json.loads((tmp_path / "tier_selection.json").read_text(encoding="utf-8"))
        assert "session_id" in data
        assert data["session_id"] == "sess-xyz-999"

    def test_main_no_session_id_in_payload_no_key_in_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """payload に session_id が無いとき tier_selection.json に session_id キーが入らず crash しない。"""
        mod = _load_hook_module()
        monkeypatch.setattr(
            mod, "TIER_SELECTION_PATH",
            str(tmp_path / "tier_selection.json"),
        )
        payload = {"prompt": "テストを書いてください"}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

        rc = mod.main()
        assert rc == 0

        data = json.loads((tmp_path / "tier_selection.json").read_text(encoding="utf-8"))
        assert "session_id" not in data


# ---------------------------------------------------------------------------
# T3 (v2.23.0): cost-aware tie-break — EPSILON / SelectionResult /
#               _cost_tiebreak / select_tier_detailed / select_tier 委譲
# ---------------------------------------------------------------------------


class TestCostTiebreak:
    """_cost_tiebreak 内部関数の単体テスト。"""

    def test_single_contender_no_tiebreak(self) -> None:
        """contenders が 1 件なら cost_tiebreak=False で最大サンプルを返す。"""
        mod = _load_hook_module()
        # haiku が圧倒的に高い → contenders = ["haiku"] のみ
        samples = {"haiku": 0.9, "sonnet": 0.5, "opus": 0.3}
        cost_map = {"haiku": 30.0, "sonnet": 18.0, "opus": 6.0}
        chosen, did_tiebreak, contenders = mod._cost_tiebreak(samples, cost_map)
        assert chosen == "haiku"
        assert did_tiebreak is False
        assert len(contenders) == 1

    def test_cost_map_none_no_tiebreak(self) -> None:
        """cost_map=None なら cost_tiebreak=False で max(samples) と同一。"""
        mod = _load_hook_module()
        samples = {"haiku": 0.81, "sonnet": 0.80, "opus": 0.79}
        # epsilon=0.05 なら全件 contenders になるが cost_map=None → 従来挙動
        chosen, did_tiebreak, contenders = mod._cost_tiebreak(samples, None)
        assert chosen == "haiku"  # max
        assert did_tiebreak is False

    def test_hi_lo_equal_all_zero_norm_picks_max_sample(self) -> None:
        """全 contender が同コスト（hi==lo）なら samples 最大を選び did_tiebreak=False。

        CR-Q-001（v2.27.0）: 全 tier コスト同値時は cost が選択に無関与なため
        did_tiebreak=False を返すよう精緻化した。chosen は argmax(sample) で不変。
        """
        mod = _load_hook_module()
        # haiku と sonnet が拮抗・同コスト
        samples = {"haiku": 0.82, "sonnet": 0.80, "opus": 0.30}
        cost_map = {"haiku": 10.0, "sonnet": 10.0, "opus": 10.0}
        chosen, did_tiebreak, contenders = mod._cost_tiebreak(samples, cost_map)
        # 全コスト同値（hi == lo）→ did_tiebreak=False（CR-Q-001・v2.27.0 精緻化済み）
        assert chosen == "haiku"
        assert did_tiebreak is False

    def test_tiebreak_picks_cheapest_among_contenders(self) -> None:
        """拮抗群の中で最安 tier が選ばれる。"""
        mod = _load_hook_module()
        epsilon = mod.EPSILON  # 0.05
        # haiku と sonnet が拮抗（差 < epsilon）、opus は遠い
        base = 0.85
        samples = {"haiku": base, "sonnet": base - epsilon + 0.01, "opus": base - 0.2}
        cost_map = {"haiku": 30.0, "sonnet": 6.0, "opus": 1.0}
        # contenders = haiku, sonnet（opusは差が大きすぎる）
        chosen, did_tiebreak, contenders = mod._cost_tiebreak(samples, cost_map)
        assert "haiku" in contenders
        assert "sonnet" in contenders
        assert "opus" not in contenders
        # haiku(30.0) > sonnet(6.0) なので sonnet が選ばれる
        assert chosen == "sonnet"
        assert did_tiebreak is True

    def test_contenders_returned_as_tuple(self) -> None:
        """contenders は tuple で返される（frozen dataclass 安全のため）。"""
        mod = _load_hook_module()
        samples = {"haiku": 0.80, "sonnet": 0.80, "opus": 0.30}
        cost_map = {"haiku": 6.0, "sonnet": 18.0, "opus": 30.0}
        _, _, contenders = mod._cost_tiebreak(samples, cost_map)
        assert isinstance(contenders, tuple)


class TestSelectTierDetailed:
    """select_tier_detailed の SelectionResult 型テスト。"""

    def test_uniform_returns_selection_result(self) -> None:
        """total_trials < LEARNING_THRESHOLD なら SelectionResult(mode="uniform") を返す。"""
        mod = _load_hook_module()
        # trials 合計 = 7 < 30
        params = {
            "haiku": (1.0, 1.0, 3),
            "sonnet": (1.0, 1.0, 2),
            "opus": (1.0, 1.0, 2),
        }
        rng = random.Random(42)
        result = mod.select_tier_detailed(params, rng=rng)
        assert result.mode == "uniform"
        assert result.tier in ("haiku", "sonnet", "opus")
        assert result.cost_tiebreak is False
        assert result.contenders == ()

    def test_uniform_cost_map_ignored(self) -> None:
        """uniform 分岐では cost_map があっても結果が変わらない（AC-3）。"""
        mod = _load_hook_module()
        params = {
            "haiku": (1.0, 1.0, 3),
            "sonnet": (1.0, 1.0, 2),
            "opus": (1.0, 1.0, 2),
        }
        cost_map = {"haiku": 6.0, "sonnet": 18.0, "opus": 30.0}
        # 同じ seed で cost_map あり/なしの結果が完全一致
        rng_none = random.Random(99)
        rng_with = random.Random(99)
        result_none = mod.select_tier_detailed(params, rng=rng_none, cost_map=None)
        result_with = mod.select_tier_detailed(params, rng=rng_with, cost_map=cost_map)
        assert result_none.tier == result_with.tier
        assert result_none.mode == result_with.mode == "uniform"
        assert result_none.cost_tiebreak is False
        assert result_with.cost_tiebreak is False

    def test_thompson_no_cost_map_matches_legacy(self) -> None:
        """cost_map=None の thompson は従来 select_tier と同一結果（AC-1）。"""
        mod = _load_hook_module()
        # haiku 圧倒的有利
        params = {
            "haiku": (20.0, 1.0, 30),
            "sonnet": (5.0, 5.0, 10),
            "opus": (2.0, 8.0, 10),
        }
        rng1 = random.Random(42)
        rng2 = random.Random(42)
        # 従来 select_tier（cost_map なし）
        tier_legacy, mode_legacy = mod.select_tier(params, rng=rng1)
        # select_tier_detailed（cost_map=None）
        result = mod.select_tier_detailed(params, rng=rng2, cost_map=None)
        assert result.tier == tier_legacy
        assert result.mode == mode_legacy
        assert result.cost_tiebreak is False

    def test_thompson_single_dominant_no_tiebreak(self) -> None:
        """単独最大 tier は cost_map があっても cost_tiebreak=False（AC-1）。"""
        mod = _load_hook_module()
        # haiku が圧倒的に高い alpha → Beta 期待値で完全に支配的
        params = {
            "haiku": (50.0, 1.0, 51),
            "sonnet": (5.0, 5.0, 20),
            "opus": (2.0, 8.0, 20),
        }
        cost_map = {"haiku": 30.0, "sonnet": 18.0, "opus": 6.0}
        # seed を固定して haiku が単独 max になることを確認
        # haiku の Beta(50,1) の期待値 ≈ 0.98 vs sonnet Beta(5,5) ≈ 0.5
        rng = random.Random(0)
        result = mod.select_tier_detailed(params, rng=rng, cost_map=cost_map)
        assert result.mode == "thompson"
        # haiku が単独最大のはずなので cost_tiebreak=False かつ haiku
        assert result.cost_tiebreak is False
        assert result.tier == "haiku"

    def test_thompson_tiebreak_picks_cheapest(self) -> None:
        """拮抗群で min-max 最安 tier が選ばれ cost_tiebreak=True（AC-2）。

        haiku と sonnet に同一の Beta パラメータを設定し、seed 固定で
        両者が EPSILON 以内に収まるサンプルを引いた場合の動作を確認。
        cost_map で sonnet が安いため sonnet が選ばれる。
        """
        mod = _load_hook_module()
        epsilon = mod.EPSILON  # 0.05

        # haiku と sonnet に同一パラメータ（Beta 分布が同一 → サンプルは seed 依存）
        # opus は低い alpha で拮抗外になりやすいパラメータ
        params = {
            "haiku": (10.0, 10.0, 30),
            "sonnet": (10.0, 10.0, 30),
            "opus": (1.0, 10.0, 10),
        }
        cost_map = {"haiku": 18.0, "sonnet": 6.0, "opus": 1.0}

        # seed を試して haiku-sonnet 拮抗 + cost_tiebreak=True になるケースを探す
        # （設計上 epsilon=0.05 で同一分布なら高確率で拮抗する）
        found = False
        for seed in range(200):
            rng = random.Random(seed)
            result = mod.select_tier_detailed(params, rng=rng, cost_map=cost_map)
            if result.cost_tiebreak and result.mode == "thompson":
                # cost_tiebreak が発動したケース: sonnet（安い方）が選ばれるはず
                assert result.tier == "sonnet", (
                    f"seed={seed}: contenders={result.contenders}, "
                    f"tier={result.tier} (expected sonnet=安い)"
                )
                assert "haiku" in result.contenders or "sonnet" in result.contenders
                found = True
                break
        assert found, "seed 0-199 の中で cost_tiebreak=True になるケースが見つからない"

    def test_deterministic_with_same_seed_and_cost_map(self) -> None:
        """同じ rng seed + cost_map で select_tier_detailed が完全再現（AC-9）。"""
        mod = _load_hook_module()
        params = {
            "haiku": (10.0, 10.0, 30),
            "sonnet": (10.0, 10.0, 30),
            "opus": (5.0, 5.0, 10),
        }
        cost_map = {"haiku": 18.0, "sonnet": 6.0, "opus": 30.0}
        seed = 42
        rng1 = random.Random(seed)
        rng2 = random.Random(seed)
        r1 = mod.select_tier_detailed(params, rng=rng1, cost_map=cost_map)
        r2 = mod.select_tier_detailed(params, rng=rng2, cost_map=cost_map)
        assert r1.tier == r2.tier
        assert r1.mode == r2.mode
        assert r1.cost_tiebreak == r2.cost_tiebreak
        assert r1.contenders == r2.contenders


class TestSelectTierLegacyCompat:
    """select_tier（委譲後）の後方互換テスト。"""

    def test_select_tier_returns_tuple(self) -> None:
        """select_tier は依然 (tier, mode) タプルを返す。"""
        mod = _load_hook_module()
        params = {
            "haiku": (10.0, 5.0, 30),
            "sonnet": (5.0, 5.0, 20),
            "opus": (2.0, 8.0, 20),
        }
        rng = random.Random(42)
        result = mod.select_tier(params, rng=rng)
        assert isinstance(result, tuple)
        assert len(result) == 2
        tier, mode = result
        assert tier in ("haiku", "sonnet", "opus")
        assert mode in ("uniform", "thompson")

    def test_select_tier_with_cost_map_returns_same_type(self) -> None:
        """cost_map を渡しても戻り値型は (str, str) タプルで不変。"""
        mod = _load_hook_module()
        params = {
            "haiku": (10.0, 5.0, 30),
            "sonnet": (5.0, 5.0, 20),
            "opus": (2.0, 8.0, 20),
        }
        cost_map = {"haiku": 6.0, "sonnet": 18.0, "opus": 30.0}
        rng = random.Random(42)
        result = mod.select_tier(params, rng=rng, cost_map=cost_map)
        assert isinstance(result, tuple)
        tier, mode = result
        assert tier in ("haiku", "sonnet", "opus")
        assert mode in ("uniform", "thompson")

    def test_existing_seed_test_unchanged(self) -> None:
        """既存テスト: 同一 seed で select_tier の結果が変わらない（後方互換）。"""
        mod = _load_hook_module()
        params = {
            "haiku": (10.0, 5.0, 14),
            "sonnet": (10.0, 5.0, 14),
            "opus": (10.0, 5.0, 14),
        }
        rng1 = random.Random(123)
        rng2 = random.Random(123)
        tier1, _ = mod.select_tier(params, rng=rng1)
        tier2, _ = mod.select_tier(params, rng=rng2)
        assert tier1 == tier2

    def test_uniform_cost_map_does_not_change_result(self) -> None:
        """uniform 分岐: cost_map の有無で select_tier 結果が不変（AC-3）。"""
        mod = _load_hook_module()
        # total_trials = 7 < 30 → uniform
        params = {
            "haiku": (1.0, 1.0, 3),
            "sonnet": (1.0, 1.0, 2),
            "opus": (1.0, 1.0, 2),
        }
        cost_map = {"haiku": 6.0, "sonnet": 18.0, "opus": 30.0}
        rng_none = random.Random(77)
        rng_with = random.Random(77)
        tier_none, mode_none = mod.select_tier(params, rng=rng_none, cost_map=None)
        tier_with, mode_with = mod.select_tier(params, rng=rng_with, cost_map=cost_map)
        assert tier_none == tier_with
        assert mode_none == mode_with == "uniform"

    def test_select_tier_mode_values_unchanged(self) -> None:
        """mode 値集合は {uniform, thompson} のまま（新値なし）。"""
        mod = _load_hook_module()
        seen_modes = set()
        # uniform
        params_uniform = {"haiku": (1.0, 1.0, 0), "sonnet": (1.0, 1.0, 0), "opus": (1.0, 1.0, 0)}
        _, m = mod.select_tier(params_uniform, rng=random.Random(1))
        seen_modes.add(m)
        # thompson
        params_thompson = {"haiku": (10.0, 5.0, 30), "sonnet": (5.0, 5.0, 20), "opus": (2.0, 8.0, 20)}
        _, m = mod.select_tier(params_thompson, rng=random.Random(1))
        seen_modes.add(m)
        assert seen_modes <= {"uniform", "thompson"}


# ---------------------------------------------------------------------------
# T4 (v2.23.0): observability + main 統合
#   - write_tier_selection の cost_tiebreak kw
#   - build_additional_context の cost_tiebreak kw
#   - main() の cost_map ハイブリッド解決 + select_tier_detailed 使用
# ---------------------------------------------------------------------------


class TestWriteTierSelectionCostTiebreak:
    """write_tier_selection の cost_tiebreak kw-only 引数テスト（AC-8）。"""

    def test_cost_tiebreak_true_adds_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cost_tiebreak=True のとき tier_selection.json に cost_tiebreak: true が入る。"""
        mod = _load_hook_module()
        target = tmp_path / "tier_selection.json"
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(target))

        mod.write_tier_selection("medium", "sonnet", "thompson", cost_tiebreak=True)
        data = json.loads(target.read_text(encoding="utf-8"))
        assert "cost_tiebreak" in data
        assert data["cost_tiebreak"] is True

    def test_cost_tiebreak_false_omits_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cost_tiebreak=False（デフォルト）のとき tier_selection.json に cost_tiebreak キーが出ない。

        既存の dict 完全一致テストと同形で確認（AC-8）。
        """
        mod = _load_hook_module()
        target = tmp_path / "tier_selection.json"
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(target))

        mod.write_tier_selection("complex", "opus", "thompson")
        data = json.loads(target.read_text(encoding="utf-8"))
        assert "cost_tiebreak" not in data
        # 既存テストと同一の期待 dict と完全一致
        assert data == {
            "complexity": "complex",
            "tier": "opus",
            "mode": "thompson",
            "suggested_model": "opus",
        }

    def test_cost_tiebreak_false_explicit_omits_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cost_tiebreak=False を明示渡しでもキーが出ない。"""
        mod = _load_hook_module()
        target = tmp_path / "tier_selection.json"
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(target))

        mod.write_tier_selection("simple", "haiku", "uniform", cost_tiebreak=False)
        data = json.loads(target.read_text(encoding="utf-8"))
        assert "cost_tiebreak" not in data


class TestBuildAdditionalContextCostTiebreak:
    """build_additional_context の cost_tiebreak kw-only 引数テスト。"""

    def _params(self) -> dict:
        return {
            "haiku": (10.0, 5.0, 30),
            "sonnet": (5.0, 5.0, 20),
            "opus": (2.0, 8.0, 20),
        }

    def test_cost_tiebreak_true_adds_suffix(self) -> None:
        """cost_tiebreak=True で suffix に cost-aware 文言が追加される。"""
        mod = _load_hook_module()
        text = mod.build_additional_context(
            "medium", "sonnet", "thompson", self._params(),
            cost_tiebreak=True,
        )
        assert "cost-aware" in text
        assert "成功率拮抗のため低コスト Tier を選択" in text

    def test_cost_tiebreak_false_no_suffix_change(self) -> None:
        """cost_tiebreak=False（デフォルト）では既存文言と完全一致（不変）。"""
        mod = _load_hook_module()
        text_default = mod.build_additional_context(
            "medium", "sonnet", "thompson", self._params(),
        )
        text_false = mod.build_additional_context(
            "medium", "sonnet", "thompson", self._params(),
            cost_tiebreak=False,
        )
        assert text_default == text_false
        assert "cost-aware" not in text_false

    def test_cost_tiebreak_combined_with_escalation(self) -> None:
        """cost_tiebreak=True と escalation_reason が両方あると両方の suffix が出る。"""
        mod = _load_hook_module()
        text = mod.build_additional_context(
            "medium", "sonnet", "thompson", self._params(),
            escalation_reason="haiku_failure_rate=0.60 (10 試行) → sonnet に昇格",
            cost_tiebreak=True,
        )
        assert "Phase 2-B 昇格" in text
        assert "cost-aware" in text


class TestMainCostMapIntegration:
    """main() の cost_map ハイブリッド解決と select_tier_detailed 使用の E2E テスト。"""

    def test_main_no_crash_when_c3_db_import_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """c3_db が import 失敗する状況でも crash せず additionalContext を stdout に出す（AC-5）。

        _load_c3_db_module を None 返しにモックして c3_db=None 経路を検証。
        cost_map=None → select_tier_detailed は従来 Thompson 動作。
        """
        mod = _load_hook_module()
        monkeypatch.setattr(
            mod, "TIER_SELECTION_PATH",
            str(tmp_path / "tier_selection.json"),
        )
        # _load_c3_db_module を None 返しに差し替え
        monkeypatch.setattr(mod, "_load_c3_db_module", lambda: None)

        payload = {"prompt": "新しい機能を追加してください"}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

        rc = mod.main()
        assert rc == 0

        captured = capsys.readouterr()
        assert captured.out
        out_data = json.loads(captured.out)
        assert "hookSpecificOutput" in out_data
        assert "additionalContext" in out_data["hookSpecificOutput"]

        # tier_selection.json も書かれていること
        json_path = tmp_path / "tier_selection.json"
        assert json_path.is_file()
        sel = json.loads(json_path.read_text(encoding="utf-8"))
        # c3_db=None → uniform params → uniform モード
        assert sel["mode"] == "uniform"
        # cost_map=None → cost_tiebreak は出ないはず
        assert "cost_tiebreak" not in sel

    def test_main_cost_tiebreak_appears_in_json_when_triggered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """cost_tiebreak が発動する状況で main 経由の tier_selection.json に cost_tiebreak=true が出る（AC-2）。

        c3_db.read_tier_cost_rate_for_complexity をモックして measured を注入し、
        拮抗する params + 安い tier が選ばれるケースを検証した。
        """
        mod = _load_hook_module()
        target = tmp_path / "tier_selection.json"
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(target))

        # haiku と sonnet が拮抗する params (同一 Beta パラメータ、trials >= 30)
        # read_tier_cost_rate_for_complexity は measured={} を返す（全て静的 fallback）
        tiebreak_params = {
            "haiku": (10.0, 10.0, 30),
            "sonnet": (10.0, 10.0, 30),
            "opus": (1.0, 10.0, 10),
        }

        mock_c3_db = types.SimpleNamespace(
            read_agent_tier_params=lambda role, complexity, **kw: tiebreak_params,
            read_tier_cost_rate_for_complexity=lambda complexity, **kw: {},  # 実測なし → 全て静的 fallback
            read_agent_failure_rate=lambda role, complexity, tier, **kw: (None, 0),  # escalation しない
        )
        monkeypatch.setattr(mod, "_load_c3_db_module", lambda: mock_c3_db)

        # seed を試して cost_tiebreak=True になるケースを探す
        found = False
        for seed in range(200):
            # tier_selection.json を毎回リセット
            if target.exists():
                target.unlink()

            # rng を seed 固定で差し込む（main 内部では random.Random() は使われないので
            # select_tier_detailed 呼び出し時の rng=None → global random を使う。
            # そのため random.seed() で制御する）
            monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "新しい機能を追加してください"})))
            random.seed(seed)

            # capsys は累積するため out を毎回確認
            rc = mod.main()
            assert rc == 0

            sel = json.loads(target.read_text(encoding="utf-8"))
            if sel.get("cost_tiebreak") is True:
                found = True
                break

        assert found, "seed 0-199 の中で cost_tiebreak=True が tier_selection.json に出るケースが見つからない"

    def test_main_cost_tiebreak_false_omits_key_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """cost_tiebreak が発動しない状況では tier_selection.json に cost_tiebreak キーが出ない（AC-8）。

        haiku を単独最大にして cost_tiebreak=False になることを確認。
        """
        mod = _load_hook_module()
        target = tmp_path / "tier_selection.json"
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(target))

        # haiku が圧倒的に有利 → 単独最大 → cost_tiebreak=False
        dominant_params = {
            "haiku": (50.0, 1.0, 51),
            "sonnet": (5.0, 5.0, 20),
            "opus": (2.0, 8.0, 20),
        }
        mock_c3_db = types.SimpleNamespace(
            read_agent_tier_params=lambda role, complexity, **kw: dominant_params,
            read_tier_cost_rate_for_complexity=lambda complexity, **kw: {},
            read_agent_failure_rate=lambda role, complexity, tier, **kw: (None, 0),  # escalation しない
        )
        monkeypatch.setattr(mod, "_load_c3_db_module", lambda: mock_c3_db)

        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "新しい機能を追加してください"})))
        random.seed(0)

        rc = mod.main()
        assert rc == 0

        sel = json.loads(target.read_text(encoding="utf-8"))
        # 単独最大 → cost_tiebreak キーが出ないはず
        assert "cost_tiebreak" not in sel
        assert sel["tier"] == "haiku"

    def test_main_rate_cost_map_no_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """main() が read_tier_cost_rate_for_complexity を呼び AttributeError なく完了する（AC-6 単位整合）。

        v2.24.0 で cost_map 源を read_tier_cost_for_complexity から
        read_tier_cost_rate_for_complexity に切り替えたため、
        SimpleNamespace に read_tier_cost_rate_for_complexity が存在しないと
        AttributeError が発生する。この属性名が正しく解決されることを確認。
        """
        mod = _load_hook_module()
        target = tmp_path / "tier_selection.json"
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(target))

        params = {
            "haiku": (5.0, 5.0, 20),
            "sonnet": (5.0, 5.0, 20),
            "opus": (5.0, 5.0, 20),
        }
        # read_tier_cost_rate_for_complexity（旧名なし）のみ定義 → 旧名があると AttributeError
        mock_c3_db = types.SimpleNamespace(
            read_agent_tier_params=lambda role, complexity, **kw: params,
            read_tier_cost_rate_for_complexity=lambda complexity, **kw: {},  # rate 関数・実測なし
            read_agent_failure_rate=lambda role, complexity, tier, **kw: (None, 0),
        )
        monkeypatch.setattr(mod, "_load_c3_db_module", lambda: mock_c3_db)

        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "テスト"})))
        random.seed(0)

        # AttributeError が発生しないこと、かつ rc=0 であること
        rc = mod.main()
        assert rc == 0
        assert target.is_file()

    def test_main_rate_cost_map_with_measured_uses_rate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """main() が rate 実測値を cost_map に使い、静的 fallback と同次元で比較する（AC-6）。

        read_tier_cost_rate_for_complexity が haiku=6.0 USD/MTok を返す場合、
        cost_map['haiku'] は 6.0 になる（tier_reference_cost('haiku')=6.0 の静的値と同次元）。
        単位混在が解消されており crash なく rc=0 で完了した。

        NOTE: cost_tiebreak の発動 assert は
        ``test_main_cost_tiebreak_appears_in_json_when_triggered`` で担保。
        本テストは rate 関数への切替が AttributeError なく完了することの確認が主眼。
        """
        mod = _load_hook_module()
        target = tmp_path / "tier_selection.json"
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(target))

        # haiku に実測 rate を返す（tier_reference_cost と同次元 USD/MTok）
        measured_rates = {"haiku": 6.0}

        params = {
            "haiku": (10.0, 10.0, 30),
            "sonnet": (10.0, 10.0, 30),
            "opus": (1.0, 10.0, 10),
        }
        mock_c3_db = types.SimpleNamespace(
            read_agent_tier_params=lambda role, complexity, **kw: params,
            read_tier_cost_rate_for_complexity=lambda complexity, **kw: dict(measured_rates),
            read_agent_failure_rate=lambda role, complexity, tier, **kw: (None, 0),
        )
        monkeypatch.setattr(mod, "_load_c3_db_module", lambda: mock_c3_db)

        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "テスト"})))
        random.seed(0)

        rc = mod.main()
        assert rc == 0
        assert target.is_file()
        sel = json.loads(target.read_text(encoding="utf-8"))
        # 拮抗群が存在するので cost_tiebreak が発動する可能性があるが crash しないことが重要
        assert sel.get("tier") in ("haiku", "sonnet", "opus")


# ---------------------------------------------------------------------------
# T2 (v2.25.0): EPSILON 調整可能化 — _resolve_epsilon / epsilon kwarg / env 未設定一致
# ---------------------------------------------------------------------------


class TestResolveEpsilon:
    """_resolve_epsilon() の env パース・バリデーションテスト（AC B-(2)）。"""

    def test_valid_value_returns_float(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """正常値 0.1 → 0.1 を返す（AC B-(1)）。"""
        mod = _load_hook_module()
        monkeypatch.setenv("C3_TIER_EPSILON", "0.1")
        assert mod._resolve_epsilon() == pytest.approx(0.1)

    def test_valid_boundary_1_returns_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """境界値 1.0 は有効（0 < x <= 1 の上限）。"""
        mod = _load_hook_module()
        monkeypatch.setenv("C3_TIER_EPSILON", "1.0")
        assert mod._resolve_epsilon() == pytest.approx(1.0)

    def test_valid_small_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """0.001 など小さい正値は有効。"""
        mod = _load_hook_module()
        monkeypatch.setenv("C3_TIER_EPSILON", "0.001")
        assert mod._resolve_epsilon() == pytest.approx(0.001)

    def test_unset_returns_default_no_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """未設定 → デフォルト 0.05・警告なし（AC B-(2)）。"""
        mod = _load_hook_module()
        monkeypatch.delenv("C3_TIER_EPSILON", raising=False)
        result = mod._resolve_epsilon()
        assert result == pytest.approx(0.05)
        # stderr に何も出ない
        assert capsys.readouterr().err == ""

    def test_empty_string_returns_default_no_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """空文字 → デフォルト 0.05・警告なし。"""
        mod = _load_hook_module()
        monkeypatch.setenv("C3_TIER_EPSILON", "")
        result = mod._resolve_epsilon()
        assert result == pytest.approx(0.05)
        assert capsys.readouterr().err == ""

    def test_non_numeric_returns_default_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """非数値 "abc" → デフォルト 0.05 + stderr 警告（AC B-(2)）。"""
        mod = _load_hook_module()
        monkeypatch.setenv("C3_TIER_EPSILON", "abc")
        result = mod._resolve_epsilon()
        assert result == pytest.approx(0.05)
        err = capsys.readouterr().err
        assert "C3_TIER_EPSILON" in err
        assert "abc" in err

    def test_zero_returns_default_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """0 → デフォルト 0.05 + stderr 警告（AC B-(2)・x <= 0）。"""
        mod = _load_hook_module()
        monkeypatch.setenv("C3_TIER_EPSILON", "0")
        result = mod._resolve_epsilon()
        assert result == pytest.approx(0.05)
        err = capsys.readouterr().err
        assert "C3_TIER_EPSILON" in err

    def test_negative_returns_default_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """-0.1 → デフォルト 0.05 + stderr 警告（AC B-(2)・x <= 0）。"""
        mod = _load_hook_module()
        monkeypatch.setenv("C3_TIER_EPSILON", "-0.1")
        result = mod._resolve_epsilon()
        assert result == pytest.approx(0.05)
        err = capsys.readouterr().err
        assert "C3_TIER_EPSILON" in err

    def test_above_1_returns_default_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """1.5 → デフォルト 0.05 + stderr 警告（AC B-(2)・x > 1）。"""
        mod = _load_hook_module()
        monkeypatch.setenv("C3_TIER_EPSILON", "1.5")
        result = mod._resolve_epsilon()
        assert result == pytest.approx(0.05)
        err = capsys.readouterr().err
        assert "C3_TIER_EPSILON" in err

    def test_nan_returns_default_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """NaN → デフォルト 0.05 + stderr 警告（AC B-(2)・NaN 排除 R3）。"""
        mod = _load_hook_module()
        monkeypatch.setenv("C3_TIER_EPSILON", "nan")
        result = mod._resolve_epsilon()
        assert result == pytest.approx(0.05)
        err = capsys.readouterr().err
        assert "C3_TIER_EPSILON" in err
        assert "NaN" in err


class TestEpsilonKwarg:
    """epsilon kwarg を変えると contenders 判定が変わることを示すテスト（AC B-(1)）。"""

    def test_larger_epsilon_widens_contenders(self) -> None:
        """epsilon を大きくすると拮抗群が広がる。"""
        mod = _load_hook_module()
        # haiku=0.85, sonnet=0.80（差 0.05）、opus=0.60
        samples = {"haiku": 0.85, "sonnet": 0.80, "opus": 0.60}
        cost_map = {"haiku": 30.0, "sonnet": 6.0, "opus": 1.0}

        # epsilon=0.04 → 差 0.05 > 0.04 なので haiku のみ contender（tiebreak 不発動）
        chosen_small, did_small, contenders_small = mod._cost_tiebreak(
            samples, cost_map, epsilon=0.04
        )
        assert "haiku" in contenders_small
        assert "sonnet" not in contenders_small
        assert did_small is False

        # epsilon=0.06 → 差 0.05 <= 0.06 なので haiku と sonnet が拮抗
        chosen_large, did_large, contenders_large = mod._cost_tiebreak(
            samples, cost_map, epsilon=0.06
        )
        assert "haiku" in contenders_large
        assert "sonnet" in contenders_large
        assert did_large is True
        # コストは sonnet の方が安いので sonnet が選ばれる
        assert chosen_large == "sonnet"

    def test_epsilon_kwarg_in_select_tier_detailed_changes_contenders(self) -> None:
        """select_tier_detailed に epsilon を明示すると contenders が変わる（AC B-(1)）。

        決定論的方式: _cost_tiebreak に固定サンプル値を直接渡し、
        epsilon の大小で contenders 幅が変わることを検証した。
        test_larger_epsilon_widens_contenders と同じアプローチ。
        """
        mod = _load_hook_module()
        # haiku=0.85, sonnet=0.80（差 0.05）、opus=0.60
        # epsilon=0.04 のとき: 差 0.05 > 0.04 → haiku のみ contender（tiebreak 不発動）
        # epsilon=0.06 のとき: 差 0.05 <= 0.06 → haiku と sonnet が拮抗（tiebreak 発動）
        samples = {"haiku": 0.85, "sonnet": 0.80, "opus": 0.60}
        cost_map = {"haiku": 30.0, "sonnet": 6.0, "opus": 1.0}

        # epsilon=0.04: haiku のみが最高サンプル（tiebreak 不発動）
        chosen_small, did_small, contenders_small = mod._cost_tiebreak(
            samples, cost_map, epsilon=0.04
        )
        assert "haiku" in contenders_small
        assert "sonnet" not in contenders_small
        assert did_small is False

        # epsilon=0.06: haiku と sonnet が拮抗（tiebreak 発動）→ 安い sonnet が選ばれる
        chosen_large, did_large, contenders_large = mod._cost_tiebreak(
            samples, cost_map, epsilon=0.06
        )
        assert "haiku" in contenders_large
        assert "sonnet" in contenders_large
        assert did_large is True
        assert chosen_large == "sonnet"

    def test_select_tier_epsilon_kwarg_propagates(self) -> None:
        """select_tier の epsilon kwarg が select_tier_detailed に伝播する。"""
        mod = _load_hook_module()
        params = {
            "haiku": (10.0, 10.0, 30),
            "sonnet": (10.0, 10.0, 30),
            "opus": (1.0, 10.0, 30),
        }
        cost_map = {"haiku": 18.0, "sonnet": 6.0, "opus": 1.0}

        # 同一 seed で epsilon=0.0001（極小・拮抗ほぼ不発）と epsilon=1.0（全件拮抗）で結果が異なる可能性
        # epsilon=1.0 なら全員が contenders になり最安 opus が選ばれる
        rng = random.Random(42)
        tier, mode = mod.select_tier(params, rng=rng, cost_map=cost_map, epsilon=1.0)
        # epsilon=1.0 で全員拮抗 → opus（最安）が選ばれる
        assert tier == "opus"
        assert mode == "thompson"


class TestEpsilonEnvUnsetMatchesLegacy:
    """env 未設定で select_tier 出力が v2.24.0 と完全一致（AC B-(3)）。"""

    def test_no_env_select_tier_detailed_matches_default_epsilon(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C3_TIER_EPSILON 未設定で select_tier_detailed の結果が EPSILON=0.05 と同一。

        env 未設定の _resolve_epsilon() は EPSILON（0.05）を返すため、
        select_tier_detailed に epsilon=None（デフォルト）を渡した場合と
        epsilon=0.05 を明示した場合が完全一致する。
        """
        mod = _load_hook_module()
        monkeypatch.delenv("C3_TIER_EPSILON", raising=False)

        params = {
            "haiku": (10.0, 10.0, 30),
            "sonnet": (10.0, 10.0, 30),
            "opus": (5.0, 5.0, 30),
        }
        cost_map = {"haiku": 18.0, "sonnet": 6.0, "opus": 30.0}

        for seed in range(50):
            rng1 = random.Random(seed)
            rng2 = random.Random(seed)
            # epsilon=None（デフォルト・EPSILON=0.05 と等価）
            r_default = mod.select_tier_detailed(params, rng=rng1, cost_map=cost_map)
            # epsilon=0.05 を明示
            r_explicit = mod.select_tier_detailed(
                params, rng=rng2, cost_map=cost_map, epsilon=0.05
            )
            assert r_default.tier == r_explicit.tier, f"seed={seed}: 挙動不一致"
            assert r_default.cost_tiebreak == r_explicit.cost_tiebreak, f"seed={seed}: cost_tiebreak 不一致"

    def test_db_epsilon_tiebreak_is_ssot(self) -> None:
        """db.EPSILON_TIEBREAK が 0.05 であり SSOT であることを確認（AC B-(4)）。"""
        from c3 import db as c3_db
        assert c3_db.EPSILON_TIEBREAK == 0.05

    def test_module_epsilon_matches_db_ssot(self) -> None:
        """select_tier モジュールの EPSILON が db.EPSILON_TIEBREAK と一致する（AC B-(4)）。"""
        mod = _load_hook_module()
        from c3 import db as c3_db
        assert mod.EPSILON == pytest.approx(c3_db.EPSILON_TIEBREAK)


class TestResolveCostLambda:
    """_resolve_cost_lambda() の env パース・バリデーションテスト（T3 AC）。"""

    def test_cost_lambda_default_is_none(self) -> None:
        """COST_LAMBDA_DEFAULT は None（db 由来・センチネル値）。"""
        mod = _load_hook_module()
        assert mod.COST_LAMBDA_DEFAULT is None

    def test_unset_returns_none_no_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """未設定 → None・警告なし。"""
        mod = _load_hook_module()
        monkeypatch.delenv("C3_TIER_COST_LAMBDA", raising=False)
        result = mod._resolve_cost_lambda()
        assert result is None
        assert capsys.readouterr().err == ""

    def test_empty_string_returns_none_no_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """空文字 → None・警告なし。"""
        mod = _load_hook_module()
        monkeypatch.setenv("C3_TIER_COST_LAMBDA", "")
        result = mod._resolve_cost_lambda()
        assert result is None
        assert capsys.readouterr().err == ""

    def test_zero_returns_0_no_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """"0" → 0.0（x == 0 は許容・cost 無視の明示オプト）・警告なし。"""
        mod = _load_hook_module()
        monkeypatch.setenv("C3_TIER_COST_LAMBDA", "0")
        result = mod._resolve_cost_lambda()
        assert result == pytest.approx(0.0)
        assert capsys.readouterr().err == ""

    def test_valid_middle_value(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """"0.3" → 0.3・警告なし。"""
        mod = _load_hook_module()
        monkeypatch.setenv("C3_TIER_COST_LAMBDA", "0.3")
        result = mod._resolve_cost_lambda()
        assert result == pytest.approx(0.3)
        assert capsys.readouterr().err == ""

    def test_valid_upper_boundary(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """"1.0" → 1.0（上限境界・許容）・警告なし。"""
        mod = _load_hook_module()
        monkeypatch.setenv("C3_TIER_COST_LAMBDA", "1.0")
        result = mod._resolve_cost_lambda()
        assert result == pytest.approx(1.0)
        assert capsys.readouterr().err == ""

    def test_non_numeric_returns_none_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """"abc" → None + stderr 警告。"""
        mod = _load_hook_module()
        monkeypatch.setenv("C3_TIER_COST_LAMBDA", "abc")
        result = mod._resolve_cost_lambda()
        assert result is None
        err = capsys.readouterr().err
        assert "C3_TIER_COST_LAMBDA" in err
        assert "abc" in err

    def test_negative_returns_none_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """"- 0.1" → None + stderr 警告（x < 0 は拒否）。"""
        mod = _load_hook_module()
        monkeypatch.setenv("C3_TIER_COST_LAMBDA", "-0.1")
        result = mod._resolve_cost_lambda()
        assert result is None
        err = capsys.readouterr().err
        assert "C3_TIER_COST_LAMBDA" in err

    def test_above_max_returns_none_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """"5.1" → None + stderr 警告（x > COST_LAMBDA_MAX は拒否）。v2.27.0 λ 上限 5.0 化に伴い "1.5" → "5.1" に更新。"""
        mod = _load_hook_module()
        monkeypatch.setenv("C3_TIER_COST_LAMBDA", "5.1")
        result = mod._resolve_cost_lambda()
        assert result is None
        err = capsys.readouterr().err
        assert "C3_TIER_COST_LAMBDA" in err

    def test_nan_returns_none_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """"nan" → None + stderr 警告。"""
        mod = _load_hook_module()
        monkeypatch.setenv("C3_TIER_COST_LAMBDA", "nan")
        result = mod._resolve_cost_lambda()
        assert result is None
        err = capsys.readouterr().err
        assert "C3_TIER_COST_LAMBDA" in err
        assert "NaN" in err

    def test_new_upper_boundary_5_0_valid(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """"5.0" → 5.0（v2.27.0 新上限境界・許容）・警告なし。"""
        mod = _load_hook_module()
        monkeypatch.setenv("C3_TIER_COST_LAMBDA", "5.0")
        result = mod._resolve_cost_lambda()
        assert result == pytest.approx(5.0)
        assert capsys.readouterr().err == ""

    def test_value_2_5_valid_in_extended_range(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """"2.5" → 2.5（旧上限 1.0 超だが新範囲 [0, 5.0] 内）・警告なし。"""
        mod = _load_hook_module()
        monkeypatch.setenv("C3_TIER_COST_LAMBDA", "2.5")
        result = mod._resolve_cost_lambda()
        assert result == pytest.approx(2.5)
        assert capsys.readouterr().err == ""

    def test_value_1_5_valid_in_extended_range(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """"1.5" → 1.5（旧上限超だが新範囲 [0, 5.0] 内）・警告なし。"""
        mod = _load_hook_module()
        monkeypatch.setenv("C3_TIER_COST_LAMBDA", "1.5")
        result = mod._resolve_cost_lambda()
        assert result == pytest.approx(1.5)
        assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# T6 (v2.27.0): CR-Q-001 精緻化 — _cost_tiebreak 全 tier コスト同値時の挙動
# ---------------------------------------------------------------------------


class TestCostTiebreakAllSameCost:
    """CR-Q-001: _cost_tiebreak で全 tier コスト同値時は did_tiebreak=False。

    cost_map の全 tier が同値のとき、hi == lo になるため cost は選択に無関与。
    chosen は argmax(sample) で不変、did_tiebreak は False。
    """

    def test_all_same_cost_did_tiebreak_false(self) -> None:
        """CR-Q-001: 全 tier コスト同値で did_tiebreak=False・chosen==argmax(sample)。"""
        mod = _load_hook_module()
        epsilon = mod.EPSILON  # 0.05
        base = 0.85
        # haiku と sonnet が拮抗（contenders が複数）、全 tier コスト同値
        samples = {"haiku": base, "sonnet": base - epsilon + 0.01, "opus": base - 0.2}
        # 全 tier に同値コストを設定（hi == lo になる）
        cost_map = {"haiku": 10.0, "sonnet": 10.0, "opus": 10.0}
        chosen, did_tiebreak, contenders = mod._cost_tiebreak(samples, cost_map)
        # 全コスト同値 → did_tiebreak=False
        assert did_tiebreak is False
        # chosen は argmax(sample) = haiku
        assert chosen == "haiku"

    def test_all_same_cost_argmax_wins(self) -> None:
        """CR-Q-001: 全 tier コスト同値・拮抗群複数で argmax(samples) が選ばれる。"""
        mod = _load_hook_module()
        # sonnet が最大サンプル
        samples = {"haiku": 0.70, "sonnet": 0.80, "opus": 0.75}
        cost_map = {"haiku": 5.0, "sonnet": 5.0, "opus": 5.0}
        chosen, did_tiebreak, _ = mod._cost_tiebreak(samples, cost_map)
        assert did_tiebreak is False
        assert chosen == "sonnet"  # argmax

    def test_distinct_cost_did_tiebreak_true_unchanged(self) -> None:
        """既存テスト不変確認: distinct コストで did_tiebreak=True のままであること。

        test_tiebreak_picks_cheapest_among_contenders のシナリオを再現して
        CR-Q-001 修正後も既存挙動が変わらないことを確認する。
        """
        mod = _load_hook_module()
        epsilon = mod.EPSILON  # 0.05
        base = 0.85
        samples = {"haiku": base, "sonnet": base - epsilon + 0.01, "opus": base - 0.2}
        cost_map = {"haiku": 30.0, "sonnet": 6.0, "opus": 1.0}  # distinct コスト
        chosen, did_tiebreak, contenders = mod._cost_tiebreak(samples, cost_map)
        assert did_tiebreak is True  # distinct コストなので tiebreak 発動
        assert chosen == "sonnet"  # sonnet が最安 contender


# ---------------------------------------------------------------------------
# T7 (v2.27.0): parity テスト — hook _resolve_cost_lambda と db.resolve_cost_lambda の一致
# ---------------------------------------------------------------------------


class TestResolveCostLambdaParity:
    """parity: hook の _resolve_cost_lambda() と db.resolve_cost_lambda() の戻り値が一致する。

    値ドリフト防止。入力マトリクスで両関数の戻り値を比較する。
    """

    _CASES = [
        # (env_value_or_None, label)
        (None, "未設定"),
        ("0", '"0"'),
        ("2.5", '"2.5"'),
        ("5.0", '"5.0"'),
        ("5.1", '"5.1"'),
        ("abc", '"abc"'),
        ("nan", '"nan"'),
        ("-1", '"-1"'),
    ]

    def _call_hook(self, env_val, monkeypatch: pytest.MonkeyPatch):
        mod = _load_hook_module()
        if env_val is None:
            monkeypatch.delenv("C3_TIER_COST_LAMBDA", raising=False)
        else:
            monkeypatch.setenv("C3_TIER_COST_LAMBDA", env_val)
        return mod._resolve_cost_lambda()

    def _call_db(self, env_val, monkeypatch: pytest.MonkeyPatch):
        from c3 import db as c3_db  # noqa: PLC0415
        if env_val is None:
            monkeypatch.delenv("C3_TIER_COST_LAMBDA", raising=False)
        else:
            monkeypatch.setenv("C3_TIER_COST_LAMBDA", env_val)
        return c3_db.resolve_cost_lambda()

    @pytest.mark.parametrize("env_val,label", _CASES)
    def test_parity(
        self,
        env_val,
        label,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """hook と db の _resolve_cost_lambda() 戻り値が一致する（入力: {label}）。"""
        hook_result = self._call_hook(env_val, monkeypatch)
        capsys.readouterr()  # hook の stderr をクリア
        db_result = self._call_db(env_val, monkeypatch)
        capsys.readouterr()  # db の stderr をクリア

        if hook_result is None:
            assert db_result is None, (
                f"label={label}: hook=None, db={db_result!r} — 不一致"
            )
        else:
            assert db_result is not None, (
                f"label={label}: hook={hook_result!r}, db=None — 不一致"
            )
            assert hook_result == pytest.approx(db_result), (
                f"label={label}: hook={hook_result!r}, db={db_result!r} — 値不一致"
            )


# ---------------------------------------------------------------------------
# T4 (v2.26.0): cost-weighting 本体 — SelectionResult.cost_weighted /
#               _cost_tiebreak 3 経路 / select_tier_detailed lam kwarg
# ---------------------------------------------------------------------------


class TestCostWeightingBackwardCompat:
    """① 後方互換（最重要）: lam 省略時の _cost_tiebreak が v2.25.0 と完全一致。

    既存 TestCostTiebreak 群が無修正で green であることが後方互換の証明。
    本クラスはその補強として「lam=None 明示で既存挙動が変わらない」を確認する。
    """

    def test_lam_none_explicit_single_contender(self) -> None:
        """lam=None 明示でも lam 省略と同一結果（経路 1: contenders<=1 → argmax）。"""
        mod = _load_hook_module()
        samples = {"haiku": 0.9, "sonnet": 0.5, "opus": 0.3}
        cost_map = {"haiku": 30.0, "sonnet": 18.0, "opus": 6.0}
        # lam 省略
        chosen_omit, did_omit, contenders_omit = mod._cost_tiebreak(samples, cost_map)
        # lam=None 明示
        chosen_none, did_none, contenders_none = mod._cost_tiebreak(samples, cost_map, lam=None)
        assert chosen_omit == chosen_none == "haiku"
        assert did_omit is False
        assert did_none is False
        assert contenders_omit == contenders_none

    def test_lam_none_explicit_tiebreak_picks_cheapest(self) -> None:
        """lam=None 明示でも拮抗群内で最安 tier が選ばれる（経路 1）。"""
        mod = _load_hook_module()
        epsilon = mod.EPSILON  # 0.05
        base = 0.85
        samples = {"haiku": base, "sonnet": base - epsilon + 0.01, "opus": base - 0.2}
        cost_map = {"haiku": 30.0, "sonnet": 6.0, "opus": 1.0}
        # lam 省略
        chosen_omit, did_omit, _ = mod._cost_tiebreak(samples, cost_map)
        # lam=None 明示
        chosen_none, did_none, _ = mod._cost_tiebreak(samples, cost_map, lam=None)
        assert chosen_omit == chosen_none == "sonnet"
        assert did_omit is True
        assert did_none is True

    def test_lam_none_seed_loop_matches_default(self) -> None:
        """② seed 一致: lam=None の select_tier_detailed が lam 省略と seed 0-49 で全一致。"""
        mod = _load_hook_module()
        params = {
            "haiku": (10.0, 10.0, 30),
            "sonnet": (10.0, 10.0, 30),
            "opus": (5.0, 5.0, 30),
        }
        cost_map = {"haiku": 18.0, "sonnet": 6.0, "opus": 30.0}
        for seed in range(50):
            rng1 = random.Random(seed)
            rng2 = random.Random(seed)
            r_omit = mod.select_tier_detailed(params, rng=rng1, cost_map=cost_map)
            r_lam_none = mod.select_tier_detailed(params, rng=rng2, cost_map=cost_map, lam=None)
            assert r_omit.tier == r_lam_none.tier, f"seed={seed}: tier 不一致"
            assert r_omit.cost_tiebreak == r_lam_none.cost_tiebreak, f"seed={seed}: cost_tiebreak 不一致"
            assert r_omit.cost_weighted is False, f"seed={seed}: lam 省略時 cost_weighted は False であるべき"
            assert r_lam_none.cost_weighted is False, f"seed={seed}: lam=None 時 cost_weighted は False であるべき"


class TestCostTiebreakLambdaZero:
    """③ λ=0: cost 無視（経路 0）。拮抗群でも argmax(sample)・did_tiebreak=False。"""

    def test_lam_zero_ignores_cost_single_contender(self) -> None:
        """lam=0 で contenders が 1 件のとき argmax(sample)・did_tiebreak=False。"""
        mod = _load_hook_module()
        samples = {"haiku": 0.9, "sonnet": 0.5, "opus": 0.3}
        cost_map = {"haiku": 30.0, "sonnet": 18.0, "opus": 6.0}
        chosen, did_tiebreak, _ = mod._cost_tiebreak(samples, cost_map, lam=0)
        assert chosen == "haiku"
        assert did_tiebreak is False

    def test_lam_zero_ignores_cost_multiple_contenders(self) -> None:
        """lam=0 で拮抗群が複数あっても cost を無視してargmax(sample)。did_tiebreak=False。"""
        mod = _load_hook_module()
        epsilon = mod.EPSILON  # 0.05
        base = 0.85
        # haiku と sonnet が拮抗。cost は sonnet の方が安い。lam=0 なら haiku（max sample）を選ぶ。
        samples = {"haiku": base, "sonnet": base - epsilon + 0.01, "opus": base - 0.2}
        cost_map = {"haiku": 30.0, "sonnet": 6.0, "opus": 1.0}
        chosen, did_tiebreak, _ = mod._cost_tiebreak(samples, cost_map, lam=0)
        # lam=0 → cost 無視 → argmax = haiku（lam=None なら sonnet が選ばれる）
        assert chosen == "haiku"
        assert did_tiebreak is False

    def test_lam_zero_float_comparison(self) -> None:
        """lam=0.0（float）でも経路 0 に入る（None == 0 は Python では False）。"""
        mod = _load_hook_module()
        samples = {"haiku": 0.81, "sonnet": 0.80, "opus": 0.79}
        cost_map = {"haiku": 6.0, "sonnet": 18.0, "opus": 30.0}
        chosen, did_tiebreak, _ = mod._cost_tiebreak(samples, cost_map, lam=0.0)
        assert chosen == "haiku"  # argmax
        assert did_tiebreak is False


class TestCostTiebreakLambdaPositive:
    """④⑤ λ>0 weighting テスト（経路 2・全 tier）。"""

    _SAMPLES = {"haiku": 0.55, "sonnet": 0.60, "opus": 0.62}
    _COST_MAP = {"haiku": 6.0, "sonnet": 18.0, "opus": 30.0}

    def test_lam_positive_prefers_cheaper_tier(self) -> None:
        """④ λ>0 大のとき安い tier（haiku）に寄る決定論ケース。cost_weighted=True。"""
        mod = _load_hook_module()
        # opus が argmax(sample=0.62)、haiku が最安(6.0)。λ 大ならコスト寄り。
        # λ=0.5 で score: haiku=0.55-0.5*0=0.55, sonnet=0.60-0.5*0.5=0.35, opus=0.62-0.5*1.0=0.12
        # → haiku が最大 score
        chosen, did_tiebreak, _ = mod._cost_tiebreak(self._SAMPLES, self._COST_MAP, lam=0.5)
        assert chosen == "haiku"

    def test_lam_positive_cost_weighted_true_in_select_tier_detailed(self) -> None:
        """④ select_tier_detailed(lam>0) で cost_weighted=True。"""
        mod = _load_hook_module()
        # trials >= 30 で Thompson 分岐に入る params
        params = {
            "haiku": (1.0, 1.0, 30),
            "sonnet": (1.0, 1.0, 30),
            "opus": (1.0, 1.0, 30),
        }
        # rng を固定して samples が _SAMPLES と同等になるのは難しいため
        # _cost_tiebreak 直接呼び出しで cost_weighted を確認する代わりに
        # select_tier_detailed で lam>0 かつ cost_map 有効 → cost_weighted=True を確認
        rng = random.Random(42)
        result = mod.select_tier_detailed(params, rng=rng, cost_map=self._COST_MAP, lam=0.5)
        assert result.cost_weighted is True
        assert result.mode == "thompson"

    def test_lam_large_vs_small_changes_chosen(self) -> None:
        """⑤ λ 大小で chosen が変わる（λ=0.1 vs λ=1.0）。

        サンプル設定: opus が argmax(0.90)、haiku が最安(6.0)、opus が最高コスト(30.0)。
        コスト min-max 正規化: haiku=0.0, sonnet=0.5, opus=1.0。

        λ=0.1:
            score_opus  = 0.90 - 0.1*1.0 = 0.80  ← 最大
            score_sonnet= 0.70 - 0.1*0.5 = 0.65
            score_haiku = 0.70 - 0.1*0.0 = 0.70
            → opus が選ばれる

        λ=1.0:
            score_haiku = 0.70 - 1.0*0.0 = 0.70  ← 最大
            score_sonnet= 0.70 - 1.0*0.5 = 0.20
            score_opus  = 0.90 - 1.0*1.0 = -0.10
            → haiku が選ばれる
        """
        mod = _load_hook_module()
        samples = {"haiku": 0.70, "sonnet": 0.70, "opus": 0.90}
        cost_map = {"haiku": 6.0, "sonnet": 18.0, "opus": 30.0}

        chosen_small, _, _ = mod._cost_tiebreak(samples, cost_map, lam=0.1)
        chosen_large, _, _ = mod._cost_tiebreak(samples, cost_map, lam=1.0)
        assert chosen_small != chosen_large, (
            f"λ=0.1 ({chosen_small}) と λ=1.0 ({chosen_large}) で chosen が変わるはず"
        )
        assert chosen_small == "opus"   # λ=0.1 では opus（サンプル最大）が score 最大
        assert chosen_large == "haiku"  # λ=1.0 では haiku（最安）が score 最大


class TestCostTiebreakCostMapNone:
    """⑥ cost_map=None: lam 値（None/0/0.5）に関わらず argmax(sample)。"""

    _SAMPLES = {"haiku": 0.55, "sonnet": 0.60, "opus": 0.62}

    def test_cost_map_none_lam_none(self) -> None:
        mod = _load_hook_module()
        chosen, did_tiebreak, _ = mod._cost_tiebreak(self._SAMPLES, None, lam=None)
        assert chosen == "opus"  # argmax
        assert did_tiebreak is False

    def test_cost_map_none_lam_zero(self) -> None:
        mod = _load_hook_module()
        chosen, did_tiebreak, _ = mod._cost_tiebreak(self._SAMPLES, None, lam=0)
        assert chosen == "opus"  # argmax
        assert did_tiebreak is False

    def test_cost_map_none_lam_positive(self) -> None:
        """cost_map=None なら lam>0 でも argmax(sample)（経路 0 が優先）。"""
        mod = _load_hook_module()
        chosen, did_tiebreak, _ = mod._cost_tiebreak(self._SAMPLES, None, lam=0.5)
        assert chosen == "opus"  # argmax
        assert did_tiebreak is False


class TestSelectionResultCostWeighted:
    """⑦ SelectionResult: cost_weighted default False・既存属性アクセス不変・uniform 不可侵。"""

    def test_cost_weighted_default_false(self) -> None:
        """cost_weighted のデフォルトは False（末尾追加・既存コード互換）。"""
        mod = _load_hook_module()
        r = mod.SelectionResult("haiku", "thompson")
        assert r.cost_weighted is False

    def test_existing_attributes_unchanged(self) -> None:
        """既存属性（tier/mode/cost_tiebreak/contenders）アクセスが不変。"""
        mod = _load_hook_module()
        r = mod.SelectionResult("sonnet", "thompson", True, ("haiku", "sonnet"), True)
        assert r.tier == "sonnet"
        assert r.mode == "thompson"
        assert r.cost_tiebreak is True
        assert r.contenders == ("haiku", "sonnet")
        assert r.cost_weighted is True

    def test_uniform_branch_cost_weighted_false(self) -> None:
        """uniform 分岐では cost_weighted は常に False（不可侵）。"""
        mod = _load_hook_module()
        # total_trials = 7 < 30 → uniform
        params = {
            "haiku": (1.0, 1.0, 3),
            "sonnet": (1.0, 1.0, 2),
            "opus": (1.0, 1.0, 2),
        }
        cost_map = {"haiku": 6.0, "sonnet": 18.0, "opus": 30.0}
        # lam>0 かつ cost_map 有効でも uniform 分岐なら cost_weighted=False
        rng = random.Random(42)
        result = mod.select_tier_detailed(params, rng=rng, cost_map=cost_map, lam=0.5)
        assert result.mode == "uniform"
        assert result.cost_weighted is False
        assert result.cost_tiebreak is False
        assert result.contenders == ()

    def test_cost_weighted_false_when_lam_none(self) -> None:
        """lam=None（env 未設定）では cost_map があっても cost_weighted=False。"""
        mod = _load_hook_module()
        params = {
            "haiku": (10.0, 10.0, 30),
            "sonnet": (10.0, 10.0, 30),
            "opus": (5.0, 5.0, 30),
        }
        cost_map = {"haiku": 6.0, "sonnet": 18.0, "opus": 30.0}
        rng = random.Random(42)
        result = mod.select_tier_detailed(params, rng=rng, cost_map=cost_map, lam=None)
        assert result.cost_weighted is False

    def test_cost_weighted_false_when_cost_map_none(self) -> None:
        """cost_map=None では lam>0 でも cost_weighted=False（経路 0 → weighting 未適用）。"""
        mod = _load_hook_module()
        params = {
            "haiku": (10.0, 10.0, 30),
            "sonnet": (10.0, 10.0, 30),
            "opus": (5.0, 5.0, 30),
        }
        rng = random.Random(42)
        result = mod.select_tier_detailed(params, rng=rng, cost_map=None, lam=0.5)
        assert result.cost_weighted is False


# ---------------------------------------------------------------------------
# T5 (v2.26.0): observability + main 統合
#   - build_additional_context の cost_weighted kw
#   - write_tier_selection の cost_weighted / cost_lambda kw
#   - main() の lam 配線（E2E 後方互換・λ 設定 E2E・degrade）
# ---------------------------------------------------------------------------


class TestBuildAdditionalContextCostWeighted:
    """② build_additional_context の cost_weighted kw テスト（T5 AC）。

    cost_weighted=False のとき現行（v2.25.0 以前）と完全一致であることが後方互換の核心。
    """

    def _params(self) -> dict:
        return {
            "haiku": (10.0, 5.0, 30),
            "sonnet": (5.0, 5.0, 20),
            "opus": (2.0, 8.0, 20),
        }

    def test_cost_weighted_false_default_matches_legacy(self) -> None:
        """② cost_weighted=False（デフォルト）で既存テスト（cost_tiebreak=False）と完全一致。

        cost_weighted 引数追加前の呼び出し結果と同一であることを確認（後方互換）。
        """
        mod = _load_hook_module()
        # cost_weighted 引数なし（既存スタイル）
        text_legacy = mod.build_additional_context(
            "medium", "sonnet", "thompson", self._params(),
        )
        # cost_weighted=False を明示
        text_false = mod.build_additional_context(
            "medium", "sonnet", "thompson", self._params(),
            cost_weighted=False,
        )
        assert text_legacy == text_false
        assert "cost-weighted" not in text_false
        assert "cost-aware" not in text_false

    def test_cost_weighted_false_with_cost_tiebreak_true_shows_legacy_suffix(self) -> None:
        """② cost_weighted=False かつ cost_tiebreak=True なら従来の cost-aware 文言（v2.25.0 一致）。"""
        mod = _load_hook_module()
        text = mod.build_additional_context(
            "medium", "sonnet", "thompson", self._params(),
            cost_tiebreak=True,
            cost_weighted=False,
        )
        assert "cost-aware" in text
        assert "成功率拮抗のため低コスト Tier を選択" in text
        assert "cost-weighted" not in text

    def test_cost_weighted_true_shows_new_suffix(self) -> None:
        """② cost_weighted=True で cost-weighted 文言が追加される。"""
        mod = _load_hook_module()
        text = mod.build_additional_context(
            "medium", "sonnet", "thompson", self._params(),
            cost_weighted=True,
        )
        assert "cost-weighted" in text
        assert "成功率とコストを加重して選択" in text

    def test_cost_weighted_true_overrides_cost_tiebreak(self) -> None:
        """② cost_weighted=True の場合 cost_tiebreak=True でも cost-weighted 文言が優先される。

        cost_weighted=True の排他条件: cost-aware 文言は出ない。
        """
        mod = _load_hook_module()
        text = mod.build_additional_context(
            "medium", "sonnet", "thompson", self._params(),
            cost_tiebreak=True,
            cost_weighted=True,
        )
        # cost-weighted が出る
        assert "cost-weighted" in text
        assert "成功率とコストを加重して選択" in text
        # cost-aware は出ない（cost_weighted が優先）
        assert "cost-aware" not in text

    def test_cost_weighted_true_combined_with_escalation(self) -> None:
        """② cost_weighted=True と escalation_reason が両方あると両方の suffix が出る。"""
        mod = _load_hook_module()
        text = mod.build_additional_context(
            "medium", "sonnet", "thompson", self._params(),
            escalation_reason="haiku_failure_rate=0.60 (10 試行) → sonnet に昇格",
            cost_weighted=True,
        )
        assert "Phase 2-B 昇格" in text
        assert "cost-weighted" in text


class TestWriteTierSelectionCostWeighted:
    """③ write_tier_selection の cost_weighted / cost_lambda kw テスト（T5 AC）。

    既存テスト（cost_tiebreak / session_id）が無修正で green であることが後方互換の証明。
    """

    def test_cost_weighted_false_no_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """③ cost_weighted=False（デフォルト）かつ cost_lambda=None で新キーが出ない（後方互換）。"""
        mod = _load_hook_module()
        target = tmp_path / "tier_selection.json"
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(target))

        mod.write_tier_selection("complex", "opus", "thompson")
        data = json.loads(target.read_text(encoding="utf-8"))
        assert "cost_weighted" not in data
        assert "cost_lambda" not in data
        # 既存テストと同一の dict と完全一致
        assert data == {
            "complexity": "complex",
            "tier": "opus",
            "mode": "thompson",
            "suggested_model": "opus",
        }

    def test_cost_weighted_true_adds_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """③ cost_weighted=True で cost_weighted: true が出る。"""
        mod = _load_hook_module()
        target = tmp_path / "tier_selection.json"
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(target))

        mod.write_tier_selection("medium", "haiku", "thompson", cost_weighted=True)
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["cost_weighted"] is True
        assert "cost_lambda" not in data

    def test_cost_lambda_non_none_adds_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """③ cost_lambda=0.5 で cost_lambda: 0.5 が出る。"""
        mod = _load_hook_module()
        target = tmp_path / "tier_selection.json"
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(target))

        mod.write_tier_selection("medium", "haiku", "thompson", cost_lambda=0.5)
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["cost_lambda"] == pytest.approx(0.5)
        assert "cost_weighted" not in data

    def test_cost_weighted_true_and_cost_lambda_both_appear(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """③ cost_weighted=True + cost_lambda=0.5 で両キーが出る。"""
        mod = _load_hook_module()
        target = tmp_path / "tier_selection.json"
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(target))

        mod.write_tier_selection(
            "medium", "haiku", "thompson",
            cost_weighted=True, cost_lambda=0.5,
        )
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["cost_weighted"] is True
        assert data["cost_lambda"] == pytest.approx(0.5)

    def test_cost_lambda_none_no_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """③ cost_lambda=None（デフォルト）では cost_lambda キーが出ない。"""
        mod = _load_hook_module()
        target = tmp_path / "tier_selection.json"
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(target))

        mod.write_tier_selection("medium", "haiku", "thompson", cost_lambda=None)
        data = json.loads(target.read_text(encoding="utf-8"))
        assert "cost_lambda" not in data

    def test_existing_cost_tiebreak_behavior_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """③ 既存の cost_tiebreak 挙動が cost_weighted/cost_lambda 追加後も不変。"""
        mod = _load_hook_module()
        target = tmp_path / "tier_selection.json"
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(target))

        mod.write_tier_selection("medium", "sonnet", "thompson", cost_tiebreak=True)
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["cost_tiebreak"] is True
        assert "cost_weighted" not in data
        assert "cost_lambda" not in data


class TestMainBackwardCompatE2E:
    """① E2E 後方互換（最重要）: env 3 種未設定で v2.25.0 出力と一致。

    env 3 種（C3_TIER_COST_LAMBDA / C3_TIER_EPSILON / C3_ESCALATION_THRESHOLD）が
    すべて未設定のとき、tier_selection.json に新キー（cost_weighted / cost_lambda）が
    出ないことを確認する。
    """

    def test_env_unset_no_new_keys_in_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """① env 3 種未設定で tier_selection.json に cost_weighted / cost_lambda が出ない。"""
        mod = _load_hook_module()
        target = tmp_path / "tier_selection.json"
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(target))

        # env 3 種をすべて削除
        monkeypatch.delenv("C3_TIER_COST_LAMBDA", raising=False)
        monkeypatch.delenv("C3_TIER_EPSILON", raising=False)
        monkeypatch.delenv("C3_ESCALATION_THRESHOLD", raising=False)

        # haiku 単独優位（cost_tiebreak も出ない状況）
        dominant_params = {
            "haiku": (50.0, 1.0, 51),
            "sonnet": (5.0, 5.0, 20),
            "opus": (2.0, 8.0, 20),
        }
        mock_c3_db = types.SimpleNamespace(
            read_agent_tier_params=lambda role, complexity, **kw: dominant_params,
            read_tier_cost_rate_for_complexity=lambda complexity, **kw: {},
            read_agent_failure_rate=lambda role, complexity, tier, **kw: (None, 0),
        )
        monkeypatch.setattr(mod, "_load_c3_db_module", lambda: mock_c3_db)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "新しい機能を追加してください"})))
        random.seed(0)

        rc = mod.main()
        assert rc == 0

        data = json.loads(target.read_text(encoding="utf-8"))
        # 新キーは出ない（v2.25.0 一致）
        assert "cost_weighted" not in data
        assert "cost_lambda" not in data

    def test_env_unset_context_text_has_no_cost_weighted_suffix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """① env 3 種未設定で additionalContext に cost-weighted 文言が出ない。"""
        mod = _load_hook_module()
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(tmp_path / "tier_selection.json"))

        monkeypatch.delenv("C3_TIER_COST_LAMBDA", raising=False)
        monkeypatch.delenv("C3_TIER_EPSILON", raising=False)
        monkeypatch.delenv("C3_ESCALATION_THRESHOLD", raising=False)

        dominant_params = {
            "haiku": (50.0, 1.0, 51),
            "sonnet": (5.0, 5.0, 20),
            "opus": (2.0, 8.0, 20),
        }
        mock_c3_db = types.SimpleNamespace(
            read_agent_tier_params=lambda role, complexity, **kw: dominant_params,
            read_tier_cost_rate_for_complexity=lambda complexity, **kw: {},
            read_agent_failure_rate=lambda role, complexity, tier, **kw: (None, 0),
        )
        monkeypatch.setattr(mod, "_load_c3_db_module", lambda: mock_c3_db)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "新しい機能を追加してください"})))
        random.seed(0)

        rc = mod.main()
        assert rc == 0

        out_data = json.loads(capsys.readouterr().out)
        context = out_data["hookSpecificOutput"]["additionalContext"]
        assert "cost-weighted" not in context


class TestMainLambdaE2E:
    """④ λ 設定 E2E: C3_TIER_COST_LAMBDA=0.5 で cost_weighted: true が json に出る。"""

    def test_lambda_env_triggers_cost_weighted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """④ C3_TIER_COST_LAMBDA=0.5 + 拮抗 params で json に cost_weighted: true が出る。

        trials >= 30 の Thompson 分岐に入るため seed ループで発動ケースを探す。
        lam=0.5 のとき cost_map が存在すれば cost_weighted=True は確定するため、
        uniform 分岐に入らなければ必ず True になる。
        """
        mod = _load_hook_module()
        target = tmp_path / "tier_selection.json"
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(target))
        monkeypatch.setenv("C3_TIER_COST_LAMBDA", "0.5")
        monkeypatch.delenv("C3_TIER_EPSILON", raising=False)
        monkeypatch.delenv("C3_ESCALATION_THRESHOLD", raising=False)

        # trials >= 30 で Thompson 分岐に入る params（cost_map も有効）
        tiebreak_params = {
            "haiku": (10.0, 10.0, 30),
            "sonnet": (10.0, 10.0, 30),
            "opus": (5.0, 5.0, 30),
        }
        mock_c3_db = types.SimpleNamespace(
            read_agent_tier_params=lambda role, complexity, **kw: tiebreak_params,
            read_tier_cost_rate_for_complexity=lambda complexity, **kw: {},  # 静的 fallback 使用
            read_agent_failure_rate=lambda role, complexity, tier, **kw: (None, 0),
        )
        monkeypatch.setattr(mod, "_load_c3_db_module", lambda: mock_c3_db)

        found = False
        for seed in range(100):
            if target.exists():
                target.unlink()
            monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "新しい機能を追加してください"})))
            random.seed(seed)
            rc = mod.main()
            assert rc == 0

            data = json.loads(target.read_text(encoding="utf-8"))
            if data.get("cost_weighted") is True:
                # cost_lambda も出ているはず
                assert data.get("cost_lambda") == pytest.approx(0.5)
                found = True
                break

        assert found, "seed 0-99 で cost_weighted: true が出るケースが見つからない"

    def test_lambda_env_context_shows_cost_weighted_suffix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """④ C3_TIER_COST_LAMBDA=0.5 で additionalContext に cost-weighted 文言が出る。

        seed ループで Thompson 分岐（trials >= 30）に入り cost_weighted=True になるケースを確認。
        """
        mod = _load_hook_module()
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(tmp_path / "tier_selection.json"))
        monkeypatch.setenv("C3_TIER_COST_LAMBDA", "0.5")
        monkeypatch.delenv("C3_TIER_EPSILON", raising=False)
        monkeypatch.delenv("C3_ESCALATION_THRESHOLD", raising=False)

        tiebreak_params = {
            "haiku": (10.0, 10.0, 30),
            "sonnet": (10.0, 10.0, 30),
            "opus": (5.0, 5.0, 30),
        }
        mock_c3_db = types.SimpleNamespace(
            read_agent_tier_params=lambda role, complexity, **kw: tiebreak_params,
            read_tier_cost_rate_for_complexity=lambda complexity, **kw: {},
            read_agent_failure_rate=lambda role, complexity, tier, **kw: (None, 0),
        )
        monkeypatch.setattr(mod, "_load_c3_db_module", lambda: mock_c3_db)

        found = False
        for seed in range(100):
            if (tmp_path / "tier_selection.json").exists():
                (tmp_path / "tier_selection.json").unlink()
            monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "新しい機能を追加してください"})))
            random.seed(seed)
            # capsys は累積するためここでは json の確認のみ
            rc = mod.main()
            assert rc == 0

            data = json.loads((tmp_path / "tier_selection.json").read_text(encoding="utf-8"))
            if data.get("cost_weighted") is True:
                found = True
                break

        assert found, "seed 0-99 で cost_weighted=True になるケースが見つからない"
        # cost_weighted=True になった最後のケースで stdout を確認する
        # capsys.readouterr() は最後の main() 出力を返す
        all_out = capsys.readouterr().out
        # 最後の JSON 行を取得（複数 main 呼び出しで stdout が複数行になる場合）
        last_line = [line for line in all_out.strip().splitlines() if line.strip()][-1]
        out_data = json.loads(last_line)
        context = out_data["hookSpecificOutput"]["additionalContext"]
        assert "cost-weighted" in context


class TestMainDegradeE2E:
    """⑤ degrade: c3_db import 失敗でも crash せず従来 Thompson 動作（後方互換）。

    既存 TestMainCostMapIntegration.test_main_no_crash_when_c3_db_import_fails
    が既にカバーしているが、T5 追加後も確認する。
    """

    def test_c3_db_import_fail_no_cost_weighted_in_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """⑤ c3_db import 失敗時、cost_map=None → cost_weighted / cost_lambda はキーとして出ない。"""
        mod = _load_hook_module()
        target = tmp_path / "tier_selection.json"
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(target))
        monkeypatch.setattr(mod, "_load_c3_db_module", lambda: None)

        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "新しい機能を追加してください"})))
        random.seed(0)

        rc = mod.main()
        assert rc == 0

        data = json.loads(target.read_text(encoding="utf-8"))
        # c3_db None → params が初期値 → trials=0 < 30 → uniform
        assert data["mode"] == "uniform"
        # cost_map=None → cost_weighted=False → キー出ない
        assert "cost_weighted" not in data
        # lam は _resolve_cost_lambda() で解決されるが cost_map=None で weighting 不発動
        # cost_lambda は lam が None（env 未設定）なら出ない
        assert "cost_lambda" not in data


# ---------------------------------------------------------------------------
# R2-T1 (v2.26.0 Round 2): CR-T-001 — lam=0.0 証跡テスト
# ---------------------------------------------------------------------------


class TestWriteTierSelectionLamZero:
    """CR-T-001: write_tier_selection(cost_lambda=0.0) の証跡テスト。

    lam=0.0 は「cost 無視の明示オプト」（経路 0）であり cost_lambda: 0.0 が json に出て
    cost_weighted キーが出ないことを確認する。
    cost_lambda is not None 判定（None のみスキップ）なので 0.0 でもキーが出る。
    """

    def test_cost_lambda_zero_appears_in_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cost_lambda=0.0 で json に cost_lambda: 0.0 が出る。"""
        mod = _load_hook_module()
        target = tmp_path / "tier_selection.json"
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(target))

        mod.write_tier_selection("medium", "haiku", "thompson", cost_lambda=0.0)
        data = json.loads(target.read_text(encoding="utf-8"))
        assert "cost_lambda" in data
        assert data["cost_lambda"] == pytest.approx(0.0)

    def test_cost_weighted_absent_when_lam_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cost_lambda=0.0（経路 0・cost 無視）のとき cost_weighted キーが出ない。"""
        mod = _load_hook_module()
        target = tmp_path / "tier_selection.json"
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(target))

        mod.write_tier_selection("medium", "haiku", "thompson", cost_lambda=0.0)
        data = json.loads(target.read_text(encoding="utf-8"))
        assert "cost_weighted" not in data


class TestMainLamZeroE2E:
    """CR-T-001: C3_TIER_COST_LAMBDA=0 の main E2E テスト。

    lam=0.0 → 経路 0（cost 無視）のため cost_weighted は出ず、cost_lambda: 0.0 が出る。
    """

    def test_lambda_zero_env_cost_lambda_in_json_no_cost_weighted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """C3_TIER_COST_LAMBDA=0 で json に cost_lambda: 0.0 が出て cost_weighted が出ない。"""
        mod = _load_hook_module()
        target = tmp_path / "tier_selection.json"
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(target))
        monkeypatch.setenv("C3_TIER_COST_LAMBDA", "0")
        monkeypatch.delenv("C3_TIER_EPSILON", raising=False)
        monkeypatch.delenv("C3_ESCALATION_THRESHOLD", raising=False)

        # trials >= 30 で Thompson 分岐に入る params（lam=0.0 → 経路 0 → cost_weighted=False）
        dominant_params = {
            "haiku": (50.0, 1.0, 51),
            "sonnet": (5.0, 5.0, 20),
            "opus": (2.0, 8.0, 20),
        }
        mock_c3_db = types.SimpleNamespace(
            read_agent_tier_params=lambda role, complexity, **kw: dominant_params,
            read_tier_cost_rate_for_complexity=lambda complexity, **kw: {},
            read_agent_failure_rate=lambda role, complexity, tier, **kw: (None, 0),
        )
        monkeypatch.setattr(mod, "_load_c3_db_module", lambda: mock_c3_db)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "新しい機能を追加してください"})))
        random.seed(0)

        rc = mod.main()
        assert rc == 0

        data = json.loads(target.read_text(encoding="utf-8"))
        # lam=0.0 → cost_lambda: 0.0 が出る（cost_lambda is not None → True）
        assert "cost_lambda" in data
        assert data["cost_lambda"] == pytest.approx(0.0)
        # lam=0.0 → 経路 0（cost 無視）→ cost_weighted=False → キーが出ない
        assert "cost_weighted" not in data
