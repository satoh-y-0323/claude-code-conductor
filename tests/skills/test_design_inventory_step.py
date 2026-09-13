"""
tests/skills/test_design_inventory_step.py

番人テスト: 「既存機能の棚卸し」工程の契約が読み手 5 ファイルに到達しているかを固定する。

正本: `.claude/reports/architecture-report-20260913-205401.md`（改訂 5・§2-0 節契約 / §4 検証方針）

測る性質:

- **P1 契約の到達**: キーワードが 5 ファイルすべてに含まれ、いずれのファイルでも raw 行の先頭が
  ``## `` で始まる行（コードフェンス内・引用内を含む）にはキーワードが現れない
  （``### `` 以下の見出し行は許容）
- **P2 配置の正しさ**: architect.md / design-critic.md の Before 区間、design-critic-rubric.md の
  レンズ 3 区間、dev-workflow SKILL.md のフェーズ B 区間にキーワードが現れる
  （design-rubric.md は区間検査なし・P1 のみ）
- **P3 SSOT**: architect.md と design-rubric.md が judgment-principles.md のパス文字列を含み、
  5 ファイルのいずれも規律本文の逐語を複写しない

設計上の制約（plan の契約）:

- ルート解決は ``get_inventory_root()`` の 1 関数に集約し、環境変数 ``C3_INVENTORY_ROOT``
  （未設定時は ``_skill_helpers.WORKTREE_ROOT``）で差し替える。ファイルの読み込みは
  ``read_target()`` 経由に限る
- ``_skill_helpers`` からは content を引数に取る純関数（``find_section_range``）のみを再利用する。
  太字マーカー区間は未対応の形のため本ファイル内の最小ヘルパー ``find_marker_block()`` で切り出す
- 区間境界は文書構造（見出し・太字マーカー）から導出し、固定行番号を使わない
- ファイルは encoding='utf-8' で読む。非 ASCII を print しない
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, NamedTuple

import pytest

from tests.skills._skill_helpers import WORKTREE_ROOT, find_section_range

# --------------------------------------------------------------------------------------
# 契約定数
# --------------------------------------------------------------------------------------

KEYWORD = "既存機能の棚卸し"
SSOT_PATH = ".claude/rules/judgment-principles.md"
DISCIPLINE_VERBATIM = "発火しうる経路が無い"
LEVEL2_PREFIX = "## "

TARGET_RELPATHS: dict[str, tuple[str, ...]] = {
    "architect": (".claude", "agents", "architect.md"),
    "design-rubric": (
        ".claude", "skills", "dev-workflow", "references", "design-rubric.md",
    ),
    "design-critic-rubric": (
        ".claude", "skills", "dev-workflow", "references", "design-critic-rubric.md",
    ),
    "design-critic": (".claude", "agents", "design-critic.md"),
    "dev-workflow-skill": (".claude", "skills", "dev-workflow", "SKILL.md"),
}

ALL_TARGETS = tuple(TARGET_RELPATHS)

# P3 前半（SSOT パス参照）が適用されるファイル
SSOT_TARGETS = ("architect", "design-rubric")

# P2 の区間定義。種別ごとに要素数が異なるため、種別を Literal で固定した NamedTuple
# 2 種（MarkerSpec / HeadingSpec）で表現する（tuple[str, ...] では spec[0] の値によって
# 後続要素数が変わることが型注釈から読み取れないため）。


class MarkerSpec(NamedTuple):
    """太字マーカー区間の仕様（開始行〜終了行の直前まで）。"""

    kind: Literal["marker"]
    start_marker: str
    end_marker: str


class HeadingSpec(NamedTuple):
    """見出し区間の仕様（見出し行〜同レベル以上の次の見出しの直前まで）。"""

    kind: Literal["heading"]
    prefix: str


SectionSpec = MarkerSpec | HeadingSpec

SECTION_SPECS: dict[str, SectionSpec] = {
    "architect": MarkerSpec("marker", "**Before:**", "**During:**"),
    "design-critic": MarkerSpec("marker", "**Before:**", "**During:**"),
    "design-critic-rubric": HeadingSpec("heading", "## レンズ 3"),
    "dev-workflow-skill": HeadingSpec("heading", "## フェーズ B"),
}

SECTION_TARGETS = tuple(SECTION_SPECS)


# --------------------------------------------------------------------------------------
# ルート解決（注入シーム）とファイル読み込み
# --------------------------------------------------------------------------------------

def get_inventory_root() -> Path:
    """検査対象ツリーのルートを返す（注入シーム）。

    環境変数 ``C3_INVENTORY_ROOT`` が設定されていればそれを、未設定なら
    ``tests/skills/_skill_helpers.py`` の ``WORKTREE_ROOT`` を使う。
    ルート解決はこの関数 1 本に集約し、他所で parents 計算を持たない。
    """
    env_root = os.environ.get("C3_INVENTORY_ROOT")
    if env_root:
        return Path(env_root)
    return WORKTREE_ROOT


def read_target(key: str) -> str:
    """対象ファイルの本文を返す。読み込みは必ずこの関数（＝ルート解決関数）を経由する。"""
    path = get_inventory_root().joinpath(*TARGET_RELPATHS[key])
    return path.read_text(encoding="utf-8")


def assert_contains(key: str, needle: str, *, present: bool = True) -> None:
    """``read_target(key)`` を呼び、``needle`` の有無を ``present`` に従って assert する。

    単純メンバーシップ assert（P1a・P3a・P3b で共通の構造）の重複を切り出した
    共通アサーションヘルパー。失敗メッセージは対象ファイルパス・``needle``・
    ``present``/``absent`` の別を含み、pytest の失敗表示だけで原因箇所が
    一意に特定できる。
    """
    content = read_target(key)
    relpath = "/".join(TARGET_RELPATHS[key])
    if present:
        assert needle in content, f"{relpath} に {needle!r} が無い（present=True）"
    else:
        assert needle not in content, f"{relpath} に {needle!r} がある（present=False）"


# --------------------------------------------------------------------------------------
# 区間切り出し（太字マーカー形は _skill_helpers に無いため最小ヘルパーを置く）
# --------------------------------------------------------------------------------------

def find_marker_block(content: str, start_marker: str, end_marker: str) -> str | None:
    """行頭が ``start_marker`` の行から、行頭が ``end_marker`` の行の直前までを返す。

    固定行番号を使わず、太字マーカー行という文書構造から境界を導出する。
    開始マーカーが見つからない場合は None を返す（終了マーカーが無い場合は末尾まで）。
    """
    lines = content.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if start is None:
            if line.startswith(start_marker):
                start = idx
            continue
        if line.startswith(end_marker):
            return "\n".join(lines[start:idx])
    if start is None:
        return None
    return "\n".join(lines[start:])


def extract_target_section(content: str, spec: SectionSpec) -> str | None:
    """P2 の区間仕様に従って本文から区間テキストを切り出す。見つからない場合は None。"""
    if isinstance(spec, MarkerSpec):
        return find_marker_block(content, spec.start_marker, spec.end_marker)
    start, end = find_section_range(content, spec.prefix)
    if start == -1:
        return None
    return content[start:end]


# --------------------------------------------------------------------------------------
# 性質の判定（本番検査と検知力実証の双方が同じ関数を通る）
# --------------------------------------------------------------------------------------

def level2_heading_lines_with_keyword(content: str) -> list[str]:
    """raw 行の先頭が ``## `` で始まりキーワードを含む行を返す（フェンス内・引用内も対象）。"""
    return [
        line
        for line in content.splitlines()
        if line.startswith(LEVEL2_PREFIX) and KEYWORD in line
    ]


