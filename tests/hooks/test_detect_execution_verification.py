"""Red tests for ``.claude/skills/dev-workflow/scripts/detect_execution_verification.py``.

フェーズ E の実行検証判定（E-0）の検出器を固定する D-1（Red）テスト。
仕様は ``architecture-report-20260802-190003.md``（本文＋改訂 1〜5）。
語彙は ADR-3F / ADR-3G の確定表、seam は ADR-8H。

[本テストが一次仕様として固定する契約（architecture が明記していない部分は tester が確定した）]

1. seam（ADR-8H の逐語）
   - ``detect(diff_text: str, untracked: list[tuple[str, str]] | None = None) -> tuple[str, list[str]]``
   - ``_run_git(args: list[str]) -> tuple[int, str]``
   - ``resolve_base(explicit: str | None) -> str | None``
   - ``collect_untracked() -> list[tuple[str, str]]``
   - CLI 入口は ``main(argv: list[str] | None = None) -> int``
     （`.claude/skills/dev-workflow/scripts/` 配下の既存 3 本と同じ形）

2. ``detect`` の第 1 戻り値は **理由コードを含まない裸のトークン**
   ``"NEEDS_VERIFY"`` / ``"NOT_NEEDED"`` / ``"UNKNOWN"`` とする。
   ``detect`` は git を触らないため ``UNKNOWN`` は必ず EMPTY_DIFF 起因であり、
   理由コード（GIT_FAILED / EMPTY_DIFF）の付与は ``main`` 側の責務。
   stdout の 1 行（``NEEDS_VERIFY<TAB>{件数}`` / ``NOT_NEEDED<TAB>0`` /
   ``UNKNOWN<TAB>{理由コード}``・ADR-4R）は ``main`` 経由でのみ assert する。

3. ``detect`` の第 2 戻り値（対象ファイル一覧）は「走査対象集合」「発火したファイルの集合」の
   いずれの実装でも通るように書いてある。すなわち
   - 集合の同一性を assert するケース（T-8 等）では**全ファイルが発火する**入力を使う
   - 発火ファイルと非発火ファイルが混在するケースでは**包含のみ**を assert する
   - ``NOT_NEEDED`` / ``UNKNOWN`` のときの一覧の中身は assert しない

4. ``resolve_base`` が投げる git コマンド列（ADR-4 改訂 4 [DC-AS-003]「固定するのは
   ``_run_git`` の第 1 引数リストと呼び出し順序」）は以下を正とする。
   - 明示 ``--base``: ``["rev-parse", "--verify", <ref>]`` のみ（失敗しても連鎖に入らない・ADR-2E）
   - 未指定: ``["merge-base", "HEAD", "@{u}"]`` →
     ``["merge-base", "HEAD", "origin/HEAD"]`` → ``["merge-base", "HEAD", "main"]`` →
     ``["merge-base", "HEAD", "master"]`` の順で、最初に rc==0 になったものを採用

5. ``_run_git`` は ``subprocess.run`` に ``["git", "-c", "core.quotePath=false", *args]`` を
   第 1 位置引数で渡し、``timeout=10`` を指定し、``shell`` を真にしない（ADR-2X・改訂 4 [DC-GP-002]）。

[テストケース]
  T-1  真陽性（エスケープ / パーサ / 状態機械を含む合成 diff で NEEDS_VERIFY）
  T-2  真陰性（文書のみ・日本語中に英単語が混じる追加行で NOT_NEEDED）
  T-3  言語非依存（.ts / .go / .cs。JS の正規表現リテラルを使うエスケープ関数を含む）
  T-4  _run_git 失敗 → UNKNOWN GIT_FAILED かつ exit 0
  T-5  resolve_base の 5 分岐（git コマンド列と呼び出し順序を固定）
  T-6  判定順序（語彙ヒット → NEEDS_VERIFY ／ tracked 0 件 → UNKNOWN EMPTY_DIFF ／ 他 → NOT_NEEDED）
  T-7  base 差分と HEAD 差分の重複で weak の種類数が二重計上されない
  T-8  ファイル名集合（+++ b/<path> が抽出元・+++ /dev/null 除外・--- /dev/null は除外しない・
       Binary files / `\\ No newline` 行は除外）
  T-9  untracked が走査対象に入る／collect_untracked の git 呼び出し引数
  T-10 untracked の読み取り失敗でも例外を出さず判定行 1 行・exit 0
  T-11 _run_git が subprocess へ渡す引数に -c core.quotePath=false が前置される
  T-12 （回帰ガード・静的検査）_run_git の subprocess.run 呼び出しが encoding="utf-8" を
       明示すること（text=True に依存しないこと）
  T-13 （回帰ガード・実 subprocess 統合テスト）実 git リポジトリ＋非 ASCII 内容の untracked
       ファイルで検出器を実行し NEEDS_VERIFY が返ること（_run_git をスタブしない経路）

[T-12 / T-13 追加の経緯]
D-2 後に親が実際にスクリプトを実行し、以下の実機欠陥を発見した:

    stdout: UNKNOWN<TAB>GIT_FAILED
    stderr: UnicodeDecodeError: 'cp932' codec can't decode byte 0xef（subprocess の _readerthread 内）

原因は `subprocess.run` に `text=True` を渡し、ロケール既定（Windows では cp932）で git 出力を
デコードしていたため。T-1〜T-11 はすべて `_run_git` をスタブ化しており、この経路を 1 行も
通っていなかった（フルスイート 2625 件緑でも実機で欠陥が残っていた）。T-12（静的検査）と
T-13（実 subprocess 統合テスト）を追加し、この欠陥クラスの再発を機械的に検出できるようにする。
"""

from __future__ import annotations

import ast
import importlib.util
import itertools
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    REPO_ROOT / ".claude" / "skills" / "dev-workflow" / "scripts" / "detect_execution_verification.py"
)

_MODULE_SERIAL = itertools.count()


def _load_module() -> types.ModuleType:
    """検出器スクリプトを importlib で毎回 fresh にロードする。

    未実装（D-1 Red）の間は FileNotFoundError で落ちる。これが「正しい理由での失敗」。
    """
    if not SCRIPT_PATH.exists():
        raise FileNotFoundError(
            f"検出器スクリプトが未実装です（D-1 Red の期待どおりの失敗）: {SCRIPT_PATH}"
        )
    name = f"detect_execution_verification_{next(_MODULE_SERIAL)}"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


@pytest.fixture
def mod() -> types.ModuleType:
    return _load_module()


# ---------------------------------------------------------------------------
# 合成 diff ビルダ
# ---------------------------------------------------------------------------


def _modified(path: str, added: list[str]) -> list[str]:
    """既存ファイルの変更セクション（--- a/path / +++ b/path）。"""
    lines = [
        f"diff --git a/{path} b/{path}",
        "index 1111111..2222222 100644",
        f"--- a/{path}",
        f"+++ b/{path}",
        f"@@ -1,1 +1,{len(added) + 1} @@",
        " unchanged first line",
    ]
    lines.extend("+" + a for a in added)
    return lines


