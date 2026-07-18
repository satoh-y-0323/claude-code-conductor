"""
tests/conftest.py

pytest 共通セットアップ。
worktree の src/ を sys.path の最初に追加し、テストがworktree のコードを使用するようにする。
.claude/hooks/ も追加し、importlib 経由で stop.py / pre_compact.py を
ロードするテストが session_utils をインポートできるようにする。
"""
import sys
from pathlib import Path

# Ensure worktree src is imported, not system-installed c3 package
worktree_root = Path(__file__).parent.parent
sys.path.insert(0, str(worktree_root / "src"))
sys.path.insert(1, str(worktree_root / ".claude" / "hooks"))
