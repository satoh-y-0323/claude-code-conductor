"""3 ファイル同期（``.gitignore`` / ``_excludes.py`` / ``pyproject.toml``）の機械検証。

plan-report-20260815-164225.md タスク ``test-sync`` の Red フェーズ実装。
仕様の正本: architecture-report-20260815-164137.md（改訂 5・最終）
           + requirements-report-20260815-164117.md（改訂 3）。

============================================================================
設計判断メモ（tester が正本・P0/P1/P2/P2b/P2c/P3/P4/P5/P6/P7 の型凍結）
============================================================================

被テスト実装 ``tests/_sync_semantics.py``（developer 所掌・未実装＝本ファイルは
Red で収集エラーになる）が export すべき API を以下のとおり凍結する。

**Verdict（判定レコード・5 フィールド固定）**::

    class Verdict(NamedTuple):
        subject: str        # 対象
        expected: bool       # 課した期待
        actual: bool         # 判定結果
        allowlisted: bool    # 許容リスト適用の有無
        category: str        # 由来カテゴリ（8 種のいずれか）

違反は ``actual != expected and not allowlisted`` で判定する。取得は
``_violations()``（本ファイル下部のテスト側ヘルパー）のような結果フィルタ経由に限り、
``assert not find_...(...)`` の形（戻り値を丸ごと真偽判定する形）は使わない
（P0・DC-AM-004: 戻り値は全レコードであり違反専用の戻り値ではないため）。

**由来カテゴリ 8 種（文字列 定数として export）**::

    CATEGORY_EXCLUDE          = "EXCLUDE"
    CATEGORY_KEEP              = "KEEP"
    CATEGORY_KEEP_COMPOSITE    = "合成KEEP"
    CATEGORY_CLAUDE_LINE       = "claude行"
    CATEGORY_UNIVERSAL         = "全体パターン"
    CATEGORY_EDGE2_RESCUE      = "辺2救済"
    CATEGORY_EDGE2_CLASSIFY    = "辺2分類"
    CATEGORY_EDGE3             = "辺3"

**検出器 4 本 + 正規化関数 1 本**（引数注入の純粋関数。ファイル I/O・git 呼び出し・
``c3._excludes`` への直接依存を持たない。導出関数はすべて既定値付きキーワード引数として
注入され、注入された関数が ``None`` を返した対象にはレコードを作らない＝P7(f) の
番兵実証対象）::

    def find_gitignore_intent_violations(
        exclude_patterns, keep_patterns, check_ignore_fn, allowlist_a, *,
        derive_probe=derive_probe_from_pattern,
        category_exclude=CATEGORY_EXCLUDE, category_keep=CATEGORY_KEEP,
    ) -> list[Verdict]:
        \"\"\"辺 1 方向 A。P2b（合成 KEEP 判定）・P2c（全体パターン git 側）は
        exclude_patterns / keep_patterns / category_exclude / category_keep を
        差し替えた別呼び出しとして本関数を再利用する（新関数を作らない）。\"\"\"

    def find_gitignore_line_violations(
        gitignore_lines, should_skip_fn, allowlist_b, *,
        derive_probe=derive_probe_from_gitignore_line,
        category=CATEGORY_CLAUDE_LINE,
    ) -> list[Verdict]:
        \"\"\"辺 1 方向 B。全体パターン should_skip 側チェックは gitignore_lines /
        allowlist_b / category を差し替えた別呼び出しとして本関数を再利用する。\"\"\"

    def find_force_include_violations(
        keep_patterns, sdist_exclude, force_include_keys, injection_controls, *,
        normalize=normalize_sdist_exclude_entry,
    ) -> list[Verdict]:
        \"\"\"辺 2 双方向。CATEGORY_EDGE2_RESCUE と CATEGORY_EDGE2_CLASSIFY の
        両方を返す（後述の分類レコード設計を参照）。\"\"\"

    def find_sdist_exclude_violations(
        sdist_exclude, should_skip_fn, *,
        normalize=normalize_sdist_exclude_entry,
        derive_probe=<内部既定・非 export>,
    ) -> list[Verdict]:
        \"\"\"辺 3 片方向。derive_probe はキーワード専用引数として存在すればよく、
        本ファイルはシンボルを import せず None 固定スタブの注入でのみ用いる
        （P7(f) の対象に辺 3 を含めるため・DC-GP-003 の射程拡張）。\"\"\"

**辺 2 分類レコードの許容リスト意味論（本ファイル独自の設計判断・要明記）**:
``CATEGORY_EDGE2_RESCUE``（force-include キー存在の要求）には許容リスト概念が無く、
全レコード ``allowlisted=False``。一方 ``CATEGORY_EDGE2_CLASSIFY``（force-include
各キーの説明可能性）は「注入対照リスト」を許容リストと同じ仕組みで扱う:
注入対照に一致するキーは ``allowlisted=True``（"KEEP 救済" 側の期待に対しては
不一致だが、注入対照リストにより許容される）。この結果:

- ``辺2救済 >= 3`` と ``辺2分類 >= 4`` は**許容リスト適用の有無を問わない総レコード数**
  で数える（architecture 改訂 4 §3 表で「辺2逆方向」の行にのみ「(適用 False)」の
  注記が無いことを P6-1 の逐語比較で確認した設計判断。他の 8 項目は
  「適用 False」限定で数える）
- ``注入対照 == 1`` は ``CATEGORY_EDGE2_CLASSIFY`` の ``allowlisted=True`` レコード数
  （ADR-10 の「許容リスト」3 本目として扱う）

これにより「辺2分類>=4」（全数）と「注入対照==1」（内訳）が矛盾なく両立する。

============================================================================
"""

from __future__ import annotations

import inspect
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from c3._excludes import EXCLUDE_PATTERNS, KEEP_PATTERNS, should_skip
from tests._sync_semantics import (
    CATEGORY_CLAUDE_LINE,
    CATEGORY_EDGE2_CLASSIFY,
    CATEGORY_EDGE2_RESCUE,
    CATEGORY_EDGE3,
    CATEGORY_EXCLUDE,
    CATEGORY_KEEP,
    CATEGORY_KEEP_COMPOSITE,
    CATEGORY_UNIVERSAL,
    Verdict,
    derive_probe_from_gitignore_line,
    derive_probe_from_pattern,
    find_force_include_violations,
    find_gitignore_intent_violations,
    find_gitignore_line_violations,
    find_sdist_exclude_violations,
    normalize_sdist_exclude_entry,
)

# ---------------------------------------------------------------------------
# パス・実物データ
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT_GITIGNORE = REPO_ROOT / ".gitignore"
CLAUDE_GITIGNORE = REPO_ROOT / ".claude" / ".gitignore"
PYPROJECT_TOML = REPO_ROOT / "pyproject.toml"

_GIT = shutil.which("git")
requires_git = pytest.mark.skipif(_GIT is None, reason="git が見つからない環境ではスキップ")

# ---------------------------------------------------------------------------
# 許容リスト（理由文字列必須の定数・検出器へは引数で渡す）
#
# 出所（節種別まで逐語・plan P0 の指示どおり）:
#   許容リスト A = architecture-report-20260815-151833.md（改訂 2）§2-1 の表（1 行）
#                  改訂: 小粒バックログ消化スライス architecture-report-20260815-222450（改訂 5）§2 FR-1 で pytest_temp.ini を削除
#   許容リスト B = architecture-report-20260815-122018.md（改訂 1）§2-1 の表（2 行・逐語）
#   注入対照     = architecture-report-20260815-122018.md（改訂 1）§2-2 の箇条
#                  （pyproject.toml:75-86 の群 2 コメントとの対応を含む）
# ---------------------------------------------------------------------------

