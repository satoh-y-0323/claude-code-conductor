"""
tests/skills/test_start_skill_bugfix_flow.py

TDD Red フェーズ: start/SKILL.md の bug-fix フローが
tester 完了後に code-reviewer と security-reviewer を 1 メッセージ内で並列起動する
旨の記述を検証するテスト。

現在の SKILL.md には「security-audit の Step 2 へ遷移し即実行」という記述のため、
両テストが FAILED となる（正しい状態）。
"""
from pathlib import Path

SKILL_PATH = Path(__file__).parents[2] / ".claude" / "skills" / "start" / "SKILL.md"


def _bugfix_table_row(content: str) -> str:
    """Step 2 テーブルの bug-fix | systematic-debugger 行を返す。"""
    for line in content.splitlines():
        if "bug-fix" in line and "systematic-debugger" in line:
            return line
    return ""


# ---------------------------------------------------------------------------
# テスト 1: bug-fix フローに code-reviewer と security-reviewer の両方が明記されているか
# ---------------------------------------------------------------------------

def test_bugfix_flow_includes_both_reviewers():
    """bug-fix フローに code-reviewer と security-reviewer の両方が明記されている。"""
    content = SKILL_PATH.read_text(encoding="utf-8")
    row = _bugfix_table_row(content)
    assert row, "bug-fix | systematic-debugger 直起動 の行が見つからない"
    assert "code-reviewer" in row, "bug-fix 行に code-reviewer がない"
    assert "security-reviewer" in row, "bug-fix 行に security-reviewer がない"
    assert "並列" in row or "1 メッセージ" in row, \
        "bug-fix 行に並列起動の明示がない"


# ---------------------------------------------------------------------------
# テスト 2: systematic-debugger → developer → tester の順序が正しいか
# ---------------------------------------------------------------------------

def test_bugfix_flow_order_maintained():
    """systematic-debugger → developer → tester の順序が正しい。"""
    content = SKILL_PATH.read_text(encoding="utf-8")
    row = _bugfix_table_row(content)
    assert row, "bug-fix | systematic-debugger 直起動 の行が見つからない"

    pos_sd = row.find("systematic-debugger")
    pos_dev = row.find("developer")
    pos_tester = row.find("tester")
    assert 0 <= pos_sd < pos_dev < pos_tester, \
        "systematic-debugger → developer → tester の順序が正しくない"

    assert "security-reviewer" in row, \
        "bug-fix 行に security-reviewer がない"