def _new_file(path: str, added: list[str]) -> list[str]:
    """新規追加ファイルのセクション（--- /dev/null / +++ b/path）。ADR-8G の主対象。"""
    lines = [
        f"diff --git a/{path} b/{path}",
        "new file mode 100644",
        "index 0000000..2222222",
        "--- /dev/null",
        f"+++ b/{path}",
        f"@@ -0,0 +1,{len(added)} @@",
    ]
    lines.extend("+" + a for a in added)
    return lines


def _deleted_file(path: str, removed: list[str]) -> list[str]:
    """削除ファイルのセクション（--- a/path / +++ /dev/null）。ADR-8G で除外対象。"""
    lines = [
        f"diff --git a/{path} b/{path}",
        "deleted file mode 100644",
        "index 1111111..0000000",
        f"--- a/{path}",
        "+++ /dev/null",
        f"@@ -1,{len(removed)} +0,0 @@",
    ]
    lines.extend("-" + r for r in removed)
    return lines


def _binary_file(path: str) -> list[str]:
    """バイナリ差分のセクション（+++ 行を持たない）。"""
    return [
        f"diff --git a/{path} b/{path}",
        "index 1111111..2222222 100644",
        f"Binary files a/{path} and b/{path} differ",
    ]


def _diff(*sections: list[str]) -> str:
    lines: list[str] = []
    for section in sections:
        lines.extend(section)
    return "\n".join(lines) + "\n"


# 合成ソース断片（語彙は ADR-3F / ADR-3G の確定表からのみ採る）
_ESCAPE_SRC = [
    "def escape_html(raw):",
    '    return raw.replace("&", "&amp;")',
]
_PARSER_SRC = [
    "class TemplateParser:",
    "    def feed(self, chunk):",
    "        self.pending.append(chunk)",
]
_STATE_MACHINE_SRC = [
    "STATE_INIT = 0",
    "def transition(current, symbol):",
    "    return current + 1",
]
_DOC_SRC = [
    "# フェーズ E の実行検証",
    "判定結果は 1 行だけ出力する。",
    "この手順は既存の review フローを壊さない。",
    "実装言語は Python だが、利用先の言語は任意でよい。",
]


# ---------------------------------------------------------------------------
# T-1: 真陽性
# ---------------------------------------------------------------------------


class TestT1TruePositive:
    def test_escape_parser_state_machine_fires(self, mod):
        """エスケープ / パーサ / 状態機械を含む合成 diff は NEEDS_VERIFY。"""
        diff = _diff(
            _modified("src/escape_util.py", _ESCAPE_SRC),
            _new_file("src/template_parser.py", _PARSER_SRC),
            _modified("src/state_machine.py", _STATE_MACHINE_SRC),
        )

        token, files = mod.detect(diff)

        assert token == "NEEDS_VERIFY"
        # 3 ファイルとも strong 語を含むため、走査対象集合 == 発火集合。
        assert set(files) == {
            "src/escape_util.py",
            "src/template_parser.py",
            "src/state_machine.py",
        }

    def test_strong_word_is_case_insensitive_substring(self, mod):
        """strong は大小文字無視の部分一致（ADR-3F）。htmlEncode が 1 種で発火する。"""
        diff = _diff(
            _new_file(
                "src/html_util.py",
                [
                    "def htmlEncode(value):",
                    "    return value",
                ],
            )
        )

        token, files = mod.detect(diff)

        assert token == "NEEDS_VERIFY"
        assert set(files) == {"src/html_util.py"}

    def test_two_distinct_weak_words_fire(self, mod):
        """weak は 2 種類で発火（ADR-3R / ADR-3G）。"""
        diff = _diff(
            _modified(
                "src/field_util.py",
                [
                    'def normalize(row):',
                    '    head = row.replace("x", "y")',
                    '    return head.split(",")',
                ],
            )
        )

        token, files = mod.detect(diff)

        assert token == "NEEDS_VERIFY"
        assert set(files) == {"src/field_util.py"}

    def test_mixed_diff_keeps_firing_file(self, mod):
        """文書ファイルが混ざっても発火し、発火したソースは一覧に含まれる。"""
        diff = _diff(
            _new_file("docs/e0-note.md", _DOC_SRC),
            _modified("src/escape_util.py", _ESCAPE_SRC),
        )

        token, files = mod.detect(diff)

        assert token == "NEEDS_VERIFY"
        assert "src/escape_util.py" in files


# ---------------------------------------------------------------------------
# T-2: 真陰性
# ---------------------------------------------------------------------------


class TestT2TrueNegative:
    def test_document_only_diff_is_not_needed(self, mod):
        """文書のみ・日本語中に英単語が混じる追加行では発火しない。"""
        diff = _diff(_new_file("docs/e0-note.md", _DOC_SRC))

        token, _files = mod.detect(diff)

        assert token == "NOT_NEEDED"

    def test_single_weak_word_does_not_fire(self, mod):
        """weak 1 種類だけでは発火しない（閾値は 2 種類）。"""
        diff = _diff(
            _modified(
                "docs/design.md",
                [
                    "本節では state の持ち方だけを説明する。",
                    "実装の詳細は別の章で扱う。",
                ],
            )
        )

        token, _files = mod.detect(diff)

        assert token == "NOT_NEEDED"

    def test_word_boundary_words_do_not_match_inside_longer_words(self, mod):
        """state / match / switch / mode は単語境界を課す（ADR-3F）。"""
        diff = _diff(
            _modified(
                "docs/glossary.md",
                [
                    "statement は文である。",
                    "matches は複数形である。",
                    "switched は過去形である。",
                    "modest は控えめという意味である。",
                ],
            )
        )

        token, _files = mod.detect(diff)

        assert token == "NOT_NEEDED"

    def test_weak_words_in_different_files_do_not_combine(self, mod):
        """判定はファイル単位（ADR-3）。別ファイルの weak 1 種同士は合算されない。"""
        diff = _diff(
            _modified("src/a_util.py", ['    head = row.replace("x", "y")']),
            _modified("src/b_util.py", ['    parts = row.split(",")']),
        )

        token, _files = mod.detect(diff)

        assert token == "NOT_NEEDED"


# ---------------------------------------------------------------------------
# T-3: 言語非依存
# ---------------------------------------------------------------------------


