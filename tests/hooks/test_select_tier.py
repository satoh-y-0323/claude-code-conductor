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