# --------------------------------------------------------------------------------------
# P1: 契約の到達
# --------------------------------------------------------------------------------------

class TestP1KeywordReach:
    """P1: キーワードが 5 ファイルに届き、レベル 2 見出し行には現れない。"""

    @pytest.mark.parametrize("key", ALL_TARGETS)
    def test_keyword_present(self, key: str) -> None:
        """P1a - 契約キーワードが対象ファイルに含まれること。"""
        assert_contains(key, KEYWORD, present=True)

    @pytest.mark.parametrize("key", ALL_TARGETS)
    def test_keyword_not_in_level2_heading(self, key: str) -> None:
        """P1b - raw 行頭 ``## `` の行（フェンス内・引用内を含む）にキーワードが無いこと。"""
        offenders = level2_heading_lines_with_keyword(read_target(key))
        assert offenders == [], (
            f"{'/'.join(TARGET_RELPATHS[key])} のレベル 2 見出し行にキーワードがある: {offenders}"
        )


# --------------------------------------------------------------------------------------
# P2: 配置の正しさ
# --------------------------------------------------------------------------------------

class TestP2Placement:
    """P2: キーワードが所定の区間内に現れる（区間境界は文書構造から導出）。"""

    @pytest.mark.parametrize("key", SECTION_TARGETS)
    def test_keyword_in_required_section(self, key: str) -> None:
        """P2 - 対象ファイルの所定区間にキーワードが現れること。"""
        spec = SECTION_SPECS[key]
        section = extract_target_section(read_target(key), spec)
        assert section is not None, (
            f"{'/'.join(TARGET_RELPATHS[key])} の区間 {spec} が見つからない（文書構造の変化）"
        )
        assert KEYWORD in section, (
            f"{'/'.join(TARGET_RELPATHS[key])} の区間 {spec} 内にキーワードが無い"
        )


