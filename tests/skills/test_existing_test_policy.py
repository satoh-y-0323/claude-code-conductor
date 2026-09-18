"""
tests/skills/test_existing_test_policy.py

番人テスト: 「既存テストの扱いを 3 段に分ける規約」が読み手に到達し、かつ陳腐化する形へ
逆行していないことを固定する。

正本: `.claude/reports/architecture-report-20260918-101819.md`
（同レポートは単一ファイルを改訂していく運用のため、版数は引用せず
**現在の frontmatter の `revision:` を正**とする。
§2-5 が検証方針 / ADR-4 が検知力の方針）

測る性質:

- **P1(a) 到達**: `.claude/CLAUDE.md` に、raw 行の先頭が ``## `` で始まる見出し行として
  キーワード「テストとプロダクトの関係」が存在する
- **P1(b) 到達**: `.claude/skills/dev-workflow/references/plan-design-guidelines.md` に、
  raw 行の先頭が ``## `` で始まる見出し行としてキーワード「ルール 21」が存在する
  （括弧の有無・前後の語を問わない部分一致とし、逐語固定にしない。ただし右側に数値境界を
  置き `ルール 210`〜`ルール 219` のような上位桁数には当てない・E 周回 1 CR-NEW-1）
- **P2 陳腐化防止**: `.claude/agents/planner.md` に「ルール N〜M」形の番号範囲が 1 件も
  出現しない（全角チルダ・半角チルダの双方を対象とし、数字と記号の間の空白の有無を問わない）

P1 はいずれも見出しレベルの降格（``### `` 以下）・本文への埋没を不充足とする。
P2 の判定は「ルール」という語に係る番号範囲に限る。設計が意図的に残す固定集合の範囲記号
（`R2〜R6` など。番号が増えないため陳腐化しない）にはマッチしない。

**検査しないもの**（射程の上限・要件 §6 と ADR-4）: 3 段の本文の逐語一致・段の個数・
分類表や自己チェックリストへの追記の有無・ルール 21 本文の文言・frontmatter。
増やすと規約の文面変更ごとに赤化する。

設計上の制約（plan の契約）:

- 判定は content を引数に取る純関数（``has_level2_heading_matching`` /
  ``find_rule_number_ranges``）に集約し、本検査（実ファイル）と対照検査
  （``tmp_path`` の偽ルート）が同じ関数を通る
- ルート解決は ``get_policy_root()`` の 1 関数に集約し、環境変数 ``C3_TEST_POLICY_ROOT``
  （未設定時は ``tests/skills/_skill_helpers.py`` の ``WORKTREE_ROOT``）で差し替える。
  ファイルの読み込みは ``read_target()`` 経由に限る。解決はモジュール import 時でなく
  **呼び出し時**に行う（モジュール定数へ束縛すると monkeypatch が no-op になる）
- env の値は ``os.path.realpath`` で正規化してから使い（シンボリックリンク・NTFS
  ジャンクション解決後の実体パス）、解決後が実在ディレクトリでなければ ``RuntimeError``
  で明示的に失敗する（実リポジトリへ黙って落ちない・E 周回 1 SR-V-002）
- ファイルは encoding='utf-8' で読む。非 ASCII を print しない
  （assert のメッセージも ASCII に保ち、非 ASCII の値は ``ascii()`` で退避する）

**検知力の実証**（plan-design-guidelines ルール 19 / ADR-4）: do-nothing スタブ 2 種は
用いず、``tmp_path`` の偽ルートに各性質の充足サンプルと違反サンプルの**両方を常設**し、
判定純関数の結果が入れ替わることを実測する（``TestVerdictSwaps``）。P2 は否定形のため
違反サンプル（範囲表記を含む planner.md 相当）を必ず置き、さらに固定集合 `R2〜R6` を
含むサンプルが**赤にならない**ことも実測する（誤ヒットの回帰ガード）。

**適用除外の宣言**: ``tmp_path`` の充足ケース（``TestPositiveControls`` と
``TestVerdictSwaps`` の充足側）は意図的に Red 時点から緑である。tester.md の
「テストが最初から Pass する場合は既存の挙動をテストしているだけなので修正する」は
本ケースに適用しない（恒久的な正の対照であり、削除・書き換えをしない）。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from tests.skills._skill_helpers import WORKTREE_ROOT

# --------------------------------------------------------------------------------------
# 契約定数
# --------------------------------------------------------------------------------------

ENV_ROOT_VAR = "C3_TEST_POLICY_ROOT"

TARGET_RELPATHS: dict[str, tuple[str, ...]] = {
    "claude-md": (".claude", "CLAUDE.md"),
    "guidelines": (
        ".claude", "skills", "dev-workflow", "references", "plan-design-guidelines.md",
    ),
    "planner": (".claude", "agents", "planner.md"),
}

LEVEL2_PREFIX = "## "

# P1 のキーワード。内部の空白の有無を問わない部分一致（逐語固定にしない）。
# `ルール 21` は右側に数値境界 `(?!\d)` を置き、上位桁数（`ルール 210`〜`ルール 219`）を
# 含む見出しには当てない（CR-NEW-1。ルール番号が 3 桁に達したときの偽陽性を防ぐ）。
SECTION_KEYWORD_PATTERN = re.compile(r"テストとプロダクトの関係")
RULE21_KEYWORD_PATTERN = re.compile(r"ルール\s*21(?!\d)")

# P2 の番号範囲。WAVE DASH (U+301C) / FULLWIDTH TILDE (U+FF5E) / ASCII TILDE (U+007E) の
# 3 形を対象とし、`ルール` という語に係るものに限る（`R2〜R6` 等の固定集合には当たらない）。
TILDE_CHARS = "〜～~"
RULE_NUMBER_RANGE_PATTERN = re.compile(
    r"ルール\s*\d+\s*[" + TILDE_CHARS + r"]\s*\d+"
)


# --------------------------------------------------------------------------------------
# ルート解決（注入シーム）とファイル読み込み
# --------------------------------------------------------------------------------------

def get_policy_root() -> Path:
    """検査対象ツリーのルートを返す（注入シーム）。

    環境変数 ``C3_TEST_POLICY_ROOT`` が設定されていればそれを、未設定なら
    ``tests/skills/_skill_helpers.py`` の ``WORKTREE_ROOT`` を使う。
    解決は呼び出しごとに行い、モジュール定数へ束縛しない。

    env の値は ``os.path.realpath`` で正規化してから使う（シンボリックリンク・NTFS
    ジャンクション解決後の実体パス）。解決後が実在するディレクトリでない場合は
    ``RuntimeError`` で明示的に失敗する（実リポジトリへ黙って落ちない・SR-V-002）。
    """
    env_root = os.environ.get(ENV_ROOT_VAR)
    if not env_root:
        return WORKTREE_ROOT
    resolved = Path(os.path.realpath(env_root))
    if not resolved.is_dir():
        raise RuntimeError(
            f"{ENV_ROOT_VAR} does not resolve to an existing directory: "
            f"{ascii(env_root)} -> {ascii(str(resolved))}"
        )
    return resolved


def target_path(key: str) -> Path:
    """対象ファイルの絶対パスを返す。パス組み立てはルート解決関数を経由する。"""
    return get_policy_root().joinpath(*TARGET_RELPATHS[key])


def read_target(key: str) -> str:
    """対象ファイルの本文を返す。読み込みは必ずこの関数（＝ルート解決関数）を経由する。"""
    return target_path(key).read_text(encoding="utf-8")


def relpath_of(key: str) -> str:
    """失敗メッセージ用の相対パス文字列（ASCII）を返す。"""
    return "/".join(TARGET_RELPATHS[key])


# --------------------------------------------------------------------------------------
# 性質の判定（本検査と対照検査の双方が同じ純関数を通る）
# --------------------------------------------------------------------------------------

def level2_heading_lines(content: str) -> list[str]:
    """raw 行の先頭が ``## `` で始まる行だけを返す純関数。

    ``### `` 以下の降格した見出し・本文行は含まない（先頭一致のみを見る）。
    """
    return [line for line in content.splitlines() if line.startswith(LEVEL2_PREFIX)]


def has_level2_heading_matching(content: str, pattern: re.Pattern[str]) -> bool:
    """``## `` レベルの見出し行に ``pattern`` が現れるか判定する純関数（P1 の判定）。"""
    return any(pattern.search(line) for line in level2_heading_lines(content))


