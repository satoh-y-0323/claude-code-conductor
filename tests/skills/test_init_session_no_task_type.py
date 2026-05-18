"""
tests/skills/test_init_session_no_task_type.py

init-session SKILL.md から prev_task_type 関連の処理が撤去されていることを検証する。

新仕様:
- Step 1 から TASK_TYPE 抽出ロジックを削除
- Step 3 サマリから「前回のタスク種別」表示を削除
- Step 5 から「/start 内の Step 0.5 で task-routing が…」の補足記述を削除
"""
from tests.skills._skill_helpers import read_init_session_skill as _init


def test_no_prev_task_type_extraction():
    """init-session SKILL.md から prev_task_type 抽出記述が消えている。"""
    assert "prev_task_type" not in _init(), \
        "init-session に prev_task_type への参照が残存している"


def test_no_task_type_regex():
    """TASK_TYPE 抽出用の正規表現記述が含まれない。"""
    assert "TASK_TYPE" not in _init(), \
        "init-session に TASK_TYPE への参照が残存している"


def test_no_task_routing_reference():
    """task-routing への参照が消えている。"""
    assert "task-routing" not in _init(), \
        "init-session に task-routing への参照が残存している"


def test_no_step0_5_reference():
    """旧 /start Step 0.5 への参照が消えている。"""
    assert "Step 0.5" not in _init(), \
        "init-session に /start の旧 Step 0.5 への参照が残存している"


def test_no_previous_task_type_summary_label():
    """Step 3 サマリの「前回のタスク種別」ラベルが撤去されている。"""
    assert "前回のタスク種別" not in _init(), \
        "init-session Step 3 サマリに「前回のタスク種別」が残存している"


def test_no_task_type_japanese_label():
    """日本語の「タスク種別」概念が撤去されている。"""
    assert "タスク種別" not in _init(), \
        "init-session に「タスク種別」（日本語）が残存している"
