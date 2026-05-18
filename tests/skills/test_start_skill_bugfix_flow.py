"""
tests/skills/test_start_skill_bugfix_flow.py

新仕様の /start SKILL.md におけるデバッグ調査フローの記述検証。

新仕様:
- Step 1 の選択肢として「デバッグ調査から」が存在する
- Step 2 で「デバッグ調査から」が systematic-debugger 起動 →
  dev-workflow Phase D（bug-fix モード）への遷移を記述する
"""
from tests.skills._skill_helpers import (
    keyword_in_neighborhood,
    read_start_skill as _start,
)


def test_step1_includes_debug_option():
    """Step 1 の AskUserQuestion に「デバッグ調査から」の選択肢が含まれる。"""
    assert "デバッグ調査から" in _start(), \
        "/start に「デバッグ調査から」の選択肢が含まれていない"


def test_step2_debug_routing_invokes_systematic_debugger():
    """Step 2 のマッピングで「デバッグ調査から」が systematic-debugger を起動する。

    「デバッグ調査から」を含む行の近傍に systematic-debugger の起動指示が現れる。
    """
    assert keyword_in_neighborhood(
        _start(), "デバッグ調査から", ("systematic-debugger",)
    ), "「デバッグ調査から」近傍に systematic-debugger の起動指示がない"


def test_step2_debug_transitions_to_dev_workflow_phase_d():
    """「デバッグ調査から」のマッピングが dev-workflow フェーズ D 遷移を含む。"""
    content = _start()
    # 「dev-workflow」と「フェーズ D」または「Phase D」の両方が近傍にある必要がある
    has_jp = keyword_in_neighborhood(
        content, "デバッグ調査から", ("フェーズ D", "dev-workflow")
    )
    has_en = keyword_in_neighborhood(
        content, "デバッグ調査から", ("Phase D", "dev-workflow")
    )
    assert has_jp or has_en, \
        "「デバッグ調査から」のマッピングに dev-workflow フェーズ D への遷移が記述されていない"


def test_old_bugfix_task_type_table_removed():
    """旧 task_type ベースの bug-fix テーブル行（| bug-fix |）が残っていない。"""
    assert "| bug-fix |" not in _start(), \
        "旧 bug-fix の task_type テーブル行が残っている"
