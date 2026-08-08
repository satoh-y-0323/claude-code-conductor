"""Files inside ``.claude/`` that are personal/working state.

Used by:

- ``c3 init``    — never copied to the destination project
- ``c3 update``  — never overwritten in the destination project
- ``hatch_build.py`` — never bundled into the wheel template (the patterns are
  duplicated there because the build hook runs *before* the package is
  importable; keep both lists in sync)

Patterns are POSIX-style and relative to the ``.claude/`` directory itself
(e.g. ``"reports/*"``, not ``".claude/reports/*"``). ``KEEP_PATTERNS`` win
over ``EXCLUDE_PATTERNS`` so that placeholder ``.gitkeep`` files survive.

A second, independent axis lives here as well: ``INIT_ONLY_PATTERNS`` (see
:func:`is_init_only`) marks files that ``c3 init`` places but ``c3 update``
must never overwrite, because the destination copy becomes user-owned once it
exists. ``should_skip`` cannot express this — it collapses three questions
(bundle into the wheel / place on init / overwrite on update) into one boolean.

See ``.claude/docs/config-policy.md`` for the distribution decision matrix
and the rationale behind each excluded pattern.
"""

from __future__ import annotations

import fnmatch

EXCLUDE_PATTERNS: tuple[str, ...] = (
    "reports/*",
    "memory/sessions/*",
    "memory/archive/*",
    "memory/patterns.json",
    "memory/agent-audit.log",
    "memory/consolidated_summary.md",
    "memory/promotion-candidates.md",
    "agent-memory/*",
    "tmp/*",
    "docs/decisions.md",
    "docs/taxonomy.md",
    "docs/c3追加予定機能リスト.md",
    "hooks/subagent_log.py",
    "settings.local.json",
    "pytest_temp.ini",
    "logs/*",
    # state/* で v2.10.0 の recall.hnsw / recall_meta.json も自動除外
    "state/*",
    # v2.14.1: parallel-agents skill が isolation:"worktree" で生成する一時 worktree。
    # マージ後に削除されるが残骸が残ることがあり、wheel に混入すると利用先に
    # 不要な agent worktree レポート（code-review-report-*.md 等）が配布される問題があった
    "worktrees/*",
    # v2.1.0: tdd-develop / worktree-tdd-workflow 廃止（planner が TDD を 3-wave に分解する設計に統一）
    "agents/tdd-develop.md",
    "skills/worktree-tdd-workflow/*",
)

KEEP_PATTERNS: tuple[str, ...] = (
    "reports/.gitkeep",
    "memory/.gitkeep",
    "memory/sessions/.gitkeep",
    "memory/archive/.gitkeep",
    "tmp/.gitkeep",
    "state/.gitkeep",
    "deletions.txt",  # 新規: c3 update が読む削除指示書
    "breaking-changes.txt",  # v2.19.0: c3 update が読む破壊的変更ログ
)


# ``c3 init`` では配置したいが ``c3 update`` では上書きしたくないファイル。
#
# ``should_skip`` は「wheel 収録 / init 配置 / update 上書き」の 3 つを 1 つの真偽値で
# 兼ねているため、「初回は配置するが以後はユーザーが育てる」領域を表現できなかった
# （``should_skip`` に入れると wheel からも消えて init でも配置されない）。
# 本リストはその 3 番目の軸だけを分離する。``should_skip`` とは独立で、
# INIT_ONLY のファイルも wheel には収録され ``c3 init`` で配置される。
#
# 3 ファイル同期（``.gitignore`` / ``_excludes.py`` / ``hatch_build.py``）の対象外。
# wheel ビルドは ``should_skip`` しか参照しないため ``hatch_build.py`` への複製は不要。
#
# **重要**: パターンは小文字・末尾ドット/スペースなしの正規形で書くこと。
# ``is_init_only()`` は ``fnmatch.fnmatchcase()`` （ケース依存）で比較するため、
# パターンが大文字・末尾ドット/スペース込みだと、その形式の入力にのみ一致し、
# 正規化済みの入力では一致しないという矛盾が生じる（例: パターン
# ``Rules/Promoted/Index.md`` は小文字版 ``rules/promoted/index.md`` に一致しない）。
# ``cli_update.py`` step 13 側で入力を正規化するため、パターン側は常に正規形であるべき。
INIT_ONLY_PATTERNS: tuple[str, ...] = (
    # 利用先で /promote-pattern が目録行を追記するユーザー所有領域
    "rules/promoted/index.md",
    # 利用先が独自の除外行を追記しうる（上書きすると次の commit で
    # 意図しないファイルが tracked になる）
    ".gitignore",
)


def should_skip(rel_posix: str) -> bool:
    """Return True if the path (relative to ``.claude/``) is personal state."""
    parts = rel_posix.split("/")
    if "__pycache__" in parts or rel_posix.endswith((".pyc", ".pyo")):
        return True
    if any(fnmatch.fnmatchcase(rel_posix, p) for p in KEEP_PATTERNS):
        return False
    return any(fnmatch.fnmatchcase(rel_posix, p) for p in EXCLUDE_PATTERNS)


def is_init_only(rel_posix: str) -> bool:
    """Return True if ``c3 init`` must place the file but ``c3 update`` must not
    overwrite it (user-owned after the first placement).

    Independent of :func:`should_skip`: an init-only file is still bundled into
    the wheel and placed by ``c3 init``; only the overwrite step is skipped.
    """
    return any(fnmatch.fnmatchcase(rel_posix, p) for p in INIT_ONLY_PATTERNS)