def find_rule_number_ranges(content: str) -> list[str]:
    """「ルール N〜M」形の番号範囲をすべて返す純関数（P2 の判定の素）。"""
    return RULE_NUMBER_RANGE_PATTERN.findall(content)


def has_rule_number_range(content: str) -> bool:
    """「ルール N〜M」形の番号範囲が 1 件以上あるか判定する純関数（P2 の判定）。"""
    return bool(find_rule_number_ranges(content))


# --------------------------------------------------------------------------------------
# 偽ルートのサンプル本文（恒久的な正・負の対照の正本）
# --------------------------------------------------------------------------------------

def _md(*lines: str) -> str:
    """行の列から Markdown 断片を組み立てる（偽ルート用サンプルの共通形）。"""
    return "".join(line + "\n" for line in lines)


_CLAUDE_MD_HEAD = "# fake CLAUDE.md"
_GUIDELINES_HEAD = "# fake plan-design-guidelines.md"
_PLANNER_HEAD = "# fake planner.md"
_RULE20_HEADING = "## gitignored 成果物の検証経路（ルール 20）"
_SELFCHECK_HEADING = "## 出力直前の自己チェックリスト"

# 充足サンプル（正の対照）
SATISFYING_CLAUDE_MD = _md(
    _CLAUDE_MD_HEAD, "## 根本解決の原則", "## テストとプロダクトの関係",
    "3 段の本文はここでは検査しない（射程の上限）。", "## 設計思想",
)
SATISFYING_GUIDELINES = _md(
    _GUIDELINES_HEAD, _RULE20_HEADING, "## 既存テストの扱いの指示（ルール 21）",
    "21. 本文の文言はここでは検査しない（射程の上限）。",
)
SATISFYING_PLANNER = _md(
    _PLANNER_HEAD, "- guidelines の全ルールと R2〜R6 を遵守する",
    "- guidelines の全ルールと自己チェックリストに違反しない",
)