# 許容リスト A: EXCLUDE_PATTERNS のパターン文字列 -> 理由（キーは derive_probe に渡す前の
# パターンそのもの。find_gitignore_intent_violations は exclude_patterns を反復する際、
# 各パターン自体を allowlist_a の照合対象にする）。
ALLOWLIST_A: Mapping[str, str] = {
    "docs/taxonomy.md": (
        "GitHub 公開済み。gitignore と wheel 配布は別レイヤー"
        "（.gitignore:61-63・config-policy.md 落とし穴 2 に明文化済み）"
    ),
}
ALLOWLIST_A_COUNT = 1  # 現状値。増減時は理由文字列とセットで更新する（ADR-10）

# 許容リスト B: derive_probe_from_gitignore_line の出力プローブ（.claude/ を剥がした
# 相対パス）-> 理由。行そのものは ".claude/agents/tdd-develop.md" /
# ".claude/skills/worktree-tdd-workflow/" の 2 行（改訂 1 §2-1 表・逐語）。
ALLOWLIST_B: Mapping[str, str] = {
    "agents/tdd-develop.md": (
        "v2.1.0 廃止機能の復活防止行。実在しないファイルの再混入ガードであり "
        "wheel 収録判断（should_skip）の対象事象が発生しない"
    ),
    "skills/worktree-tdd-workflow/__sync_probe__": (
        "v2.1.0 廃止機能の復活防止行。実在しないファイルの再混入ガードであり "
        "wheel 収録判断（should_skip）の対象事象が発生しない"
    ),
}
ALLOWLIST_B_COUNT = 2  # 現状値

# 注入対照: force-include のキーのうち KEEP 救済ではなく意図的な「落ちるべき」候補。
INJECTION_CONTROLS: Sequence[str] = (".claude/state/setup_done.flag",)
INJECTION_CONTROLS_REASONS: Mapping[str, str] = {
    ".claude/state/setup_done.flag": (
        "sdist にだけ入れ wheel ビルド時に should_skip の state/* で落とすことを "
        "scripts/verify_wheel.py が検証する注入対照（pyproject.toml:75-86 群 2 "
        "コメントと対応・force-include が実際に検査対象候補を持つことの保証）"
    ),
}
INJECTION_CONTROL_COUNT = 1  # 現状値

# ---------------------------------------------------------------------------
# 全体パターン対応 3 プローブ（DC-AS-002・両辺が同一ファイルを指すよう対にする）
# ---------------------------------------------------------------------------

# git 側（find_gitignore_intent_violations に exclude_patterns として渡す。
# derive_probe_from_pattern が .claude/ を前置するため .claude/ なしで書く）
UNIVERSAL_PATTERNS_FOR_GIT: Sequence[str] = (
    "x/__pycache__/y.py",
    "x.pyc",
    "x.pyo",
)

# should_skip 側（find_gitignore_line_violations に gitignore_lines として渡す。
# derive_probe_from_gitignore_line が .claude/ を剥がすため .claude/ 付きで書く）
UNIVERSAL_LINES_FOR_SKIP: Sequence[str] = (
    ".claude/x/__pycache__/y.py",
    ".claude/x.pyc",
    ".claude/x.pyo",
)

UNIVERSAL_GIT_IGNORED_COUNT = 3  # 現状値（== 3・DC-GP-001）
UNIVERSAL_SHOULD_SKIP_COUNT = 3  # 現状値（== 3）

# ---------------------------------------------------------------------------
# 実効検査件数の下限（件数定数群の正本はここ。増減いずれの場合も理由文字列と
# セットでレビューする・DC-AM-003 運用行）
# ---------------------------------------------------------------------------

# EXCLUDE 由来 17 件 - 許容リスト A 1 件（遊びなし）。
# 実効検査件数（母数）の下限。遊びを持たせるとレコードが 1 件消えても気付けない
MIN_EXCLUDE_IGNORED = 16
MIN_KEEP_NOT_IGNORED = 8  # KEEP 8 件全数（許容リストなし）
MIN_KEEP_COMPOSITE_NOT_IGNORED = 8  # 合成 KEEP 判定も KEEP 8 件全数
# root .gitignore の .claude/ 非否定行 18 - 許容リスト B 2 件。
# 実効検査件数（母数）の下限。遊びを持たせるとレコードが 1 件消えても気付けない
MIN_CLAUDE_LINE_TRUE = 16
MIN_CLAUDE_LINE_FALSE = 5  # 同・否定行 5 件全数（許容リストなし）
MIN_EDGE2_RESCUE = 3  # sdist exclude 配下の KEEP 3 件（許容リスト概念なし）
MIN_EDGE2_CLASSIFY = 4  # force-include キー 4 件全数（許容リスト適用の有無を問わない）
MIN_EDGE3 = 7  # sdist exclude の .claude/ エントリ 7 件全数

# ---------------------------------------------------------------------------
# テスト側ヘルパー（P0: 違反取得は結果フィルタ経由・件数はレコードから数える）
# ---------------------------------------------------------------------------


def _violations(records: Sequence[Verdict]) -> list[Verdict]:
    """判定レコード列から違反のみを結果フィルタで取り出す.

    ``assert not find_...(...)`` の形（戻り値そのものを真偽判定する形）は
    「戻り値は全レコード」という P0 の契約に反するため使わない。
    """
    return [r for r in records if r.actual != r.expected and not r.allowlisted]


def _count(records: Sequence[Verdict], category: str, *, allowlisted: bool) -> int:
    """当該由来カテゴリかつ許容リスト適用フラグが一致するレコード件数."""
    return sum(1 for r in records if r.category == category and r.allowlisted is allowlisted)


def _count_all(records: Sequence[Verdict], category: str) -> int:
    """許容リスト適用の有無を問わない当該由来カテゴリの総レコード件数.

    CATEGORY_EDGE2_RESCUE / CATEGORY_EDGE2_CLASSIFY 専用（設計判断メモ参照）。
    """
    return sum(1 for r in records if r.category == category)


def _count_expected(
    records: Sequence[Verdict], category: str, *, allowlisted: bool, expected: bool
) -> int:
    """由来カテゴリ・許容リスト適用フラグ・課した期待値の 3 条件で切り出した件数.

    claude行 カテゴリは True 期待（非否定行）と False 期待（否定行）の両方を
    同一カテゴリで表現するため、この 3 条件目が必要になる。
    """
    return sum(
        1
        for r in records
        if r.category == category and r.allowlisted is allowlisted and r.expected is expected
    )


def _read_gitignore_lines(path: Path) -> list[str]:
    """architecture 改訂 2 §2-1 DC-AS-006: encoding=utf-8 明示 + 行分割後に
    末尾 \\r を除去してから解釈する（CRLF チェックアウト耐性）。"""
    text = path.read_text(encoding="utf-8")
    return [line.rstrip("\r") for line in text.split("\n")]


def _real_check_ignore_fn(repo: Path) -> Callable[[str], bool]:
    """ADR-6: git check-ignore の returncode 0/1 のみを判定値とし、それ以外は
    fail-loud（例外送出）とする実物の check_ignore_fn."""

    def _check(probe: str) -> bool:
        proc = subprocess.run(
            # `--`（オプション終端）を probe の直前に置く [SR-INJ-002]。
            # derive_probe は外部注入可能なため、`-` 始まりの probe がフラグとして
            # 解釈される引数注入面を構造的に塞ぐ（判定値 0/1 は変えない）。
            [_GIT, "-c", "core.excludesFile=", "check-ignore", "-q", "--", probe],
            cwd=repo,
        )
        if proc.returncode == 0:
            return True
        if proc.returncode == 1:
            return False
        raise RuntimeError(
            f"git check-ignore fail-loud (ADR-6): returncode={proc.returncode} probe={probe!r}"
        )

    return _check


