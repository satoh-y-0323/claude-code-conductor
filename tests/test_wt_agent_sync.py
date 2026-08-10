"""wt_* 並列 worktree バリアントと本体 agent 定義の同期検査（静的テスト）。

## 検査する性質

`.claude/agents/` の本体 3 本（`developer.md` / `systematic-debugger.md` /
`tester.md`）と、対応する並列 worktree バリアント 3 本（`wt_developer.md` /
`wt_systematic-debugger.md` / `wt_tester.md`）の **行単位 diff の差分行すべて**が、
下記「既知の意図的変換 9 カテゴリ」のいずれかの許容パターンに一致すること。

一致しない差分行が 1 行でもあれば、それは本体側の更新が wt_* 側に反映されていない
（あるいはその逆の）**同期漏れ**なので、ファイル名・行番号・行内容を列挙して fail させる。

## 既知の意図的変換 9 カテゴリ

| # | 変換 | 許容パターン（実装は本ファイルの PAIR_RULES / INSERT_RULES / DELETE_RULES） |
|---|---|---|
| 1 | frontmatter `name` の `wt_` プレフィックス | `name: {base}` → `name: wt_{base}`（完全一致） |
| 2 | `permissionMode: bypassPermissions` の追加 | 追加行が当該文字列と完全一致 |
| 3 | `description` の「並列 worktree 専用」差し替え | `description: ` 行 → `description: 並列 worktree 専用 {base}。...` |
| 4 | H1 見出しの `(worktree-parallel)` 付与と直後の blockquote 段落挿入 | H1 は `{旧見出し} (worktree-parallel)` と完全一致。blockquote は「空行 + 定型 1 行目 + `>` + 単発起動段落」の 4 行ブロック |
| 5 | Memory 書き込み先パスの `wt_` 化 | `.claude/agent-memory/{base}/` → `.claude/agent-memory/wt_{base}/` 置換のみで一致 |
| 6 | レポート出力の task_id ベース化 | 旧: timestamp 機構への言及行 / 新: `.claude/reports/*-{task_id}.md` を含む行 |
| 7 | 本文中のピア agent 名の `wt_` 化 | 既知 agent 名（3 本）を `wt_` 化する置換のみで一致 |
| 8 | 「- 直接起動版: ...」行の追加 | `` - 直接起動版: `{base}` (worktree なしの単発実行向け) `` と完全一致 |
| 9 | 「保険（task_id が読み取れない異常系のみ）」条項の追加 | 保険条項の必須語（保険見出し・`report-timestamp`・`{timestamp}` レポートパス・「通常運用ではこの経路に入ってはいけない」）を全て含む |

## 過剰な許容をしない設計

- 変換 1 / 2 / 4(H1) / 8 は**完全一致**。可変部は base agent 名のみ。
- 変換 4 の blockquote は 1 行目を定型文字列との完全一致で縛り、単発起動段落も
  ``元の `{base}` agent を使うこと。`` で終わることを要求する（任意の `> ` 行は通さない）。
- 変換 5 / 7 は「置換を適用したら他方と完全一致する」という関係で判定するので、
  同じ行に別の編集が同居していれば一致せず検出される。
- 変換 3 / 6 / 9 は日本語本文が変わるため関係式で縛れない。代わりに
  「新旧それぞれが当該カテゴリ固有のトークンを含むこと」を要求する。
  特に変換 6 の新側は `.claude/reports/<name>-{task_id}.md` というパス形状を必須にしている。
- どのルールも「任意の 1 行を通す」ワイルドカードを持たない。したがって本文の
  記述変更・句読点の全角/半角ゆれ・箇条書きの追加削除は、すべて未分類として fail する。

## 既知の限界

- 行単位の判定なので、ある行が「変換 6 の対象であり、かつ同じ行に同期漏れも含む」
  場合、変換 6 として通ってしまう可能性がある（変換 3 / 6 / 9 のみ）。
- DELETE_RULES は timestamp 機構に言及する行の削除を無条件に許容する。これは
  変換 6 が「旧 2 行 → 新 1 行」に畳む形を取るため（`wt_developer.md` の Stuck Signal）。
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths & Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / ".claude" / "agents"

#: 本体 agent（stem）と wt_* バリアントの対応。wt 側は "wt_" + stem。
BASE_AGENTS: tuple[str, ...] = ("developer", "systematic-debugger", "tester")

CATEGORY_NAMES: dict[int, str] = {
    1: "frontmatter name の wt_ プレフィックス",
    2: "permissionMode: bypassPermissions の追加",
    3: "description の「並列 worktree 専用」差し替え",
    4: "H1 の (worktree-parallel) 付与と blockquote 段落挿入",
    5: "Memory 書き込み先パスの wt_ 化",
    6: "レポート出力の task_id ベース化",
    7: "本文中のピア agent 名の wt_ 化",
    8: "「- 直接起動版: ...」行の追加",
    9: "「保険（異常系のみ）」条項の追加",
}

#: 変換 4 の blockquote 1 行目（3 ファイルで完全一致することを実測済み）
BANNER_LINE_1 = (
    "> 本 agent は `parallel-agents` skill が `isolation: \"worktree\"` 付きで起動する "
    "**並列実行専用** バリアント。`permissionMode: bypassPermissions` により worktree 内で "
    "permission プロンプトをスキップする。worktree 外への書き込みは "
    "`.claude/hooks/worktree_guard.py` (PreToolUse, `PO_WORKTREE_GUARD=1`) でガードされる。"
)
BANNER_LINE_2 = ">"

PERMISSION_MODE_LINE = "permissionMode: bypassPermissions"
H1_SUFFIX = " (worktree-parallel)"

#: 変換 6 / 9 で「timestamp 機構への言及」と判定するトークン
TIMESTAMP_MARKERS = ("report-timestamp", "{timestamp}", "YYYYMMDD-HHMMSS")

#: 変換 6 の新側が必ず持つレポートパス形状
REPORT_TASK_ID_RE = re.compile(r"\.claude/reports/[A-Za-z][A-Za-z-]*-\{task_id\}\.md")
#: 変換 9 の保険条項が必ず持つレポートパス形状
REPORT_TIMESTAMP_RE = re.compile(r"\.claude/reports/[A-Za-z][A-Za-z-]*-\{timestamp\}\.md")

#: 変換 9 の保険条項に必須の語
FALLBACK_CLAUSE_TOKENS = (
    "保険（task_id がプロンプトから読み取れない異常系のみ）",
    "report-timestamp",
    "通常運用ではこの経路に入ってはいけない",
)

#: 変換 5 / 7 で wt_ 化する既知 agent 名（長いものから当てる）
_AGENT_NAME_RE = re.compile(
    r"(?<![\w-])(" + "|".join(sorted(BASE_AGENTS, key=len, reverse=True)) + r")(?![\w-])"
)


# ---------------------------------------------------------------------------
# 検出ロジック（純粋関数）
# ---------------------------------------------------------------------------


def wtize(line: str) -> str:
    """行中の既知 agent 名を `wt_` プレフィックス付きに置換する。"""
    return _AGENT_NAME_RE.sub(lambda m: "wt_" + m.group(1), line)


def _has_timestamp_marker(line: str) -> bool:
    return any(marker in line for marker in TIMESTAMP_MARKERS)


def _banner_line_3_re(base: str) -> re.Pattern[str]:
    return re.compile(
        r"^> 単発起動（.+）では本 agent を\*\*使わない\*\*。元の `"
        + re.escape(base)
        + r"` agent を使うこと。$"
    )


# --- pair rules: 本体側 1 行 <-> wt 側 1 行 の対応で説明できる変換 ------------


def _rule_name(removed: str, added: str, base: str) -> bool:
    return removed == f"name: {base}" and added == f"name: wt_{base}"


def _rule_description(removed: str, added: str, base: str) -> bool:
    return removed.startswith("description: ") and added.startswith(
        f"description: 並列 worktree 専用 {base}。"
    )


def _rule_h1(removed: str, added: str, base: str) -> bool:
    return removed.startswith("# ") and added == removed + H1_SUFFIX


def _rule_memory_path(removed: str, added: str, base: str) -> bool:
    old_path = f".claude/agent-memory/{base}/MEMORY.md"
    if old_path not in removed:
        return False
    new_path = f".claude/agent-memory/wt_{base}/MEMORY.md"
    return added == removed.replace(old_path, new_path)


def _rule_report_task_id(removed: str, added: str, base: str) -> bool:
    return _has_timestamp_marker(removed) and bool(REPORT_TASK_ID_RE.search(added))


def _rule_peer_rename(removed: str, added: str, base: str) -> bool:
    return added != removed and added == wtize(removed)


PAIR_RULES: tuple[tuple[int, object], ...] = (
    (1, _rule_name),
    (3, _rule_description),
    (4, _rule_h1),
    (5, _rule_memory_path),
    (6, _rule_report_task_id),
    (7, _rule_peer_rename),
)


# --- insert rules: wt 側にのみ現れる行 ---------------------------------------


def _rule_permission_mode(added: str, base: str) -> bool:
    return added == PERMISSION_MODE_LINE


def _rule_direct_variant_line(added: str, base: str) -> bool:
    return added == f"- 直接起動版: `{base}` (worktree なしの単発実行向け)"


def _rule_fallback_clause(added: str, base: str) -> bool:
    if not all(token in added for token in FALLBACK_CLAUSE_TOKENS):
        return False
    return bool(REPORT_TIMESTAMP_RE.search(added))


INSERT_RULES: tuple[tuple[int, object], ...] = (
    (2, _rule_permission_mode),
    (8, _rule_direct_variant_line),
    (9, _rule_fallback_clause),
)


# --- delete rules: 本体側にのみ残る行 ----------------------------------------


def _rule_timestamp_line_removed(removed: str, base: str) -> bool:
    """変換 6 で畳まれた timestamp 機構の手順行。"""
    return _has_timestamp_marker(removed)


DELETE_RULES: tuple[tuple[int, object], ...] = ((6, _rule_timestamp_line_removed),)


@dataclass
class Unmatched:
    """既知変換に分類できなかった差分行。"""

    side: str  # "base" | "wt"
    filename: str
    lineno: int  # 1-origin
    text: str

    def render(self) -> str:
        sign = "-" if self.side == "base" else "+"
        return f"  {sign} {self.filename}:{self.lineno}: {self.text}"


@dataclass
class DiffAnalysis:
    unmatched: list[Unmatched] = field(default_factory=list)
    categories: set[int] = field(default_factory=set)
    changed_line_count: int = 0


def _find_banner_block(added: list[str], base: str) -> int | None:
    """挿入行の中から blockquote バナー 4 行ブロック（空行 + 3 行）の開始位置を返す。"""
    line3_re = _banner_line_3_re(base)
    for k in range(len(added) - 3):
        if (
            added[k] == ""
            and added[k + 1] == BANNER_LINE_1
            and added[k + 2] == BANNER_LINE_2
            and line3_re.match(added[k + 3])
        ):
            return k
    return None


def analyze_pair(
    base_lines: list[str],
    wt_lines: list[str],
    base: str,
    base_filename: str = "",
    wt_filename: str = "",
) -> DiffAnalysis:
    """本体 / wt の行リストを比較し、既知変換に分類できない差分行を返す。"""
    base_filename = base_filename or f"{base}.md"
    wt_filename = wt_filename or f"wt_{base}.md"

    analysis = DiffAnalysis()
    matcher = difflib.SequenceMatcher(a=base_lines, b=wt_lines, autojunk=False)

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed = base_lines[i1:i2]
        added = wt_lines[j1:j2]
        analysis.changed_line_count += len(removed) + len(added)

        pending_rem = dict(enumerate(removed))
        pending_add = dict(enumerate(added))

        # 変換 4: blockquote バナー 4 行ブロック（空行を含むため個別行ルールにしない）
        banner_start = _find_banner_block(added, base)
        if banner_start is not None:
            for offset in range(4):
                pending_add.pop(banner_start + offset, None)
            analysis.categories.add(4)

        for category, rule in PAIR_RULES:
            for i in sorted(pending_rem):
                if i not in pending_rem:
                    continue
                for j in sorted(pending_add):
                    if rule(pending_rem[i], pending_add[j], base):
                        pending_rem.pop(i)
                        pending_add.pop(j)
                        analysis.categories.add(category)
                        break

        for category, rule in INSERT_RULES:
            for j in sorted(pending_add):
                if rule(pending_add[j], base):
                    pending_add.pop(j)
                    analysis.categories.add(category)

        for category, rule in DELETE_RULES:
            for i in sorted(pending_rem):
                if rule(pending_rem[i], base):
                    pending_rem.pop(i)
                    analysis.categories.add(category)

        for i in sorted(pending_rem):
            analysis.unmatched.append(
                Unmatched("base", base_filename, i1 + i + 1, pending_rem[i])
            )
        for j in sorted(pending_add):
            analysis.unmatched.append(
                Unmatched("wt", wt_filename, j1 + j + 1, pending_add[j])
            )

    return analysis


# ---------------------------------------------------------------------------
# ファイル読み込みヘルパ
# ---------------------------------------------------------------------------


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _pair_paths(base: str) -> tuple[Path, Path]:
    return AGENTS_DIR / f"{base}.md", AGENTS_DIR / f"wt_{base}.md"


def _analyze_real_pair(base: str) -> DiffAnalysis:
    base_path, wt_path = _pair_paths(base)
    return analyze_pair(
        _read_lines(base_path), _read_lines(wt_path), base, base_path.name, wt_path.name
    )


def _format_failure(base: str, analysis: DiffAnalysis) -> str:
    header = (
        f"{base}.md と wt_{base}.md の差分に、既知の意図的変換 9 カテゴリで"
        f"説明できない行が {len(analysis.unmatched)} 件あります（同期漏れの疑い）:"
    )
    return "\n".join([header, *(u.render() for u in analysis.unmatched)])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFixtureSanity:
    """検査対象そのものの番兵。glob 書き損じ・ファイル欠落で空の緑にならないようにする。"""

    def test_all_agent_pairs_exist_and_are_non_empty(self):
        assert len(BASE_AGENTS) == 3, "検査対象は本体 3 本 / wt_* 3 本の想定"
        for base in BASE_AGENTS:
            base_path, wt_path = _pair_paths(base)
            assert base_path.is_file(), f"{base_path} が存在しない"
            assert wt_path.is_file(), f"{wt_path} が存在しない"
            assert _read_lines(base_path), f"{base_path} が空"
            assert _read_lines(wt_path), f"{wt_path} が空"

    def test_pairs_actually_differ(self):
        """全ペアに差分が存在すること（差分ゼロなら本検査は何も検証していない）。"""
        for base in BASE_AGENTS:
            analysis = _analyze_real_pair(base)
            assert analysis.changed_line_count > 0, (
                f"{base}.md と wt_{base}.md に差分が無い。"
                "wt_* バリアントの意図的変換が失われている可能性がある"
            )


class TestWtAgentSync:
    """本検査: 差分行がすべて既知の意図的変換で説明できること。"""

    @pytest.mark.parametrize("base", BASE_AGENTS)
    def test_diff_only_contains_known_transformations(self, base: str):
        analysis = _analyze_real_pair(base)
        assert not analysis.unmatched, _format_failure(base, analysis)

    def test_all_nine_categories_are_exercised(self):
        """9 カテゴリすべてが実ファイルで少なくとも 1 回使われていること。

        使われていないカテゴリがあれば、その許容パターンは（実態と合っていない
        ために）機能していないか、対応する意図的変換が失われている。
        """
        seen: set[int] = set()
        for base in BASE_AGENTS:
            seen |= _analyze_real_pair(base).categories
        missing = sorted(set(CATEGORY_NAMES) - seen)
        assert not missing, "実ファイルで一度も一致しなかったカテゴリ: " + ", ".join(
            f"{c}({CATEGORY_NAMES[c]})" for c in missing
        )


class TestDetectionPower:
    """検知力の実証（過剰な許容をしていないことの確認）。

    負の対照は「同期済みの組が緑」（`developer` / `systematic-debugger` ペア）。
    正の対照はここで注入するドリフト。
    """

    NEGATIVE_CONTROL = "developer"

    def _synced_lines(self) -> tuple[list[str], list[str]]:
        base_path, wt_path = _pair_paths(self.NEGATIVE_CONTROL)
        return _read_lines(base_path), _read_lines(wt_path)

    def test_negative_control_synced_pair_is_clean(self):
        base_lines, wt_lines = self._synced_lines()
        analysis = analyze_pair(base_lines, wt_lines, self.NEGATIVE_CONTROL)
        assert not analysis.unmatched

    def test_detects_body_text_drift(self):
        """wt 側の本文 1 行を書き換えたら検出されること。"""
        base_lines, wt_lines = self._synced_lines()
        target = next(
            i for i, line in enumerate(wt_lines) if line.startswith("## Core Mandate")
        )
        mutated = list(wt_lines)
        mutated[target + 1] = mutated[target + 1] + "（本体に無い追記）"
        analysis = analyze_pair(base_lines, mutated, self.NEGATIVE_CONTROL)
        assert analysis.unmatched

    def test_detects_punctuation_width_drift(self):
        """句読点の全角/半角ゆれ（実在した同期漏れと同型）を検出できること。"""
        base_lines, wt_lines = self._synced_lines()
        target = next(
            i for i, line in enumerate(wt_lines) if "（" in line and "）" in line
        )
        mutated = list(wt_lines)
        mutated[target] = mutated[target].replace("）", ")", 1)
        analysis = analyze_pair(base_lines, mutated, self.NEGATIVE_CONTROL)
        assert analysis.unmatched

    def test_detects_arbitrary_inserted_line(self):
        """wt 側だけに任意の行が増えたら検出されること（任意行を通さない）。"""
        base_lines, wt_lines = self._synced_lines()
        mutated = list(wt_lines)
        mutated.append("- 本体側に存在しない任意の追記行")
        analysis = analyze_pair(base_lines, mutated, self.NEGATIVE_CONTROL)
        assert analysis.unmatched

    def test_detects_dropped_line(self):
        """本体側にある行が wt 側で欠落したら検出されること。"""
        base_lines, wt_lines = self._synced_lines()
        target = next(
            i for i, line in enumerate(wt_lines) if line.startswith("## Key Scope")
        )
        mutated = wt_lines[:target] + wt_lines[target + 1 :]
        analysis = analyze_pair(base_lines, mutated, self.NEGATIVE_CONTROL)
        assert analysis.unmatched

    def test_banner_rule_rejects_altered_banner(self):
        """blockquote バナーが改変されたら「任意の `> ` 行」として通さないこと。"""
        base_lines, wt_lines = self._synced_lines()
        target = next(i for i, line in enumerate(wt_lines) if line == BANNER_LINE_1)
        mutated = list(wt_lines)
        mutated[target] = "> 適当に書き換えたバナー行"
        analysis = analyze_pair(base_lines, mutated, self.NEGATIVE_CONTROL)
        assert analysis.unmatched

    def test_name_rule_rejects_wrong_prefix(self):
        """`name:` が別名に差し替わったら通さないこと。"""
        base_lines, wt_lines = self._synced_lines()
        mutated = [
            "name: wt_something-else" if line.startswith("name: ") else line
            for line in wt_lines
        ]
        analysis = analyze_pair(base_lines, mutated, self.NEGATIVE_CONTROL)
        assert analysis.unmatched


class TestWtizeHelper:
    """`wtize` が既に wt_ 化された名前を二重変換しないこと。"""

    def test_prefixes_known_agent_names(self):
        assert wtize("- ピア: tester（TDD）") == "- ピア: wt_tester（TDD）"
        assert (
            wtize("依頼元: systematic-debugger 経由") == "依頼元: wt_systematic-debugger 経由"
        )

    def test_does_not_double_prefix(self):
        assert wtize("- ピア: wt_tester（TDD）") == "- ピア: wt_tester（TDD）"

    def test_does_not_touch_unrelated_words(self):
        assert wtize("testers と developers") == "testers と developers"