class TestT3LanguageAgnostic:
    def test_typescript_regex_literal_fires(self, mod):
        """JS/TS の正規表現リテラル形 replace(/ が strong で拾われる（ADR-3R [DC-AS-004]）。

        NOTE: 関数名を `esc` にしてあるのは、strong 語 `escape` / `encode` を含めず
        「正規表現リテラル形だけで発火する」ことを load-bearing にするため。
        """
        diff = _diff(
            _new_file(
                "web/src/html.ts",
                [
                    "const MAP: Record<string, string> = { '&': '&amp;' };",
                    "export function esc(s: string): string {",
                    "  return s.replace(/[&<>]/g, (c) => MAP[c]);",
                    "}",
                ],
            )
        )

        token, files = mod.detect(diff)

        assert token == "NEEDS_VERIFY"
        assert "web/src/html.ts" in files

    def test_go_mustcompile_fires(self, mod):
        diff = _diff(
            _new_file(
                "internal/rule/rule.go",
                [
                    "package rule",
                    'var pattern = regexp.MustCompile("^[a-z]+$")',
                ],
            )
        )

        token, files = mod.detect(diff)

        assert token == "NEEDS_VERIFY"
        assert "internal/rule/rule.go" in files

    def test_csharp_regex_ctor_fires(self, mod):
        diff = _diff(
            _new_file(
                "src/Validation/Rule.cs",
                [
                    "public sealed class Rule {",
                    '    private static readonly Regex Pattern = new Regex("^[a-z]+$");',
                    "}",
                ],
            )
        )

        token, files = mod.detect(diff)

        assert token == "NEEDS_VERIFY"
        assert "src/Validation/Rule.cs" in files


# ---------------------------------------------------------------------------
# T-4: git 実行失敗 → UNKNOWN GIT_FAILED / exit 0
# ---------------------------------------------------------------------------


class TestT4GitFailure:
    def test_all_git_calls_fail_yields_unknown_git_failed(self, mod, monkeypatch, capsys):
        monkeypatch.setattr(mod, "_run_git", lambda args: (128, ""))

        rc = mod.main([])

        out = capsys.readouterr().out
        assert out.splitlines() == ["UNKNOWN\tGIT_FAILED"]
        assert rc == 0

    def test_git_binary_missing_yields_unknown_git_failed(self, mod, monkeypatch, capsys):
        """git 不在（_run_git が 127 を返す形）でも同じ扱い・exit 0（ADR-5）。"""
        monkeypatch.setattr(mod, "_run_git", lambda args: (127, ""))

        rc = mod.main([])

        out = capsys.readouterr().out
        assert out.splitlines() == ["UNKNOWN\tGIT_FAILED"]
        assert rc == 0


# ---------------------------------------------------------------------------
# T-5: ベース解決の 5 分岐
# ---------------------------------------------------------------------------


def _recording_run_git(calls: list[list[str]], responder):
    def _fake(args):
        calls.append(list(args))
        return responder(list(args))

    return _fake


class TestT5ResolveBase:
    def test_explicit_base_resolved(self, mod, monkeypatch):
        """--base 明示が解決できたら、その 1 コマンドだけで確定する。"""
        calls: list[list[str]] = []

        def responder(args):
            if args[:2] == ["rev-parse", "--verify"]:
                # 実装が「引数をそのまま返す」「stdout を返す」のどちらでも
                # 同じ値になるよう、stdout に ref 名そのものを載せる。
                return (0, "abc123\n")
            return (1, "")

        monkeypatch.setattr(mod, "_run_git", _recording_run_git(calls, responder))

        base = mod.resolve_base("abc123")

        assert calls == [["rev-parse", "--verify", "abc123"]]
        assert base == "abc123"

    def test_explicit_base_unresolvable_does_not_fall_back(self, mod, monkeypatch):
        """ADR-2E: 明示 --base が解決不能でも連鎖降格しない。"""
        calls: list[list[str]] = []
        monkeypatch.setattr(mod, "_run_git", _recording_run_git(calls, lambda args: (128, "")))

        base = mod.resolve_base("no-such-ref-for-e0")

        assert calls == [["rev-parse", "--verify", "no-such-ref-for-e0"]]
        assert base is None

    def test_explicit_base_unresolvable_yields_unknown_git_failed(self, mod, monkeypatch, capsys):
        """明示 --base の解決不能は UNKNOWN GIT_FAILED（EMPTY_DIFF ではない）・exit 0。"""

        def responder(args):
            if args[:1] == ["rev-parse"]:
                return (128, "")
            return (0, "")

        monkeypatch.setattr(mod, "_run_git", _recording_run_git([], responder))

        rc = mod.main(["--base", "no-such-ref-for-e0"])

        out = capsys.readouterr().out
        assert out.splitlines() == ["UNKNOWN\tGIT_FAILED"]
        assert rc == 0

    def test_upstream_merge_base(self, mod, monkeypatch):
        """--base 未指定なら @{u} との merge-base が最初に試される。"""
        calls: list[list[str]] = []

        def responder(args):
            if args == ["merge-base", "HEAD", "@{u}"]:
                return (0, "deadbeef\n")
            return (1, "")

        monkeypatch.setattr(mod, "_run_git", _recording_run_git(calls, responder))

        base = mod.resolve_base(None)

        assert calls == [["merge-base", "HEAD", "@{u}"]]
        assert base == "deadbeef"

    def test_origin_head_fallback(self, mod, monkeypatch):
        """@{u} が解決できなければ origin/HEAD へフォールバックする。"""
        calls: list[list[str]] = []

        def responder(args):
            if args == ["merge-base", "HEAD", "origin/HEAD"]:
                return (0, "cafe1234\n")
            return (128, "")

        monkeypatch.setattr(mod, "_run_git", _recording_run_git(calls, responder))

        base = mod.resolve_base(None)

        assert calls == [
            ["merge-base", "HEAD", "@{u}"],
            ["merge-base", "HEAD", "origin/HEAD"],
        ]
        assert base == "cafe1234"

    def test_all_candidates_unresolvable(self, mod, monkeypatch):
        """全解決不能なら None。試行順序は @{u} → origin/HEAD → main → master。"""
        calls: list[list[str]] = []
        monkeypatch.setattr(mod, "_run_git", _recording_run_git(calls, lambda args: (128, "")))

        base = mod.resolve_base(None)

        assert calls == [
            ["merge-base", "HEAD", "@{u}"],
            ["merge-base", "HEAD", "origin/HEAD"],
            ["merge-base", "HEAD", "main"],
            ["merge-base", "HEAD", "master"],
        ]
        assert base is None


# ---------------------------------------------------------------------------
# T-6: 判定順序（ADR-2W）
# ---------------------------------------------------------------------------


