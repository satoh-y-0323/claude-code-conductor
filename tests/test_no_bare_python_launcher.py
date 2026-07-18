"""
Lint regression guard: 配布物に無印 `python` 起動子が残っていないことを機械検査する。

architecture-report-20260718-114347.md §2-5 (test-lint タスク定義) に基づく。

走査対象（DC-AM-002 で明示列挙・固定）:
  1. .claude/settings.json
     - hooks 各エントリの "command" 値（bare "python" のみ = 起動子違反）
     - statusLine.command 文字列の先頭トークン（"python ..." で始まる埋め込み形）
     - permissions.allow の "Bash(python ..." パターン
  2. .claude/skills/**/SKILL.md
  3. .claude/agents/*.md

`.claude/docs/` は走査対象外（人間向けリファレンスで LLM 実行文脈を持たない）。
除外ディレクトリ: `.claude/skills/autonomous-mode/`（配布対象外・git 非追跡）。

実行文脈の判定（正規表現で確定・散文は拾わない）:
  - フェンス付きコードブロック（``` ... ```）内の行で、行頭（インデント除去後）が
    `python` に続き空白または行末（"python " / "python -m " / "python -c " を包含する一般形）
  - 同コードブロック内の行に `| python` （パイプ後の python 起動）が含まれる
  - インラインコードスパン（単一バッククォート `...`）内で、スパンの先頭が上記と同じ形で
    始まる、またはスパン内に `| python` を含む

上記コンテキスト（フェンスまたはインラインスパン）の外側にあるプレーン散文中の "python" 語は
一切検出しない（test_prose_mention_of_python_is_not_flagged で実証）。

許容リスト（DC-AM-002）: 安定部分文字列の部分一致で管理する。現時点の初期値は
report-timestamp SKILL.md の退避文言 canonical テキスト（architecture §2-3(d) SSOT）のみ。
これは散文かつ backtick を含まない規約のため、通常は許容リストに頼らず「散文は拾わない」
ルールだけで安全だが、保険として登録する。

Red フェーズ: 本リリース対象の置換（c3 run 化）が未実施のため、settings.json / SKILL.md /
agents/*.md に無印 python 起動子が多数残っており、このテストは失敗する。これは機能未実装
（置換タスク未着手）による正しい失敗であり、テスト自体の欠陥ではない。
置換タスク（impl-settings-replace 等）完了後に Green化する。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_DIR = REPO_ROOT / ".claude"
SETTINGS_JSON = CLAUDE_DIR / "settings.json"
SKILLS_DIR = CLAUDE_DIR / "skills"
AGENTS_DIR = CLAUDE_DIR / "agents"

# 配布対象外（git 非追跡・自律モード skill は開発中のため除外）
EXCLUDED_SKILL_DIR_NAMES = {"autonomous-mode"}

# ---------------------------------------------------------------------------
# 許容リスト（DC-AM-002: 安定部分文字列の部分一致）
# ---------------------------------------------------------------------------
# report-timestamp SKILL.md の退避文言 canonical テキスト（architecture §2-3(d) SSOT）の
# 安定部分文字列。この文言はプレーン散文かつ backtick 非使用の規約のため、通常は
# 「散文は拾わない」ルールのみで既に非検出だが、保険として明示登録する。
ALLOWLIST_SUBSTRINGS: list[str] = [
    "c3 doctor で環境を確認",
]

# ---------------------------------------------------------------------------
# 検出ロジック
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^\s*```")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
# 行頭（lstrip 後）が "python" + (空白 or 行末) で始まる = python / python -m / python -c を包含
_LAUNCHER_HEAD_RE = re.compile(r"^python(?=\s|$)")
# パイプ直後の python 起動（"| python ..."）
_LAUNCHER_PIPE_RE = re.compile(r"\|\s*python(?=\s|$)")
# && / 単独 & / ; / ` / $( の直後の python 起動（複合コマンド形式）。
# alternation は `&&` を先に置き、単独 `&` は `&(?!&)`（次が & でない = バックグラウンド区切り）で
# 拾う。`&&` を先頭に置くことで `&&` を単独 & 2 個として誤重複マッチさせない
# （`&(?!&)` は `&&` の 1 個目でも lookahead が失敗するため、順序に依らず安全だが可読性のため && を先頭に置く）。
_LAUNCHER_COMPOUND_RE = re.compile(r"(?:&&|&(?!&)|;|`|\$\()\s*python(?=\s|$)")

Violation = tuple[str, int, str]  # (file, line_no, line_text)


def _is_allowed(raw_line: str) -> bool:
    return any(s in raw_line for s in ALLOWLIST_SUBSTRINGS)


def _looks_like_launcher(text: str, *, full_line: str | None = None) -> bool:
    """text（コードブロック行 or インラインスパン内容）が python 起動形か判定する。"""
    stripped = text.lstrip()
    if _LAUNCHER_HEAD_RE.match(stripped):
        return True
    haystack = full_line if full_line is not None else text
    if _LAUNCHER_PIPE_RE.search(haystack):
        return True
    if _LAUNCHER_COMPOUND_RE.search(haystack):
        return True
    return False


def find_markdown_violations(path: Path) -> list[Violation]:
    """SKILL.md / agents/*.md 1 ファイルから無印 python 起動子の残存を検出する。

    - フェンス付きコードブロック内: 行頭 python / python -m / python -c、パイプ後の | python
    - フェンス外のインラインコードスパン（単一バッククォート）: 上記と同じ形
    - 上記いずれのコンテキストにも属さないプレーン散文中の "python" は検出しない
    """
    violations: list[Violation] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    in_fence = False
    for i, line in enumerate(lines, start=1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if _is_allowed(line):
            continue
        if in_fence:
            if _looks_like_launcher(line.lstrip(), full_line=line):
                violations.append((str(path), i, line.strip()))
            continue
        # フェンス外: インラインコードスパンのみ検査（散文は無視）
        for span in _INLINE_CODE_RE.findall(line):
            if _looks_like_launcher(span, full_line=span):
                violations.append((str(path), i, line.strip()))
                break
    return violations


def find_settings_json_violations(path: Path) -> list[Violation]:
    """.claude/settings.json から無印 python 起動子の残存を検出する。

    - hooks 各エントリの "command": "python"（bare exact 値）
    - statusLine.command の "python ..." 埋め込み形（先頭トークンが python）
    - permissions.allow の "Bash(python ..." パターン
    """
    violations: list[Violation] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines, start=1):
        if _is_allowed(line):
            continue
        stripped = line.strip()
        # hooks の command 値（bare "python"）
        if re.search(r'"command"\s*:\s*"python"\s*,?\s*$', stripped):
            violations.append((str(path), i, stripped))
            continue
        # statusLine.command の埋め込み形（"python " で始まる文字列値）
        if re.search(r'"command"\s*:\s*"python\s', stripped):
            violations.append((str(path), i, stripped))
            continue
        # permissions.allow の "Bash(python ..." パターン
        if re.search(r'"Bash\(python\s', stripped):
            violations.append((str(path), i, stripped))
            continue
    return violations


def _iter_skill_md_files() -> list[Path]:
    if not SKILLS_DIR.exists():
        return []
    files = sorted(SKILLS_DIR.glob("**/SKILL.md"))
    return [
        f
        for f in files
        if not (EXCLUDED_SKILL_DIR_NAMES & set(f.relative_to(SKILLS_DIR).parts))
    ]


