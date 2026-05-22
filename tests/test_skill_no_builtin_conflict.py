"""skill ディレクトリと Claude Code Built-in コマンドの衝突回避を保証する回帰テスト。"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_review_phase_skill_exists():
    """v2.15.1 でリネームした /review-phase skill が存在すること。"""
    path = REPO_ROOT / ".claude" / "skills" / "review-phase" / "SKILL.md"
    assert path.exists(), f"review-phase skill SKILL.md が存在しません: {path}"


def test_old_code_review_skill_not_exists():
    """v2.15.1 でリネームした旧 /code-review skill が削除されていること（Built-in /code-review と衝突するため）。"""
    path = REPO_ROOT / ".claude" / "skills" / "code-review" / "SKILL.md"
    assert not path.exists(), (
        f"旧 code-review skill SKILL.md が残存しています: {path}\n"
        "Claude Code v2.1.147 で追加された Built-in /code-review と衝突するため削除してください。"
    )