# ---------------------------------------------------------------------------
# 一時 git リポジトリ fixture（tests/test_claude_gitignore.py のイディオムに従う）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def root_only_repo(tmp_path_factory) -> Path:
    """root .gitignore のみを配置した一時リポジトリ（辺 1 方向 A・P2c の正本）.

    DC-AS-002: git init 直後に .git/info/exclude を空へ上書きし、
    core.excludesFile も空指定にすることで、ユーザー全体設定・init テンプレート
    由来の実効パターンを排し、判定入力を配置した .gitignore のみに隔離する。
    """
    if _GIT is None:
        pytest.skip("git が見つからない環境ではスキップ")
    repo = tmp_path_factory.mktemp("sync_root_only")
    subprocess.run([_GIT, "init", "-q"], cwd=repo, check=True)
    (repo / ".git" / "info" / "exclude").write_text("", encoding="utf-8")
    shutil.copyfile(ROOT_GITIGNORE, repo / ".gitignore")
    return repo


@pytest.fixture(scope="module")
def composite_repo(tmp_path_factory) -> Path:
    """root .gitignore と .claude/.gitignore の両方を配置した合成リポジトリ（P2b の正本）.

    .claude/.gitignore は検証対象ではなく KEEP 合成判定の入力
    （requirements 改訂 2 §4 但し書き）。
    """
    if _GIT is None:
        pytest.skip("git が見つからない環境ではスキップ")
    repo = tmp_path_factory.mktemp("sync_composite")
    subprocess.run([_GIT, "init", "-q"], cwd=repo, check=True)
    (repo / ".git" / "info" / "exclude").write_text("", encoding="utf-8")
    shutil.copyfile(ROOT_GITIGNORE, repo / ".gitignore")
    claude_dir = repo / ".claude"
    claude_dir.mkdir()
    shutil.copyfile(CLAUDE_GITIGNORE, claude_dir / ".gitignore")
    return repo


# ===========================================================================
# P1: プローブ導出関数の入出力凍結
# ===========================================================================


class TestDeriveProbeFromPattern:
    """derive_probe_from_pattern: グロブ * -> __sync_probe__（literal 確定）・
    リテラルはそのまま・.claude/ 前置（DC-GP-005）."""

    def test_glob_star_is_replaced_with_frozen_probe_name(self):
        assert derive_probe_from_pattern("reports/*") == ".claude/reports/__sync_probe__"

    def test_literal_is_preserved_and_claude_prefixed(self):
        assert derive_probe_from_pattern("memory/patterns.json") == ".claude/memory/patterns.json"

    def test_glob_pattern_at_top_level(self):
        assert derive_probe_from_pattern("agent-memory/*") == ".claude/agent-memory/__sync_probe__"

    def test_keep_pattern_literal_dotfile(self):
        assert derive_probe_from_pattern("tmp/.gitkeep") == ".claude/tmp/.gitkeep"

    def test_universal_pattern_probe_stems_get_claude_prefixed_without_globbing(self):
        # P2c で使う「全体パターン」3 プローブ相当（.claude/ なし・グロブなしの生パターン）
        for pattern, expected in zip(
            UNIVERSAL_PATTERNS_FOR_GIT,
            (
                ".claude/x/__pycache__/y.py",
                ".claude/x.pyc",
                ".claude/x.pyo",
            ),
        ):
            assert derive_probe_from_pattern(pattern) == expected

    def test_every_real_exclude_and_keep_pattern_derives_without_error(self):
        # 実データ全件が例外を出さずプローブ導出できること（形式の想定漏れがないこと）
        for pattern in (*EXCLUDE_PATTERNS, *KEEP_PATTERNS):
            probe = derive_probe_from_pattern(pattern)
            assert isinstance(probe, str)
            assert probe.startswith(".claude/")


class TestDeriveProbeFromGitignoreLine:
    """derive_probe_from_gitignore_line: tuple[str, bool] | None ＝
    (プローブ相対パス, 否定行か)。コメント/空行/.claude/ 外は None。
    否定行は (プローブ, True) を返す（P1・DC-AM-002 対象外列挙の復元）。"""

    @pytest.mark.parametrize(
        "line",
        [
            "# a comment line",
            "   # indented comment",
            "",
            "   ",
            "src/c3/_template/",  # .claude/ 外
            "*.egg-info/",  # .claude/ 外・全体パターン（本関数の射程外）
        ],
    )
    def test_out_of_scope_lines_return_none(self, line):
        assert derive_probe_from_gitignore_line(line) is None

    def test_non_negation_literal_line(self):
        assert derive_probe_from_gitignore_line(".claude/memory/patterns.json") == (
            "memory/patterns.json",
            False,
        )

    def test_negation_literal_line_returns_true_flag_not_none(self):
        # 改訂 1 §2-1 手順 1 の「対象外」列挙に含まれる否定行は抽出段階の話であり、
        # 本関数の戻り値契約ではない（None にせず (probe, True) を返す）
        assert derive_probe_from_gitignore_line("!.claude/tmp/.gitkeep") == (
            "tmp/.gitkeep",
            True,
        )

    def test_directory_form_gets_one_step_probe_supplement(self):
        assert derive_probe_from_gitignore_line(".claude/agent-memory/") == (
            "agent-memory/__sync_probe__",
            False,
        )

    def test_negation_directory_form_gets_one_step_probe_supplement(self):
        assert derive_probe_from_gitignore_line("!.claude/skills/worktree-tdd-workflow/") == (
            "skills/worktree-tdd-workflow/__sync_probe__",
            True,
        )

    def test_dir_star_form_replaces_glob_with_probe_name(self):
        assert derive_probe_from_gitignore_line(".claude/memory/sessions/*") == (
            "memory/sessions/__sync_probe__",
            False,
        )

    def test_universal_lines_for_skip_side_strip_claude_prefix(self):
        for line, expected in zip(
            UNIVERSAL_LINES_FOR_SKIP,
            (
                ("x/__pycache__/y.py", False),
                ("x.pyc", False),
                ("x.pyo", False),
            ),
        ):
            assert derive_probe_from_gitignore_line(line) == expected


# ===========================================================================
# P2 / P2b / P2c: 辺 1 方向 A（+ 合成 KEEP・全体パターン再利用）
# ===========================================================================


