"""Git detection and init helpers for ``c3 init`` (no UI; subprocess only)."""

from __future__ import annotations

import subprocess
from enum import Enum
from pathlib import Path


class GitStatus(Enum):
    INSIDE_REPO = "inside_repo"  # git 管理下（親 repo のサブディレクトリ含む）
    NOT_A_REPO = "not_a_repo"    # git 管理外（git は存在する）
    GIT_MISSING = "git_missing"  # git コマンドが PATH に無い


def detect_git_status(target_root: Path) -> GitStatus:
    """Return the git status of *target_root* by running ``git rev-parse``.

    returncode 分類:
    - exit 0 かつ stdout が "true" -> INSIDE_REPO（git 管理下）
    - returncode == 128（git 管理外の慣例値）-> NOT_A_REPO
    - 128 以外の予期しない非ゼロ（権限/I-O エラー等）-> INSIDE_REPO
      （安全側フォールバック：git init を誘発しない）
    - TimeoutExpired -> INSIDE_REPO（安全側フォールバック：git init を誘発しない）
    - git コマンド不在（FileNotFoundError）-> GIT_MISSING
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(target_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return GitStatus.GIT_MISSING
    except subprocess.TimeoutExpired:
        # タイムアウト時は安全側（git init を誘発しない）に倒す
        return GitStatus.INSIDE_REPO
    if result.returncode == 0 and result.stdout.strip() == "true":
        return GitStatus.INSIDE_REPO
    if result.returncode == 128:
        return GitStatus.NOT_A_REPO
    # 予期しない returncode（権限/I-O エラー等）は安全側に倒し git init を誘発しない
    return GitStatus.INSIDE_REPO


def git_init(target_root: Path) -> bool:
    """Run ``git init`` in *target_root*. Return True on success (exit 0).

    git 不在・非 0 終了・タイムアウトのいずれでも False を返す（例外は送出しない）。
    """
    try:
        result = subprocess.run(
            ["git", "init"],
            cwd=str(target_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return False
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0