# --------------------------------------------------------------------------------------
# P3: SSOT
# --------------------------------------------------------------------------------------

class TestP3Ssot:
    """P3: 規律はフルパス参照で指し、本文を複写しない。"""

    @pytest.mark.parametrize("key", SSOT_TARGETS)
    def test_judgment_principles_path_referenced(self, key: str) -> None:
        """P3a - judgment-principles.md のパス文字列を含むこと。"""
        assert_contains(key, SSOT_PATH, present=True)

    @pytest.mark.parametrize("key", ALL_TARGETS)
    def test_discipline_body_not_duplicated(self, key: str) -> None:
        """P3b - 規律本文の逐語を複写していないこと。"""
        assert_contains(key, DISCIPLINE_VERBATIM, present=False)


# --------------------------------------------------------------------------------------
# 検知力の実証（plan-design-guidelines ルール 19）
#
# tmp_path に 5 ファイル相当の偽ルートを組み、性質ごとに、その性質が適用される全ファイルへ
# 正の対照（充足）と負の対照を 1 件ずつ与えて緑と赤が入れ替わることを実測する。
# 本番ファイルは一時改変しない。
# --------------------------------------------------------------------------------------

def _fake_contents() -> dict[str, str]:
    """全性質を充足する偽ルートの本文（各ファイルでキーワードはちょうど 1 回出現）。"""
    return {
        "architect": (
            "# Architect\n"
            "\n"
            "## Workflow\n"
            "\n"
            "**Before:**\n"
            "- requirements-report を Read する\n"
            f"- 触る領域の `{KEYWORD}` を行う。判定規律は `{SSOT_PATH}` §2\n"
            "\n"
            "**During:**\n"
            "- 技術選定の根拠とトレードオフを必ず記録する\n"
            "\n"
            "**After:**\n"
            "- architecture-report に Write する\n"
        ),
        "design-rubric": (
            "# Design Rubric\n"
            "\n"
            "## 動的確認のフロー\n"
            "\n"
            f"### Step 1: 既知情報の取り込みと{KEYWORD}\n"
            f"- 「無い」の申告の規律は `{SSOT_PATH}` §2\n"
        ),
        "design-critic-rubric": (
            "# Design Critic Rubric\n"
            "\n"
            "## レンズ 2: 曖昧さ\n"
            "\n"
            "### 着眼点\n"
            "- 用語の多義性\n"
            "\n"
            "## レンズ 3: 抜け漏れ\n"
            "\n"
            "### 着眼点\n"
            f"- `{KEYWORD}` の欠落\n"
            "\n"
            "## design-review-report 出力形式\n"
            "\n"
            "- finding ID 体系は変更しない\n"
        ),
        "design-critic": (
            "# Design Critic\n"
            "\n"
            "## Workflow\n"
            "\n"
            "**Before:**\n"
            "- design-critic-rubric.md を Read する\n"
            f"- architecture-report の `{KEYWORD}` 節と現物を突き合わせる\n"
            "\n"
            "**During:**\n"
            "- 3 レンズを順に適用する\n"
            "\n"
            "**After:**\n"
            "- design-review-report に Write する\n"
        ),
        "dev-workflow-skill": (
            "# dev-workflow\n"
            "\n"
            "## フェーズ A: ヒアリング\n"
            "\n"
            "- interviewer を起動する\n"
            "\n"
            "## フェーズ B: 設計\n"
            "\n"
            "### B-1〜B-2: 動的設計確認（ルーブリック型）\n"
            f"- 床 4 観点の確認に入る前に `{KEYWORD}` を行う\n"
            "\n"
            "## フェーズ C: 計画\n"
            "\n"
            "- planner を起動する\n"
        ),
    }