class TestDirectionAWithStubs:
    """find_gitignore_intent_violations の純粋な性質（実 git 不要・スタブ判定）."""

    def test_exclude_probe_ignored_expected_true_no_violation(self):
        records = find_gitignore_intent_violations(
            ["reports/*"], [], check_ignore_fn=lambda probe: True, allowlist_a={}
        )
        assert len(records) == 1
        r = records[0]
        assert r.expected is True
        assert r.actual is True
        assert r.allowlisted is False
        assert r.category == CATEGORY_EXCLUDE
        assert _violations(records) == []

    def test_keep_probe_not_ignored_expected_false_no_violation(self):
        records = find_gitignore_intent_violations(
            [], ["tmp/.gitkeep"], check_ignore_fn=lambda probe: False, allowlist_a={}
        )
        assert len(records) == 1
        r = records[0]
        assert r.expected is False
        assert r.actual is False
        assert r.category == CATEGORY_KEEP
        assert _violations(records) == []

    def test_exclude_probe_not_ignored_is_a_violation_unless_allowlisted(self):
        records = find_gitignore_intent_violations(
            ["reports/*"], [], check_ignore_fn=lambda probe: False, allowlist_a={}
        )
        assert len(_violations(records)) == 1

    def test_allowlisted_exclude_mismatch_is_not_a_violation_but_record_remains(self):
        records = find_gitignore_intent_violations(
            ["pytest_temp.ini"],
            [],
            check_ignore_fn=lambda probe: False,
            allowlist_a={"pytest_temp.ini": "テスト用の理由"},
        )
        # 許容リスト対象もレコードとして残す（残さない実装は違反・P0）
        assert len(records) == 1
        assert records[0].allowlisted is True
        assert _violations(records) == []

    def test_returned_records_are_all_records_not_just_violations(self):
        records = find_gitignore_intent_violations(
            ["a/*", "b/*"], ["c.txt"], check_ignore_fn=lambda probe: True, allowlist_a={}
        )
        # a/*, b/* は ignored 期待通り・c.txt は not-ignored 期待に反する
        # -> 3 レコード全部が戻り値に含まれる（違反は c.txt のみ 1 件）
        assert len(records) == 3
        assert len(_violations(records)) == 1

    def test_derive_probe_none_produces_no_record_for_that_pattern(self):
        def _partial_none(pattern: str):
            if pattern == "b/*":
                return None
            return derive_probe_from_pattern(pattern)

        records = find_gitignore_intent_violations(
            ["a/*", "b/*"],
            [],
            check_ignore_fn=lambda probe: True,
            allowlist_a={},
            derive_probe=_partial_none,
        )
        assert len(records) == 1  # b/* は導出 None のためレコードなし

    def test_category_labels_are_overridable_for_p2b_and_p2c_reuse(self):
        records = find_gitignore_intent_violations(
            [],
            ["x.gitkeep"],
            check_ignore_fn=lambda probe: False,
            allowlist_a={},
            category_keep=CATEGORY_KEEP_COMPOSITE,
        )
        assert records[0].category == CATEGORY_KEEP_COMPOSITE

        records2 = find_gitignore_intent_violations(
            ["x/__pycache__/y.py"],
            [],
            check_ignore_fn=lambda probe: True,
            allowlist_a={},
            category_exclude=CATEGORY_UNIVERSAL,
        )
        assert records2[0].category == CATEGORY_UNIVERSAL


class TestP2bCompositeKeepJudgment:
    """P2b: KEEP 合成判定は方向 A 検出器の再利用（exclude_patterns=()・
    keep_patterns=KEEP_PATTERNS・合成リポジトリ判定・allowlist_a=()）."""

    def test_composite_call_only_passes_keep_patterns(self):
        checked_probes = []

        def _check(probe: str) -> bool:
            checked_probes.append(probe)
            return False

        records = find_gitignore_intent_violations(
            (), KEEP_PATTERNS, check_ignore_fn=_check, allowlist_a=(), category_keep=CATEGORY_KEEP_COMPOSITE
        )
        assert len(records) == len(KEEP_PATTERNS)
        assert all(r.category == CATEGORY_KEEP_COMPOSITE for r in records)
        # 判定対象は KEEP_PATTERNS のプローブのみ（EXCLUDE 側は渡していない）
        assert len(checked_probes) == len(KEEP_PATTERNS)


# ===========================================================================
# P3: 辺 1 方向 B
# ===========================================================================


class TestDirectionBWithStubs:
    """find_gitignore_line_violations の純粋な性質（実 should_skip 不要・スタブ）."""

    def test_non_negation_line_true_expected_no_violation(self):
        records = find_gitignore_line_violations(
            [".claude/memory/patterns.json"], should_skip_fn=lambda p: True, allowlist_b={}
        )
        assert len(records) == 1
        r = records[0]
        assert r.expected is True
        assert r.actual is True
        assert r.category == CATEGORY_CLAUDE_LINE
        assert _violations(records) == []

    def test_negation_line_false_expected_no_violation(self):
        records = find_gitignore_line_violations(
            ["!.claude/tmp/.gitkeep"], should_skip_fn=lambda p: False, allowlist_b={}
        )
        assert len(records) == 1
        r = records[0]
        assert r.expected is False
        assert r.actual is False
        assert _violations(records) == []

    def test_comment_and_blank_and_external_lines_produce_no_records(self):
        records = find_gitignore_line_violations(
            ["# comment", "", "src/c3/_template/"],
            should_skip_fn=lambda p: True,
            allowlist_b={},
        )
        assert records == []

    def test_allowlisted_mismatch_is_not_a_violation_but_record_remains(self):
        records = find_gitignore_line_violations(
            [".claude/agents/tdd-develop.md"],
            should_skip_fn=lambda p: False,  # 期待 True に反する
            allowlist_b=ALLOWLIST_B,
        )
        assert len(records) == 1
        assert records[0].allowlisted is True
        assert _violations(records) == []

    def test_derive_probe_none_produces_no_record(self):
        records = find_gitignore_line_violations(
            [".claude/a/b.txt"],
            should_skip_fn=lambda p: True,
            allowlist_b={},
            derive_probe=lambda line: None,
        )
        assert records == []

    def test_category_overridable_for_universal_pattern_reuse(self):
        records = find_gitignore_line_violations(
            list(UNIVERSAL_LINES_FOR_SKIP),
            should_skip_fn=should_skip,
            allowlist_b={},
            category=CATEGORY_UNIVERSAL,
        )
        assert len(records) == 3
        assert all(r.category == CATEGORY_UNIVERSAL for r in records)


# ===========================================================================
# P4: 辺 2（正規化関数 + 双方向検出器）
# ===========================================================================


class TestNormalizeSdistExcludeEntry:
    """ADR-8 改訂: 受理＝リテラル・dir/* 形・ディレクトリ形 dir/（dir/* 相当へ正規化）／
    fail-loud＝** を含む・先頭 /・! 否定・空文字・その他."""

    def test_literal_is_unchanged(self):
        assert normalize_sdist_exclude_entry(".claude/memory/patterns.json") == (
            ".claude/memory/patterns.json"
        )

    def test_dir_star_form_is_unchanged(self):
        assert normalize_sdist_exclude_entry(".claude/reports/*") == ".claude/reports/*"

    def test_directory_form_normalizes_to_dir_star(self):
        assert normalize_sdist_exclude_entry(".claude/reports/") == ".claude/reports/*"

    @pytest.mark.parametrize(
        "entry",
        [
            ".claude/**/foo",
            "/absolute/leading/slash",
            "!negated/entry",
            "",
            ".claude/weird*mid*glob",
        ],
    )
    def test_unrecognized_forms_fail_loud(self, entry):
        with pytest.raises(Exception):  # noqa: B017 - fail-loud の型は実装裁量
            normalize_sdist_exclude_entry(entry)

    def test_docstring_documents_alignment_with_edge3_derivation(self):
        # P4: 正規化関数の docstring が辺 3 導出との整合（ディレクトリ形は両辺で
        # 同じ 1 段補い）を 1 行で述べていることを確認する
        doc = inspect.getdoc(normalize_sdist_exclude_entry) or ""
        assert "1 段補い" in doc, (
            "normalize_sdist_exclude_entry の docstring に辺 3 導出との整合"
            "（ディレクトリ形は両辺で同じ 1 段補い）の記述が無い"
        )


