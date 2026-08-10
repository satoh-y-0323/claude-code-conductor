"""
S2: dev-workflow SKILL.md 構造是正の到達目標状態を検査する静的テスト。

検査は純テキスト走査（pathlib + 文字列 / 正規表現）のみで行い、subprocess・実行系は使わない。

テスト群の区分:

- **Red 群** (`test_red_*`): 到達目標状態でのみ緑になる。改訂前の SKILL.md / references に対しては赤。
  - references 2 ファイル（c3-omission-declaration.md / record-protocol.md）の実在と内部アンカー（C 群 / D 群）
  - SKILL.md から references への導線（B 群）
  - C-3 ステップ 0 の構造化見出し（A 群）
  - ⑦天秤の E-3 節での明文化と C-3 節からの参照（E 群）
- **Green 群** (`test_green_*`): 現行でも到達目標でも緑のまま維持される **番人**。
  C-3 ステップ 0 の fail-safe 本文（複数マッチ不成立・優先順位表・消費済マーカー）が
  構造是正の過程で失われないことを見張る。最初から Pass するのは仕様であり、
  削除・反転・改変してはならない。

検知力（ルール 19）: 静的テキスト検査のため do-nothing スタブ 2 種は原理的に非適用。
代替として「改訂前 SKILL.md で赤（test-s2 で実測）・改訂後で緑（confirm-s2 で実測）」の
両状態反転をもって双方向の実証とする。
"""

from __future__ import annotations

import sys
from pathlib import Path

from tests.skills._skill_helpers import SKILLS_DIR, read_dev_workflow_skill

# --------------------------------------------------------------------------
# 対象パス
# --------------------------------------------------------------------------

DEV_WORKFLOW_DIR = SKILLS_DIR / "dev-workflow"
REFERENCES_DIR = DEV_WORKFLOW_DIR / "references"
OMISSION_DECLARATION_PATH = REFERENCES_DIR / "c3-omission-declaration.md"
RECORD_PROTOCOL_PATH = REFERENCES_DIR / "record-protocol.md"

# --------------------------------------------------------------------------
# 共有アンカー literal（impl 側 prompt と同一文字列。逐語で含有判定する）
# --------------------------------------------------------------------------

# A 群: C-3 ステップ 0 の構造化見出し（内部に装飾記号を挟まず 1 行に逐語で置かれる）
A_GROUP_STEP0_HEADINGS = (
    "(1) 判定対象の抽出",
    "(2) 複数マッチ検査",
    "(3) 優先順位表",
)

# B 群: SKILL.md から references への導線
B_GROUP_OMISSION_LINK = "references/c3-omission-declaration.md"
B_GROUP_RECORD_LINK = "references/record-protocol.md"

# C 群: references/c3-omission-declaration.md 内のアンカー
C_GROUP_ANCHORS = (
    "直接反映のみ",
    "帰属アンカー",
    "上流文書との無差分",
    "新規導入なし",
    "C-3省略宣言: plan-report-",
    "現在地:",
)

# D 群: references/record-protocol.md 内のアンカー
D_GROUP_ANCHORS = (
    "--complexity",
    "--execution",
    "帰属根拠:明確",
    "帰属根拠:要判断",
)

# E 群: ⑦天秤（E-3 節）とその C-3 節からの参照
E_GROUP_BALANCE_ANCHORS = (
    "2 案比較",
    "PR diff 量",
    "トリガー付き起票",
    "機構自体の保守コスト",
)
E_GROUP_C3_REFERENCE = "E-3 の裁定の判断基準を適用する"

# 番人群（C-3 ステップ 0 の fail-safe 本文）は各 test_green_* 内に逐語で置く。
# 番人の判定材料を定数へ寄せると一括改変で番人ごと無力化できてしまうため。

# --------------------------------------------------------------------------
# 節境界の定義（行頭一致の見出し・強調行。終端は名指しの literal を使う）
# --------------------------------------------------------------------------

SECTION_C2 = ("### C-2: plan-report の生成と承認", "### C-3: 計画監査ゲート（opt-in）")
SECTION_C3 = ("### C-3: 計画監査ゲート（opt-in）", "## フェーズ D: 実装")
SECTION_C3_STEP0 = ("**ステップ 0: 転記行の確認（HITL 専用）**", "**ステップ 1:")
SECTION_TIER_ROUTING = ("## tier-routing 結果記録の運用", "## フェーズ A: ヒアリング")
SECTION_E3 = ("### E-3:", "## 引き継ぎバックログの照合")