def _write_fake_root(tmp_path: Path, contents: dict[str, str]) -> Path:
    """偽ルートへ 5 ファイルを配置してルートパスを返す。"""
    root = tmp_path / "fake_root"
    for key, relpath in TARGET_RELPATHS.items():
        path = root.joinpath(*relpath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents[key], encoding="utf-8")
    return root


def _install_fake_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: dict[str, str]
) -> None:
    """偽ルートを組み、注入シーム（環境変数）経由で検査対象を差し替える。"""
    root = _write_fake_root(tmp_path, contents)
    monkeypatch.setenv("C3_INVENTORY_ROOT", str(root))


# --- 負の対照を作る変異（いずれも本番ファイルではなく偽ルートの本文に対して適用する） ---

def _drop_keyword(text: str) -> str:
    """キーワードを取り除く（P1a の負の対照）。"""
    return text.replace(KEYWORD, "設計の検討")


def _add_plain_level2_heading(text: str) -> str:
    """行頭 ``## `` の平文見出しにキーワードを置く（P1b の負の対照・フェンス外）。"""
    return text + f"\n## 付録: {KEYWORD}（見出しに置いた例）\n"


def _add_fenced_level2_heading(text: str) -> str:
    """コードフェンス内の行頭 ``## `` 行にキーワードを置く（P1b の負の対照・フェンス内）。"""
    fence = "```"
    return text + f"\n{fence}markdown\n## 1. {KEYWORD}（射程）\n{fence}\n"


def _move_keyword_out_of_section(text: str) -> str:
    """キーワードを区間外へ移す（P2 の負の対照）。ファイル内には残すため P1a は緑のまま。"""
    return _drop_keyword(text) + f"\n- 付録: `{KEYWORD}` は区間外のここに書かれている\n"


def _drop_ssot_path(text: str) -> str:
    """SSOT のパス参照を取り除く（P3a の負の対照）。"""
    return text.replace(SSOT_PATH, "（参照は書かない）")


def _add_discipline_copy(text: str) -> str:
    """規律本文の逐語を複写する（P3b の負の対照）。"""
    return text + f"\n- 到達可能性は「{DISCIPLINE_VERBATIM}」で判定する（複写）\n"


def _mutate(key: str, mutation) -> dict[str, str]:
    """1 ファイルだけに変異を当てた偽ルート本文を返す。"""
    contents = _fake_contents()
    contents[key] = mutation(contents[key])
    return contents