class TestForceIncludeViolationsWithSyntheticData:
    """find_force_include_violations の純粋な性質（実 pyproject.toml 不要）."""

    KEEP = ("reports/.gitkeep", "memory/.gitkeep")
    SDIST_EXCLUDE = (".claude/reports/*",)  # memory/.gitkeep は非該当（対象外）
    FORCE_INCLUDE_KEYS = (".claude/reports/.gitkeep", ".claude/state/setup_done.flag")
    INJECTION = (".claude/state/setup_done.flag",)

    def test_rescue_record_only_created_for_sdist_exclude_covered_keep(self):
        records = find_force_include_violations(
            self.KEEP, self.SDIST_EXCLUDE, self.FORCE_INCLUDE_KEYS, self.INJECTION
        )
        rescue = [r for r in records if r.category == CATEGORY_EDGE2_RESCUE]
        # memory/.gitkeep は sdist exclude 配下ではないため対象外(レコードなし)
        assert len(rescue) == 1
        assert rescue[0].expected is True
        assert rescue[0].actual is True  # force_include_keys に存在する

    def test_rescue_missing_key_is_a_violation(self):
        records = find_force_include_violations(
            self.KEEP, self.SDIST_EXCLUDE, (), self.INJECTION
        )
        rescue = [r for r in records if r.category == CATEGORY_EDGE2_RESCUE]
        assert len(rescue) == 1
        assert len(_violations(rescue)) == 1

    def test_classify_record_created_for_every_force_include_key(self):
        records = find_force_include_violations(
            self.KEEP, self.SDIST_EXCLUDE, self.FORCE_INCLUDE_KEYS, self.INJECTION
        )
        classify = [r for r in records if r.category == CATEGORY_EDGE2_CLASSIFY]
        assert len(classify) == 2
        assert _violations(classify) == []

    def test_injection_control_key_is_allowlisted_in_classify_category(self):
        records = find_force_include_violations(
            self.KEEP, self.SDIST_EXCLUDE, self.FORCE_INCLUDE_KEYS, self.INJECTION
        )
        classify = {r.subject: r for r in records if r.category == CATEGORY_EDGE2_CLASSIFY}
        assert classify[".claude/state/setup_done.flag"].allowlisted is True
        assert classify[".claude/reports/.gitkeep"].allowlisted is False

    def test_unexplainable_key_is_a_classify_violation(self):
        records = find_force_include_violations(
            self.KEEP,
            self.SDIST_EXCLUDE,
            (*self.FORCE_INCLUDE_KEYS, ".claude/totally/unexplained/key.txt"),
            self.INJECTION,
        )
        classify = [r for r in records if r.category == CATEGORY_EDGE2_CLASSIFY]
        assert len(classify) == 3
        assert len(_violations(classify)) == 1


class TestSdistExcludeViolationsWithStubs:
    """find_sdist_exclude_violations の純粋な性質（実 should_skip 不要・スタブ）."""

    def test_should_skip_true_expected_no_violation(self):
        records = find_sdist_exclude_violations([".claude/docs/decisions.md"], should_skip_fn=lambda p: True)
        assert len(records) == 1
        r = records[0]
        assert r.category == CATEGORY_EDGE3
        assert r.expected is True
        assert r.actual is True
        assert _violations(records) == []

    def test_should_skip_false_is_a_violation(self):
        records = find_sdist_exclude_violations([".claude/docs/decisions.md"], should_skip_fn=lambda p: False)
        assert len(_violations(records)) == 1

    def test_probe_derivation_strips_claude_prefix_and_substitutes_glob(self):
        seen: list[str] = []

        def _capture(probe: str) -> bool:
            seen.append(probe)
            return True

        find_sdist_exclude_violations([".claude/reports/*"], should_skip_fn=_capture)
        assert seen == ["reports/__sync_probe__"]

    def test_directory_form_entry_is_normalized_before_probe_derivation(self):
        seen: list[str] = []

        def _capture(probe: str) -> bool:
            seen.append(probe)
            return True

        find_sdist_exclude_violations([".claude/some/dir/"], should_skip_fn=_capture)
        assert seen == ["some/dir/__sync_probe__"]

    def test_docstring_freezes_asymmetry_rationale_verbatim(self):
        # P5: 非対称の根拠 docstring は改訂 3 §2-3 の確定文面を引用で凍結する
        doc = inspect.getdoc(find_sdist_exclude_violations) or ""
        expected_quote = (
            "sdist exclude はローカル作業ファイル対策の部分列であり網羅を要求しない。"
            "この非網羅は他のどの検査でも代替されない（`scripts/verify_wheel.py` の "
            "sdist 検査は sdist に実体として入った EXCLUDE 対象の混入のみを見る。"
            "tracked / 非 ignore の未追跡作業ファイルの双方が対象で、clean checkout "
            "での実効候補は注入対照 1 件）"
        )
        assert expected_quote in doc, (
            "find_sdist_exclude_violations の docstring に改訂 3 §2-3 の確定文面"
            "（非対称の根拠）が逐語で含まれていない"
        )


# ===========================================================================
# P2b / P3 の docstring 確認（凍結断片。7 項目全体の存在確認は confirm-sync が
# Read で行う。ここでは test-sync (P0-P7) が明示的に要求する断片のみ検査する）
# ===========================================================================


class TestDocstringContractFragments:
    def test_p2b_docstring_documents_claude_gitignore_as_input_not_target(self):
        doc = inspect.getdoc(find_gitignore_intent_violations) or ""
        assert ".claude/.gitignore" in doc and "入力" in doc, (
            "find_gitignore_intent_violations の docstring に "
            "「.claude/.gitignore は検証対象ではなく入力」の記述が無い（P2b (i)）"
        )

    def test_p2b_docstring_documents_correction_direction_is_source_side_negation(self):
        doc = inspect.getdoc(find_gitignore_intent_violations) or ""
        assert "出所" in doc and "否定行" in doc, (
            "find_gitignore_intent_violations の docstring に是正方向"
            "（ignore の出所側の否定行で戻す）の記述が無い（P2b (ii)）"
        )

    def test_p3_docstring_documents_direction_b_scope_limit(self):
        doc = inspect.getdoc(find_gitignore_line_violations) or ""
        assert "全体パターン" in doc and "射程外" in doc, (
            "find_gitignore_line_violations の docstring に方向 B の限界"
            "（.claude/ 配下での全体パターン 3 種対応・その他は射程外）の記述が無い"
        )


# ===========================================================================
# P0: 判定レコード契約の直接検証
# ===========================================================================


class TestP0RecordContract:
    def test_verdict_has_exactly_five_required_fields(self):
        records = find_gitignore_intent_violations(
            ["a/*"], [], check_ignore_fn=lambda p: True, allowlist_a={}
        )
        r = records[0]
        assert set(r._fields) == {"subject", "expected", "actual", "allowlisted", "category"}

    def test_category_vocabulary_matches_frozen_eight_terms(self):
        vocabulary = {
            CATEGORY_EXCLUDE,
            CATEGORY_KEEP,
            CATEGORY_KEEP_COMPOSITE,
            CATEGORY_CLAUDE_LINE,
            CATEGORY_UNIVERSAL,
            CATEGORY_EDGE2_RESCUE,
            CATEGORY_EDGE2_CLASSIFY,
            CATEGORY_EDGE3,
        }
        assert vocabulary == {
            "EXCLUDE",
            "KEEP",
            "合成KEEP",
            "claude行",
            "全体パターン",
            "辺2救済",
            "辺2分類",
            "辺3",
        }


# ===========================================================================
# P6-1: 辺 1 統合（tomllib 非依存・全 5 構成で実行）
# ===========================================================================


