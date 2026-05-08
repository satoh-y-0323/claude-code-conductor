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
INIT_HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "init_c3_db.py"


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
        from parallel_orchestra import c3_db
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

        from parallel_orchestra import c3_db
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

        from parallel_orchestra import c3_db
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
