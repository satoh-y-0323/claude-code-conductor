"""
tests/skills/_skill_helpers.py

skills 配下のテストで共通利用するヘルパー。

- WORKTREE_ROOT / SKILLS_DIR: リポジトリルートと skills ディレクトリの絶対パス
- read_skill(): SKILL.md を読み込んで文字列で返す（存在しない場合は空文字）
"""
from __future__ import annotations

from pathlib import Path

WORKTREE_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = WORKTREE_ROOT / ".claude" / "skills"

# SKILL.md を読むテストが使うスキルの絶対パス
SKILL_PATHS = {
    "start": SKILLS_DIR / "start" / "SKILL.md",
    "init-session": SKILLS_DIR / "init-session" / "SKILL.md",
    "setup": SKILLS_DIR / "setup" / "SKILL.md",
}


def read_skill(skill_name: str) -> str:
    """指定スキルの SKILL.md を読み込んで返す。存在しない場合は空文字列を返す。"""
    path = SKILL_PATHS.get(skill_name)
    if path is None or not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


# 個別スキルの読み込み専用ショートカット
def read_start_skill() -> str:
    """`.claude/skills/start/SKILL.md` の内容を返す。"""
    return read_skill("start")


def read_init_session_skill() -> str:
    """`.claude/skills/init-session/SKILL.md` の内容を返す。"""
    return read_skill("init-session")


def read_setup_skill() -> str:
    """`.claude/skills/setup/SKILL.md` の内容を返す。"""
    return read_skill("setup")