# 違反サンプル（負の対照）。`downgraded` / `wave-dash` は判定反転の実測にも使う。
VIOLATING_CLAUDE_MD_DOWNGRADED = _md(_CLAUDE_MD_HEAD, "### テストとプロダクトの関係")
VIOLATING_GUIDELINES_DOWNGRADED = _md(
    _GUIDELINES_HEAD, "### 既存テストの扱いの指示（ルール 21）"
)
# 「ルール 21」の上位桁数（`ルール 210`〜`ルール 219`）の見出しだけを置いたサンプル。
# P1(b) のキーワード判定に右側の数値境界が無いと、ルール 21 の節が消えていても緑になる
# （偽陽性・CR-NEW-1）。恒久的な負の対照としてこのサンプルを常設する。
VIOLATING_GUIDELINES_RULE210 = _md(
    _GUIDELINES_HEAD, "## 将来のルールの節（ルール 210）"
)
VIOLATING_PLANNER_WAVE_DASH = _md(
    _PLANNER_HEAD, "- guidelines のルール 1〜20 と R2〜R6 を遵守する"
)

# 違反サンプルの索引（parametrize の id を短く保つため label で引く）
P1A_VIOLATIONS: dict[str, str] = {
    "absent": _md(_CLAUDE_MD_HEAD, "## 根本解決の原則", "## 設計思想"),
    "downgraded": VIOLATING_CLAUDE_MD_DOWNGRADED,
    "buried": _md(
        _CLAUDE_MD_HEAD, "## 設計思想",
        "テストとプロダクトの関係について本文で触れるだけで節を持たない。",
    ),
}
P1B_VIOLATIONS: dict[str, str] = {
    "absent": _md(_GUIDELINES_HEAD, _RULE20_HEADING, _SELFCHECK_HEADING),
    "downgraded": VIOLATING_GUIDELINES_DOWNGRADED,
    "buried": _md(
        _GUIDELINES_HEAD, _SELFCHECK_HEADING,
        "- [ ] ルール 21: 本文で触れるだけで節を持たない。",
    ),
    "higher-digit-210": VIOLATING_GUIDELINES_RULE210,
}
P2_VIOLATIONS: dict[str, str] = {
    "wave-dash-U+301C": VIOLATING_PLANNER_WAVE_DASH,
    "fullwidth-tilde-U+FF5E": _md(_PLANNER_HEAD, "- guidelines のルール 1～21 を遵守する"),
    "ascii-tilde-U+007E": _md(_PLANNER_HEAD, "- guidelines のルール 1~21 を遵守する"),
    "no-spaces": _md(_PLANNER_HEAD, "- guidelines のルール1〜20を遵守する"),
}