def _iter_agent_md_files() -> list[Path]:
    if not AGENTS_DIR.exists():
        return []
    return sorted(AGENTS_DIR.glob("*.md"))


def collect_all_violations() -> list[Violation]:
    violations: list[Violation] = []
    if SETTINGS_JSON.exists():
        violations.extend(find_settings_json_violations(SETTINGS_JSON))
    for f in _iter_skill_md_files():
        violations.extend(find_markdown_violations(f))
    for f in _iter_agent_md_files():
        violations.extend(find_markdown_violations(f))
    return violations


def _format_violations(violations: list[Violation]) -> str:
    return "\n".join(f"  {file}:{line_no}: {text}" for file, line_no, text in violations)


# ---------------------------------------------------------------------------
# メインテスト（Red: 現状は置換未実施のため多数の違反を検出して失敗する）
# ---------------------------------------------------------------------------


class TestNoBarePythonLauncherInDistribution:
    """配布物（settings.json / SKILL.md / agents/*.md）に無印 python 起動子が残らないこと。"""

    def test_settings_json_has_no_bare_python_launcher(self):
        assert SETTINGS_JSON.exists(), f"{SETTINGS_JSON} が見つかりません"
        violations = find_settings_json_violations(SETTINGS_JSON)
        assert not violations, (
            "settings.json に無印 python 起動子が残っています"
            "（c3 run 形式へ置換してください。architecture §2-2 参照）。\n"
            "検出箇所:\n" + _format_violations(violations)
        )

    def test_skill_md_files_have_no_bare_python_launcher(self):
        files = _iter_skill_md_files()
        assert files, "SKILL.md が1件も見つかりません（走査対象パスの確認が必要）"
        violations: list[Violation] = []
        for f in files:
            violations.extend(find_markdown_violations(f))
        assert not violations, (
            "SKILL.md に無印 python 起動子が残っています"
            "（c3 run 形式へ置換してください。architecture §2-3 参照）。\n"
            "検出箇所:\n" + _format_violations(violations)
        )

    def test_agents_md_files_have_no_bare_python_launcher(self):
        files = _iter_agent_md_files()
        assert files, "agents/*.md が1件も見つかりません（走査対象パスの確認が必要）"
        violations: list[Violation] = []
        for f in files:
            violations.extend(find_markdown_violations(f))
        assert not violations, (
            "agents/*.md に無印 python 起動子が残っています"
            "（c3 run -m 形式へ置換してください。architecture §2-3 参照）。\n"
            "検出箇所:\n" + _format_violations(violations)
        )

    def test_no_violations_anywhere_in_distribution(self):
        """3 集合を合算した最終確認。file:line 一覧を assert メッセージに含める。"""
        violations = collect_all_violations()
        assert not violations, (
            f"配布物に無印 python 起動子が {len(violations)} 件残存しています。\n"
            "置換漏れの特定用 file:line 一覧:\n" + _format_violations(violations)
        )


