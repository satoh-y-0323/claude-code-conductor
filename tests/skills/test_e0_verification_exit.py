"""E-0「省略の出口（1 問ゲート）」を凍結する静的テスト。

上流: architecture-report-20260813-183024 §2（出口ブロック仕様）/ §6（検証方針・P1〜P8）。

検査は純テキスト走査（pathlib + 正規表現）のみで行い、subprocess・実行系は使わない。

**節範囲の契約（§6・逸脱禁止）**:
`re.MULTILINE` の行頭アンカー ``^#### NEEDS_VERIFY または UNKNOWN の場合`` から、
次の行頭見出し（``^### `` または ``^## ``）の直前までを切り出す。
``^#### `` は終端にならない（``###``/``##`` の直後に空白を要求するため）。
非アンカーの見出し検索（インラインの ``## `` に一致する実装）と近傍固定ウィンドウ検索は禁止。

**読み込み契約**: `_skill_helpers.read_skill()` は未登録名に対して空文字列を返す
fail-silent API のため使わない（空文字列は全ての「不在」assert を素通りさせ、
負の対照 P6 を空の緑にする）。パスを明示して読み、各テストの冒頭で
`assert content` の空ガードを置く。`extract_section` / `find_section_range` も
``## `` 境界専用のため使わない。

**性質と対応表（§6 の定義表と同一）**:

===== ===================================== ==========================
性質   検査内容                              Red フェーズでの期待
===== ===================================== ==========================
P1     節範囲内に A1                         赤（実装前）
P2     節範囲内に A5                         赤（実装前）
P3     節範囲内に A6                         赤（実装前）
P4     autonomous-mode/SKILL.md に既存文言     **緑**（既存文言の凍結）
P5     節範囲内に A7                         赤（実装前）
P6     節範囲内に A4 かつ全文に否定文が不在    赤（実装前）
P7     節範囲内に A3                         赤（実装前）
P8     節範囲内に ``AskUserQuestion``         赤（実装前）
===== ===================================== ==========================

P4 は既存文言の**回帰ガード**であり、実装前から緑であることが正しい
（tester.md の「テストが最初から Pass する場合は修正する」規範は P4 に適用しない。
architecture-report §7「P4 適用除外文」）。

**検知力の実証（ルール 19）**: 文言凍結型の静的検査のため do-nothing スタブ 2 種は
原理的に非適用。代替として「実装前の SKILL.md で P1〜P3・P5〜P8 が赤（test-e0exit で実測）・
実装後に緑へ反転（confirm-e0exit で実測）」の両状態反転をもって双方向の実証とする。
"""

from __future__ import annotations

import re
from pathlib import Path

# --------------------------------------------------------------------------
# 対象パス（明示・fail-silent API を経由しない）
# --------------------------------------------------------------------------

WORKTREE_ROOT = Path(__file__).resolve().parents[2]
DEV_WORKFLOW_SKILL_PATH = WORKTREE_ROOT / ".claude" / "skills" / "dev-workflow" / "SKILL.md"
AUTONOMOUS_MODE_SKILL_PATH = WORKTREE_ROOT / ".claude" / "skills" / "autonomous-mode" / "SKILL.md"

# --------------------------------------------------------------------------
# 節範囲の契約（§6）
# --------------------------------------------------------------------------

#: E-0 出口ブロックが属する節の開始見出し（行頭アンカー・逐語）
SECTION_HEADING = "#### NEEDS_VERIFY または UNKNOWN の場合"

#: 節の終端となる行頭見出し。``### `` / ``## `` のみ（``#### `` は終端にしない）。
_SECTION_END_RE = re.compile(r"^(?:###|##)\s", re.MULTILINE)

# --------------------------------------------------------------------------
# 凍結アンカー literal（impl 側 prompt と同一文字列・逐語で含有判定する。
# 内部に ** ・バッククォート・括弧を挟まない）
# --------------------------------------------------------------------------

