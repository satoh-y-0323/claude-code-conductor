"""
tests/skills/test_start_skill_security_audit_phase.py

新仕様の /start SKILL.md における「レビューから」フローの検証。

新仕様:
- Step 1 の選択肢として「レビューから」が存在する
- Step 2 で「レビューから」を選ぶと dev-workflow フェーズ E に直接遷移する
- レビュー指摘ありの loop-back（フェーズ C へ戻る）は dev-workflow フェーズ E が担うため、
  /start から旧 Step 3（フェーズ F/G/H）専用フェーズを撤去する
"""
from tests.skills._skill_helpers import (
    keyword_in_neighborhood,
    read_start_skill as _start,
)


# ---------------------------------------------------------------------------
# テスト 1: Step 0.5（task_type 確認）が撤去されている
# ---------------------------------------------------------------------------

def test_step0_5_removed():
    """旧 Step 0.5（タスク種別確認）見出しが SKILL.md に存在しない。"""
    assert "## Step 0.5" not in _start(), \
        "旧 Step 0.5（タスク種別確認）が残存している。task_type 概念を撤去すること。"


# ---------------------------------------------------------------------------
# テスト 2: Step 3 / フェーズ F/G/H が撤去されている
# ---------------------------------------------------------------------------

def test_step3_and_phase_fgh_removed():
    """旧 Step 3 とフェーズ F/G/H の見出しが撤去されている。"""
    content = _start()
    assert "## Step 3" not in content, \
        "旧 Step 3（security-audit 承認後フェーズ）が残存している"
    for heading in ("## フェーズ F", "## フェーズ G", "## フェーズ H"):
        assert heading not in content, f"旧 {heading} が残存している"


# ---------------------------------------------------------------------------
# テスト 3: Step 1 に「レビューから」の選択肢が含まれる
# ---------------------------------------------------------------------------

def test_step1_includes_review_option():
    """Step 1 の AskUserQuestion に「レビューから」の選択肢が含まれる。"""
    assert "レビューから" in _start(), \
        "/start に「レビューから」の選択肢が含まれていない"


# ---------------------------------------------------------------------------
# テスト 4: Step 2 で「レビューから」が dev-workflow フェーズ E に遷移する
# ---------------------------------------------------------------------------

def test_step2_review_routes_to_phase_e():
    """「レビューから」のマッピングが dev-workflow フェーズ E への遷移を含む。"""
    content = _start()
    has_jp = keyword_in_neighborhood(
        content, "レビューから", ("フェーズ E", "dev-workflow")
    )
    has_en = keyword_in_neighborhood(
        content, "レビューから", ("Phase E", "dev-workflow")
    )
    assert has_jp or has_en, \
        "「レビューから」のマッピングに dev-workflow フェーズ E への遷移が記述されていない"


# ---------------------------------------------------------------------------
# テスト 5: Step 0 のレポート整理は引き続き存在する
# ---------------------------------------------------------------------------

def test_step0_report_archive_preserved():
    """Step 0（レポートの整理）は維持されている。"""
    content = _start()
    assert "## Step 0:" in content, "Step 0 が失われている"
    assert "archive" in content.lower() or "アーカイブ" in content, \
        "Step 0 にレポートアーカイブの記述が含まれていない"
