"""
tests/skills/test_start_skill_new_flow.py

新仕様の /start SKILL.md 構造を検証する。

新仕様の要点:
- Step 0.5（task_type 確認）と Step 3（フェーズ F/G/H）を撤去
- Step 1 で「標準ワークフロー / 実装から / デバッグ調査から / レビューから」の 4 択
- 標準ワークフロー選択時は Step 1.5 で「ヒアリング / 設計 / 計画」のサブ選択
- Step 2 で開始地点 → dev-workflow 各フェーズへの遷移マッピング
- SKILL.md 全体から旧概念（task_type / TASK_TYPE / task-routing / タスク種別）が撤去されている
"""
from tests.skills._skill_helpers import find_section_range, read_start_skill as _start


# ---------------------------------------------------------------------------
# 構造: 撤去対象セクションが存在しないこと
# ---------------------------------------------------------------------------

def test_step0_5_heading_absent():
    """旧 Step 0.5 見出しが存在しない。"""
    assert "## Step 0.5" not in _start()


def test_step3_heading_absent():
    """旧 Step 3 見出しが存在しない。"""
    assert "## Step 3" not in _start()


def test_phase_fgh_headings_absent():
    """旧フェーズ F / G / H 見出しが存在しない。"""
    content = _start()
    for heading in ("## フェーズ F", "## フェーズ G", "## フェーズ H"):
        assert heading not in content


# ---------------------------------------------------------------------------
# 構造: 必須セクションが存在すること
# ---------------------------------------------------------------------------

def test_step0_present():
    assert "## Step 0:" in _start()


def test_step1_present():
    assert "## Step 1:" in _start()


def test_step2_present():
    assert "## Step 2:" in _start()


# ---------------------------------------------------------------------------
# Step 1: 4 つの選択肢
# ---------------------------------------------------------------------------

def test_step1_four_options():
    """Step 1 の選択肢として 4 つの開始地点が含まれる。"""
    content = _start()
    required_labels = (
        "標準ワークフロー",
        "実装から",
        "デバッグ調査から",
        "レビューから",
    )
    missing = [label for label in required_labels if label not in content]
    assert not missing, f"Step 1 に以下の選択肢ラベルが含まれていない: {missing}"


# ---------------------------------------------------------------------------
# Step 1.5: 標準ワークフローのサブ選択
# ---------------------------------------------------------------------------

def test_step1_5_substep_for_standard_workflow():
    """標準ワークフロー選択時のサブ選択（ヒアリング / 設計 / 計画）が記述されている。"""
    content = _start()
    assert "## Step 1.5" in content, "Step 1.5 の見出しが存在しない"
    for label in ("ヒアリング", "設計", "計画"):
        assert label in content, f"Step 1.5 のサブ選択に「{label}」が含まれていない"


# ---------------------------------------------------------------------------
# Step 2: マッピング表が dev-workflow フェーズ A〜E を参照する（Step 2 セクション限定）
# ---------------------------------------------------------------------------

def test_step2_references_all_dev_workflow_phases():
    """Step 2 セクション内で dev-workflow フェーズ A / B / C / D / E が全て参照される。

    全文検索ではなく Step 2 の範囲に限定することで、他セクションの偶然の出現で
    偽 Pass にならないようにする。
    """
    content = _start()
    start, end = find_section_range(content, "## Step 2:")
    assert start >= 0, "Step 2 セクションが見つからない"
    step2_section = content[start:end]
    for phase_label in ("フェーズ A", "フェーズ B", "フェーズ C", "フェーズ D", "フェーズ E"):
        assert phase_label in step2_section, \
            f"Step 2 マッピング内に「{phase_label}」への遷移記述がない"


# ---------------------------------------------------------------------------
# 撤去確認: task_type / TASK_TYPE / task-routing / タスク種別 への参照が無いこと
# ---------------------------------------------------------------------------

def test_no_task_type_references():
    """SKILL.md 全文から旧概念（英語トークン + 日本語表記）が撤去されている。

    Note: チェック対象には日本語の「タスク種別」も含める。英語表記だけだと
    日本語による概念再導入を見落とすため。
    """
    content = _start()
    forbidden = ("task_type", "TASK_TYPE", "task-routing", "タスク種別")
    found = [token for token in forbidden if token in content]
    assert not found, f"/start SKILL.md に以下の旧概念が残存している: {found}"


# ---------------------------------------------------------------------------
# AskUserQuestion ベースであること（Step 1 / 1.5）
# ---------------------------------------------------------------------------

def test_step1_uses_ask_user_question():
    """Step 1 が AskUserQuestion で選択肢を提示する。"""
    content = _start()
    start, end = find_section_range(content, "## Step 1:")
    assert start >= 0, "Step 1 セクションが見つからない"
    step1_section = content[start:end]
    assert "AskUserQuestion" in step1_section, \
        "Step 1 に AskUserQuestion の使用記述がない"