A1_EXIT_BLOCK_TITLE = "省略の出口（HITL 専用）"
A3_SCOPE_NEEDS_VERIFY_ONLY = "NEEDS_VERIFY のみを対象とする"
A4_UNKNOWN_OUT_OF_SCOPE = "UNKNOWN は本出口の対象外"
A5_FAIL_SAFE = "判断に迷う場合は従来どおり tester を起動する"
A6_NOT_APPLIED_IN_AUTONOMOUS = "自律モードでは本出口を適用しない"
A7_RECORD_FORMAT = "省略(既検証:"

#: P8 は固有アンカーでなく既存語の節範囲内存在を見る（§6 脚注）
P8_ASK_USER_QUESTION = "AskUserQuestion"

#: P4: autonomous-mode/SKILL.md 側の既存文言（変更しないことの凍結）
P4_AUTONOMOUS_FROZEN = "例外なく tester を起動する"

#: P6 の負の対照: SKILL.md 全文に現れてはならない文言
P6_FORBIDDEN_PHRASE = "UNKNOWN も省略できる"


# --------------------------------------------------------------------------
# ヘルパー
# --------------------------------------------------------------------------


def _read(path: Path) -> str:
    """指定パスを UTF-8 で読む。存在しなければ空文字列（空ガードで検出させる）。"""
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _e0_branch_section(content: str) -> str:
    """``#### NEEDS_VERIFY または UNKNOWN の場合`` 節の本文を切り出す。

    行頭アンカー（`re.MULTILINE`）で見出しを探し、そこから次の行頭 ``### `` /
    ``## `` 見出しの直前まで（見つからなければ末尾まで）を返す。
    見出しが存在しなければ空文字列を返す。
    """
    head = re.search("^" + re.escape(SECTION_HEADING), content, re.MULTILINE)
    if head is None:
        return ""
    start = head.start()
    rest_offset = head.end()
    end_match = _SECTION_END_RE.search(content, rest_offset)
    end = end_match.start() if end_match else len(content)
    return content[start:end]


def _load_section() -> tuple[str, str]:
    """(SKILL.md 全文, E-0 分岐節本文) を返す。呼び出し側で空ガードすること。"""
    content = _read(DEV_WORKFLOW_SKILL_PATH)
    return content, _e0_branch_section(content)


# --------------------------------------------------------------------------
# P1〜P8
# --------------------------------------------------------------------------


def test_p1_exit_block_exists_in_section() -> None:
    """P1: 出口の存在 — 節範囲内に A1 が存在する。"""
    content, section = _load_section()
    assert content, f"SKILL.md が読めない/空: {DEV_WORKFLOW_SKILL_PATH}"
    assert section, f"節見出しが見つからない（アンカー破損を疑う）: {SECTION_HEADING}"

    assert A1_EXIT_BLOCK_TITLE in section, (
        f"E-0 分岐節に出口ブロック見出し {A1_EXIT_BLOCK_TITLE!r} が無い"
    )


def test_p2_fail_safe_in_section() -> None:
    """P2: fail-safe — 節範囲内に A5 が存在する。"""
    content, section = _load_section()
    assert content, f"SKILL.md が読めない/空: {DEV_WORKFLOW_SKILL_PATH}"
    assert section, f"節見出しが見つからない（アンカー破損を疑う）: {SECTION_HEADING}"

    assert A5_FAIL_SAFE in section, (
        f"E-0 分岐節に fail-safe 規定 {A5_FAIL_SAFE!r} が無い"
    )


def test_p3_not_applied_in_autonomous_mode() -> None:
    """P3: 自律不適用 — 節範囲内に A6 が存在する。"""
    content, section = _load_section()
    assert content, f"SKILL.md が読めない/空: {DEV_WORKFLOW_SKILL_PATH}"
    assert section, f"節見出しが見つからない（アンカー破損を疑う）: {SECTION_HEADING}"

    assert A6_NOT_APPLIED_IN_AUTONOMOUS in section, (
        f"E-0 分岐節に自律モード不適用の規定 {A6_NOT_APPLIED_IN_AUTONOMOUS!r} が無い"
    )


