"""Tests for YAML escaping of `writes` paths in build_wave_manifest_text.

Task: fix-manifest-yaml-escape
Fix [Sec Low-New1]: `build_wave_manifest_text` の `writes` フィールドに
YAML エスケープを追加する。

Red phase: these tests verify the acceptance criteria. Some will fail before
the fix is applied (paths with special characters).
"""

from __future__ import annotations

import textwrap

import pytest

from c3.po.manifest import build_wave_manifest_text, _yaml_quote


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(id_: str, writes: list[str] | None = None, **extras) -> dict:
    task = {
        "id": id_,
        "agent": "dummy-agent",
        "read_only": False,
        "prompt": f"do {id_}",
    }
    if writes is not None:
        task["writes"] = writes
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


# ---------------------------------------------------------------------------
# Tests for _yaml_quote function existence and correctness
# ---------------------------------------------------------------------------


class TestYamlQuoteExists:
    """Verify _yaml_quote is importable and works correctly."""

    def test_yaml_quote_exists_and_is_callable(self):
        """_yaml_quote 関数が manifest.py に存在し呼び出し可能であること。"""
        assert callable(_yaml_quote)

    def test_yaml_quote_plain_path(self):
        """通常のパスはダブルクォートで囲まれること。"""
        result = _yaml_quote("src/foo/bar.py")
        assert result == '"src/foo/bar.py"'

    def test_yaml_quote_empty_string(self):
        """空文字列は `""` になること。"""
        result = _yaml_quote("")
        assert result == '""'

    def test_yaml_quote_path_with_colon(self):
        """コロンを含むパスがクォートされること。"""
        result = _yaml_quote("C:/path/to/file.py")
        assert result == '"C:/path/to/file.py"'

    def test_yaml_quote_path_with_hash(self):
        """シャープを含むパスがクォートされること。"""
        result = _yaml_quote("src/foo#bar.py")
        assert result == '"src/foo#bar.py"'

    def test_yaml_quote_path_with_brackets(self):
        """角括弧を含むパスがクォートされること。"""
        result = _yaml_quote("src/[foo]/bar.py")
        assert result == '"src/[foo]/bar.py"'

    def test_yaml_quote_path_with_double_quote(self):
        """ダブルクォートを含むパスが適切にエスケープされること。"""
        result = _yaml_quote('src/say "hello".py')
        assert result == '"src/say \\"hello\\".py"'

    def test_yaml_quote_path_with_backslash(self):
        """バックスラッシュを含むパスが適切にエスケープされること。"""
        result = _yaml_quote("src\\foo\\bar.py")
        assert result == '"src\\\\foo\\\\bar.py"'


# ---------------------------------------------------------------------------
# Tests for build_wave_manifest_text writes field escaping
# ---------------------------------------------------------------------------


class TestWritesFieldEscaping:
    """build_wave_manifest_text の writes フィールドが YAML 安全にエスケープされること。"""

    def test_normal_path_rendered_safely(self):
        """通常パス `src/foo/bar.py` がエスケープ後も正しく読み込めること。"""
        fm = _fm(_task("t1", writes=["src/foo/bar.py"]))
        text = build_wave_manifest_text(fm, wave_index=0)
        # The writes line must be quoted
        assert '      - "src/foo/bar.py"' in text

    def test_path_with_colon_is_escaped(self):
        """コロンを含むパスが writes フィールドで YAML 安全にエスケープされること。

        修正前: `      - C:/path/to/file.py`  (YAML 解析でマッピングになる)
        修正後: `      - "C:/path/to/file.py"` (クォートで保護)
        """
        fm = _fm(_task("t1", writes=["C:/path/to/file.py"]))
        text = build_wave_manifest_text(fm, wave_index=0)
        # Must be quoted to avoid YAML treating it as a mapping
        assert '      - "C:/path/to/file.py"' in text

    def test_path_with_hash_is_escaped(self):
        """シャープを含むパスが writes フィールドで YAML 安全にエスケープされること。

        修正前: `      - src/foo#comment`  (YAML パーサがコメントとして扱う恐れ)
        修正後: `      - "src/foo#comment"`
        """
        fm = _fm(_task("t1", writes=["src/foo#comment"]))
        text = build_wave_manifest_text(fm, wave_index=0)
        assert '      - "src/foo#comment"' in text

    def test_path_with_square_brackets_is_escaped(self):
        """角括弧を含むパスが writes フィールドで YAML 安全にエスケープされること。"""
        fm = _fm(_task("t1", writes=["src/[feature]/bar.py"]))
        text = build_wave_manifest_text(fm, wave_index=0)
        assert '      - "src/[feature]/bar.py"' in text

    def test_multiple_writes_all_escaped(self):
        """複数の writes パスがそれぞれエスケープされること。"""
        paths = ["src/auth/login.py", "tests/test_login.py"]
        fm = _fm(_task("t1", writes=paths))
        text = build_wave_manifest_text(fm, wave_index=0)
        assert '      - "src/auth/login.py"' in text
        assert '      - "tests/test_login.py"' in text

    def test_writes_none_does_not_render_section(self):
        """writes が None の場合は writes: セクションが出力されないこと（既存動作）。"""
        fm = _fm(_task("t1"))
        text = build_wave_manifest_text(fm, wave_index=0)
        assert "writes:" not in text

    def test_writes_empty_list_does_not_render_section(self):
        """writes が空リストの場合は writes: セクションが出力されないこと（既存動作）。"""
        fm = _fm(_task("t1", writes=[]))
        text = build_wave_manifest_text(fm, wave_index=0)
        assert "writes:" not in text