# 固定集合の範囲記号のみを含むサンプル（赤にならないことの実測に使う）
FIXED_SET_ONLY_PLANNER = _md(
    _PLANNER_HEAD, "- 自動検査ルール R2〜R6 を遵守する（固定集合であり増えない）",
    "- フェーズ D-1〜D-5 のゲートに従う",
)

FAKE_ROOT_DIRNAME = "fake_root"


def write_fake_root(
    base: Path,
    *,
    claude_md: str = SATISFYING_CLAUDE_MD,
    guidelines: str = SATISFYING_GUIDELINES,
    planner: str = SATISFYING_PLANNER,
    dirname: str = FAKE_ROOT_DIRNAME,
) -> Path:
    """``base`` 配下に検査対象 3 ファイルだけを持つ偽ルートを作り、そのルートを返す。

    本番ファイルは一切改変しない（ルール 18）。

    ``base`` は ``os.path.realpath`` で正規化してから使う。``get_policy_root()`` が env の値を
    realpath 正規化するため、正規化前のパスを期待値に使うと一時ディレクトリ自体がリンクである
    環境（macOS の ``/var`` → ``/private/var`` など）で同一性の assert が成り立たなくなる。
    """
    root = Path(os.path.realpath(base)) / dirname
    contents = {"claude-md": claude_md, "guidelines": guidelines, "planner": planner}
    for key, content in contents.items():
        path = root.joinpath(*TARGET_RELPATHS[key])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def make_dir_link(link: Path, target: Path) -> None:
    """``link`` から ``target`` へのディレクトリリンクを作る（realpath 正規化の検査用）。

    Windows は NTFS ジャンクション（非管理者でも作成できる）・他 OS はシンボリックリンクを
    使う。作成できない環境では skip する（影響を受けるのは正規化の検査 1 件のみ）。
    """
    try:
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(str(target), str(link), target_is_directory=True)
    except (OSError, AttributeError, ImportError) as exc:
        pytest.skip(f"cannot create a directory link here: {ascii(str(exc))}")


def use_fake_root(monkeypatch: pytest.MonkeyPatch, base: Path, **contents: str) -> Path:
    """偽ルートを作り、環境変数シームを差し替えて ``read_target()`` の読み先を移す。"""
    root = write_fake_root(base, **contents)
    monkeypatch.setenv(ENV_ROOT_VAR, str(root))
    return root


# --------------------------------------------------------------------------------------
# P1(a) / P1(b) / P2 の本検査（実ファイル・ルート解決関数を経由して読む）
# --------------------------------------------------------------------------------------

class TestRealFiles:
    """現行の本番ファイルに対する 3 判定。Red 時点ではいずれも赤になる。"""

    @pytest.fixture(autouse=True)
    def _real_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """本検査は実リポジトリを見る（環境に残った注入シームを外す）。"""
        monkeypatch.delenv(ENV_ROOT_VAR, raising=False)

    def test_p1a_claude_md_has_three_tier_section_heading(self) -> None:
        """P1(a): .claude/CLAUDE.md に `## ` レベルの「テストとプロダクトの関係」節がある。"""
        key = "claude-md"
        content = read_target(key)
        assert has_level2_heading_matching(content, SECTION_KEYWORD_PATTERN), (
            f"{relpath_of(key)}: no level-2 heading line matching "
            f"{ascii(SECTION_KEYWORD_PATTERN.pattern)} "
            f"(level-2 headings found: {len(level2_heading_lines(content))})"
        )

    def test_p1b_guidelines_has_rule21_heading(self) -> None:
        """P1(b): plan-design-guidelines.md に `## ` レベルの「ルール 21」見出しがある。"""
        key = "guidelines"
        content = read_target(key)
        assert has_level2_heading_matching(content, RULE21_KEYWORD_PATTERN), (
            f"{relpath_of(key)}: no level-2 heading line matching "
            f"{ascii(RULE21_KEYWORD_PATTERN.pattern)} "
            f"(level-2 headings found: {len(level2_heading_lines(content))})"
        )

    def test_p2_planner_has_no_rule_number_range(self) -> None:
        """P2: planner.md に「ルール N〜M」形の番号範囲が 1 件も無い。"""
        key = "planner"
        found = find_rule_number_ranges(read_target(key))
        assert found == [], (
            f"{relpath_of(key)}: hardcoded rule number range(s) found: "
            f"{ascii(found)} (pattern={ascii(RULE_NUMBER_RANGE_PATTERN.pattern)})"
        )