# ---------------------------------------------------------------------------
# autonomous-mode 除外の確認（除外ディレクトリが走査対象に含まれないこと）
# ---------------------------------------------------------------------------


class TestAutonomousModeExcluded:
    def test_autonomous_mode_dir_not_in_scanned_skill_files(self):
        files = _iter_skill_md_files()
        for f in files:
            assert "autonomous-mode" not in f.parts, (
                f"{f} は配布対象外の autonomous-mode 配下だが走査対象に含まれている"
            )


# ---------------------------------------------------------------------------
# 散文除外の実証（DC-AM-002: 散文中の python 語は拾わない）
# ---------------------------------------------------------------------------


class TestProseIsNotFlagged:
    """バッククォート・コードブロックに属さないプレーン散文中の "python" は検出しない。

    実例: report-timestamp/SKILL.md の "c3 run を第一とする。c3 が解決できない環境
    （venv 未 activate 等）では python3（無ければ python）で直接実行して退避し" という
    箇条書き行。"python" という文字列を含むが、これはバッククォートで囲われていない
    プレーン散文であり、実行文脈を持たない（architecture-report-20260718-114347.md
    §2-3(d) の canonical 退避文言）。
    """

    def test_report_timestamp_prose_mention_of_python_is_not_flagged(self):
        target = SKILLS_DIR / "report-timestamp" / "SKILL.md"
        assert target.exists(), f"{target} が見つかりません"
        violations = find_markdown_violations(target)
        flagged_lines = {line_no for _, line_no, _ in violations}
        # "c3 run を第一とする。c3 が解決できない環境（venv 未 activate 等）では
        # python3（無ければ python）で直接実行して退避し" の行がプレーン散文の
        # "python" 単独言及であるため検出対象に含まれないことを確認する。
        prose_line_no = None
        for i, line in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
            if "c3 が解決できない環境" in line:
                prose_line_no = i
                break
        assert prose_line_no is not None, (
            "report-timestamp/SKILL.md の想定プレーン散文行が見つかりません"
            "（文言が変更された場合はテストの前提を更新すること）"
        )
        assert prose_line_no not in flagged_lines, (
            f"プレーン散文行 {prose_line_no} が誤って検出されました: "
            f"{_format_violations(violations)}"
        )

    def test_synthetic_prose_python_mention_not_flagged(self, tmp_path):
        md = tmp_path / "SKILL.md"
        md.write_text(
            "# example\n\n"
            "このスクリプトは python で書かれており、依存は標準ライブラリのみです。\n",
            encoding="utf-8",
        )
        assert find_markdown_violations(md) == []

    def test_synthetic_json_prose_mention_not_flagged(self):
        """mcp-config SKILL.md の JSON 例示中の「python」言及（実行形でない）は検出しない。"""
        target = SKILLS_DIR / "mcp-config" / "SKILL.md"
        if not target.exists():
            pytest.skip("mcp-config SKILL.md が見つかりません")
        violations = find_markdown_violations(target)
        flagged_lines = {line_no for _, line_no, _ in violations}
        for i, line in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
            if "node / python / バイナリ等" in line:
                assert i not in flagged_lines, (
                    f"JSON 例示中の非実行形 python 言及が誤って検出されました: line {i}"
                )
                return
        pytest.skip("対象行が見つからないため前提が変化している可能性があります")


