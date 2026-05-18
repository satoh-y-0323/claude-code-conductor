"""
tests/skills/_skill_helpers.py

skills 配下の SKILL.md を検証するテストで共通利用するヘルパー。

- SKILL_PATHS: 主要スキルの絶対パスを集中管理
- read_skill(): SKILL.md を読み込んで文字列で返す（存在しない場合は空文字）
- extract_section(): 指定見出しから次の同レベル見出しまでを抽出
- NEIGHBOR_WINDOW_LINES: 「見出し近傍」を判定する際の窓幅（行数）。テストファイル間で共有する。

意図: 旧来は各テストファイルで `_read_skill()` / `_extract_section()` を重複実装していた。
保守時の漏れを防ぐため、共通ヘルパーをここに集約する。
"""
from __future__ import annotations

import re
from pathlib import Path

WORKTREE_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = WORKTREE_ROOT / ".claude" / "skills"

# 主要スキルの SKILL.md 絶対パス
SKILL_PATHS = {
    "start": SKILLS_DIR / "start" / "SKILL.md",
    "dev-workflow": SKILLS_DIR / "dev-workflow" / "SKILL.md",
    "init-session": SKILLS_DIR / "init-session" / "SKILL.md",
}

# 見出しの「近傍」を判定するときに走査する行数。
# Step 2 マッピング表のようにラベル行と遷移先行が複数行に分かれていても拾えるよう、
# 余裕を持たせて 8 行とする。SKILL.md の記述が更にリッチになったら増やす。
NEIGHBOR_WINDOW_LINES = 8


def read_skill(skill_name: str) -> str:
    """指定スキルの SKILL.md を読み込んで返す。存在しない場合は空文字列を返す。"""
    path = SKILL_PATHS.get(skill_name)
    if path is None or not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


# 個別スキルの読み込み専用ショートカット（複数テストファイル間の `_start()` 等の重複を解消）
def read_start_skill() -> str:
    """`.claude/skills/start/SKILL.md` の内容を返す。"""
    return read_skill("start")


def read_dev_workflow_skill() -> str:
    """`.claude/skills/dev-workflow/SKILL.md` の内容を返す。"""
    return read_skill("dev-workflow")


def read_init_session_skill() -> str:
    """`.claude/skills/init-session/SKILL.md` の内容を返す。"""
    return read_skill("init-session")


def keyword_in_neighborhood(
    content: str,
    anchor: str,
    keywords: tuple[str, ...] | list[str],
    *,
    window_lines: int = NEIGHBOR_WINDOW_LINES,
    require_all: bool = True,
) -> bool:
    """`anchor` を含む行の近傍 `window_lines` 行に指定キーワードが現れるか判定する。

    Args:
        content: 検索対象のテキスト（通常 SKILL.md 全文）。
        anchor: 起点として探す文字列（例: "デバッグ調査から"）。これを含む行が
            複数あっても先頭からの全候補を試す。
        keywords: 近傍に出現してほしいキーワード群。
        window_lines: anchor の出現行から数えて何行先まで近傍とみなすか。
        require_all: True のとき全 keywords が同一近傍内に揃うこと、
            False のとき少なくとも 1 件以上含まれることを判定する。

    Returns:
        条件を満たす近傍が 1 つでも見つかれば True、無ければ False。
    """
    lines = content.splitlines()
    for idx, line in enumerate(lines):
        if anchor not in line:
            continue
        window = "\n".join(lines[idx: idx + window_lines])
        if require_all:
            if all(kw in window for kw in keywords):
                return True
        else:
            if any(kw in window for kw in keywords):
                return True
    return False


def extract_section(content: str, heading: str) -> str:
    """指定見出しから次の同レベル ## 見出しまでのテキストを返す。

    見つからない場合は空文字列を返す。
    """
    pattern = re.compile(
        r"(^" + re.escape(heading) + r".*?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(content)
    return match.group(1) if match else ""


def find_section_range(content: str, start_heading_prefix: str) -> tuple[int, int]:
    """指定見出しから次の同レベル見出しまでの (start_index, end_index) を返す。

    `start_heading_prefix` は行頭の Markdown 見出しに対してのみマッチする
    （`re.MULTILINE` の `^` を使用）。コードブロックや段落中の偶然の出現は無視する。

    **制約**: 範囲の終端は次の `## ` 見出しで終わる前提で実装されている。
    `### ` 見出しの範囲を切り出したい場合は本関数では対応していない（次の
    `## ` まで取得してしまうため）。`### ` の境界が必要な場合は専用ロジックを
    呼び出し側で実装すること。

    見つからない場合は (-1, -1) を返す。
    """
    head_match = re.search(
        r"^" + re.escape(start_heading_prefix),
        content,
        re.MULTILINE,
    )
    if head_match is None:
        return (-1, -1)
    start = head_match.start()
    # 次の行頭 "## " を本文から探す
    next_heading = re.search(
        r"^##\s",
        content[start + len(start_heading_prefix):],
        re.MULTILINE,
    )
    if next_heading is None:
        return (start, len(content))
    return (start, start + len(start_heading_prefix) + next_heading.start())