class TestT6DecisionOrder:
    def test_vocabulary_hit_wins_over_empty_tracked(self, mod):
        """1. 語彙ヒットがあれば tracked 0 件でも NEEDS_VERIFY。"""
        token, files = mod.detect("", untracked=[("src/new_escaper.py", "def escape(s):\n    return s\n")])

        assert token == "NEEDS_VERIFY"
        assert "src/new_escaper.py" in files

    def test_untracked_is_not_in_empty_diff_denominator(self, mod):
        """2. ヒット無し + tracked 0 件 なら、untracked が 1 本あっても UNKNOWN。"""
        token, _files = mod.detect(
            "", untracked=[("docs/e0-note.md", "\n".join(_DOC_SRC) + "\n")]
        )

        assert token == "UNKNOWN"

    def test_no_input_at_all_is_unknown(self, mod):
        token, _files = mod.detect("", untracked=[])

        assert token == "UNKNOWN"

    def test_tracked_present_without_hit_is_not_needed(self, mod):
        """3. それ以外は NOT_NEEDED。"""
        token, _files = mod.detect(
            _diff(_new_file("docs/e0-note.md", _DOC_SRC)), untracked=[]
        )

        assert token == "NOT_NEEDED"

    def test_empty_diff_reason_code_is_reported_by_main(self, mod, monkeypatch, capsys):
        """走査対象 0 件は stdout で UNKNOWN<TAB>EMPTY_DIFF（ADR-5R）。"""
        monkeypatch.setattr(mod, "_run_git", lambda args: (0, ""))
        monkeypatch.setattr(mod, "collect_untracked", lambda: [])

        rc = mod.main([])

        out = capsys.readouterr().out
        assert out.splitlines() == ["UNKNOWN\tEMPTY_DIFF"]
        assert rc == 0


# ---------------------------------------------------------------------------
# T-7: 二重計上しない（ADR-2R）
# ---------------------------------------------------------------------------


class TestT7NoDoubleCount:
    def test_same_added_line_in_both_diffs_does_not_reach_weak_threshold(self, mod):
        """base 差分と HEAD 差分に同じ追加行（weak 1 種）が現れても発火しない。

        出現「回数」で数える実装なら 2 になって誤発火する。「種類数」で数えれば 1 のまま。
        """
        section = _modified("src/field_util.py", ['    head = row.replace("x", "y")'])
        both = _diff(section) + _diff(section)

        token, _files = mod.detect(both)

        assert token == "NOT_NEEDED"

    def test_distinct_weak_words_across_both_diffs_do_fire(self, mod):
        """同一ファイルの追加行は 2 経路にまたがっても通算される（種類数 2 で発火）。"""
        base_side = _modified("src/field_util.py", ['    head = row.replace("x", "y")'])
        head_side = _modified("src/field_util.py", ['    parts = head.split(",")'])
        both = _diff(base_side) + _diff(head_side)

        token, files = mod.detect(both)

        assert token == "NEEDS_VERIFY"
        assert set(files) == {"src/field_util.py"}


# ---------------------------------------------------------------------------
# T-8: ファイル名集合（ADR-8G）
# ---------------------------------------------------------------------------


class TestT8FileNameSet:
    def test_file_name_extraction(self, mod):
        """+++ b/<path> のみを抽出元とし、+++ /dev/null は除外・--- /dev/null は除外しない。

        期待集合の全ファイルが strong 語を含むため、実装が「走査対象集合」を返しても
        「発火集合」を返しても同じ集合になる。
        """
        modified_section = _modified("src/normal_parser.py", _PARSER_SRC)
        # `\ No newline at end of file` 行はファイル名でも追加行でもない。
        modified_section.append("\\ No newline at end of file")

        diff = _diff(
            modified_section,
            _new_file("src/new_lexer.py", ["def lexer(source):", "    return []"]),
            _deleted_file("src/old_tokenizer.py", ["def tokenize(s):", "    return []"]),
            _binary_file("assets/logo.png"),
            # ADR-2X: core.quotePath=false により非 ASCII 名は生の UTF-8 で出る
            _new_file("src/検証_util.py", ["def sanitize(value):", "    return value"]),
        )

        token, files = mod.detect(diff)

        assert token == "NEEDS_VERIFY"
        assert set(files) == {
            "src/normal_parser.py",
            "src/new_lexer.py",
            "src/検証_util.py",
        }
        assert "/dev/null" not in files
        assert "src/old_tokenizer.py" not in files
        assert "assets/logo.png" not in files
        assert not any("No newline" in f for f in files)


# ---------------------------------------------------------------------------
# T-9: untracked（ADR-2U / ADR-8H）
# ---------------------------------------------------------------------------


def _untracked_run_git(calls: list[list[str]], root: Path, names: list[str]):
    """ls-files に応答する _run_git スタブ。

    実装が「cwd 相対のパスをそのまま読む」形でも「rev-parse --show-toplevel 起点で読む」形でも
    成立するよう、両方の問い合わせに応答する。
    """

    def _fake(args):
        calls.append(list(args))
        if list(args)[:1] == ["ls-files"]:
            return (0, "".join(n + "\n" for n in names))
        if list(args) == ["rev-parse", "--show-toplevel"]:
            return (0, str(root) + "\n")
        return (0, "")

    return _fake


class TestT9Untracked:
    def test_untracked_content_is_scanned(self, mod):
        """detect に untracked を渡すと、その全内容が語彙判定にかかる。"""
        token, files = mod.detect(
            "",
            untracked=[
                ("src/new_escaper.py", "def escape_html(raw):\n    return raw\n"),
                ("docs/e0-note.md", "\n".join(_DOC_SRC) + "\n"),
            ],
        )

        assert token == "NEEDS_VERIFY"
        assert "src/new_escaper.py" in files

    def test_collect_untracked_git_arguments(self, mod, monkeypatch, tmp_path):
        """collect_untracked は _run_git を ls-files --others --exclude-standard で呼ぶ。"""
        calls: list[list[str]] = []
        (tmp_path / "new_escaper.py").write_text("def escape(s):\n    return s\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            mod, "_run_git", _untracked_run_git(calls, tmp_path, ["new_escaper.py"])
        )

        result = mod.collect_untracked()

        assert ["ls-files", "--others", "--exclude-standard"] in calls
        assert [p for p, _ in result if p.endswith("new_escaper.py")]
        assert any("escape" in content for _p, content in result)

    def test_collect_untracked_returns_empty_on_git_failure(self, mod, monkeypatch):
        """ls-files が失敗しても例外を出さず空リストを返す。"""
        monkeypatch.setattr(mod, "_run_git", lambda args: (128, ""))

        assert mod.collect_untracked() == []


# ---------------------------------------------------------------------------
# T-10: untracked の読み取り失敗（ADR-2V）
# ---------------------------------------------------------------------------