# ---------------------------------------------------------------------------
# 検出ロジック単体テスト（合成ケース・Green後もリグレッションガードとして機能させる）
# ---------------------------------------------------------------------------


class TestDetectorUnitCasesForMarkdown:
    def test_detects_bare_python_line_head_in_fenced_code_block(self, tmp_path):
        md = tmp_path / "SKILL.md"
        md.write_text(
            "before\n```bash\npython .claude/hooks/foo.py\n```\nafter\n",
            encoding="utf-8",
        )
        violations = find_markdown_violations(md)
        assert len(violations) == 1
        assert violations[0][1] == 3

    def test_detects_python_dash_m_in_fenced_code_block(self, tmp_path):
        md = tmp_path / "SKILL.md"
        md.write_text(
            "```bash\npython -m py_compile foo.py\n```\n",
            encoding="utf-8",
        )
        violations = find_markdown_violations(md)
        assert len(violations) == 1

    def test_detects_python_dash_c_in_fenced_code_block(self, tmp_path):
        md = tmp_path / "SKILL.md"
        md.write_text(
            '```bash\npython -c "print(1)"\n```\n',
            encoding="utf-8",
        )
        violations = find_markdown_violations(md)
        assert len(violations) == 1

    def test_detects_piped_python_in_fenced_code_block(self, tmp_path):
        md = tmp_path / "SKILL.md"
        md.write_text(
            "```bash\ngrep foo bar.tmp | python .claude/hooks/mode_line.py\n```\n",
            encoding="utf-8",
        )
        violations = find_markdown_violations(md)
        assert len(violations) == 1

    def test_detects_python_in_inline_code_span(self, tmp_path):
        md = tmp_path / "SKILL.md"
        md.write_text(
            "- Python ファイルを変更した場合: `python -m py_compile foo.py` を実行する\n",
            encoding="utf-8",
        )
        violations = find_markdown_violations(md)
        assert len(violations) == 1

    def test_detects_piped_python_in_inline_code_span(self, tmp_path):
        md = tmp_path / "SKILL.md"
        md.write_text(
            "> `grep foo bar.tmp | python .claude/hooks/mode_line.py`\n",
            encoding="utf-8",
        )
        violations = find_markdown_violations(md)
        assert len(violations) == 1

    def test_detects_python_after_ampersand_and(self, tmp_path):
        """&& の直後の python 起動（複合コマンド形式）を検出する。"""
        md = tmp_path / "SKILL.md"
        md.write_text(
            "```bash\n(cd .claude/hooks && python -c \"print('test')\")\n```\n",
            encoding="utf-8",
        )
        violations = find_markdown_violations(md)
        assert len(violations) == 1

    def test_detects_python_after_single_ampersand(self, tmp_path):
        """単独 &（バックグラウンド区切り）の直後の python 起動を検出する。"""
        md = tmp_path / "SKILL.md"
        md.write_text(
            "```bash\nsome_daemon & python .claude/hooks/foo.py\n```\n",
            encoding="utf-8",
        )
        violations = find_markdown_violations(md)
        assert len(violations) == 1

    def test_single_ampersand_detection_does_not_break_double_ampersand(self, tmp_path):
        """単独 & 対応（&(?!&)）を追加しても既存の && 検出が壊れず、
        && 行が単独 & 2 個として誤重複マッチしない（1 行 = 1 検出）ことの自己検証。
        """
        md = tmp_path / "SKILL.md"
        md.write_text(
            "```bash\n(cd .claude/hooks && python -c \"print('x')\")\n```\n",
            encoding="utf-8",
        )
        violations = find_markdown_violations(md)
        assert len(violations) == 1
        # 正規表現レベルでも && が単独 & として二重解釈されないことを直接確認する。
        assert len(_LAUNCHER_COMPOUND_RE.findall("cmd && python foo.py")) == 1

    def test_detects_python_after_semicolon(self, tmp_path):
        """; の直後の python 起動（複合コマンド形式）を検出する。"""
        md = tmp_path / "SKILL.md"
        md.write_text(
            "```bash\necho done; python .claude/hooks/foo.py\n```\n",
            encoding="utf-8",
        )
        violations = find_markdown_violations(md)
        assert len(violations) == 1

    def test_detects_python_after_backtick(self, tmp_path):
        """`` ` `` によるコマンド置換内の python 起動を検出する。"""
        md = tmp_path / "SKILL.md"
        md.write_text(
            "```bash\nresult=`python .claude/hooks/foo.py`\n```\n",
            encoding="utf-8",
        )
        violations = find_markdown_violations(md)
        assert len(violations) == 1

    def test_detects_python_after_dollar_paren(self, tmp_path):
        """$( によるコマンド置換内の python 起動を検出する。"""
        md = tmp_path / "SKILL.md"
        md.write_text(
            "```bash\nresult=$(python .claude/hooks/foo.py)\n```\n",
            encoding="utf-8",
        )
        violations = find_markdown_violations(md)
        assert len(violations) == 1

    def test_python3_launcher_is_not_flagged(self, tmp_path):
        """python3 は退避経路として許容される起動子であり、無印 python の検出対象外。"""
        md = tmp_path / "SKILL.md"
        md.write_text(
            "```bash\npython3 .claude/hooks/foo.py\n```\n",
            encoding="utf-8",
        )
        assert find_markdown_violations(md) == []

    def test_c3_run_launcher_is_not_flagged(self, tmp_path):
        """置換後（Green）の c3 run 形式は検出対象外。"""
        md = tmp_path / "SKILL.md"
        md.write_text(
            "```bash\nc3 run .claude/hooks/foo.py\n```\n",
            encoding="utf-8",
        )
        assert find_markdown_violations(md) == []

    def test_allowlist_substring_suppresses_violation(self, tmp_path):
        """許容リストの安定部分文字列を含む行は、実行文脈（インラインコードスパン）
        であっても検出対象から除外されることを確認する（保険としての許容リスト機能）。
        """
        md = tmp_path / "SKILL.md"
        md.write_text(
            "- 退避時は `python foo.py` を実行し、c3 doctor で環境を確認する\n",
            encoding="utf-8",
        )
        assert find_markdown_violations(md) == []


