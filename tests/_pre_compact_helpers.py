"""
tests/_pre_compact_helpers.py

.claude/hooks/pre_compact.py 系テスト（tests/test_pre_compact.py /
tests/test_precompact_additional.py）で共通利用するヘルパー。

- WORKTREE_ROOT / PRE_COMPACT_PY: pre_compact.py の絶対パス
- _load_pre_compact_module(): pre_compact.py をモジュールとしてロードする（__main__ 実行なし）
- _run_main_in_process(): pre_compact.py の main() を in-process 実行し、実 sessions dir に
  一切触れない（architecture-report-20260704-065052.md §4.2 / §8-1 案 A の in-process 方式）

意図: 旧来は tests/test_pre_compact.py と tests/test_precompact_additional.py の両方で
`_load_pre_compact_module()` / `_run_main_in_process()` をほぼ同一実装のまま重複定義していた
（CR-M-001）。前例 `tests/skills/_skill_helpers.py` に倣い、本モジュールへ一本化する。

`_run_main_in_process()` の戻り値は `(module, sessions_dir, fake_stdout)` の 3-tuple。
`test_pre_compact.py` は 3 値すべてを使用する。`test_precompact_additional.py` は
`fake_stdout` のみを使うため、呼び出し側で `_, _, fake_stdout = _run_main_in_process(...)`
のように必要な要素だけ取り出す（stdout の値・main() の実行内容は従来と不変更）。
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

WORKTREE_ROOT = Path(__file__).resolve().parent.parent
PRE_COMPACT_PY = WORKTREE_ROOT / ".claude" / "hooks" / "pre_compact.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_pre_compact_module() -> types.ModuleType:
    """pre_compact.py をモジュールとしてロードする（__main__ 実行なし）。

    pre_compact.py はモジュールレベルで session_utils を import するため、
    sys.path に hooks ディレクトリを追加してからロードする。
    """
    hooks_dir = str(PRE_COMPACT_PY.parent)
    if hooks_dir not in sys.path:
        sys.path.insert(0, hooks_dir)
    spec = importlib.util.spec_from_file_location("pre_compact", PRE_COMPACT_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


def _run_main_in_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict
) -> tuple[types.ModuleType, Path, io.StringIO]:
    """pre_compact.py の main() を in-process 実行し、実 sessions dir に一切触れない。

    architecture-report-20260704-065052.md §4.2 の in-process 方式（T1 の
    TestMainDebounce と同一パターン）:
      - `mod.SESSIONS_DIR` を tmp_path 配下に override
      - `sys.stdin` を JSON payload の StringIO に差し替え
      - `os.getcwd` を tmp_path に固定して非 worktree 経路を保証
      - `sys.stdout` を明示的に StringIO に差し替えて捕捉する
        （capsys は使わない。reconfigure 済み hook では capsys が効かない
        場合があるため。tests/agent-memory の I-01 パターンに準拠）
    """
    module = _load_pre_compact_module()
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(module, "SESSIONS_DIR", str(sessions_dir))
    monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    fake_stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    module.main()

    return module, sessions_dir, fake_stdout
