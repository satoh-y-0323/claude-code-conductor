"""
tests/skills/test_marker_compliance.py

マーカー遵守の機械検査（architecture-report-20260724-184435.md §2-8 last bullet・
plan-report-20260724-184743.md T4・test-report-20260724-200043.md §2-6）。

規約文の「存在確認」とは別立ての「遵守そのもの」の検査。tier_autoapply.py の
RED_APPLY_ROLES は tester 起動プロンプト内マーカー値（`C3_TASK_ID:` の `test-`
プレフィックス）のみを注入条件にキーとするため、確認フェーズ（D-3/D-5・parallel の
confirm- 相当）の起動プロンプトに `C3_TASK_ID: test-` を書くと Red 限定注入が
誤発火する。これを恒久 CI 回帰網として禁止する:

- dev-workflow SKILL.md の D-3 / D-5 節本文に `C3_TASK_ID: test-` が現れない
- parallel-agents SKILL.md（confirm- 相当節本文＝全 test- タスクは `{task_id}`
  プレースホルダのため、リテラル `C3_TASK_ID: test-` は全ファイルで現れない）

検査対象文字列は厳密に `C3_TASK_ID: test-`（コロン + 空白 + test-）であり、record
コマンドの `--task test-X` とは別物として誤検知しない（本ファイル末尾で discriminator
を明示的に固定する）。fence 内の例示コマンド行も検査対象に含める（スコープ限定は
「節見出し境界」のみで、コードフェンスを除外しない）。
"""
from __future__ import annotations

import re

from tests.skills._skill_helpers import SKILLS_DIR

DEV_WORKFLOW_SKILL_PATH = SKILLS_DIR / "dev-workflow" / "SKILL.md"
PARALLEL_AGENTS_SKILL_PATH = SKILLS_DIR / "parallel-agents" / "SKILL.md"

# 禁止マーカー（確認フェーズ起動プロンプトへの Red マーカー混入）。
FORBIDDEN_MARKER = "C3_TASK_ID: test-"

_HEADING_RE = re.compile(r"^#{1,6}\s")


def _read(path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _extract_heading_block(content: str, heading_prefix: str) -> str:
    """`heading_prefix` から始まる見出し行から、次の見出し行（レベル問わず）の
    直前までを抽出する。

    fenced code block（```）内の bash コメント行（`# ...`）が Markdown 見出しへ
    誤マッチして抽出が code fence 途中で打ち切られないよう、fence 内では見出し
    判定をスキップする。見つからない場合は空文字列を返す。
    """
    lines = content.splitlines(keepends=True)
    start_idx = None
    for i, line in enumerate(lines):
        if line.startswith(heading_prefix):
            start_idx = i
            break
    if start_idx is None:
        return ""
    end_idx = len(lines)
    in_fence = False
    for j in range(start_idx + 1, len(lines)):
        stripped = lines[j].lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _HEADING_RE.match(lines[j]):
            end_idx = j
            break
    return "".join(lines[start_idx:end_idx])


# ---------------------------------------------------------------------------
# dev-workflow SKILL.md: D-3 / D-5 節本文に禁止マーカーが現れない
# ---------------------------------------------------------------------------

def test_dev_workflow_d3_body_has_no_test_marker():
    """D-3 節本文に `C3_TASK_ID: test-` が現れない（0 hits・遵守検査）。"""
    content = _read(DEV_WORKFLOW_SKILL_PATH)
    section = _extract_heading_block(content, "### D-3:")
    assert section, "D-3 節が見つからない"
    assert FORBIDDEN_MARKER not in section, (
        "D-3 節本文に Red マーカー `C3_TASK_ID: test-` が混入している"
        "（確認フェーズへの Red 限定注入が誤発火するため禁止）"
    )


def test_dev_workflow_d5_body_has_no_test_marker():
    """D-5 節本文に `C3_TASK_ID: test-` が現れない（0 hits・遵守検査）。"""
    content = _read(DEV_WORKFLOW_SKILL_PATH)
    section = _extract_heading_block(content, "### D-5:")
    assert section, "D-5 節が見つからない"
    assert FORBIDDEN_MARKER not in section, (
        "D-5 節本文に Red マーカー `C3_TASK_ID: test-` が混入している"
        "（確認フェーズへの Red 限定注入が誤発火するため禁止）"
    )


# ---------------------------------------------------------------------------
# parallel-agents SKILL.md: confirm- 相当節本文（＝全ファイル）に禁止マーカーが
# 現れない。test- タスクのマーカーは `{task_id}` プレースホルダで表現され、リテラル
# `C3_TASK_ID: test-` は本 SKILL のどこにも書かれてはならない（T4 後も 0 hits 維持）。
# ---------------------------------------------------------------------------

def test_parallel_agents_has_no_literal_test_marker():
    """parallel-agents SKILL.md に `C3_TASK_ID: test-` が現れない（0 hits・遵守検査）。"""
    content = _read(PARALLEL_AGENTS_SKILL_PATH)
    assert content, "parallel-agents SKILL.md が見つからない"
    assert FORBIDDEN_MARKER not in content, (
        "parallel-agents SKILL.md に Red マーカーのリテラル `C3_TASK_ID: test-` が"
        "混入している（マーカーは `{task_id}` プレースホルダで表現すること）"
    )


# ---------------------------------------------------------------------------
# discriminator: 検査パターンは record コマンドの `--task test-X` を誤検知しない
# ---------------------------------------------------------------------------

def test_forbidden_marker_pattern_does_not_match_task_flag():
    """禁止パターン `C3_TASK_ID: test-` は `--task test-X` を誤検知しない。

    D-3 節本文には Red success/failure 記録の `--task test-{plan タスクID}` が
    正当に存在するが、これは禁止マーカー（`C3_TASK_ID: test-`）とは別物であり、
    遵守検査が false positive を出さないことを固定する。
    """
    # discriminator 文字列に禁止マーカーが含まれないこと（パターン設計の自己検査）。
    sample_task_flag = "--task test-t4"
    assert FORBIDDEN_MARKER not in sample_task_flag

    # D-3 節本文には --task test- 形式の record 例が存在し、かつ禁止マーカーは無い。
    content = _read(DEV_WORKFLOW_SKILL_PATH)
    d3 = _extract_heading_block(content, "### D-3:")
    assert "--task test-" in d3, (
        "D-3 節に Red 帰属の record `--task test-{plan タスクID}` が見当たらない"
    )
    assert FORBIDDEN_MARKER not in d3


# ---------------------------------------------------------------------------
# positive control: D-1 節にはマーカー規約（禁止マーカーそのもの）が存在する
# ---------------------------------------------------------------------------

def test_dev_workflow_d1_has_marker_regulation():
    """D-1 節にはマーカー規約 `C3_TASK_ID: test-{plan タスクID}` が存在する
    （検査対象の pattern が意味を持つことの positive control）。"""
    content = _read(DEV_WORKFLOW_SKILL_PATH)
    section = _extract_heading_block(content, "### D-1:")
    assert section, "D-1 節が見つからない"
    assert FORBIDDEN_MARKER in section, (
        "D-1 節に Red 起動マーカー規約 `C3_TASK_ID: test-...` が存在しない"
        "（マーカー義務の逐語規定が未反映）"
    )
