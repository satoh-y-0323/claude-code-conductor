"""Tests for .claude/hooks/record_tier_outcome.py

F-005 MVP: Tier outcome 記録 CLI の検証。

テストケース:
  1. tier_selection.json があり、--outcome success で α+=1、json は削除される
  2. --outcome failure で β+=1、json は削除される
  3. tier_selection.json が無い場合は何もせず exit 0
  4. tier_selection.json が壊れた JSON の場合は何もせず exit 0
  5. DB 不在時は exit 0、json は削除されない（リトライ可能）
  6. --outcome の値が不正なら argparse がエラー（exit 2）
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "record_tier_outcome.py"
SCHEMA_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "schema.sql"
INIT_HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "session_start.py"


def _create_c3_db(db_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("init_c3_db_rt", INIT_HOOK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    mod.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))


def _load_hook_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("record_tier_outcome_t", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


def _write_selection(path: Path, complexity: str, tier: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"complexity": complexity, "tier": tier, "mode": "thompson"}),
        encoding="utf-8",
    )


class TestRecordTierOutcome:

    def test_success_increments_alpha_and_deletes_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_selection(sel_path, "simple", "haiku")

        mod = _load_hook_module()
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(sel_path))

        # locate_c3_db を tmp_path 配下に向ける
        from c3 import db as c3_db
        monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: db_path)

        rc = mod.main(["--outcome", "success"])
        assert rc == 0

        params = c3_db.read_tier_params("simple", db_path=db_path)
        assert params["haiku"] == (2.0, 1.0, 1)
        assert not sel_path.exists()  # 削除されている

    def test_failure_increments_beta_and_deletes_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_selection(sel_path, "complex", "opus")

        mod = _load_hook_module()
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(sel_path))

        from c3 import db as c3_db
        monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: db_path)

        rc = mod.main(["--outcome", "failure"])
        assert rc == 0

        params = c3_db.read_tier_params("complex", db_path=db_path)
        assert params["opus"] == (1.0, 2.0, 1)
        assert not sel_path.exists()

    def test_no_selection_file_returns_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _load_hook_module()
        monkeypatch.setattr(
            mod, "TIER_SELECTION_PATH",
            str(tmp_path / "nonexistent.json"),
        )
        rc = mod.main(["--outcome", "success"])
        assert rc == 0

    def test_corrupt_selection_file_returns_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _load_hook_module()
        sel_path = tmp_path / "tier_selection.json"
        sel_path.write_text("not valid json", encoding="utf-8")
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(sel_path))

        rc = mod.main(["--outcome", "success"])
        assert rc == 0

    def test_db_unavailable_keeps_selection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DB が無い場合、json は削除されない（次回リトライ可能）。"""
        sel_path = tmp_path / "state" / "tier_selection.json"
        _write_selection(sel_path, "medium", "sonnet")

        mod = _load_hook_module()
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(sel_path))

        from c3 import db as c3_db
        monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: None)

        rc = mod.main(["--outcome", "success"])
        assert rc == 0
        # json は残っている
        assert sel_path.is_file()

    def test_invalid_outcome_arg_exits_2(self) -> None:
        mod = _load_hook_module()
        with pytest.raises(SystemExit) as exc_info:
            mod.main(["--outcome", "wrong"])
        assert exc_info.value.code == 2  # argparse error


class TestPromptHistoryAppend:
    """Phase 2-C: prompt-history.jsonl への追記検証。"""

    def _write_selection_with_prompt(
        self, path: Path, *, complexity: str, tier: str,
        prompt_prefix: str, prompt_hash: str,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "complexity": complexity,
                "tier": tier,
                "mode": "thompson",
                "prompt_prefix": prompt_prefix,
                "prompt_hash": prompt_hash,
            }),
            encoding="utf-8",
        )

    def test_appends_record_on_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)
        sel_path = tmp_path / "state" / "tier_selection.json"
        history_path = tmp_path / "logs" / "prompt-history.jsonl"
        self._write_selection_with_prompt(
            sel_path,
            complexity="simple", tier="haiku",
            prompt_prefix="typo を修正してください",
            prompt_hash="abcdef0123456789",
        )

        mod = _load_hook_module()
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(sel_path))
        monkeypatch.setattr(mod, "PROMPT_HISTORY_PATH", str(history_path))

        from c3 import db as c3_db
        monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: db_path)

        rc = mod.main(["--outcome", "success"])
        assert rc == 0
        assert history_path.is_file()
        lines = history_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["complexity"] == "simple"
        assert record["tier"] == "haiku"
        assert record["outcome"] == "success"
        assert record["prompt_prefix"] == "typo を修正してください"
        assert record["prompt_hash"] == "abcdef0123456789"
        assert "ts" in record

    def test_appends_record_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)
        sel_path = tmp_path / "state" / "tier_selection.json"
        history_path = tmp_path / "logs" / "prompt-history.jsonl"
        self._write_selection_with_prompt(
            sel_path,
            complexity="complex", tier="opus",
            prompt_prefix="リファクタしてください",
            prompt_hash="0000111122223333",
        )

        mod = _load_hook_module()
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(sel_path))
        monkeypatch.setattr(mod, "PROMPT_HISTORY_PATH", str(history_path))

        from c3 import db as c3_db
        monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: db_path)

        rc = mod.main(["--outcome", "failure"])
        assert rc == 0
        record = json.loads(
            history_path.read_text(encoding="utf-8").strip().splitlines()[-1]
        )
        assert record["outcome"] == "failure"
        assert record["complexity"] == "complex"
        assert record["tier"] == "opus"

    def test_skip_when_selection_lacks_prompt_info(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """旧フォーマットの tier_selection.json（prompt_prefix なし）では追記しない。"""
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)
        sel_path = tmp_path / "state" / "tier_selection.json"
        # prompt_prefix / prompt_hash 無しの旧フォーマット
        _write_selection(sel_path, "simple", "haiku")
        history_path = tmp_path / "logs" / "prompt-history.jsonl"

        mod = _load_hook_module()
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(sel_path))
        monkeypatch.setattr(mod, "PROMPT_HISTORY_PATH", str(history_path))

        from c3 import db as c3_db
        monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: db_path)

        rc = mod.main(["--outcome", "success"])
        assert rc == 0
        # 追記されていない
        assert not history_path.exists()

    def test_appends_to_existing_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """既存 prompt-history.jsonl に追記される（上書きしない）。"""
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)
        sel_path = tmp_path / "state" / "tier_selection.json"
        history_path = tmp_path / "logs" / "prompt-history.jsonl"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(
            json.dumps({
                "ts": "2026-05-01T00:00:00+09:00",
                "prompt_prefix": "既存エントリ",
                "prompt_hash": "deadbeefcafebabe",
                "complexity": "medium",
                "tier": "sonnet",
                "outcome": "success",
            }) + "\n",
            encoding="utf-8",
        )
        self._write_selection_with_prompt(
            sel_path,
            complexity="simple", tier="haiku",
            prompt_prefix="新しいエントリ",
            prompt_hash="1234567890abcdef",
        )

        mod = _load_hook_module()
        monkeypatch.setattr(mod, "TIER_SELECTION_PATH", str(sel_path))
        monkeypatch.setattr(mod, "PROMPT_HISTORY_PATH", str(history_path))

        from c3 import db as c3_db
        monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: db_path)

        rc = mod.main(["--outcome", "success"])
        assert rc == 0
        lines = history_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["prompt_prefix"] == "既存エントリ"
        assert json.loads(lines[1])["prompt_prefix"] == "新しいエントリ"