# --------------------------------------------------------------------------
# 節の切り出し
#
# `_skill_helpers.extract_section()` / `find_section_range()` は「次の `## ` 見出しまで」
# 固定であり `### ` / 強調行の境界に非対応（同ヘルパーの docstring に明記）。
# S2 で必要な境界は `### ` 見出しと `**ステップ N:` 強調行なので、ここで専用に切り出す。
# 重複定義を避けるためファイル読み込みは `read_dev_workflow_skill()` を流用する。
# --------------------------------------------------------------------------


def _fence_mask(lines: list[str]) -> list[bool]:
    """各行がコードフェンス内（フェンス区切り行自体を含む）かどうかの真偽値列を返す。

    見出し様の文字列がコードフェンス内に現れても節境界として誤マッチさせないための前処理。
    """
    mask: list[bool] = []
    in_fence = False
    for line in lines:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            mask.append(True)  # フェンス区切り行自体は境界候補にしない
        else:
            mask.append(in_fence)
    return mask


def _find_line_index(lines: list[str], mask: list[bool], prefix: str, start: int = 0) -> int:
    """コードフェンス外で `prefix` から始まる最初の行の index を返す。見つからなければ -1。"""
    for idx in range(start, len(lines)):
        if mask[idx]:
            continue
        if lines[idx].startswith(prefix):
            return idx
    return -1


def extract_range(content: str, start_prefix: str, end_prefix: str) -> str:
    """`start_prefix` で始まる行から `end_prefix` で始まる行の直前までを返す。

    どちらもコードフェンス外の行頭一致でのみ判定する。
    開始行が見つからない場合は空文字列、終端行が見つからない場合は末尾までを返す。
    """
    lines = content.splitlines()
    mask = _fence_mask(lines)
    start = _find_line_index(lines, mask, start_prefix)
    if start < 0:
        return ""
    end = _find_line_index(lines, mask, end_prefix, start + 1)
    if end < 0:
        end = len(lines)
    return "\n".join(lines[start:end])


def _section(bounds: tuple[str, str]) -> str:
    """SKILL.md から節境界定義タプルに対応する範囲を切り出す。"""
    return extract_range(read_dev_workflow_skill(), bounds[0], bounds[1])


# ==========================================================================
# Red 群: 到達目標状態でのみ緑になる
# ==========================================================================


class TestReferenceFilesExist:
    """Red - 分離先 references 2 ファイルの実在。"""

    def test_red_c3_omission_declaration_file_exists(self):
        assert OMISSION_DECLARATION_PATH.is_file(), (
            f"RED FAIL: {OMISSION_DECLARATION_PATH} が存在しない"
        )

    def test_red_record_protocol_file_exists(self):
        assert RECORD_PROTOCOL_PATH.is_file(), (
            f"RED FAIL: {RECORD_PROTOCOL_PATH} が存在しない"
        )


class TestReferenceFileAnchors:
    """Red - 分離先 references の内容アンカー（C 群 / D 群）。"""

    def test_red_c_group_anchors_in_omission_declaration(self):
        """C 群 6 literal が c3-omission-declaration.md に揃っていること。"""
        assert OMISSION_DECLARATION_PATH.is_file(), (
            f"RED FAIL: {OMISSION_DECLARATION_PATH} が存在しない"
        )
        content = OMISSION_DECLARATION_PATH.read_text(encoding="utf-8")
        missing = [a for a in C_GROUP_ANCHORS if a not in content]
        assert not missing, f"RED FAIL: C 群アンカー欠落 {missing} in {OMISSION_DECLARATION_PATH.name}"

    def test_red_d_group_anchors_in_record_protocol(self):
        """D 群 4 literal が record-protocol.md に揃っていること。"""
        assert RECORD_PROTOCOL_PATH.is_file(), (
            f"RED FAIL: {RECORD_PROTOCOL_PATH} が存在しない"
        )
        content = RECORD_PROTOCOL_PATH.read_text(encoding="utf-8")
        missing = [a for a in D_GROUP_ANCHORS if a not in content]
        assert not missing, f"RED FAIL: D 群アンカー欠落 {missing} in {RECORD_PROTOCOL_PATH.name}"