class TestT10UntrackedReadFailure:
    def test_unreadable_untracked_files_do_not_raise(self, mod, monkeypatch, tmp_path, capsys):
        """デコード不能なバイト・存在しないパスがあっても判定行 1 行・exit 0。"""
        (tmp_path / "broken_utf8.py").write_bytes(b"# \xff\xfe not utf-8\ndef f():\n    pass\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            mod,
            "_run_git",
            _untracked_run_git([], tmp_path, ["broken_utf8.py", "vanished.py"]),
        )

        rc = mod.main([])

        out = capsys.readouterr().out
        assert len(out.splitlines()) == 1
        assert out.split("\t")[0] in {"NEEDS_VERIFY", "NOT_NEEDED", "UNKNOWN"}
        assert rc == 0

    def test_binary_untracked_file_is_excluded_from_scan(self, mod, monkeypatch, tmp_path, capsys):
        """先頭 8KB に NUL を含むファイルは走査対象から外す（語彙を含んでいても発火しない）。"""
        (tmp_path / "blob.bin").write_bytes(b"\x00\x01def escape_html(raw):\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(mod, "_run_git", _untracked_run_git([], tmp_path, ["blob.bin"]))

        rc = mod.main([])

        out = capsys.readouterr().out
        assert out.splitlines() == ["UNKNOWN\tEMPTY_DIFF"]
        assert rc == 0


# ---------------------------------------------------------------------------
# T-11: -c core.quotePath=false の前置（ADR-2X）
# ---------------------------------------------------------------------------


class _SubprocessCalled(Exception):
    """subprocess.run に到達したことを示すセンチネル。"""


class TestT11QuotePathPrefix:
    def test_run_git_prefixes_core_quotepath_false(self, monkeypatch):
        captured: list[tuple[tuple, dict]] = []

        def fake_run(*args, **kwargs):
            captured.append((args, kwargs))
            raise _SubprocessCalled()

        # モジュールのロード前に差し替えることで、`import subprocess` 形でも
        # `from subprocess import run` 形でも同じ fake が使われる。
        monkeypatch.setattr(subprocess, "run", fake_run)
        module = _load_module()

        try:
            module._run_git(["diff", "HEAD"])
        except _SubprocessCalled:
            pass

        assert captured, "_run_git が subprocess.run を呼んでいない"
        args, kwargs = captured[0]
        assert args, "コマンド列は第 1 位置引数で渡すこと"
        cmd = list(args[0])
        assert Path(cmd[0]).name in {"git", "git.exe"}
        assert cmd[1:3] == ["-c", "core.quotePath=false"]
        assert cmd[3:] == ["diff", "HEAD"]
        assert kwargs.get("timeout") == 10
        assert not kwargs.get("shell")


# ---------------------------------------------------------------------------
# T-12: 回帰ガード（静的検査）— _run_git の subprocess.run は encoding="utf-8" 明示
# ---------------------------------------------------------------------------


def _find_function_def(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"関数定義 {name} が見つからない: {SCRIPT_PATH}")


def _find_subprocess_run_call(func_node: ast.FunctionDef) -> ast.Call:
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            target = node.func
            is_attr_run = isinstance(target, ast.Attribute) and target.attr == "run"
            is_bare_run = isinstance(target, ast.Name) and target.id == "run"
            if is_attr_run or is_bare_run:
                return node
    raise AssertionError("_run_git 内に subprocess.run(...) 呼び出しが見つからない")


class TestT12RunGitEncodingStatic:
    """回帰ガード: _run_git の subprocess.run 呼び出しが encoding="utf-8" を明示すること。

    2026-08-02 実機欠陥（text=True でロケール既定 cp932 デコードし、非 ASCII を含む
    git 出力で UnicodeDecodeError → 誤って UNKNOWN GIT_FAILED になる）の再発防止。
    T-1〜T-11 は `_run_git` を monkeypatch でスタブ化しており subprocess.run 呼び出しの
    実引数を一切見ないため、この静的検査が唯一 kwargs を固定するテストになる。
    """

    def test_run_git_subprocess_call_has_explicit_utf8_encoding(self):
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(SCRIPT_PATH))

        run_git_func = _find_function_def(tree, "_run_git")
        call_node = _find_subprocess_run_call(run_git_func)

        kwargs_by_name = {kw.arg: kw.value for kw in call_node.keywords if kw.arg is not None}

        assert "encoding" in kwargs_by_name, (
            "subprocess.run に encoding kwarg が無い（text=True 依存はロケール既定を使うため NG）"
        )
        encoding_node = kwargs_by_name["encoding"]
        assert isinstance(encoding_node, ast.Constant), "encoding は文字列リテラルで固定すること"
        assert encoding_node.value == "utf-8"

        # text=True / universal_newlines=True に依存していないこと（cp932 UnicodeDecodeError の原因）。
        assert "text" not in kwargs_by_name, "text=True に依存すべきでない"
        assert "universal_newlines" not in kwargs_by_name

    def test_run_git_subprocess_call_uses_shell_false_and_timeout(self):
        """T-11 と同じ引数を静的にも確認する（AST 版・monkeypatch 不要）。"""
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(SCRIPT_PATH))

        run_git_func = _find_function_def(tree, "_run_git")
        call_node = _find_subprocess_run_call(run_git_func)
        kwargs_by_name = {kw.arg: kw.value for kw in call_node.keywords if kw.arg is not None}

        assert "timeout" in kwargs_by_name
        assert isinstance(kwargs_by_name["timeout"], ast.Constant)
        assert kwargs_by_name["timeout"].value == 10

        # shell=True を明示していないこと（渡すなら False で明示していてもよい）。
        if "shell" in kwargs_by_name:
            shell_node = kwargs_by_name["shell"]
            assert isinstance(shell_node, ast.Constant)
            assert shell_node.value is False


# ---------------------------------------------------------------------------
# T-13: 回帰ガード（実 subprocess 統合テスト）— スタブを使わない経路を1本確保
# ---------------------------------------------------------------------------


def _run_real_git(cwd: Path, *args: str) -> None:
    """テスト用の実リポジトリを組み立てるための実 git 呼び出し（セットアップ専用）。

    検出器の `_run_git` とは無関係（検出器側は monkeypatch しない）。
    """
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )


class TestT13RealSubprocessIntegration:
    """回帰ガード: _run_git を monkeypatch せず、実 subprocess を通す経路を確保する。

    T-1〜T-11・T-12 はすべて `_run_git` をスタブ化しており、実際に
    `subprocess.run` → git バイナリ → デコードという経路を 1 行も通っていなかった。
    フルスイート緑（2625 件）でも実機で `UnicodeDecodeError` による誤検出が起きたのはこのため。
    ここでは一時ディレクトリに実際の git リポジトリを作り、非 ASCII を含む
    ファイル名またはファイル内容を untracked として置いた状態で検出器の `main()` を実行する。
    Windows / POSIX 両方で `git` バイナリの呼び出しのみに依存し、シェル機能は使わない。
    """

    def test_real_git_repo_with_non_ascii_untracked_content_needs_verify(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        # 空リポジトリで `git diff HEAD` が失敗しない（HEAD が存在する）よう初回コミットを作る。
        _run_real_git(tmp_path, "init")
        _run_real_git(tmp_path, "config", "user.email", "e0-test@example.com")
        _run_real_git(tmp_path, "config", "user.name", "E0 Test")
        (tmp_path / "README.md").write_text("# placeholder\n", encoding="utf-8")
        _run_real_git(tmp_path, "add", "README.md")
        _run_real_git(tmp_path, "commit", "-m", "initial commit")

        # 非 ASCII なファイル名 + 非 ASCII コメントを含む strong 語（escape）ヒットの
        # untracked ファイルを配置する（ADR-2X の非 ASCII 名想定にも合わせる）。
        (tmp_path / "検証_util.py").write_text(
            "def escape_html(raw):\n"
            '    """日本語のドキュメント文字列。非 ASCII を含む。"""\n'
            "    return raw.replace('&', '&amp;')\n",
            encoding="utf-8",
        )

        monkeypatch.chdir(tmp_path)
        module = _load_module()

        rc = module.main([])

        captured = capsys.readouterr()
        lines = captured.out.splitlines()
        assert len(lines) == 1, f"stdout は判定行 1 行のみのはず: {captured.out!r}"
        assert lines[0].startswith("NEEDS_VERIFY\t"), (
            f"実 git 経路（text=True 起因の UnicodeDecodeError なら GIT_FAILED になる）: {lines[0]!r}"
        )
        assert rc == 0

    def test_real_git_repo_with_non_ascii_filename_untracked_needs_verify(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """ファイル内容だけでなくファイル名自体が非 ASCII でも同じ経路で発火する。"""
        _run_real_git(tmp_path, "init")
        _run_real_git(tmp_path, "config", "user.email", "e0-test@example.com")
        _run_real_git(tmp_path, "config", "user.name", "E0 Test")
        (tmp_path / "README.md").write_text("# placeholder\n", encoding="utf-8")
        _run_real_git(tmp_path, "add", "README.md")
        _run_real_git(tmp_path, "commit", "-m", "initial commit")

        (tmp_path / "パーサー.py").write_text(
            "class TemplateParser:\n"
            "    def feed(self, chunk):\n"
            "        self.pending.append(chunk)\n",
            encoding="utf-8",
        )

        monkeypatch.chdir(tmp_path)
        module = _load_module()

        rc = module.main([])

        captured = capsys.readouterr()
        lines = captured.out.splitlines()
        assert len(lines) == 1
        assert lines[0].startswith("NEEDS_VERIFY\t")
        assert rc == 0


# ---------------------------------------------------------------------------
# T-14: 回帰ガード（E 周回 1 是正・[SR-V-002]）— untracked 読み取りの封じ込め
# ---------------------------------------------------------------------------


class TestT14ContainmentGuard:
    """[SR-V-002] のガード（symlink チェック + resolve() 封じ込め）の回帰テスト。

    本テスト実行環境（Windows・非管理者）では ``os.symlink()`` が
    ``OSError: [WinError 1314] クライアントは要求された特権を保有していません`` で
    失敗するため（実測済み）、実 symlink の作成には依存しない。代わりに
    ``ls-files --others`` が返す相対パスに ``..`` を含む値（攻撃者が細工した
    PR ブランチが symlink 経由で到達させうるのと同型の adversarial input）を
    ``_run_git`` monkeypatch で直接注入し、``collect_untracked`` の
    resolve() 封じ込め判定そのものを検査する。
    """

    def test_path_escaping_repo_root_is_skipped(self, mod, monkeypatch, tmp_path, capsys):
        """repo_root 外に resolve() されるパスは読み取られずスキップされる。"""
        outside_dir = tmp_path.parent / f"{tmp_path.name}_outside_e0"
        outside_dir.mkdir(exist_ok=True)
        secret = outside_dir / "secret.txt"
        secret.write_text("escape sanitize outside-secret-content\n", encoding="utf-8")

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        monkeypatch.chdir(repo_dir)

        traversal_path = "../" + f"{outside_dir.name}/secret.txt"
        monkeypatch.setattr(
            mod,
            "_run_git",
            lambda args: (0, traversal_path + "\n") if args[:1] == ["ls-files"] else (0, ""),
        )

        result = mod.collect_untracked()

        assert result == [], "封じ込め外のファイルは読み取り対象に入ってはならない"
        err = capsys.readouterr().err
        assert "outside repository" in err or "outside" in err.lower()

    def test_symlink_creation_is_privilege_restricted_in_this_environment(self):
        """実 symlink が作れない本環境の制約を記録する（正直な申告・代替検査の根拠）。"""
        import os

        target = None
        try:
            os.symlink("nonexistent-target-for-e0-probe", "nonexistent-link-for-e0-probe")
            target = "created"
        except OSError as e:
            target = e
        finally:
            try:
                os.remove("nonexistent-link-for-e0-probe")
            except OSError:
                pass

        # 特権があれば symlink 作成に成功しうる（CI 等）。本テストは「作れない」ことの
        # 固定ではなく、環境依存の事実を記録するのみ（assert は常に真）。
        assert target is not None


# ---------------------------------------------------------------------------
# T-15: 回帰ガード（E 周回 1 是正・[SR-NEW]）— untracked ファイルのサイズ上限
# ---------------------------------------------------------------------------


class TestT15SizeLimitGuard:
    def test_oversized_untracked_file_is_skipped(self, mod, monkeypatch, tmp_path, capsys):
        """サイズ上限（1MB）を超える untracked ファイルは読み取らずスキップする。"""
        big = tmp_path / "big.txt"
        # 1MB を確実に超えるサイズ（1MB + 数百バイト分の余裕）。
        big.write_text("escape sanitize " * 100_000, encoding="utf-8")
        assert big.stat().st_size > 1024 * 1024

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(mod, "_run_git", _untracked_run_git([], tmp_path, ["big.txt"]))

        result = mod.collect_untracked()

        assert result == []
        err = capsys.readouterr().err
        assert "big.txt" in err
        assert "size" in err.lower()

    def test_small_untracked_file_is_not_affected_by_size_limit(self, mod, monkeypatch, tmp_path):
        """上限未満のファイルは従来どおり読み取られる（既存挙動の非退行）。"""
        small = tmp_path / "small_escaper.py"
        small.write_text("def escape_html(raw):\n    return raw\n", encoding="utf-8")

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            mod, "_run_git", _untracked_run_git([], tmp_path, ["small_escaper.py"])
        )

        result = mod.collect_untracked()

        assert len(result) == 1
        assert result[0][0].endswith("small_escaper.py")


# ---------------------------------------------------------------------------
# T-16: 回帰ガード（E 周回 1 是正・[CR-NEW]）— --print0 出力に \r が付着しない
# ---------------------------------------------------------------------------


class TestT16NoCarriageReturnInPrint0:
    """stdout の判定行（--print0 含む）に \\r が混入しないことを固定する。

    D-2 後の実機欠陥（Windows で改行が \\r\\n になり、--print0 の最終ファイル名末尾に
    \\r が付着）の再発防止（[CR-NEW]）。実プロセスを起動し生バイト列を検査する
    （capsys 経由では reconfigure(newline="") の効果を観測できないため）。
    """

    def test_print0_raw_bytes_have_no_carriage_return(self, tmp_path):
        _run_real_git(tmp_path, "init")
        _run_real_git(tmp_path, "config", "user.email", "e0-test@example.com")
        _run_real_git(tmp_path, "config", "user.name", "E0 Test")
        (tmp_path / "README.md").write_text("# placeholder\n", encoding="utf-8")
        _run_real_git(tmp_path, "add", "README.md")
        _run_real_git(tmp_path, "commit", "-m", "initial commit")

        (tmp_path / "a_escaper.py").write_text(
            "def escape_html(raw):\n    return raw\n", encoding="utf-8"
        )
        (tmp_path / "b_lexer.py").write_text(
            "def lexer(source):\n    return []\n", encoding="utf-8"
        )

        proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--print0"],
            cwd=tmp_path,
            capture_output=True,
            timeout=15,
        )

        assert proc.returncode == 0
        assert b"\r" not in proc.stdout, f"--print0 の生バイト列に \\r が混入: {proc.stdout!r}"
        assert proc.stdout.startswith(b"NEEDS_VERIFY\t")

    def test_default_stdout_raw_bytes_have_no_carriage_return(self, tmp_path):
        """--print0 なしの既定出力でも \\r が混入しない（同じ reconfigure の効果を確認）。"""
        _run_real_git(tmp_path, "init")
        _run_real_git(tmp_path, "config", "user.email", "e0-test@example.com")
        _run_real_git(tmp_path, "config", "user.name", "E0 Test")
        (tmp_path / "README.md").write_text("# placeholder\n", encoding="utf-8")
        _run_real_git(tmp_path, "add", "README.md")
        _run_real_git(tmp_path, "commit", "-m", "initial commit")

        proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            cwd=tmp_path,
            capture_output=True,
            timeout=15,
        )

        assert proc.returncode == 0
        assert b"\r" not in proc.stdout
        assert b"\r" not in proc.stderr