def test_p4_autonomous_mode_skill_wording_frozen() -> None:
    """P4: autonomous-mode 側の凍結（既存文言の回帰ガード・実装前から緑が正しい）。

    tester.md の「テストが最初から Pass する場合は修正する」規範は本テストに適用しない
    （architecture-report-20260813-183024 §7「P4 適用除外文」）。
    """
    content = _read(AUTONOMOUS_MODE_SKILL_PATH)
    assert content, f"autonomous-mode/SKILL.md が読めない/空: {AUTONOMOUS_MODE_SKILL_PATH}"

    assert P4_AUTONOMOUS_FROZEN in content, (
        f"autonomous-mode/SKILL.md の既存規定 {P4_AUTONOMOUS_FROZEN!r} が失われている"
        "（E-0 出口の追加で自律モード側を変更してはならない）"
    )


def test_p5_record_format_in_section() -> None:
    """P5: 証跡書式 — 節範囲内に A7 が存在する。"""
    content, section = _load_section()
    assert content, f"SKILL.md が読めない/空: {DEV_WORKFLOW_SKILL_PATH}"
    assert section, f"節見出しが見つからない（アンカー破損を疑う）: {SECTION_HEADING}"

    assert A7_RECORD_FORMAT in section, (
        f"E-0 分岐節に判定行の拡張書式 {A7_RECORD_FORMAT!r} が無い"
    )


def test_p6_unknown_excluded_with_negative_control() -> None:
    """P6: UNKNOWN 除外＋負の対照。

    肯定（節範囲内に A4）と否定（SKILL.md 全文に禁止文言が不在）を**同一関数内で
    同じ content に対して** assert する。分割すると否定側が単独で緑になり
    （実装前でも禁止文言は不在のため）空の緑になる。
    """
    content, section = _load_section()
    assert content, f"SKILL.md が読めない/空: {DEV_WORKFLOW_SKILL_PATH}"
    assert section, f"節見出しが見つからない（アンカー破損を疑う）: {SECTION_HEADING}"

    assert A4_UNKNOWN_OUT_OF_SCOPE in section, (
        f"E-0 分岐節に UNKNOWN 除外の規定 {A4_UNKNOWN_OUT_OF_SCOPE!r} が無い"
    )
    assert P6_FORBIDDEN_PHRASE not in content, (
        f"SKILL.md 全文に禁止文言 {P6_FORBIDDEN_PHRASE!r} が現れている"
        "（UNKNOWN は本出口の対象外であり省略できない）"
    )


def test_p7_scope_limited_to_needs_verify() -> None:
    """P7: 適用範囲 — 節範囲内に A3 が存在する。"""
    content, section = _load_section()
    assert content, f"SKILL.md が読めない/空: {DEV_WORKFLOW_SKILL_PATH}"
    assert section, f"節見出しが見つからない（アンカー破損を疑う）: {SECTION_HEADING}"

    assert A3_SCOPE_NEEDS_VERIFY_ONLY in section, (
        f"E-0 分岐節に適用範囲の規定 {A3_SCOPE_NEEDS_VERIFY_ONLY!r} が無い"
    )


def test_p8_one_question_gate_uses_ask_user_question() -> None:
    """P8: 1 問ゲートであること — 節範囲内に ``AskUserQuestion`` が存在する。"""
    content, section = _load_section()
    assert content, f"SKILL.md が読めない/空: {DEV_WORKFLOW_SKILL_PATH}"
    assert section, f"節見出しが見つからない（アンカー破損を疑う）: {SECTION_HEADING}"

    assert P8_ASK_USER_QUESTION in section, (
        f"E-0 分岐節に {P8_ASK_USER_QUESTION!r} が無い（1 問ゲートが未設置）"
    )
