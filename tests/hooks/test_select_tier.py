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
# ---------------------------------------------------------------------------


class TestC3DbTierBandit:

    def test_read_returns_defaults_when_no_rows(self, tmp_path: Path) -> None:
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        from c3 import db as c3_db
        params = c3_db.read_tier_params("medium", db_path=db_path)
        assert set(params.keys()) == {"haiku", "sonnet", "opus"}
        for tier, (alpha, beta, trials) in params.items():
            assert alpha == 1.0
            assert beta == 1.0
            assert trials == 0

    def test_update_success_increments_alpha(self, tmp_path: Path) -> None:
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        from c3 import db as c3_db
        ok = c3_db.update_tier_params(
            "simple", "haiku", success=True, db_path=db_path,
        )
        assert ok is True

        params = c3_db.read_tier_params("simple", db_path=db_path)
        assert params["haiku"] == (2.0, 1.0, 1)  # alpha=1+1, beta=1, trials=1

    def test_update_failure_increments_beta(self, tmp_path: Path) -> None:
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        from c3 import db as c3_db
        c3_db.update_tier_params(
            "complex", "opus", success=False, db_path=db_path,
        )

        params = c3_db.read_tier_params("complex", db_path=db_path)
        assert params["opus"] == (1.0, 2.0, 1)

    def test_update_accumulates(self, tmp_path: Path) -> None:
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)

        from c3 import db as c3_db
        for _ in range(3):
            c3_db.update_tier_params(
                "medium", "sonnet", success=True, db_path=db_path,
            )
        for _ in range(2):
            c3_db.update_tier_params(
                "medium", "sonnet", success=False, db_path=db_path,
            )

        params = c3_db.read_tier_params("medium", db_path=db_path)
        assert params["sonnet"] == (4.0, 3.0, 5)  # 1+3, 1+2, 5 trials

    def test_db_not_found_returns_initial(self, tmp_path: Path) -> None:
        db_path = tmp_path / "missing" / "c3.db"
        from c3 import db as c3_db
        params = c3_db.read_tier_params("medium", db_path=db_path)
        # defaults
        assert all(p == (1.0, 1.0, 0) for p in params.values())

        ok = c3_db.update_tier_params(
            "medium", "haiku", success=True, db_path=db_path,
        )
        assert ok is False


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
# T3: session_id 記録（AC-3 / AC-9）
# ---------------------------------------------------------------------------


class TestSessionIdRecording:
    """write_tier_selection / main の session_id 記録を検証する。"""

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
        """全 contender が同コスト（hi==lo → norm=0.0）なら samples 最大を選ぶ。"""
        mod = _load_hook_module()
        # haiku と sonnet が拮抗・同コスト
        samples = {"haiku": 0.82, "sonnet": 0.80, "opus": 0.30}
        cost_map = {"haiku": 10.0, "sonnet": 10.0, "opus": 10.0}
        chosen, did_tiebreak, contenders = mod._cost_tiebreak(samples, cost_map)
        # norm は全員 0.0 → tie-break キー (0.0, -sample) の最小 = sample 最大
        assert chosen == "haiku"
        assert did_tiebreak is True

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
        拮抗する params + 安い tier が選ばれるケースを検証する。
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
            read_tier_params=lambda complexity, **kw: tiebreak_params,
            read_tier_cost_rate_for_complexity=lambda complexity, **kw: {},  # 実測なし → 全て静的 fallback
            read_tier_failure_rate=lambda complexity, tier: (None, 0),  # escalation しない
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
            read_tier_params=lambda complexity, **kw: dominant_params,
            read_tier_cost_rate_for_complexity=lambda complexity, **kw: {},
            read_tier_failure_rate=lambda complexity, tier: (None, 0),  # escalation しない
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
            read_tier_params=lambda complexity, **kw: params,
            read_tier_cost_rate_for_complexity=lambda complexity, **kw: {},  # rate 関数・実測なし
            read_tier_failure_rate=lambda complexity, tier: (None, 0),
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
        単位混在が解消されており crash なく rc=0 で完了することを確認。

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
            read_tier_params=lambda complexity, **kw: params,
            read_tier_cost_rate_for_complexity=lambda complexity, **kw: dict(measured_rates),
            read_tier_failure_rate=lambda complexity, tier: (None, 0),
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