# ---------------------------------------------------------------------------
# T-17: 回帰ガード（E 周回 1 是正・[SR-R-001]）— 例外メッセージ本文を出力しない
# ---------------------------------------------------------------------------


class TestT17ExceptionMessageSymmetry:
    """main() のトップレベル例外ハンドラが例外メッセージ本文を出力しないことを固定する。

    collect_untracked 内の個別ファイル読み取り失敗時と同じ書式
    （``type(e).__name__`` のみ）に揃っていることを確認する。
    """

    def test_top_level_exception_does_not_leak_message_body(self, mod, monkeypatch, capsys):
        def _boom():
            raise ValueError("sensitive-detail-that-must-not-leak")

        monkeypatch.setattr(mod, "resolve_base", lambda explicit: None)
        monkeypatch.setattr(mod, "_collect_diffs", lambda base: ("", False))
        monkeypatch.setattr(mod, "collect_untracked", _boom)

        rc = mod.main([])

        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out.splitlines() == ["UNKNOWN\tGIT_FAILED"]
        assert "sensitive-detail-that-must-not-leak" not in captured.err
        assert "ValueError" in captured.err


# ---------------------------------------------------------------------------
# T-18: 回帰ガード（E 周回 2 是正・[CR-NEW] / [SR-AI-001]）—
#   発火対象ファイルと skip 警告が同一 stderr に混在しても、--print0 の stdout
#   リダイレクト経路には警告が一切混入しないことを実 subprocess で固定する。
# ---------------------------------------------------------------------------


class TestT18Print0StdoutIsolatedFromSkipWarnings:
    """コードレビュー周回 2 [CR-NEW] Medium の主発見（発火対象ファイル + サイズ超過
    untracked ファイルが同一 stderr に混在する）を実 subprocess で再現し、
    是正後の受け渡し経路（`--print0` の stdout のみを tester へ渡す）が
    警告行から構造的に分離されていることを固定する。
    `main()` を直接呼ぶ単体テスト（monkeypatch 経由）ではなく、周回 2 の
    code-review-report が指摘した「main() 経由の実 subprocess 統合シナリオ」の穴を埋める。
    """

    def test_print0_stdout_excludes_warning_lines_when_skip_and_fire_coexist(
        self, tmp_path: Path
    ):
        _run_real_git(tmp_path, "init")
        _run_real_git(tmp_path, "config", "user.email", "e0-test@example.com")
        _run_real_git(tmp_path, "config", "user.name", "E0 Test")
        (tmp_path / "README.md").write_text("# placeholder\n", encoding="utf-8")
        _run_real_git(tmp_path, "add", "README.md")
        _run_real_git(tmp_path, "commit", "-m", "initial commit")

        # 発火対象ファイル（strong 語彙 "escape" を含む）
        (tmp_path / "new_escaper.py").write_text(
            "def escape(x):\n    return x.replace('<', '&lt;')\n", encoding="utf-8"
        )
        # サイズ上限超過ファイル（1MB 超・skip warning を誘発）
        (tmp_path / "huge_untracked.bin").write_bytes(b"0" * (1024 * 1024 + 100))

        proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--print0"],
            cwd=tmp_path,
            capture_output=True,
            timeout=15,
        )

        assert proc.returncode == 0
        # stderr 側には skip 警告が実際に出ていることを前提として確認する
        # （警告が出ない環境でこのテストが無意味に緑化するのを防ぐ）。
        assert b"Warning: skipping huge_untracked.bin" in proc.stderr

        # --print0 の stdout（tester への受け渡し経路）には警告文言が一切混入しない。
        assert b"Warning" not in proc.stdout
        assert proc.stdout == b"NEEDS_VERIFY\t1\tnew_escaper.py\n"
        assert b"huge_untracked.bin" not in proc.stdout


# ---------------------------------------------------------------------------
# T-19: E-0 自身の出力ファイルの自己参照遮断（E 周回 3 [CR-NEW] Medium・回帰ガード）
# ---------------------------------------------------------------------------