class TestDetectorUnitCasesForSettingsJson:
    def test_detects_bare_python_hook_command(self, tmp_path):
        settings = tmp_path / "settings.json"
        settings.write_text(
            '{\n  "hooks": {\n    "Stop": [\n      {\n        "hooks": [\n'
            '          {\n            "type": "command",\n'
            '            "command": "python",\n'
            '            "args": ["foo.py"]\n          }\n        ]\n      }\n    ]\n  }\n}\n',
            encoding="utf-8",
        )
        violations = find_settings_json_violations(settings)
        assert len(violations) == 1

    def test_detects_statusline_bare_python(self, tmp_path):
        settings = tmp_path / "settings.json"
        settings.write_text(
            '{\n  "statusLine": {\n    "type": "command",\n'
            '    "command": "python \\"${CLAUDE_PROJECT_DIR}/.claude/hooks/statusline.py\\""\n  }\n}\n',
            encoding="utf-8",
        )
        violations = find_settings_json_violations(settings)
        assert len(violations) == 1

    def test_detects_permissions_allow_bash_python_pattern(self, tmp_path):
        settings = tmp_path / "settings.json"
        settings.write_text(
            '{\n  "permissions": {\n    "allow": [\n'
            '      "Bash(python .claude/hooks/stop.py*)"\n    ]\n  }\n}\n',
            encoding="utf-8",
        )
        violations = find_settings_json_violations(settings)
        assert len(violations) == 1

    def test_c3_run_settings_forms_are_not_flagged(self, tmp_path):
        settings = tmp_path / "settings.json"
        settings.write_text(
            '{\n  "statusLine": {\n    "command": "c3 run \\"${CLAUDE_PROJECT_DIR}/statusline.py\\""\n  },\n'
            '  "hooks": {\n    "Stop": [\n      {\n        "hooks": [\n'
            '          {\n            "command": "c3",\n'
            '            "args": ["run", "foo.py"]\n          }\n        ]\n      }\n    ]\n  },\n'
            '  "permissions": {\n    "allow": [\n'
            '      "Bash(c3 run .claude/hooks/stop.py*)"\n    ]\n  }\n}\n',
            encoding="utf-8",
        )
        assert find_settings_json_violations(settings) == []
