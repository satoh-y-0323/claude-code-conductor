"""
tests/conftest.py

pytest 共通セットアップ。
.claude/hooks/ を sys.path に追加し、importlib 経由で stop.py / pre_compact.py を
ロードするテストが session_utils をインポートできるようにする。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / ".claude" / "hooks"))