class TestSkillNavigationLinks:
    """Red - B 群: SKILL.md 各節から references への導線が張られていること。"""

    def test_red_omission_link_in_c2_section(self):
        section = _section(SECTION_C2)
        assert section, "RED FAIL: C-2 節を切り出せない"
        count = section.count(B_GROUP_OMISSION_LINK)
        assert count >= 1, (
            f"RED FAIL: C-2 節の '{B_GROUP_OMISSION_LINK}' 出現数 {count}、期待 >= 1"
        )

    def test_red_omission_link_in_c3_section(self):
        section = _section(SECTION_C3)
        assert section, "RED FAIL: C-3 節を切り出せない"
        count = section.count(B_GROUP_OMISSION_LINK)
        assert count >= 1, (
            f"RED FAIL: C-3 節の '{B_GROUP_OMISSION_LINK}' 出現数 {count}、期待 >= 1"
        )

    def test_red_record_protocol_link_in_tier_routing_section(self):
        section = _section(SECTION_TIER_ROUTING)
        assert section, "RED FAIL: 冒頭 tier-routing 記録節を切り出せない"
        count = section.count(B_GROUP_RECORD_LINK)
        assert count >= 1, (
            f"RED FAIL: tier-routing 記録節の '{B_GROUP_RECORD_LINK}' 出現数 {count}、期待 >= 1"
        )

    def test_red_record_protocol_link_in_e3_section(self):
        section = _section(SECTION_E3)
        assert section, "RED FAIL: E-3 節を切り出せない"
        count = section.count(B_GROUP_RECORD_LINK)
        assert count >= 1, (
            f"RED FAIL: E-3 節の '{B_GROUP_RECORD_LINK}' 出現数 {count}、期待 >= 1"
        )


class TestC3Step0Structure:
    """Red - A 群: C-3 ステップ 0 が 3 段の構造化見出しを持つこと。"""

    def test_red_a_group_headings_in_c3_step0(self):
        """A 群 3 literal が C-3 ステップ 0 範囲内に、装飾を挟まず 1 行内に逐語で存在すること。"""
        section = _section(SECTION_C3_STEP0)
        assert section, "RED FAIL: C-3 ステップ 0 範囲を切り出せない"
        lines = section.splitlines()
        missing = [h for h in A_GROUP_STEP0_HEADINGS if not any(h in line for line in lines)]
        assert not missing, (
            f"RED FAIL: A 群見出し欠落 {missing}（C-3 ステップ 0 範囲内・1 行逐語で必要）"
        )


class TestBalanceCriteria:
    """Red - E 群: ⑦天秤の E-3 節での明文化と C-3 節からの参照。"""

    def test_red_balance_anchors_in_e3_section(self):
        section = _section(SECTION_E3)
        assert section, "RED FAIL: E-3 節を切り出せない"
        missing = [a for a in E_GROUP_BALANCE_ANCHORS if a not in section]
        assert not missing, f"RED FAIL: E-3 節の⑦天秤アンカー欠落 {missing}"

    def test_red_balance_reference_in_c3_section(self):
        section = _section(SECTION_C3)
        assert section, "RED FAIL: C-3 節を切り出せない"
        assert E_GROUP_C3_REFERENCE in section, (
            f"RED FAIL: C-3 節に '{E_GROUP_C3_REFERENCE}' が無い"
        )


# ==========================================================================
# Green 群（番人）: 現行でも到達目標でも緑のまま。改変・反転・削除しない。
# ==========================================================================


class TestGuardianC3Step0FailSafe:
    """Green（番人）- C-3 ステップ 0 の fail-safe 本文が構造是正で失われないこと。

    毎回効く fail-safe の本文残置を見張る番人であり、全文含有ではなく
    「C-3 ステップ 0 範囲内にあること」を判定する（本文が別節へ流出したら赤になる）。
    """

    def test_green_guardian_consumed_marker_in_c3_step0(self):
        section = _section(SECTION_C3_STEP0)
        assert section, "GUARDIAN FAIL: C-3 ステップ 0 範囲を切り出せない"
        assert "(消費済)" in section, "GUARDIAN FAIL: '(消費済)' が C-3 ステップ 0 範囲内から消えた"

    def test_green_guardian_priority_table_header_in_c3_step0(self):
        section = _section(SECTION_C3_STEP0)
        assert section, "GUARDIAN FAIL: C-3 ステップ 0 範囲を切り出せない"
        assert "| 順 | 判定 | 入口 | 消費処理 |" in section, (
            "GUARDIAN FAIL: 優先順位表ヘッダが C-3 ステップ 0 範囲内から消えた"
        )

    def test_green_guardian_multi_match_failsafe_in_c3_step0(self):
        section = _section(SECTION_C3_STEP0)
        assert section, "GUARDIAN FAIL: C-3 ステップ 0 範囲を切り出せない"
        assert "複数マッチ不成立" in section, (
            "GUARDIAN FAIL: '複数マッチ不成立' が C-3 ステップ 0 範囲内から消えた"
        )


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