# ---------------------------------------------------------------------------
# Round-trip tests: generate manifest text, re-parse, check writes values
# ---------------------------------------------------------------------------


class TestWritesRoundTrip:
    """生成した YAML テキストが独自パーサで正しく再解析できることを確認する。"""

    def _parse_writes_from_text(self, text: str) -> list[str]:
        """Extract the writes list from a manifest text using the manifest parser."""
        from c3.po.manifest import _parse_yaml, _FRONTMATTER_RE

        match = _FRONTMATTER_RE.match(text)
        assert match is not None, "No frontmatter found in generated text"
        fm = _parse_yaml(match.group(1))
        return fm["tasks"][0].get("writes", [])

    def test_normal_path_round_trips(self):
        """通常パスが生成・解析ラウンドトリップで値を保持すること。"""
        path = "src/foo/bar.py"
        fm = _fm(_task("t1", writes=[path]))
        text = build_wave_manifest_text(fm, wave_index=0)
        writes = self._parse_writes_from_text(text)
        assert writes == [path]

    def test_path_with_colon_round_trips(self):
        """コロンを含むパスがラウンドトリップで値を保持すること。

        これは修正前に失敗するはず（YAML 解析でマッピングになるため）。
        """
        path = "C:/Users/foo/project/src/bar.py"
        fm = _fm(_task("t1", writes=[path]))
        text = build_wave_manifest_text(fm, wave_index=0)
        writes = self._parse_writes_from_text(text)
        assert writes == [path], (
            f"Expected writes=[{path!r}] but got {writes!r}. "
            "This likely means the path was not YAML-escaped in the writes field."
        )

    def test_path_with_hash_round_trips(self):
        """シャープを含むパスがラウンドトリップで値を保持すること。"""
        path = "src/foo#bar.py"
        fm = _fm(_task("t1", writes=[path]))
        text = build_wave_manifest_text(fm, wave_index=0)
        writes = self._parse_writes_from_text(text)
        assert writes == [path]

    def test_path_with_brackets_round_trips(self):
        """角括弧を含むパスがラウンドトリップで値を保持すること。"""
        path = "src/[feature]/bar.py"
        fm = _fm(_task("t1", writes=[path]))
        text = build_wave_manifest_text(fm, wave_index=0)
        writes = self._parse_writes_from_text(text)
        assert writes == [path]

    def test_multiple_special_paths_round_trip(self):
        """複数の特殊文字パスがすべてラウンドトリップで正しく保持されること。"""
        paths = [
            "src/foo/bar.py",
            "src/auth#login.py",
            "src/[feature]/baz.py",
        ]
        fm = _fm(_task("t1", writes=paths))
        text = build_wave_manifest_text(fm, wave_index=0)
        writes = self._parse_writes_from_text(text)
        assert writes == paths