# --------------------------------------------------------------------------------------
# ルート解決シームが実際に読み先を変えることの実測
# --------------------------------------------------------------------------------------

class TestRootResolutionSeam:
    """`get_policy_root()` 1 関数への集約と、env 差し替えの実効を測る。"""

    def test_unset_env_resolves_to_worktree_root(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """env 未設定なら _skill_helpers.WORKTREE_ROOT に解決される。"""
        monkeypatch.delenv(ENV_ROOT_VAR, raising=False)
        assert get_policy_root() == WORKTREE_ROOT

    def test_read_target_reads_the_file_under_resolved_root(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """env 未設定時の読み込み内容が実リポジトリのファイルと一致する。"""
        monkeypatch.delenv(ENV_ROOT_VAR, raising=False)
        for key in TARGET_RELPATHS:
            expected = WORKTREE_ROOT.joinpath(*TARGET_RELPATHS[key]).read_text(
                encoding="utf-8"
            )
            assert read_target(key) == expected, (
                f"{relpath_of(key)}: read_target() did not read the file "
                "under the resolved root"
            )

    def test_env_override_changes_what_read_target_reads(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """env を差し替えると読み先が偽ルートへ実際に移る（解決が呼び出し時である証拠）。"""
        monkeypatch.delenv(ENV_ROOT_VAR, raising=False)
        real_contents = {key: read_target(key) for key in TARGET_RELPATHS}

        root = use_fake_root(monkeypatch, tmp_path)

        assert get_policy_root() == root
        for key in TARGET_RELPATHS:
            assert target_path(key) == root.joinpath(*TARGET_RELPATHS[key])
            assert read_target(key) != real_contents[key], (
                f"{relpath_of(key)}: env override did not change the read target"
            )
        assert read_target("claude-md") == SATISFYING_CLAUDE_MD
        assert read_target("guidelines") == SATISFYING_GUIDELINES
        assert read_target("planner") == SATISFYING_PLANNER


class TestRootResolutionContainment:
    """env で渡したルートの realpath 正規化と、解決不能なルートでの明示的失敗（SR-V-002）。"""

    def test_env_root_is_realpath_normalized(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """リンク経由の env 値は realpath 解決後の実体ディレクトリになる。"""
        real_root = write_fake_root(tmp_path, dirname="real_root")
        link = tmp_path / "link_root"
        make_dir_link(link, real_root)
        monkeypatch.setenv(ENV_ROOT_VAR, str(link))

        resolved = get_policy_root()

        assert resolved == Path(os.path.realpath(real_root)), (
            f"env root was not realpath-normalized: {ascii(str(resolved))}"
        )
        assert resolved != link, (
            f"resolved root is still the link path: {ascii(str(resolved))}"
        )
        assert read_target("claude-md") == SATISFYING_CLAUDE_MD

    def test_nonexistent_env_root_fails_explicitly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """解決後が実在しないなら明示的に失敗する（実リポジトリへ黙って落ちない）。"""
        monkeypatch.setenv(ENV_ROOT_VAR, str(tmp_path / "missing_root"))
        with pytest.raises(RuntimeError, match=ENV_ROOT_VAR):
            get_policy_root()
        with pytest.raises(RuntimeError, match=ENV_ROOT_VAR):
            read_target("claude-md")

    def test_env_root_pointing_to_a_file_fails_explicitly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """解決後がディレクトリでない（ファイル）なら明示的に失敗する。"""
        not_a_dir = tmp_path / "not_a_dir.md"
        not_a_dir.write_text("x\n", encoding="utf-8")
        monkeypatch.setenv(ENV_ROOT_VAR, str(not_a_dir))
        with pytest.raises(RuntimeError, match=ENV_ROOT_VAR):
            get_policy_root()

    def test_empty_env_value_still_falls_back_to_worktree_root(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """空文字は「未設定」と同じ扱いのまま（既存挙動の固定）。"""
        monkeypatch.setenv(ENV_ROOT_VAR, "")
        assert get_policy_root() == WORKTREE_ROOT


# --------------------------------------------------------------------------------------
# 恒久的な正の対照（充足サンプル → 3 判定が緑）
# --------------------------------------------------------------------------------------

class TestPositiveControls:
    """偽ルートの充足サンプルで 3 判定が成立する。Red 時点から意図的に緑（適用除外）。"""

    def test_p1a_satisfied_on_fake_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        use_fake_root(monkeypatch, tmp_path)
        assert has_level2_heading_matching(
            read_target("claude-md"), SECTION_KEYWORD_PATTERN
        )

    def test_p1b_satisfied_on_fake_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        use_fake_root(monkeypatch, tmp_path)
        assert has_level2_heading_matching(
            read_target("guidelines"), RULE21_KEYWORD_PATTERN
        )

    def test_p2_satisfied_on_fake_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        use_fake_root(monkeypatch, tmp_path)
        assert find_rule_number_ranges(read_target("planner")) == []


# --------------------------------------------------------------------------------------
# 恒久的な負の対照（違反サンプル → 判定が赤側に振れる）
# --------------------------------------------------------------------------------------

class TestNegativeControls:
    """偽ルートの違反サンプルで判定が赤側に振れる（判定関数の恒真化を検出する）。"""

    @pytest.mark.parametrize("label", list(P1A_VIOLATIONS))
    def test_p1a_violation_is_detected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, label: str
    ) -> None:
        use_fake_root(monkeypatch, tmp_path, claude_md=P1A_VIOLATIONS[label])
        assert not has_level2_heading_matching(
            read_target("claude-md"), SECTION_KEYWORD_PATTERN
        ), f"P1(a) violation not detected: {label}"

    @pytest.mark.parametrize("label", list(P1B_VIOLATIONS))
    def test_p1b_violation_is_detected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, label: str
    ) -> None:
        use_fake_root(monkeypatch, tmp_path, guidelines=P1B_VIOLATIONS[label])
        assert not has_level2_heading_matching(
            read_target("guidelines"), RULE21_KEYWORD_PATTERN
        ), f"P1(b) violation not detected: {label}"

    @pytest.mark.parametrize("label", list(P2_VIOLATIONS))
    def test_p2_violation_is_detected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, label: str
    ) -> None:
        use_fake_root(monkeypatch, tmp_path, planner=P2_VIOLATIONS[label])
        found = find_rule_number_ranges(read_target("planner"))
        assert found != [], f"P2 violation not detected: {label}"

    def test_p2_does_not_flag_fixed_set_ranges(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """固定集合の範囲記号（`R2〜R6` / `D-1〜D-5`）では赤にならない（誤ヒットの回帰ガード）。

        これを外すと planner.md:37 の `R2〜R6` に当たり恒久赤になる。
        """
        use_fake_root(monkeypatch, tmp_path, planner=FIXED_SET_ONLY_PLANNER)
        found = find_rule_number_ranges(read_target("planner"))
        assert found == [], f"fixed-set range wrongly flagged: {ascii(found)}"


# --------------------------------------------------------------------------------------
# 判定結果が入れ替わることの実測（ルール 19 の両方向反転・ADR-4 の代替宣言）
# --------------------------------------------------------------------------------------

class TestVerdictSwaps:
    """同一の判定純関数が、充足サンプルと違反サンプルで True / False に入れ替わる。"""

    def test_p1a_verdict_swaps(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        use_fake_root(monkeypatch, tmp_path, claude_md=SATISFYING_CLAUDE_MD, dirname="ok")
        satisfied = has_level2_heading_matching(
            read_target("claude-md"), SECTION_KEYWORD_PATTERN
        )
        use_fake_root(
            monkeypatch, tmp_path, claude_md=VIOLATING_CLAUDE_MD_DOWNGRADED, dirname="ng"
        )
        violated = has_level2_heading_matching(
            read_target("claude-md"), SECTION_KEYWORD_PATTERN
        )
        assert (satisfied, violated) == (True, False)

    def test_p1b_verdict_swaps(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        use_fake_root(monkeypatch, tmp_path, guidelines=SATISFYING_GUIDELINES, dirname="ok")
        satisfied = has_level2_heading_matching(
            read_target("guidelines"), RULE21_KEYWORD_PATTERN
        )
        use_fake_root(
            monkeypatch, tmp_path, guidelines=VIOLATING_GUIDELINES_DOWNGRADED, dirname="ng"
        )
        violated = has_level2_heading_matching(
            read_target("guidelines"), RULE21_KEYWORD_PATTERN
        )
        assert (satisfied, violated) == (True, False)

    def test_p2_verdict_swaps(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        use_fake_root(monkeypatch, tmp_path, planner=SATISFYING_PLANNER, dirname="ok")
        satisfied = has_rule_number_range(read_target("planner"))
        use_fake_root(
            monkeypatch, tmp_path, planner=VIOLATING_PLANNER_WAVE_DASH, dirname="ng"
        )
        violated = has_rule_number_range(read_target("planner"))
        # P2 は否定形のため、充足側が False・違反側が True になる。
        assert (satisfied, violated) == (False, True)


# --------------------------------------------------------------------------------------
# 判定純関数の単体検査（content を直接渡す・ファイル I/O を挟まない）
# --------------------------------------------------------------------------------------

class TestPureFunctions:
    """本検査と対照検査が通るのと同じ純関数を content 直渡しで検査する。"""

    def test_level2_heading_lines_excludes_downgraded_and_body(self) -> None:
        content = "# h1\n## h2\n### h3\n#### h4\n本文\n"
        assert level2_heading_lines(content) == ["## h2"]

    def test_level2_heading_lines_requires_space_after_hashes(self) -> None:
        assert level2_heading_lines("##nospace\n") == []

    def test_has_level2_heading_matching_tolerates_internal_space(self) -> None:
        assert has_level2_heading_matching("## X（ルール21）\n", RULE21_KEYWORD_PATTERN)
        assert has_level2_heading_matching("## X（ルール 21）\n", RULE21_KEYWORD_PATTERN)

    def test_has_level2_heading_matching_ignores_other_rule_numbers(self) -> None:
        assert not has_level2_heading_matching(
            "## X（ルール 20）\n", RULE21_KEYWORD_PATTERN
        )

    def test_has_level2_heading_matching_ignores_higher_digit_rule_numbers(self) -> None:
        """`ルール 210`〜`ルール 219` のような上位桁数には当たらない（右側の数値境界）。"""
        for heading in ("## X（ルール 210）\n", "## X（ルール 219）\n", "## ルール 2100\n"):
            assert not has_level2_heading_matching(heading, RULE21_KEYWORD_PATTERN), (
                f"wrongly matched a higher-digit rule number: {ascii(heading)}"
            )

    def test_has_level2_heading_matching_still_matches_rule21_boundaries(self) -> None:
        """右側の数値境界を入れても「ルール 21」の実在形には当たり続ける。"""
        for heading in (
            "## X（ルール 21）\n", "## ルール 21\n", "## ルール 21・22 の節\n",
            "## 既存テストの扱いの指示（ルール 21）\n",
        ):
            assert has_level2_heading_matching(heading, RULE21_KEYWORD_PATTERN), (
                f"failed to match a real rule-21 heading: {ascii(heading)}"
            )

    def test_find_rule_number_ranges_collects_every_occurrence(self) -> None:
        content = "ルール 1〜20 と ルール 1〜21 と R2〜R6\n"
        assert len(find_rule_number_ranges(content)) == 2

    def test_find_rule_number_ranges_ignores_non_rule_ranges(self) -> None:
        assert find_rule_number_ranges("R2〜R6 / D-1〜D-5 / 1〜20\n") == []

    def test_has_rule_number_range_is_false_for_open_expression(self) -> None:
        assert not has_rule_number_range("全ルールと R2〜R6 を遵守する\n")
