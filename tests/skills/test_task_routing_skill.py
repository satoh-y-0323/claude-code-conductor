from pathlib import Path
import re

SKILL_PATH = Path(__file__).parents[2] / ".claude" / "skills" / "task-routing" / "SKILL.md"

def _extract_section(content: str, heading: str) -> str:
    """指定見出しから次の ## 見出しまでのテキストを返す。"""
    lines = content.splitlines()
    in_section = False
    result = []
    for line in lines:
        if line.startswith("## ") and heading.rstrip(":") in line:
            in_section = True
            result.append(line)
            continue
        if in_section:
            if line.startswith("## ") and heading.rstrip(":") not in line:
                break
            result.append(line)
    return "\n".join(result)


def test_bugfix_includes_security_reviewer():
    """Step 2 の bug-fix 編成テーブルに security-reviewer が含まれる。"""
    content = SKILL_PATH.read_text(encoding="utf-8")
    section = _extract_section(content, "## Step 2:")
    bugfix_part = section[section.find("### bug-fix"):section.find("### feature")]
    # テーブル行として含まれていることを確認（「省略」という文脈での言及は不可）
    lines_with_sr = [
        line for line in bugfix_part.splitlines()
        if "security-reviewer" in line and "省略" not in line
    ]
    assert len(lines_with_sr) > 0, \
        "bug-fix 編成テーブルに security-reviewer がない（「省略」以外の文脈で含まれていない）"


def test_bugfix_parallel_review():
    """bug-fix 編成に並列レビューの明示がある。"""
    content = SKILL_PATH.read_text(encoding="utf-8")
    section = _extract_section(content, "## Step 2:")
    bugfix_part = section[section.find("### bug-fix"):section.find("### feature")]
    has_parallel = "並列" in bugfix_part or "1 メッセージ" in bugfix_part
    assert has_parallel, \
        "bug-fix 編成に並列起動の記述がない"


def test_bugfix_no_old_note():
    """bug-fix セクションに「security-reviewer は省略」という旧文言が存在しない。"""
    content = SKILL_PATH.read_text(encoding="utf-8")
    section = _extract_section(content, "## Step 2:")
    bugfix_part = section[section.find("### bug-fix"):section.find("### feature")]
    assert "security-reviewer は省略" not in bugfix_part, \
        "旧文言「security-reviewer は省略」が bug-fix セクションに残っている"


def test_security_audit_phase_fgh_reference():
    """security-audit セクションに start のフェーズ F/G/H への参照がある。"""
    content = SKILL_PATH.read_text(encoding="utf-8")
    section = _extract_section(content, "## Step 2:")
    sa_start = section.find("### security-audit")
    sa_end = section.find("### docs")
    sa_part = section[sa_start:sa_end]
    has_ref = "フェーズ F" in sa_part or "F/G/H" in sa_part or "/start" in sa_part
    assert has_ref, \
        "security-audit セクションに /start または フェーズ F/G/H の参照がない"


def test_step4_bugfix_parallel_execution():
    """Step 4 の bug-fix 実行指示が docs と分離され、並列起動が明示されている。"""
    content = SKILL_PATH.read_text(encoding="utf-8")
    section = _extract_section(content, "## Step 4:")
    # bug-fix と docs が同一行で扱われていないこと
    assert "bug-fix / docs" not in section, \
        "Step 4 で bug-fix と docs が同一行で扱われている（分離すること）"
    # bug-fix の最終レビューが並列起動であること
    assert "security-reviewer" in section or "並列" in section, \
        "Step 4 の bug-fix 記述に security-reviewer または並列起動の記述がない"