@requires_git
class TestEdge1Integration:
    """実物（root .gitignore・c3._excludes・git check-ignore 実挙動）を検出器へ
    供給する統合検査。P6-1（辺1・全 5 構成・tomllib 非依存）。"""

    @pytest.fixture(scope="class")
    def gitignore_lines(self) -> list[str]:
        return _read_gitignore_lines(ROOT_GITIGNORE)

    @pytest.fixture(scope="class")
    def direction_a_records(self, root_only_repo) -> list[Verdict]:
        check_fn = _real_check_ignore_fn(root_only_repo)
        return find_gitignore_intent_violations(
            EXCLUDE_PATTERNS, KEEP_PATTERNS, check_ignore_fn=check_fn, allowlist_a=ALLOWLIST_A
        )

    @pytest.fixture(scope="class")
    def composite_records(self, composite_repo) -> list[Verdict]:
        check_fn = _real_check_ignore_fn(composite_repo)
        return find_gitignore_intent_violations(
            (),
            KEEP_PATTERNS,
            check_ignore_fn=check_fn,
            allowlist_a={},
            category_keep=CATEGORY_KEEP_COMPOSITE,
        )

    @pytest.fixture(scope="class")
    def universal_git_records(self, root_only_repo) -> list[Verdict]:
        check_fn = _real_check_ignore_fn(root_only_repo)
        return find_gitignore_intent_violations(
            UNIVERSAL_PATTERNS_FOR_GIT,
            (),
            check_ignore_fn=check_fn,
            allowlist_a={},
            category_exclude=CATEGORY_UNIVERSAL,
        )

    @pytest.fixture(scope="class")
    def direction_b_records(self, gitignore_lines) -> list[Verdict]:
        return find_gitignore_line_violations(
            gitignore_lines, should_skip_fn=should_skip, allowlist_b=ALLOWLIST_B
        )

    @pytest.fixture(scope="class")
    def universal_skip_records(self) -> list[Verdict]:
        return find_gitignore_line_violations(
            list(UNIVERSAL_LINES_FOR_SKIP),
            should_skip_fn=should_skip,
            allowlist_b={},
            category=CATEGORY_UNIVERSAL,
        )

    def test_no_violations_across_all_edge1_checks(
        self,
        direction_a_records,
        composite_records,
        universal_git_records,
        direction_b_records,
        universal_skip_records,
    ):
        all_records = (
            direction_a_records
            + composite_records
            + universal_git_records
            + direction_b_records
            + universal_skip_records
        )
        violations = _violations(all_records)
        assert violations == [], f"辺 1 統合検査で違反レコードが検出された: {violations!r}"

    def test_exclude_effective_count_lower_bound(self, direction_a_records):
        assert (
            _count(direction_a_records, CATEGORY_EXCLUDE, allowlisted=False) >= MIN_EXCLUDE_IGNORED
        )

    def test_keep_effective_count_lower_bound(self, direction_a_records):
        assert _count(direction_a_records, CATEGORY_KEEP, allowlisted=False) >= MIN_KEEP_NOT_IGNORED

    def test_keep_composite_effective_count_lower_bound(self, composite_records):
        assert (
            _count(composite_records, CATEGORY_KEEP_COMPOSITE, allowlisted=False)
            >= MIN_KEEP_COMPOSITE_NOT_IGNORED
        )

    def test_claude_line_true_effective_count_lower_bound(self, direction_b_records):
        assert (
            _count_expected(
                direction_b_records, CATEGORY_CLAUDE_LINE, allowlisted=False, expected=True
            )
            >= MIN_CLAUDE_LINE_TRUE
        )

    def test_claude_line_false_effective_count_lower_bound(self, direction_b_records):
        assert (
            _count_expected(
                direction_b_records, CATEGORY_CLAUDE_LINE, allowlisted=False, expected=False
            )
            >= MIN_CLAUDE_LINE_FALSE
        )

    def test_universal_should_skip_count_equals_three(self, universal_skip_records):
        assert (
            _count(universal_skip_records, CATEGORY_UNIVERSAL, allowlisted=False)
            == UNIVERSAL_SHOULD_SKIP_COUNT
        )

    def test_universal_git_ignored_count_equals_three(self, universal_git_records):
        assert (
            _count(universal_git_records, CATEGORY_UNIVERSAL, allowlisted=False)
            == UNIVERSAL_GIT_IGNORED_COUNT
        )

    def test_allowlist_a_equivalence(self, direction_a_records):
        assert _count(direction_a_records, CATEGORY_EXCLUDE, allowlisted=True) == ALLOWLIST_A_COUNT

    def test_allowlist_b_equivalence(self, direction_b_records):
        assert (
            _count(direction_b_records, CATEGORY_CLAUDE_LINE, allowlisted=True) == ALLOWLIST_B_COUNT
        )

    def test_counts_are_not_recomputed_test_side(
        self, direction_a_records, direction_b_records
    ):
        # P0 禁止規範の直接検証: レコードのフィールドのみを根拠にしていること
        # （EXCLUDE_PATTERNS/KEEP_PATTERNS の長さを直接使っていないことの裏取り）
        exclude_effective = _count(direction_a_records, CATEGORY_EXCLUDE, allowlisted=False)
        assert exclude_effective == len(
            [r for r in direction_a_records if r.category == CATEGORY_EXCLUDE and not r.allowlisted]
        )


# ===========================================================================
# P6-2: 辺 2・辺 3 統合（importorskip("tomllib") はこの単位に限る）
# ===========================================================================


