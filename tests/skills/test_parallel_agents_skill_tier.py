"""
tests/skills/test_parallel_agents_skill_tier.py

T8-T3 の文言整合テスト（Red フェーズで追加された）。

architecture-report-20260707-163654.md §6/§10-3 と
plan-report-20260707-164606.md test-t3 のケース定義 (a)-(g) を固定する。

対象: .claude/skills/parallel-agents/SKILL.md。

Red フェーズ時点（SKILL.md 未改訂）では (a)(b)(c)(d)(f)(g) が Red になるのが
正しかった。(e)（wt_tester は --tier を付けない・不変）は当時から記述済みで
緑だった。現在は同一コミットで SKILL.md 改訂（Green）が完了している。
"""
from __future__ import annotations

import re

from tests.skills._skill_helpers import SKILLS_DIR

PARALLEL_AGENTS_SKILL_PATH = SKILLS_DIR / "parallel-agents" / "SKILL.md"

_BASH_BLOCK_RE = re.compile(r"```bash\n(.*?)```", re.DOTALL)
_HEADING_RE = re.compile(r"^#{1,6}\s")


def _read_parallel_agents_skill() -> str:
    """`.claude/skills/parallel-agents/SKILL.md` の内容を返す。存在しない場合は空文字列。"""
    if not PARALLEL_AGENTS_SKILL_PATH.exists():
        return ""
    return PARALLEL_AGENTS_SKILL_PATH.read_text(encoding="utf-8")


def _extract_heading_block(content: str, heading_prefix: str) -> str:
    """`heading_prefix` から始まる見出し行から、次のいずれかの見出し行（レベル問わず）の直前までを抽出する。

    "## " 境界専用の汎用抽出では足りず、
    "### 2-C:" や "#### 2-F-4:" のような深い見出し粒度を厳密に切り出すために本関数を用意する。
    fenced code block（```）内の bash コメント行（`# ...`）が Markdown 見出し
    （`^#{1,6}\\s`）に誤マッチして抽出が code fence 途中で打ち切られないよう、
    fence 内では見出し判定をスキップする。
    見つからない場合は空文字列を返す。
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


def _developer_bash_blocks(section: str) -> list[str]:
    """セクション内の bash コードブロックのうち `--role developer` を含むものだけを返す。"""
    return [b for b in _BASH_BLOCK_RE.findall(section) if "--role developer" in b]


# ---------------------------------------------------------------------------
# (a) 2-C: C3_TASK_ID マーカー注入手順
# ---------------------------------------------------------------------------

def test_2c_has_task_id_marker_injection():
    """2-C セクションに `C3_TASK_ID: {task_id}` マーカー行の注入手順が存在する（§6-1）。"""
    content = _read_parallel_agents_skill()
    section = _extract_heading_block(content, "### 2-C:")
    assert section, "2-C セクションが見つからない"
    assert "C3_TASK_ID: {task_id}" in section, (
        "2-C に C3_TASK_ID マーカー行の注入手順が見当たらない（§6-1 未反映）"
    )


# ---------------------------------------------------------------------------
# (b) 2-E / 2-F-4: wt_developer record 例から --tier 明示が撤去されている
# ---------------------------------------------------------------------------

def test_2e_wt_developer_record_example_has_no_tier_flag():
    """2-E の wt_developer→developer record 例に `--tier` を明示する行が残っていない（撤去済み・§6-2）。"""
    content = _read_parallel_agents_skill()
    section = _extract_heading_block(content, "### 2-E:")
    assert section, "2-E セクションが見つからない"
    blocks = _developer_bash_blocks(section)
    assert blocks, "2-E に --role developer の record bash 例が見つからない"
    for block in blocks:
        assert "--tier" not in block, (
            "2-E の wt_developer→developer record 例に --tier 明示行が残存している"
        )


def test_2f4_wt_developer_record_example_has_no_tier_flag():
    """2-F-4 の wt_developer→developer record 例に `--tier` を明示する行が残っていない（撤去済み・§6-2）。"""
    content = _read_parallel_agents_skill()
    section = _extract_heading_block(content, "#### 2-F-4:")
    assert section, "2-F-4 セクションが見つからない"
    blocks = _developer_bash_blocks(section)
    assert blocks, "2-F-4 に --role developer の record bash 例が見つからない"
    for block in blocks:
        assert "--tier" not in block, (
            "2-F-4 の wt_developer→developer record 例に --tier 明示行が残存している"
        )
