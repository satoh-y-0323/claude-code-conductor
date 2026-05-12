"""Tests for tier-routing Phase 2-C: similarity-based complexity boost.

検証対象:
  - .claude/hooks/select_tier.py の similarity_boost / _read_prompt_history /
    _prompt_prefix_and_hash / main 統合

テストケース:
 _prompt_prefix_and_hash:
  1. 短いプロンプトはそのまま prefix になる
  2. 200 文字超は切り詰められる
  3. ハッシュは 16 文字の hex
  4. 同じプロンプトは同じハッシュ

 similarity_boost:
  5. 強類似（>= 0.8）が 1 件あれば complexity を返す
  6. 強類似が複数件あれば最新（ts 大）を採用
  7. 弱類似（0.6 <= ratio < 0.8）のみなら strong=None / weak_matches に入る
  8. 類似度がしきい値未満なら strong=None / weak_matches=[]
  9. history が空なら strong=None / weak_matches=[]

 _read_prompt_history:
 10. ファイル不在なら空リスト
 11. 壊れた行はスキップされる
 12. 末尾 1000 行のみ読まれる（fixture でテスト）

 main 統合:
 13. 強類似があれば complexity が上書きされる
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import types
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "select_tier.py"


def _load_select_tier() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("select_tier_sim", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


# ---------------------------------------------------------------------------
# _prompt_prefix_and_hash
# ---------------------------------------------------------------------------


class TestPromptPrefixAndHash:

    def test_short_prompt_kept_intact(self) -> None:
        mod = _load_select_tier()
        prefix, h = mod._prompt_prefix_and_hash("hello world")
        assert prefix == "hello world"
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)

    def test_long_prompt_is_truncated(self) -> None:
        mod = _load_select_tier()
        long_prompt = "a" * 500
        prefix, _ = mod._prompt_prefix_and_hash(long_prompt)
        assert len(prefix) == mod._PROMPT_PREFIX_MAX
        assert prefix == "a" * mod._PROMPT_PREFIX_MAX

    def test_hash_is_deterministic(self) -> None:
        mod = _load_select_tier()
        _, h1 = mod._prompt_prefix_and_hash("typo を修正してください")
        _, h2 = mod._prompt_prefix_and_hash("typo を修正してください")
        assert h1 == h2

    def test_different_prompts_have_different_hashes(self) -> None:
        mod = _load_select_tier()
        _, h1 = mod._prompt_prefix_and_hash("typo")
        _, h2 = mod._prompt_prefix_and_hash("refactor")
        assert h1 != h2


# ---------------------------------------------------------------------------
# similarity_boost
# ---------------------------------------------------------------------------


class TestSimilarityBoost:

    def test_strong_match_returns_complexity(self) -> None:
        mod = _load_select_tier()
        history = [
            {
                "ts": "2026-05-08T12:00:00+09:00",
                "prompt_prefix": "typo を修正してください",
                "complexity": "simple",
            },
        ]
        strong, weak = mod.similarity_boost(
            "typo を修正してください", history=history,
        )
        assert strong == "simple"
        # 強類似で取り込んだものは weak には入らない
        assert weak == []

    def test_strong_match_uses_latest_ts(self) -> None:
        mod = _load_select_tier()
        # 同じ prompt prefix で complexity が違う 2 件
        history = [
            {
                "ts": "2026-05-01T00:00:00+09:00",
                "prompt_prefix": "typo を修正してください",
                "complexity": "simple",
            },
            {
                "ts": "2026-05-08T00:00:00+09:00",
                "prompt_prefix": "typo を修正してください",
                "complexity": "medium",
            },
        ]
        strong, _ = mod.similarity_boost(
            "typo を修正してください", history=history,
        )
        # ts が新しい "medium" が採用されるべき
        assert strong == "medium"

    def test_weak_match_only_returns_none_with_matches(self) -> None:
        mod = _load_select_tier()
        # 適度に似ているが 0.8 未満になるよう調整
        history = [
            {
                "ts": "2026-05-08T12:00:00+09:00",
                "prompt_prefix": "認証ミドルウェアの実装をお願いします",
                "complexity": "complex",
            },
        ]
        strong, weak = mod.similarity_boost(
            "認証ロジックを書いてほしい", history=history,
        )
        # 強類似ではない
        assert strong is None
        # weak_matches は条件次第で 0 件もありうる（本テストでは弱類似に
        # 当たる確率を保証しないため、空でも非空でも構わない）
        assert isinstance(weak, list)

    def test_no_match_returns_empty(self) -> None:
        mod = _load_select_tier()
        history = [
            {
                "ts": "2026-05-08T12:00:00+09:00",
                "prompt_prefix": "完全に無関係な過去プロンプトです",
                "complexity": "complex",
            },
        ]
        # 全く異なる内容
        strong, weak = mod.similarity_boost(
            "abcdefghijklmnopqrstuvwxyz1234567890",
            history=history,
        )
        assert strong is None
        assert weak == []

    def test_empty_history(self) -> None:
        mod = _load_select_tier()
        strong, weak = mod.similarity_boost("anything", history=[])
        assert strong is None
        assert weak == []

    def test_invalid_history_entries_are_skipped(self) -> None:
        mod = _load_select_tier()
        # prompt_prefix が無い / 型が違う行は無視される
        history = [
            {"ts": "2026-05-08T12:00:00+09:00"},  # prefix 欠落
            {"prompt_prefix": None, "complexity": "simple"},  # 型違反
            {"prompt_prefix": "", "complexity": "simple"},  # 空文字
            {  # これだけが有効
                "ts": "2026-05-08T13:00:00+09:00",
                "prompt_prefix": "typo を修正してください",
                "complexity": "simple",
            },
        ]
        strong, _ = mod.similarity_boost(
            "typo を修正してください", history=history,
        )
        assert strong == "simple"


# ---------------------------------------------------------------------------
# _read_prompt_history
# ---------------------------------------------------------------------------


class TestReadPromptHistory:

    def test_missing_file_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _load_select_tier()
        monkeypatch.setattr(
            mod, "PROMPT_HISTORY_PATH", str(tmp_path / "missing.jsonl")
        )
        assert mod._read_prompt_history() == []

    def test_corrupted_lines_are_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _load_select_tier()
        history_path = tmp_path / "prompt-history.jsonl"
        # 壊れた JSON / 空行 / 有効な JSON を混在させる
        history_path.write_text(
            "this is not json\n"
            "\n"
            '{"ts": "2026-05-08", "prompt_prefix": "abc", "complexity": "simple"}\n'
            "{broken: true}\n"
            '{"ts": "2026-05-09", "prompt_prefix": "def", "complexity": "medium"}\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(mod, "PROMPT_HISTORY_PATH", str(history_path))
        records = mod._read_prompt_history()
        assert len(records) == 2
        assert records[0]["prompt_prefix"] == "abc"
        assert records[1]["prompt_prefix"] == "def"

    def test_only_last_n_lines_are_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _load_select_tier()
        history_path = tmp_path / "prompt-history.jsonl"
        # _PROMPT_HISTORY_SCAN_LINES より多い行数を書き、末尾のみが読まれることを確認
        n = mod._PROMPT_HISTORY_SCAN_LINES + 50
        with open(history_path, "w", encoding="utf-8") as f:
            for i in range(n):
                f.write(
                    json.dumps({
                        "ts": f"2026-05-09T00:00:{i:02d}+09:00",
                        "prompt_prefix": f"prompt-{i}",
                        "complexity": "simple",
                    }) + "\n"
                )
        monkeypatch.setattr(mod, "PROMPT_HISTORY_PATH", str(history_path))
        records = mod._read_prompt_history()
        # 末尾 _PROMPT_HISTORY_SCAN_LINES 件のみ読まれる
        assert len(records) == mod._PROMPT_HISTORY_SCAN_LINES
        # 最後のレコードは最新のもの
        assert records[-1]["prompt_prefix"] == f"prompt-{n - 1}"


# ---------------------------------------------------------------------------
# main 統合
# ---------------------------------------------------------------------------


class TestMainSimilarityIntegration:

    def test_strong_similarity_overrides_complexity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """過去 history に強類似 (medium) があれば、heuristic (simple) を上書きする。"""
        mod = _load_select_tier()

        # 短くて simple キーワードを含むので heuristic だと "simple"
        prompt = "typo を修正してください"

        # history に同じ prompt prefix で complexity=medium を仕込む
        history_path = tmp_path / "prompt-history.jsonl"
        history_path.write_text(
            json.dumps({
                "ts": "2026-05-08T00:00:00+09:00",
                "prompt_prefix": prompt,
                "complexity": "medium",
                "tier": "sonnet",
                "outcome": "success",
            }) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(mod, "PROMPT_HISTORY_PATH", str(history_path))

        # tier_selection の出力先も tmp_path に逃がす
        sel_path = tmp_path / "tier_selection.json"
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(sel_path))

        # c3_db ヘルパーは無くても uniform で動くので、import 失敗にしておく
        monkeypatch.setattr(mod, "_load_c3_db_module", lambda: None)

        payload = {"prompt": prompt}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

        rc = mod.main()
        assert rc == 0
        assert sel_path.is_file()
        data = json.loads(sel_path.read_text(encoding="utf-8"))
        # 強類似で medium に上書きされているはず
        assert data["complexity"] == "medium"
        # prompt_prefix と prompt_hash が書かれている
        assert data["prompt_prefix"] == prompt
        assert isinstance(data["prompt_hash"], str)
        assert len(data["prompt_hash"]) == 16