class TestT19SelfOutputExclusion:
    """code-review-report-20260802-230011.md の [CR-NEW] Medium が実機再現した
    「`.claude/.gitignore` が届かない環境（`c3 update` 経路）で E-0 の出力ファイル
    自身が次回実行の走査対象に混入し続ける」自己参照を、実装側の無条件除外
    （`_SELF_OUTPUT_PATTERNS` / `_is_self_output`）が gitignore の有無に関わらず
    遮断することを固定する。
    """

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            # 一致すべき（SKILL.md が生成する正規の命名）
            (".claude/state/e0-targets-1234567890.txt", True),
            (".claude/state/e0-targets-1785680870-731-5199.txt", True),
            (".claude/state/e0-targets-.txt", True),
            # 一致すべきでない（誤って正規のソースファイルを除外＝fail-open にしないこと）
            ("state/e0-targets-x.txt", False),  # 先頭 .claude/ が無い
            (".claude/state/sub/e0-targets-x.txt", False),  # サブディレクトリ経由
            (".claude/state/e0-targets-x.txt.bak", False),  # 拡張子が異なる
            (".claude/state/E0-TARGETS-x.txt", False),  # 大文字（厳密一致）
            (".claude/state/e0-targetsx.txt", False),  # ハイフンが無い
            (".claude/state/setup_done.flag", False),  # 無関係な既存 state ファイル
            ("src/c3/e0-targets-fake.py", False),  # 正規ソースファイルの偽装
            ("e0-targets-x.txt", False),  # ルート直下単独
            ("other/state/e0-targets-x.txt", False),  # 別トップディレクトリ
            # E 周回 4 回帰（code-review-report-20260802-233534.md [CR-NEW] Medium）:
            # `.claude/` の前に余分なディレクトリが付くケース。`PurePosixPath.match` の
            # 右アンカー挙動では誤って True（誤除外）になっていたが、`fnmatch.fnmatchcase`
            # 方式では文字列全体の完全一致になるため False（除外しない）が正しい。
            ("vendor/sub/.claude/state/e0-targets-x.txt", False),
            ("a/b/c/.claude/state/e0-targets-y.txt", False),
            ("deeply/nested/vendor/.claude/state/e0-targets-z.txt", False),
        ],
    )
    def test_is_self_output_boundary_cases(self, mod, path, expected):
        assert mod._is_self_output(path) is expected

    def test_is_self_output_allows_star_to_cross_path_separator(self, mod):
        """developer 裁定（E 周回 4）: `fnmatch` の `*` はパス区切りを跨ぐため、
        `.claude/state/e0-targets-a/b.txt` のようにパターンの `*` 部分に `/` を
        含むパスも一致する（除外される）。これは許容として明文化された挙動であり、
        Red にする項目ではない（docstring L122-124 の逐語と一致することを固定する）。
        """
        assert mod._is_self_output(".claude/state/e0-targets-a/b.txt") is True

    def test_is_self_output_normalizes_windows_separators(self, mod):
        """git ls-files は POSIX 区切りで返すが、正規化前提が壊れていないことも確認する。"""
        assert mod._is_self_output(".claude\\state\\e0-targets-x.txt") is True

    def test_collect_untracked_excludes_self_output_without_warning(
        self, mod, monkeypatch, tmp_path, capsys
    ):
        """自身の出力ファイルは collect_untracked の走査対象から無条件に除外され、
        かつ「スキップ警告」を出さない（自分の出力を無視するのは正常動作のため）。
        """
        (tmp_path / "new_escaper.py").write_text(
            "def escape(x):\n    return x\n", encoding="utf-8"
        )
        state_dir = tmp_path / ".claude" / "state"
        state_dir.mkdir(parents=True)
        self_output = state_dir / "e0-targets-1111111111-222-33333.txt"
        self_output.write_text("NEEDS_VERIFY\t1\tnew_escaper.py\n", encoding="utf-8")

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            mod,
            "_run_git",
            _untracked_run_git(
                [],
                tmp_path,
                ["new_escaper.py", ".claude/state/e0-targets-1111111111-222-33333.txt"],
            ),
        )

        result = mod.collect_untracked()

        assert [p for p, _ in result] == ["new_escaper.py"]
        assert capsys.readouterr().err == ""

    def test_two_consecutive_runs_without_gitignore_do_not_self_reference(
        self, tmp_path: Path
    ):
        """gitignore を持たない一時リポジトリで E-0 を 2 回連続実行しても、
        2 回目の出力（stdout）に 1 回目の出力ファイル自身が含まれないこと
        （code-review-report が実機再現した Medium の回帰ガード）。
        """
        _run_real_git(tmp_path, "init")
        _run_real_git(tmp_path, "config", "user.email", "e0-test@example.com")
        _run_real_git(tmp_path, "config", "user.name", "E0 Test")
        (tmp_path / "README.md").write_text("# placeholder\n", encoding="utf-8")
        _run_real_git(tmp_path, "add", "README.md")
        _run_real_git(tmp_path, "commit", "-m", "initial commit")

        # code-review-report の実機再現と同一シナリオ：発火ファイルが 1 件のみ。
        (tmp_path / "new_escaper.py").write_text(
            "def escape(x):\n    return x\n", encoding="utf-8"
        )

        state_dir = tmp_path / ".claude" / "state"
        state_dir.mkdir(parents=True)
        # .claude/.gitignore は意図的に置かない（INIT_ONLY が届かない既存利用先の再現）。

        run1_out = state_dir / "e0-targets-run1.txt"
        proc1 = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--print0"],
            cwd=tmp_path,
            capture_output=True,
            timeout=15,
        )
        assert proc1.returncode == 0
        run1_out.write_bytes(proc1.stdout)
        assert proc1.stdout == b"NEEDS_VERIFY\t1\tnew_escaper.py\n"

        run2_out = state_dir / "e0-targets-run2.txt"
        proc2 = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--print0"],
            cwd=tmp_path,
            capture_output=True,
            timeout=15,
        )
        assert proc2.returncode == 0
        run2_out.write_bytes(proc2.stdout)

        # 修正前（gitignore 一本足）は run1 のファイル名が再度混入して
        # NEEDS_VERIFY\t2\t...e0-targets-run1.txt\x00new_escaper.py\n になっていた。
        assert proc2.stdout == b"NEEDS_VERIFY\t1\tnew_escaper.py\n"
        assert b"e0-targets-run1" not in proc2.stdout

    def test_nested_vendor_dir_e0_targets_lookalike_is_not_excluded(
        self, tmp_path: Path
    ):
        """統合レベル回帰（E 周回 4・code-review-report [CR-NEW] Medium の実機再現の固定）。

        `vendor/subproject/.claude/state/e0-targets-*.txt` という、正規の E-0 出力パス
        （リポジトリ直下の `.claude/state/`）より深い場所に、E-0 の出力ではない通常の
        untracked ソースファイル（strong 語彙 `escape` を含む）を置く。
        `PurePosixPath.match` の右アンカー挙動（是正前の実装）ではこのパスも誤って
        自己出力とみなし除外していたため `UNKNOWN\tEMPTY_DIFF` にすり替わっていたが、
        `fnmatch.fnmatchcase` 方式（是正後）ではリポジトリ直下からの完全一致のみが
        対象になるため除外されず、`NEEDS_VERIFY` になることを実 subprocess で固定する。
        """
        _run_real_git(tmp_path, "init")
        _run_real_git(tmp_path, "config", "user.email", "e0-test@example.com")
        _run_real_git(tmp_path, "config", "user.name", "E0 Test")
        (tmp_path / "README.md").write_text("# placeholder\n", encoding="utf-8")
        _run_real_git(tmp_path, "add", "README.md")
        _run_real_git(tmp_path, "commit", "-m", "initial commit")

        vendor_state_dir = tmp_path / "vendor" / "subproject" / ".claude" / "state"
        vendor_state_dir.mkdir(parents=True)
        (vendor_state_dir / "e0-targets-not-an-output.txt").write_text(
            "def escape(x):\n    return x\n", encoding="utf-8"
        )

        proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--print0"],
            cwd=tmp_path,
            capture_output=True,
            timeout=15,
        )

        assert proc.returncode == 0
        assert proc.stdout.startswith(b"NEEDS_VERIFY\t"), (
            f"vendor 配下のネストした e0-targets-*.txt が誤除外され "
            f"UNKNOWN\\tEMPTY_DIFF にすり替わっていないか: {proc.stdout!r}"
        )
        assert b"e0-targets-not-an-output.txt" in proc.stdout
