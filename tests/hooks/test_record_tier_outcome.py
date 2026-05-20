"""Tests for .claude/skills/dev-workflow/scripts/record_tier_outcome.py

tier-routing MVP: Tier outcome 記録 CLI の検証。

テストケース:
  1. tier_selection.json があり、--outcome success で α+=1、json は削除される
  2. --outcome failure で β+=1、json は削除される
  3. tier_selection.json が無い場合は何もせず exit 0
  4. tier_selection.json が壊れた JSON の場合は何もせず exit 0
  5. DB 不在時は exit 0、json は削除されない（リトライ可能）
  6. --outcome の値が不正なら argparse がエラー（exit 2）

b2 追加テストケース (TestClaudeDirAssertion):
  7. 正常配置（.claude/skills/dev-workflow/scripts/）でロードしても AssertionError が出ない
  8. 3 階層遡れない場所に置いた場合に AssertionError が発生する

b3 追加テストケース (TestPromptHistoryAppend):
  9. prompt_prefix に U+2028 を含む selection を書いたとき、jsonl 行に生の U+2028 が含まれない
 10. prompt_prefix に U+2029 を含む selection を書いたとき、jsonl 行に生の U+2029 が含まれない
 11. json.loads 後に prompt_prefix が元の文字列（U+2028/U+2029 入り）に戻ること
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import types
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".claude" / "skills" / "dev-workflow" / "scripts" / "record_tier_outcome.py"
SCHEMA_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "schema.sql"
INIT_HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "session_start.py"

# U+2028 LINE SEPARATOR / U+2029 PARAGRAPH SEPARATOR（実体文字を埋め込まず chr() で参照）
_LS = chr(0x2028)  # LINE SEPARATOR
_PS = chr(0x2029)  # PARAGRAPH SEPARATOR


def _create_c3_db(db_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("init_c3_db_rt", INIT_HOOK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    mod.apply_schema(db_path=str(db_path), schema_path=str(SCHEMA_PATH))


def _load_hook_module(name: str = "record_tier_outcome_t") -> types.ModuleType:
    """HOOK_PATH からモジュールをロードする。

    name は spec_from_file_location の第 1 引数（sys.modules キャッシュキー）。
    テスト間の汚染を避けるため、テストごとに異なる名前を渡すことができる。
    """
    spec = importlib.util.spec_from_file_location(name, HOOK_PATH)
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

    # ------------------------------------------------------------------
    # b3: U+2028 / U+2029 エスケープ検証（新規 Red テスト）
    # ------------------------------------------------------------------

    def test_line_separator_u2028_not_in_jsonl(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """prompt_prefix に U+2028 を含む selection を書いたとき、
        jsonl の該当行に生の U+2028 が含まれないこと。

        NOTE: Python の str.splitlines() は U+2028 を行区切りとして扱うため、
        生の U+2028 が書き込まれると splitlines() で分割された行には U+2028 が
        残らない。本テストでは split('\\n') を使って JSONL の 1 行を取り出し、
        その行に U+2028 が含まれるかを検証する。

        [b3 Green 回帰防止] _append_prompt_history の U+2028 エスケープ処理は実装済み。本テストは PASS を維持する回帰防止テスト。
        """
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)
        sel_path = tmp_path / "state" / "tier_selection.json"
        history_path = tmp_path / "logs" / "prompt-history.jsonl"

        # U+2028 LINE SEPARATOR を含む prompt_prefix（chr() で参照 — 実体文字を埋め込まない）
        prefix_with_ls = "テスト" + _LS + "区切り"
        self._write_selection_with_prompt(
            sel_path,
            complexity="simple", tier="haiku",
            prompt_prefix=prefix_with_ls,
            prompt_hash="aabbccddeeff0011",
        )

        hook = _load_hook_module("record_tier_outcome_b3")
        monkeypatch.setattr(hook, "TIER_SELECTION_PATH", str(sel_path))
        monkeypatch.setattr(hook, "PROMPT_HISTORY_PATH", str(history_path))

        from c3 import db as c3_db
        monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: db_path)

        rc = hook.main(["--outcome", "success"])
        assert rc == 0
        assert history_path.is_file()

        raw_content = history_path.read_text(encoding="utf-8")
        # split('\n') を使って JSONL の 1 行目を取り出す
        # （splitlines() は U+2028 でも行分割するため不可）
        raw_line = raw_content.split("\n")[0]
        assert _LS not in raw_line, (
            "U+2028 (LINE SEPARATOR) が jsonl 行に生のまま含まれている。"
            "_append_prompt_history で json.dumps 後に str.replace でエスケープする必要がある。"
        )

    def test_paragraph_separator_u2029_not_in_jsonl(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """prompt_prefix に U+2029 を含む selection を書いたとき、
        jsonl の該当行に生の U+2029 が含まれないこと。

        NOTE: Python の str.splitlines() は U+2029 を行区切りとして扱うため、
        split('\\n') で検証する。

        [b3 Green 回帰防止] _append_prompt_history の U+2029 エスケープ処理は実装済み。本テストは PASS を維持する回帰防止テスト。
        """
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)
        sel_path = tmp_path / "state" / "tier_selection.json"
        history_path = tmp_path / "logs" / "prompt-history.jsonl"

        # U+2029 PARAGRAPH SEPARATOR を含む prompt_prefix（chr() で参照）
        prefix_with_ps = "段落" + _PS + "セパレータ"
        self._write_selection_with_prompt(
            sel_path,
            complexity="simple", tier="haiku",
            prompt_prefix=prefix_with_ps,
            prompt_hash="1122334455667788",
        )

        hook = _load_hook_module("record_tier_outcome_b3b")
        monkeypatch.setattr(hook, "TIER_SELECTION_PATH", str(sel_path))
        monkeypatch.setattr(hook, "PROMPT_HISTORY_PATH", str(history_path))

        from c3 import db as c3_db
        monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: db_path)

        rc = hook.main(["--outcome", "success"])
        assert rc == 0
        assert history_path.is_file()

        raw_content = history_path.read_text(encoding="utf-8")
        # split('\n') を使って JSONL の 1 行目を取り出す
        raw_line = raw_content.split("\n")[0]
        assert _PS not in raw_line, (
            "U+2029 (PARAGRAPH SEPARATOR) が jsonl 行に生のまま含まれている。"
            "_append_prompt_history で json.dumps 後に str.replace でエスケープする必要がある。"
        )

    def test_line_separator_roundtrip_via_json_loads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """エスケープ後に json.loads すると prompt_prefix が元の文字列（U+2028/U+2029 入り）に戻ること。

        エスケープ処理（\\u2028 / \\u2029 置換）後、jsonl 行を json.loads すると
        JSON の \\uXXXX エスケープが Python の unicode 文字に復元される。

        [b3 Green 回帰防止] _append_prompt_history の U+2028/U+2029 エスケープ処理は実装済み。本テストは PASS を維持する回帰防止テスト。
        """
        db_path = tmp_path / "c3.db"
        _create_c3_db(db_path)
        sel_path = tmp_path / "state" / "tier_selection.json"
        history_path = tmp_path / "logs" / "prompt-history.jsonl"

        # U+2028 / U+2029 両方を含む prompt_prefix（chr() で参照）
        original_prefix = "前" + _LS + "後" + _PS + "末"
        self._write_selection_with_prompt(
            sel_path,
            complexity="simple", tier="haiku",
            prompt_prefix=original_prefix,
            prompt_hash="ffeeddccbbaa9988",
        )

        hook = _load_hook_module("record_tier_outcome_b3c")
        monkeypatch.setattr(hook, "TIER_SELECTION_PATH", str(sel_path))
        monkeypatch.setattr(hook, "PROMPT_HISTORY_PATH", str(history_path))

        from c3 import db as c3_db
        monkeypatch.setattr(c3_db, "locate_c3_db", lambda start=None: db_path)

        rc = hook.main(["--outcome", "success"])
        assert rc == 0

        raw_content = history_path.read_text(encoding="utf-8")
        raw_line = raw_content.split("\n")[0]
        # 生の U+2028 / U+2029 が含まれていない前提で json.loads する
        assert _LS not in raw_line, "U+2028 が jsonl 行に生のまま含まれている"
        assert _PS not in raw_line, "U+2029 が jsonl 行に生のまま含まれている"
        record = json.loads(raw_line)
        # json.loads で \\u2028 / \\u2029 → 元の U+2028 / U+2029 に復元される
        assert record["prompt_prefix"] == original_prefix


# ---------------------------------------------------------------------------
# b2: _CLAUDE_DIR 実行時アサーション検証（新規 Red テスト）
# ---------------------------------------------------------------------------


class TestClaudeDirAssertion:
    """b2 タスク: record_tier_outcome.py の _CLAUDE_DIR アサーション検証。

    b2 実装済み: _CLAUDE_DIR 直後の assert により、誤配置時に AssertionError が発生する。
    本クラスはその振る舞いを保証する回帰防止テスト。

    正常配置テストは HOOK_PATH で _load_from_path を呼んで
    AssertionError が出ないことを確認する。
    """

    def _load_from_path(self, hook_path: Path) -> types.ModuleType:
        spec = importlib.util.spec_from_file_location("record_tier_outcome_assert_t", hook_path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[attr-defined]
        return mod

    def test_normal_placement_no_assertion_error(self) -> None:
        """正常配置（.claude/skills/dev-workflow/scripts/）でロードしても AssertionError が出ないこと。

        [b2 Green 前提] HOOK_PATH が正しい 3 階層構造に置かれているため、
        _CLAUDE_DIR が '.claude' で終わるアサーションは通過する。
        このテストは b2 実装後も PASS を維持すること。
        """
        # AssertionError が出なければ PASS
        try:
            self._load_from_path(HOOK_PATH)
        except AssertionError as exc:
            pytest.fail(
                f"正常配置でのモジュールロードが AssertionError で失敗した: {exc}"
            )

    def test_wrong_placement_raises_assertion_error(self, tmp_path: Path) -> None:
        """スクリプトを 3 階層遡れない場所（tmp_path 直下）に置いた場合に
        AssertionError が発生すること。

        [b2 Green 回帰防止] _CLAUDE_DIR 直後の assert は実装済み。本テストは誤配置を検出することを確認する回帰防止テスト。
        """
        # tmp_path/scripts/record_tier_outcome.py に配置
        # _SCRIPT_DIR = tmp_path/scripts/
        # _CLAUDE_DIR = tmp_path.parent.parent（'.claude' で終わらない）
        wrong_scripts = tmp_path / "scripts"
        wrong_scripts.mkdir()
        wrong_hook = wrong_scripts / "record_tier_outcome.py"
        shutil.copy(HOOK_PATH, wrong_hook)

        with pytest.raises(AssertionError, match=r"_CLAUDE_DIR resolution broke"):
            self._load_from_path(wrong_hook)