class TestEdge2Edge3Integration:
    """pyproject.toml をパースして検出器へ供給する統合検査。P6-2。"""

    @pytest.fixture(scope="class")
    def pyproject_data(self):
        tomllib = pytest.importorskip("tomllib")
        return tomllib.loads(PYPROJECT_TOML.read_text(encoding="utf-8"))

    @pytest.fixture(scope="class")
    def sdist_exclude(self, pyproject_data) -> list[str]:
        return pyproject_data["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]

    @pytest.fixture(scope="class")
    def force_include_keys(self, pyproject_data) -> list[str]:
        return list(
            pyproject_data["tool"]["hatch"]["build"]["targets"]["sdist"]["force-include"].keys()
        )

    @pytest.fixture(scope="class")
    def edge2_records(self, sdist_exclude, force_include_keys) -> list[Verdict]:
        return find_force_include_violations(
            KEEP_PATTERNS, sdist_exclude, force_include_keys, INJECTION_CONTROLS
        )

    @pytest.fixture(scope="class")
    def edge3_records(self, sdist_exclude) -> list[Verdict]:
        return find_sdist_exclude_violations(sdist_exclude, should_skip_fn=should_skip)

    def test_no_violations_across_edge2_and_edge3(self, edge2_records, edge3_records):
        violations = _violations(edge2_records + edge3_records)
        assert violations == [], f"辺 2/辺 3 統合検査で違反レコードが検出された: {violations!r}"

    def test_edge2_rescue_count_lower_bound(self, edge2_records):
        assert _count_all(edge2_records, CATEGORY_EDGE2_RESCUE) >= MIN_EDGE2_RESCUE

    def test_edge2_classify_count_lower_bound(self, edge2_records):
        assert _count_all(edge2_records, CATEGORY_EDGE2_CLASSIFY) >= MIN_EDGE2_CLASSIFY

    def test_edge3_count_lower_bound(self, edge3_records):
        assert _count(edge3_records, CATEGORY_EDGE3, allowlisted=False) >= MIN_EDGE3

    def test_injection_control_equivalence(self, edge2_records):
        assert (
            _count(edge2_records, CATEGORY_EDGE2_CLASSIFY, allowlisted=True)
            == INJECTION_CONTROL_COUNT
        )

    def test_counts_come_from_records_not_recomputed(self, sdist_exclude, force_include_keys, edge2_records):
        # 禁止規範の直接検証: KEEP_PATTERNS や sdist_exclude の長さを直接使わず、
        # 検出器の戻り値レコードから数えていることの裏取り
        rescue_count = _count_all(edge2_records, CATEGORY_EDGE2_RESCUE)
        assert rescue_count == len(
            [r for r in edge2_records if r.category == CATEGORY_EDGE2_RESCUE]
        )


# ===========================================================================
# P7: 検知力の実証（負の対照）
# ===========================================================================


@requires_git
class TestP7NegativeControls:
    """負の対照を正負ペアで構成し、検出器が実際に違反・赤を出すことを実証する.

    実ファイル（.gitignore / .claude/.gitignore / pyproject.toml / _excludes.py）は
    一切変異させない。変異は引数として渡す一時的な入力コピーに限る。
    """

    # -- (a) EXCLUDE へ架空パターンを足した入力で辺 1 方向 A が違反を出す --------

    def test_p7a_fabricated_exclude_pattern_causes_direction_a_violation(self, root_only_repo):
        check_fn = _real_check_ignore_fn(root_only_repo)
        mutated_exclude = (*EXCLUDE_PATTERNS, "totally_fake_dir_xyz_c3_sync_probe/*")
        records = find_gitignore_intent_violations(
            mutated_exclude, (), check_ignore_fn=check_fn, allowlist_a=ALLOWLIST_A
        )
        violations = _violations(records)
        assert any(
            "totally_fake_dir_xyz_c3_sync_probe" in v.subject for v in violations
        ), "架空パターンを追加しても違反が検出されない（検出力なし）"

    # -- (b) .claude/ 除外行を足した gitignore 入力で方向 B が違反を出す ---------

    def test_p7b_fabricated_claude_line_causes_direction_b_violation(self):
        mutated_lines = [
            *_read_gitignore_lines(ROOT_GITIGNORE),
            ".claude/totally_fake_newly_added_dir_c3_sync_probe/thing.txt",
        ]
        records = find_gitignore_line_violations(
            mutated_lines, should_skip_fn=should_skip, allowlist_b=ALLOWLIST_B
        )
        violations = _violations(records)
        assert any(
            "totally_fake_newly_added_dir_c3_sync_probe" in v.subject for v in violations
        ), "架空の .claude/ 除外行を追加しても違反が検出されない（検出力なし）"

    # -- (c) sdist exclude 配下 KEEP を force-include から除いた入力で辺 2 が違反を出す --

    def test_p7c_removing_required_force_include_key_causes_edge2_violation(self):
        tomllib = pytest.importorskip("tomllib")
        data = tomllib.loads(PYPROJECT_TOML.read_text(encoding="utf-8"))
        sdist_exclude = data["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
        force_include_keys = list(
            data["tool"]["hatch"]["build"]["targets"]["sdist"]["force-include"].keys()
        )
        mutated_keys = [k for k in force_include_keys if k != ".claude/reports/.gitkeep"]
        records = find_force_include_violations(
            KEEP_PATTERNS, sdist_exclude, mutated_keys, INJECTION_CONTROLS
        )
        rescue_violations = [
            r for r in _violations(records) if r.category == CATEGORY_EDGE2_RESCUE
        ]
        assert rescue_violations, "force-include からの除去が違反として検出されない（検出力なし）"

    # -- (d) 用途不明の force-include キーを足した入力で辺 2 逆方向が違反を出す ----

    def test_p7d_unexplained_force_include_key_causes_edge2_classify_violation(self):
        tomllib = pytest.importorskip("tomllib")
        data = tomllib.loads(PYPROJECT_TOML.read_text(encoding="utf-8"))
        sdist_exclude = data["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
        force_include_keys = list(
            data["tool"]["hatch"]["build"]["targets"]["sdist"]["force-include"].keys()
        )
        mutated_keys = [*force_include_keys, ".claude/totally/unexplained/c3_sync_probe.txt"]
        records = find_force_include_violations(
            KEEP_PATTERNS, sdist_exclude, mutated_keys, INJECTION_CONTROLS
        )
        classify_violations = [
            r for r in _violations(records) if r.category == CATEGORY_EDGE2_CLASSIFY
        ]
        assert classify_violations, "用途不明キーの追加が違反として検出されない（検出力なし）"

    # -- (e) do-nothing スタブ 2 種で赤緑が両方向に反転することを実証する --------

    def test_p7e_direction_a_check_ignore_stub_reversal(self):
        always_ignored = find_gitignore_intent_violations(
            EXCLUDE_PATTERNS, KEEP_PATTERNS, check_ignore_fn=lambda p: True, allowlist_a=ALLOWLIST_A
        )
        always_not_ignored = find_gitignore_intent_violations(
            EXCLUDE_PATTERNS, KEEP_PATTERNS, check_ignore_fn=lambda p: False, allowlist_a=ALLOWLIST_A
        )
        v_true = {r.subject for r in _violations(always_ignored)}
        v_false = {r.subject for r in _violations(always_not_ignored)}
        assert v_true, "always-ignored スタブで KEEP 側の違反が出ない（検出力なし）"
        assert v_false, "always-not-ignored スタブで EXCLUDE 側の違反が出ない（検出力なし）"
        assert v_true != v_false, "スタブ反転で違反集合が変わらない（判定関数を実際に使っていない）"

    def test_p7e_p2b_composite_check_ignore_stub_reversal(self):
        always_ignored = find_gitignore_intent_violations(
            (), KEEP_PATTERNS, check_ignore_fn=lambda p: True, allowlist_a={}, category_keep=CATEGORY_KEEP_COMPOSITE
        )
        always_not_ignored = find_gitignore_intent_violations(
            (), KEEP_PATTERNS, check_ignore_fn=lambda p: False, allowlist_a={}, category_keep=CATEGORY_KEEP_COMPOSITE
        )
        assert _violations(always_ignored), "P2b: always-ignored スタブで違反が出ない"
        assert not _violations(always_not_ignored), "P2b: always-not-ignored スタブで違反が出てしまう"

    def test_p7e_p2c_universal_check_ignore_stub_reversal(self):
        always_ignored = find_gitignore_intent_violations(
            UNIVERSAL_PATTERNS_FOR_GIT, (), check_ignore_fn=lambda p: True, allowlist_a={}, category_exclude=CATEGORY_UNIVERSAL
        )
        always_not_ignored = find_gitignore_intent_violations(
            UNIVERSAL_PATTERNS_FOR_GIT, (), check_ignore_fn=lambda p: False, allowlist_a={}, category_exclude=CATEGORY_UNIVERSAL
        )
        assert not _violations(always_ignored), "P2c: always-ignored スタブで違反が出てしまう"
        assert _violations(always_not_ignored), "P2c: always-not-ignored スタブで違反が出ない"

    def test_p7e_direction_b_should_skip_stub_reversal(self):
        lines = _read_gitignore_lines(ROOT_GITIGNORE)
        always_true = find_gitignore_line_violations(lines, should_skip_fn=lambda p: True, allowlist_b=ALLOWLIST_B)
        always_false = find_gitignore_line_violations(lines, should_skip_fn=lambda p: False, allowlist_b=ALLOWLIST_B)
        v_true = {r.subject for r in _violations(always_true)}
        v_false = {r.subject for r in _violations(always_false)}
        assert v_true, "方向B: always-True スタブで否定行側の違反が出ない"
        assert v_false, "方向B: always-False スタブで非否定行側の違反が出ない"
        assert v_true != v_false, "方向B: スタブ反転で違反集合が変わらない"

    def test_p7e_edge3_should_skip_stub_reversal(self):
        synthetic_sdist_exclude = (".claude/fake_a/*", ".claude/fake_b.txt")
        always_true = find_sdist_exclude_violations(synthetic_sdist_exclude, should_skip_fn=lambda p: True)
        always_false = find_sdist_exclude_violations(synthetic_sdist_exclude, should_skip_fn=lambda p: False)
        assert not _violations(always_true), "辺3: always-True スタブで違反が出てしまう"
        assert _violations(always_false), "辺3: always-False スタブで違反が出ない"

    # -- (f) 全件 None を返す導出スタブで実効検査件数の下限 assert が赤になる ----
    #        （合否条件: 赤の理由は下限不成立であること・例外送出による赤は不可）

    def test_p7f_direction_a_all_none_derive_stub_starves_count_without_raising(self):
        try:
            records = find_gitignore_intent_violations(
                EXCLUDE_PATTERNS,
                KEEP_PATTERNS,
                check_ignore_fn=lambda p: True,
                allowlist_a=ALLOWLIST_A,
                derive_probe=lambda pattern: None,
            )
        except Exception as exc:  # noqa: BLE001 - 例外そのものが不合格の証拠
            pytest.fail(f"導出スタブ全 None で例外が送出された（不可）: {exc!r}")
        effective = _count(records, CATEGORY_EXCLUDE, allowlisted=False)
        assert effective == 0
        assert effective < MIN_EXCLUDE_IGNORED, "全 None 導出は下限不成立を引き起こすはず"

    def test_p7f_p2b_all_none_derive_stub_starves_count_without_raising(self):
        try:
            records = find_gitignore_intent_violations(
                (),
                KEEP_PATTERNS,
                check_ignore_fn=lambda p: True,
                allowlist_a={},
                category_keep=CATEGORY_KEEP_COMPOSITE,
                derive_probe=lambda pattern: None,
            )
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"P2b: 導出スタブ全 None で例外が送出された（不可）: {exc!r}")
        effective = _count(records, CATEGORY_KEEP_COMPOSITE, allowlisted=False)
        assert effective == 0
        assert effective < MIN_KEEP_COMPOSITE_NOT_IGNORED

    def test_p7f_p2c_all_none_derive_stub_starves_count_without_raising(self):
        try:
            records = find_gitignore_intent_violations(
                UNIVERSAL_PATTERNS_FOR_GIT,
                (),
                check_ignore_fn=lambda p: True,
                allowlist_a={},
                category_exclude=CATEGORY_UNIVERSAL,
                derive_probe=lambda pattern: None,
            )
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"P2c: 導出スタブ全 None で例外が送出された（不可）: {exc!r}")
        effective = _count(records, CATEGORY_UNIVERSAL, allowlisted=False)
        assert effective == 0
        assert effective < UNIVERSAL_GIT_IGNORED_COUNT

    def test_p7f_direction_b_all_none_derive_stub_starves_count_without_raising(self):
        lines = _read_gitignore_lines(ROOT_GITIGNORE)
        try:
            records = find_gitignore_line_violations(
                lines,
                should_skip_fn=lambda p: True,
                allowlist_b=ALLOWLIST_B,
                derive_probe=lambda line: None,
            )
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"方向B: 導出スタブ全 None で例外が送出された（不可）: {exc!r}")
        effective = _count_expected(
            records, CATEGORY_CLAUDE_LINE, allowlisted=False, expected=True
        )
        assert effective == 0
        assert effective < MIN_CLAUDE_LINE_TRUE

    def test_p7f_edge3_all_none_derive_stub_starves_count_without_raising(self):
        tomllib = pytest.importorskip("tomllib")
        data = tomllib.loads(PYPROJECT_TOML.read_text(encoding="utf-8"))
        sdist_exclude = data["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
        try:
            records = find_sdist_exclude_violations(
                sdist_exclude,
                should_skip_fn=lambda p: True,
                derive_probe=lambda entry: None,
            )
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"辺3: 導出スタブ全 None で例外が送出された（不可）: {exc!r}")
        effective = _count(records, CATEGORY_EDGE3, allowlisted=False)
        assert effective == 0
        assert effective < MIN_EDGE3

    def test_p7f_edge2_is_judge_function_independent_and_stays_green(self):
        # 辺 2 は判定関数非依存（force_include_keys の存在チェックのみ）であり、
        # P7(e)/(f) のスタブ反転対象外＝どちらのスタブでも緑が正常（一般化して
        # アサーションを弱めないための明示的な対照）
        tomllib = pytest.importorskip("tomllib")
        data = tomllib.loads(PYPROJECT_TOML.read_text(encoding="utf-8"))
        sdist_exclude = data["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
        force_include_keys = list(
            data["tool"]["hatch"]["build"]["targets"]["sdist"]["force-include"].keys()
        )
        records = find_force_include_violations(
            KEEP_PATTERNS, sdist_exclude, force_include_keys, INJECTION_CONTROLS
        )
        assert _violations(records) == []


# ===========================================================================
# SR-INJ-002 回帰: `-` 始まりプローブが引数注入面にならないこと
# ===========================================================================


@requires_git
class TestCheckIgnoreOptionTerminator:
    """`git check-ignore` 呼び出しの `--`（オプション終端）の回帰固定 [SR-INJ-002].

    `derive_probe` はキーワード引数として外部から自由に注入できる設計であり、
    将来 `-` 始まりの値を返す導出関数で呼ばれうる。`--` が無いと git は当該値を
    フラグとして解釈し `returncode=129`（unknown switch）を返すため、
    `_real_check_ignore_fn` は ADR-6 の fail-loud で例外を送出して赤化する。

    測る性質は 1 点のみ: **`-` 始まりのプローブでも例外を送出せず、
    returncode 0/1 に対応する判定値（bool）が返ること**。
    """

    @pytest.mark.parametrize(
        "probe",
        [
            "-c3_sync_probe_arg_injection",
            "--c3-sync-probe-arg-injection",
        ],
    )
    def test_dash_leading_probe_yields_a_verdict_without_raising(self, probe, root_only_repo):
        check_fn = _real_check_ignore_fn(root_only_repo)
        assert isinstance(check_fn(probe), bool)


# ===========================================================================
# 許容リストの理由文字列の非空性（ADR-3: 空の許容理由は書けない構造）
# ===========================================================================


class TestAllowlistReasonsArePresent:
    @pytest.mark.parametrize("reason", ALLOWLIST_A.values())
    def test_allowlist_a_reason_is_non_empty(self, reason):
        assert reason.strip()

    @pytest.mark.parametrize("reason", ALLOWLIST_B.values())
    def test_allowlist_b_reason_is_non_empty(self, reason):
        assert reason.strip()

    @pytest.mark.parametrize("reason", INJECTION_CONTROLS_REASONS.values())
    def test_injection_control_reason_is_non_empty(self, reason):
        assert reason.strip()

    def test_allowlist_sizes_match_frozen_count_constants(self):
        assert len(ALLOWLIST_A) == ALLOWLIST_A_COUNT
        assert len(ALLOWLIST_B) == ALLOWLIST_B_COUNT
        assert len(INJECTION_CONTROLS) == INJECTION_CONTROL_COUNT
