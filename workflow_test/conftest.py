import sys
import os

# workflow_test/ をパスに追加して src パッケージとして認識させる
_wt_root = os.path.dirname(os.path.abspath(__file__))
if _wt_root not in sys.path:
    sys.path.insert(0, _wt_root)
