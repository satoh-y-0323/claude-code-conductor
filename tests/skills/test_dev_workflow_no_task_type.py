"""
tests/skills/test_dev_workflow_no_task_type.py

dev-workflow SKILL.md における task_type 概念の撤去と
Phase D bug-fix モード追加を検証する。

新仕様:
- フェーズ A 冒頭の TASK_TYPE 抽出 / Skill(task-routing) 呼び出しブロックを削除
- A-4 で生成する requirements-report のフロントマターから task_type を削除
- Phase D-0 に bug-fix モード（当日 debug-analysis-*.md 検出時）を追加
  - D-1（Red tester）と D-4（Refactor）はスキップ
  - developer 起動 → tester（Green 確認）→ Phase E
"""
import re

from tests.skills._skill_helpers import (
    extract_section,
    keyword_in_neighborhood,
    read_dev_workflow_skill as _dev_workflow,
)


# ---------------------------------------------------------------------------
# Phase A: TASK_TYPE 抽出 / Skill(task-routing) 呼び出しが撤去されている
# ---------------------------------------------------------------------------

def test_phase_a_no_task_type_block():
    """Phase A 冒頭から TASK_TYPE 抽出ブロックが撤去されている。"""
    phase_a = extract_section(_dev_workflow(), "## フェーズ A:")
    assert phase_a, "フェーズ A セクションが見つからない"
    assert "### TASK_TYPE" not in phase_a, \
        "Phase A に旧 TASK_TYPE 確認ブロックが残存している"


def test_phase_a_no_task_routing_call():
    """Phase A から Skill(task-routing) の呼び出し記述が撤去されている。"""
    phase_a = extract_section(_dev_workflow(), "## フェーズ A:")
    assert "task-routing" not in phase_a, \
        "Phase A に task-routing への参照が残存している"


def test_dev_workflow_no_task_type_anywhere():
    """dev-workflow SKILL.md 全文から旧概念（英語トークン + 日本語表記）が撤去されている。

    日本語の「タスク種別」も対象に含め、概念の再導入を防ぐ。
    """
    content = _dev_workflow()
    forbidden = ("task_type", "TASK_TYPE", "task-routing", "タスク種別")
    found = [token for token in forbidden if token in content]
    assert not found, f"dev-workflow SKILL.md に旧概念が残存: {found}"


# ---------------------------------------------------------------------------
# A-4: requirements-report の frontmatter から task_type が消えている
# ---------------------------------------------------------------------------

def test_requirements_report_no_task_type_frontmatter():
    """A-4 セクションの requirements-report 生成指示に task_type フロントマターが含まれない。"""
    content = _dev_workflow()
    pattern = re.compile(
        r"(^### A-4.*?)(?=^### |^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(content)
    assert match, "A-4 セクションが見つからない"
    section = match.group(1)
    assert "task_type" not in section, \
        "A-4 セクションに task_type フロントマター指示が残存している"


# ---------------------------------------------------------------------------
# Phase D-0: bug-fix モード判定の追加
# ---------------------------------------------------------------------------

def test_phase_d0_has_bug_fix_mode():
    """Phase D-0 に bug-fix モードの判定記述がある。"""
    phase_d = extract_section(_dev_workflow(), "## フェーズ D:")
    assert phase_d, "フェーズ D セクションが見つからない"
    assert "debug-analysis" in phase_d, \
        "Phase D に debug-analysis への言及がない"
    assert "bug-fix" in phase_d, \
        "Phase D に bug-fix モードの記述がない"


def test_bug_fix_mode_skips_red_phase():
    """bug-fix モードでは D-1（Red tester）と D-4（Refactor）をスキップする旨が記述されている。"""
    phase_d = extract_section(_dev_workflow(), "## フェーズ D:")
    # 「スキップ」と「Red」または「D-1」が bug-fix 行の近傍に揃っている
    has_red = keyword_in_neighborhood(phase_d, "bug-fix", ("スキップ", "Red"))
    has_d1 = keyword_in_neighborhood(phase_d, "bug-fix", ("スキップ", "D-1"))
    assert has_red or has_d1, \
        "bug-fix モードで Red フェーズ（D-1）をスキップする記述が見当たらない"


def test_bug_fix_mode_has_d3_completion_handling():
    """bug-fix モードに D-3（tester 動作確認）完了後の手順が記述されている。

    必要な要素:
    - tester 動作確認の `[x]` 化指示
    - 不合格時の D-2 リトライ
    - フェーズ E への遷移
    """
    phase_d = extract_section(_dev_workflow(), "## フェーズ D:")
    # bug-fix モード章を粗く抽出
    bug_fix_section_start = phase_d.find("**bug-fix モードの場合:**")
    assert bug_fix_section_start >= 0, "bug-fix モード章が見つからない"
    bug_fix_section = phase_d[bug_fix_section_start:]
    # D-3 章までの範囲（次の "### D-" 以降は別章なので含めない）
    next_subheading = re.search(r"^### D-", bug_fix_section, re.MULTILINE)
    if next_subheading:
        bug_fix_section = bug_fix_section[: next_subheading.start()]

    assert "AskUserQuestion" in bug_fix_section, \
        "bug-fix モードの動作確認手順に AskUserQuestion が含まれていない"
    assert "tester: 動作確認" in bug_fix_section, \
        "bug-fix モードの動作確認タスク `- [ ] tester: 動作確認` への言及がない"
    assert "フェーズ E" in bug_fix_section, \
        "bug-fix モードに フェーズ E への遷移記述がない"
    assert "D-2" in bug_fix_section, \
        "bug-fix モードに D-2 への戻り（不合格時の再修正）記述がない"


def test_bug_fix_mode_passes_debug_analysis_as_path_only():
    """bug-fix モードの D-2 で debug-analysis のファイルパスのみを渡し、内容は agent 側で Read させる旨が記述されている [SR-AI-001]。"""
    phase_d = extract_section(_dev_workflow(), "## フェーズ D:")
    bug_fix_section_start = phase_d.find("**bug-fix モードの場合:**")
    assert bug_fix_section_start >= 0
    bug_fix_section = phase_d[bug_fix_section_start:]
    # 「ファイルパスのみ」または「パスのみ」が含まれる
    assert "パスのみ" in bug_fix_section, \
        "bug-fix モードに「パスのみ」（プロンプトインジェクション対策）の記述がない"
    assert "agent 側で Read" in bug_fix_section or "agent 側で Read" in phase_d, \
        "bug-fix モードに「内容は agent 側で Read させる」記述がない"


def test_bug_fix_mode_requires_today_timestamp():
    """bug-fix モード判定は当日タイムスタンプの debug-analysis に限定される。

    前セッションの残骸 debug-analysis による意図しない bug-fix モード突入を防ぐ。
    """
    phase_d = extract_section(_dev_workflow(), "## フェーズ D:")
    d0_start = phase_d.find("### D-0:")
    assert d0_start >= 0, "D-0 サブ見出しが見つからない"
    # D-0 章の範囲（次の "### " まで）
    next_sub = re.search(r"^### D-", phase_d[d0_start + len("### D-0:"):], re.MULTILINE)
    if next_sub:
        d0_section = phase_d[d0_start: d0_start + len("### D-0:") + next_sub.start()]
    else:
        d0_section = phase_d[d0_start:]
    assert "当日" in d0_section or "今日の日付" in d0_section, \
        "D-0 の bug-fix モード判定に当日タイムスタンプ条件が記述されていない"
