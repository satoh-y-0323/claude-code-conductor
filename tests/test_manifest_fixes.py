"""Tests for the 4 fixes planned for c3.po.manifest.

Fix 1 (Code Medium-1):
    L339 の `rest is None` をデッドコードとして削除する。
    `if rest == "" or rest is None:` → `if rest == ""`
    動作変更なし → 現行コードでも通るはず。

Fix 2 (Code Medium-6):
    `_scalar` 関数でダブルクォート文字列のエスケープシーケンスを処理する。
    `\\` → `\`、`\"` → `"`、`\n` → 改行 の変換を追加する。
    現行コードではエスケープ展開しないため失敗するはず。

Fix 3 (Code Low-2):
    `validate_manifest` 内の変数名 `version` を `plan_version` に変更する。
    変数名リファクタリングのみ → 動作変更なし → 現行コードでも通るはず。

Fix 4 (Code Low-4):
    `build_wave_manifest_text` に `waves=None` オプション引数を追加する。
    呼び出し元が既に waves を持つ場合の重複計算を防ぐ。
    現行コードにはそのシグネチャが存在しないため失敗するはず。
"""

from __future__ import annotations

import inspect
import textwrap
from pathlib import Path

import pytest

from c3.po.manifest import (
    build_wave_manifest_text,
    validate_manifest,
    extract_frontmatter,
    compute_waves,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _task(id_: str, depends_on: list[str] | None = None, **extras) -> dict:
    task = {
        "id": id_,
        "agent": "dummy-agent",
        "read_only": False,
        "prompt": f"do {id_}",
    }
    if depends_on is not None:
        task["depends_on"] = depends_on
    task.update(extras)
    return task


def _fm(*tasks: dict, **extras) -> dict:
    fm = {
        "po_plan_version": "0.1",
        "name": "test-plan",
        "cwd": "../..",
        "tasks": list(tasks),
    }
    fm.update(extras)
    return fm


def _make_plan_report(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "plan-report.md"
    p.write_text(content, encoding="utf-8")
    return p


def _make_claude_root(root: Path, agents: list[str] | None = None) -> Path:
    agents_dir = root / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    for name in (agents or ["dummy-agent"]):
        (agents_dir / f"{name}.md").write_text(f"# {name}\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Fix 1: `rest is None` はデッドコード — 削除しても動作は変わらない
# ---------------------------------------------------------------------------


class TestFix1DeadCodeRestIsNone:
    """Fix 1: `rest is None` はデッドコードの削除。

    `content.partition(":")` は str.partition の仕様上 rest が None になることはない。
    削除後も動作が同じであることを確認する。
    """

    def test_empty_value_key_parsed_as_block(self, tmp_path: Path):
        """コロンの後に値がない行 (`key:`) はブロックとして次の行を読み込む。"""
        yaml_content = textwrap.dedent("""\
            ---
            key:
              nested: value
            ---
            body
        """)
        p = _make_plan_report(tmp_path, yaml_content)
        fm = extract_frontmatter(p)
        assert fm is not None
        assert fm["key"] == {"nested": "value"}

    def test_key_with_empty_string_value_handled(self, tmp_path: Path):
        """コロン後のスペースのみの行 (`key: `) も `rest == ""` として扱われる。"""
        yaml_content = textwrap.dedent("""\
            ---
            po_plan_version: "0.1"
            name: "test"
            cwd: ".."
            tasks:
              - id: t1
                agent: dummy-agent
                read_only: false
                prompt: "hello"
            ---
            body
        """)
        p = _make_plan_report(tmp_path, yaml_content)
        fm = extract_frontmatter(p)
        assert fm is not None
        assert fm["po_plan_version"] == "0.1"

    def test_nested_mapping_under_defaults(self, tmp_path: Path):
        """defaults: の後に値がない場合、ネストされたマッピングとして解析される。"""
        yaml_content = textwrap.dedent("""\
            ---
            po_plan_version: "0.1"
            name: "test"
            cwd: ".."
            defaults:
              max_retries: 2
            tasks:
              - id: t1
                agent: dummy-agent
                read_only: false
                prompt: "hello"
            ---
            body
        """)
        p = _make_plan_report(tmp_path, yaml_content)
        fm = extract_frontmatter(p)
        assert fm is not None
        assert fm["defaults"]["max_retries"] == 2


# ---------------------------------------------------------------------------
# Fix 2: `_scalar` でダブルクォート文字列のエスケープシーケンスを展開する
# ---------------------------------------------------------------------------


class TestFix2EscapeSequences:
    """Fix 2: `_scalar` でエスケープ展開 (`\\\\`→`\\`, `\\"`→`"`, `\\n`→改行)。

    現行コードは展開しないため、これらのテストは Red (失敗) になるはず。
    """

    def test_backslash_escape_in_double_quoted_scalar(self, tmp_path: Path):
        """ダブルクォート内 `\\\\` は `\\` に展開される。"""
        yaml_content = textwrap.dedent("""\
            ---
            po_plan_version: "0.1"
            name: "path\\\\to\\\\file"
            cwd: ".."
            tasks:
              - id: t1
                agent: dummy-agent
                read_only: false
                prompt: "hello"
            ---
            body
        """)
        p = _make_plan_report(tmp_path, yaml_content)
        fm = extract_frontmatter(p)
        assert fm is not None
        # After fix: "path\\to\\file" — each \\\\ should become a single backslash
        assert fm["name"] == "path\\to\\file"

    def test_double_quote_escape_in_double_quoted_scalar(self, tmp_path: Path):
        """ダブルクォート内 `\\"` は `"` に展開される。"""
        yaml_content = textwrap.dedent('''\
            ---
            po_plan_version: "0.1"
            name: "say \\"hello\\""
            cwd: ".."
            tasks:
              - id: t1
                agent: dummy-agent
                read_only: false
                prompt: "hello"
            ---
            body
        ''')
        p = _make_plan_report(tmp_path, yaml_content)
        fm = extract_frontmatter(p)
        assert fm is not None
        # After fix: name should be: say "hello"
        assert fm["name"] == 'say "hello"'

    def test_newline_escape_in_double_quoted_scalar(self, tmp_path: Path):
        """ダブルクォート内 `\\n` は改行文字に展開される。"""
        yaml_content = textwrap.dedent("""\
            ---
            po_plan_version: "0.1"
            name: "line1\\nline2"
            cwd: ".."
            tasks:
              - id: t1
                agent: dummy-agent
                read_only: false
                prompt: "hello"
            ---
            body
        """)
        p = _make_plan_report(tmp_path, yaml_content)
        fm = extract_frontmatter(p)
        assert fm is not None
        # After fix: name should contain an actual newline
        assert fm["name"] == "line1\nline2"

    def test_single_quoted_scalar_not_affected(self, tmp_path: Path):
        """シングルクォートはエスケープ展開しない（YAML 仕様どおり）。"""
        yaml_content = textwrap.dedent("""\
            ---
            po_plan_version: "0.1"
            name: 'no\\\\escape'
            cwd: ".."
            tasks:
              - id: t1
                agent: dummy-agent
                read_only: false
                prompt: "hello"
            ---
            body
        """)
        p = _make_plan_report(tmp_path, yaml_content)
        fm = extract_frontmatter(p)
        assert fm is not None
        # Single-quoted: backslashes are literal, no expansion
        assert fm["name"] == "no\\\\escape"

    def test_unquoted_scalar_not_affected_by_escape(self, tmp_path: Path):
        """クォートなしスカラーはエスケープ展開しない。"""
        yaml_content = textwrap.dedent("""\
            ---
            po_plan_version: "0.1"
            name: plain
            cwd: ".."
            tasks:
              - id: t1
                agent: dummy-agent
                read_only: false
                prompt: "hello"
            ---
            body
        """)
        p = _make_plan_report(tmp_path, yaml_content)
        fm = extract_frontmatter(p)
        assert fm is not None
        assert fm["name"] == "plain"


# ---------------------------------------------------------------------------
# Fix 3: `validate_manifest` 内の変数名 `version` → `plan_version`
# ---------------------------------------------------------------------------


class TestFix3VariableRename:
    """Fix 3: `validate_manifest` 内 `version` → `plan_version` へのリネーム。

    これはリファクタリングのみ。動作変更なし → 現行コードでも通るはず。
    """

    def test_validate_correct_version_no_error(self, tmp_path: Path):
        """正しいバージョン `0.1` はエラーなし。"""
        yaml_content = textwrap.dedent("""\
            ---
            po_plan_version: "0.1"
            name: "test"
            cwd: ".."
            tasks:
              - id: t1
                agent: dummy-agent
                read_only: false
                prompt: "hello"
            ---
            body
        """)
        _make_claude_root(tmp_path)
        p = _make_plan_report(tmp_path, yaml_content)
        errors = validate_manifest(p, tmp_path)
        assert errors == []

    def test_validate_wrong_version_returns_error(self, tmp_path: Path):
        """バージョンが `0.2` の場合はエラーを返す。"""
        yaml_content = textwrap.dedent("""\
            ---
            po_plan_version: "0.2"
            name: "test"
            cwd: ".."
            tasks:
              - id: t1
                agent: dummy-agent
                read_only: false
                prompt: "hello"
            ---
            body
        """)
        _make_claude_root(tmp_path)
        p = _make_plan_report(tmp_path, yaml_content)
        errors = validate_manifest(p, tmp_path)
        assert any("po_plan_version" in e for e in errors), errors

    def test_validate_missing_version_returns_error(self, tmp_path: Path):
        """バージョンが存在しない場合もエラーを返す。"""
        yaml_content = textwrap.dedent("""\
            ---
            name: "test"
            cwd: ".."
            tasks:
              - id: t1
                agent: dummy-agent
                read_only: false
                prompt: "hello"
            ---
            body
        """)
        _make_claude_root(tmp_path)
        p = _make_plan_report(tmp_path, yaml_content)
        errors = validate_manifest(p, tmp_path)
        assert any("po_plan_version" in e for e in errors), errors


# ---------------------------------------------------------------------------
# Fix 4: `build_wave_manifest_text` に `waves=None` オプション引数を追加する
# ---------------------------------------------------------------------------


class TestFix4WavesOptionalArg:
    """Fix 4: `build_wave_manifest_text(manifest, wave_index, waves=None)` のシグネチャ追加。

    現行コードには `waves` キーワード引数が存在しないため、渡した場合は TypeError になる。
    修正後は waves を受け取り、None 以外の場合は compute_waves を呼ばないこと。
    """

    def test_build_wave_manifest_accepts_waves_kwarg(self):
        """waves キーワード引数を受け入れる (TypeError が発生しないこと)。

        現行コードでは TypeError が発生するため Red。
        """
        fm = _fm(_task("a"), _task("b", depends_on=["a"]))
        pre_waves = compute_waves(fm)
        # This should NOT raise TypeError after fix
        text = build_wave_manifest_text(fm, wave_index=0, waves=pre_waves)
        assert "id: a" in text

    def test_build_wave_manifest_waves_none_behaves_same_as_no_kwarg(self):
        """waves=None のときは従来どおり compute_waves を内部で呼ぶ。"""
        fm = _fm(_task("a"), _task("b", depends_on=["a"]))
        text_without = build_wave_manifest_text(fm, wave_index=0)
        # After fix: waves=None should give same result
        text_with_none = build_wave_manifest_text(fm, wave_index=0, waves=None)
        assert text_without == text_with_none

    def test_build_wave_manifest_with_precomputed_waves_same_result(self):
        """事前計算した waves を渡した場合も同じ出力が得られる。"""
        fm = _fm(
            _task("x"),
            _task("y", depends_on=["x"]),
            _task("z", depends_on=["x"]),
        )
        pre_waves = compute_waves(fm)
        text_auto = build_wave_manifest_text(fm, wave_index=1)
        text_pre = build_wave_manifest_text(fm, wave_index=1, waves=pre_waves)
        assert text_auto == text_pre

    def test_build_wave_manifest_waves_parameter_in_signature(self):
        """関数シグネチャに `waves` パラメータが存在することを確認する。

        現行コードには存在しないため Red。
        """
        sig = inspect.signature(build_wave_manifest_text)
        assert "waves" in sig.parameters, (
            f"build_wave_manifest_text の引数に 'waves' がありません。"
            f"現在のシグネチャ: {sig}"
        )

    def test_build_wave_manifest_keyword_only_body_still_works(self):
        """既存の keyword-only 引数 `body` が waves 追加後も機能すること。"""
        fm = _fm(_task("a"))
        text = build_wave_manifest_text(fm, wave_index=0, body="custom body")
        assert "custom body" in text