class TestDetectionPowerP1:
    """P1 の検知力: 適用 5 ファイルそれぞれで正・負の対照が緑/赤に入れ替わる。"""

    @pytest.mark.parametrize("key", ALL_TARGETS)
    def test_keyword_presence_flips(
        self, key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P1a - 正の対照で検出 True・キーワード削除で False に反転する。"""
        _install_fake_root(tmp_path, monkeypatch, _fake_contents())
        assert KEYWORD in read_target(key), "正の対照が緑にならない（対照の組み方の誤り）"

        _install_fake_root(tmp_path, monkeypatch, _mutate(key, _drop_keyword))
        assert KEYWORD not in read_target(key), "負の対照（キーワード削除）を検知できない"

    @pytest.mark.parametrize(
        "mutation_name,mutation",
        [
            ("plain", _add_plain_level2_heading),
            ("fenced", _add_fenced_level2_heading),
        ],
    )
    @pytest.mark.parametrize("key", ALL_TARGETS)
    def test_level2_heading_flips(
        self,
        key: str,
        mutation_name: str,
        mutation,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """P1b - 正の対照で違反 0 件・``## `` 行へ置くと違反 1 件に反転する（平文/フェンス内）。"""
        _install_fake_root(tmp_path, monkeypatch, _fake_contents())
        assert level2_heading_lines_with_keyword(read_target(key)) == [], (
            "正の対照が緑にならない（対照の組み方の誤り）"
        )

        _install_fake_root(tmp_path, monkeypatch, _mutate(key, mutation))
        offenders = level2_heading_lines_with_keyword(read_target(key))
        assert len(offenders) == 1, (
            f"負の対照（{mutation_name} のレベル 2 見出し）を検知できない: {offenders}"
        )


class TestDetectionPowerP2:
    """P2 の検知力: 適用 4 ファイルそれぞれで区間内/区間外が緑/赤に入れ替わる。"""

    @pytest.mark.parametrize("key", SECTION_TARGETS)
    def test_section_placement_flips(
        self, key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P2 - 区間内にあれば True・区間外へ移すと False に反転する（全文では緑のまま）。"""
        spec = SECTION_SPECS[key]

        _install_fake_root(tmp_path, monkeypatch, _fake_contents())
        section = extract_target_section(read_target(key), spec)
        assert section is not None, "正の対照で区間が切り出せない（ヘルパーの誤り）"
        assert KEYWORD in section, "正の対照が緑にならない（対照の組み方の誤り）"

        _install_fake_root(
            tmp_path, monkeypatch, _mutate(key, _move_keyword_out_of_section)
        )
        content = read_target(key)
        moved_section = extract_target_section(content, spec)
        assert moved_section is not None, "負の対照で区間が切り出せない（ヘルパーの誤り）"
        assert KEYWORD in content, "負の対照はファイル全文にはキーワードを残す組み方である"
        assert KEYWORD not in moved_section, "負の対照（区間外への移動）を検知できない"


class TestDetectionPowerP3:
    """P3 の検知力: パス参照 2 ファイル・複写禁止 5 ファイルで緑/赤が入れ替わる。"""

    @pytest.mark.parametrize("key", SSOT_TARGETS)
    def test_ssot_path_flips(
        self, key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P3a - 正の対照でパス参照あり・削除すると無しに反転する。"""
        _install_fake_root(tmp_path, monkeypatch, _fake_contents())
        assert SSOT_PATH in read_target(key), "正の対照が緑にならない（対照の組み方の誤り）"

        _install_fake_root(tmp_path, monkeypatch, _mutate(key, _drop_ssot_path))
        assert SSOT_PATH not in read_target(key), "負の対照（パス参照の削除）を検知できない"

    @pytest.mark.parametrize("key", ALL_TARGETS)
    def test_discipline_copy_flips(
        self, key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P3b - 正の対照で複写なし・複写を入れると検知に反転する。"""
        _install_fake_root(tmp_path, monkeypatch, _fake_contents())
        assert DISCIPLINE_VERBATIM not in read_target(key), (
            "正の対照が緑にならない（対照の組み方の誤り）"
        )

        _install_fake_root(tmp_path, monkeypatch, _mutate(key, _add_discipline_copy))
        assert DISCIPLINE_VERBATIM in read_target(key), (
            "負の対照（規律本文の複写）を検知できない"
        )


class TestInjectionSeam:
    """ルート解決の注入シームが効いていること（既定値と env 差し替えの双方）。"""

    def test_default_root_is_worktree_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """env 未設定なら _skill_helpers.WORKTREE_ROOT を使う。"""
        monkeypatch.delenv("C3_INVENTORY_ROOT", raising=False)
        assert get_inventory_root() == WORKTREE_ROOT

    def test_env_override_is_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """env 設定時はそのルート配下のファイルを読む。"""
        _install_fake_root(tmp_path, monkeypatch, _fake_contents())
        assert get_inventory_root() == tmp_path / "fake_root"
        assert "# Architect" in read_target("architect")
