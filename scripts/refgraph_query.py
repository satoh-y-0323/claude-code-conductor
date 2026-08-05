"""参照抽出器のクエリ層 — 網羅的な関係抽出から読む側が絞る道具.

`docs/refgraph-contract.md` §5-1「クエリ層」に基づく。
抽出器が網羅的に採った関係（36,498 辺・実リポジトリ）を 2 つの軸で絞り込む：

- **軸 1: 出所のカテゴリで外す** — live / history / derived / dev / tests
- **軸 2: 派生の参照先を原本へ畳む** — `src/c3/_template/...` → `...`

本モジュールは `scripts/` に置かれる配布外の dev ツール。
判定（到達可能性・削除可否）を配布物に置かない
（契約 §1-2 / §5-1）ため、配布外とした。
"""

from __future__ import annotations

import sys
from dataclasses import replace
from typing import TYPE_CHECKING

# stdout / stderr の reconfigure（§CLAUDE.md §9-3 / 契約 §5-1 の dev tool 規律）
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

# 型チェック時のみ c3.refgraph をインポート（bootstrap テスト対応）
if TYPE_CHECKING:
    import c3.refgraph as refgraph

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

# 契約 §5-1 表の 5 カテゴリ
CATEGORIES = ("live", "history", "derived", "dev", "tests")

# カテゴリ定義（契約 §5-1 表）
_TEMPLATE_PREFIX = "src/c3/_template/"
_DEV_PREFIX = ".dev/"
_TESTS_PREFIX = "tests/"
_HISTORY_PREFIXES = (
    ".claude/reports/",
    ".claude/memory/",
    ".claude/agent-memory/",
    ".claude/tmp/",
)
_HISTORY_EXACT = ("CHANGELOG.md",)

# ---------------------------------------------------------------------------
# 軸 1: カテゴリ判定（純粋関数・パス文字列のみ）
# ---------------------------------------------------------------------------


def categorize(path: str) -> str:
    """ルート相対パスをカテゴリに分類する（契約 §5-1 表）.

    判定はルート相対 POSIX パスの**前方一致**で行い、成分境界で切る。
    - `derived`: `src/c3/_template/` で始まる
    - `dev`: `.dev/` で始まる
    - `tests`: `tests/` で始まる
    - `history`: `.claude/reports/` / `.claude/memory/` / `.claude/agent-memory/` /
      `.claude/tmp/` で始まる、または `CHANGELOG.md` と完全一致
    - `live`: 上記以外

    **成分境界を意識する**:
    - `.dev/` は巻き込む（正）、`.devcontainer/` は巻き込まない（反例 1）
    - `CHANGELOG.md` は完全一致（反例 2）
    - `.claude/tmp-scratch.md` は巻き込まない

    **実装**: パスを `/` で分解し、成分単位で前方一致判定する。
    """
    # 完全一致（CHANGELOG.md のみ）
    if path == "CHANGELOG.md":
        return "history"

    # パスを `/` で分解
    components = path.split("/")

    # 派生: src/c3/_template/...
    if (
        len(components) >= 3
        and components[0] == "src"
        and components[1] == "c3"
        and components[2] == "_template"
    ):
        return "derived"

    # dev: .dev/...
    if components[0] == ".dev":
        return "dev"

    # tests: tests/...
    if components[0] == "tests":
        return "tests"

    # history: .claude/(reports|memory|agent-memory|tmp)/...
    if components[0] == ".claude" and len(components) >= 2:
        if components[1] in ("reports", "memory", "agent-memory", "tmp"):
            return "history"

    # 上記以外は live
    return "live"


# ---------------------------------------------------------------------------
# 軸 1 の道具: filter_links
# ---------------------------------------------------------------------------


def filter_links(
    links, categories: tuple[str, ...] | list[str]
) -> tuple[refgraph.Link, ...]:
    """**source** のカテゴリで辺を絞る（契約 §5-1 軸 1）.

    引数 `categories` に存在しないカテゴリ名があれば `ValueError` を上げる
    （黙って空を返すと綴り間違いが「そのカテゴリは 0 件」に化ける）。

    **注意**: `target` のカテゴリで絞ってはいけない。
    live 文書が `_template/` を指す辺は live（軸 2 で畳むべきもの）。
    """
    categories_set = set(categories)

    # 未知のカテゴリ名を検出
    unknown = sorted(categories_set - set(CATEGORIES))
    if unknown:
        raise ValueError(f"unknown category: {unknown}")

    # 空なカテゴリ指定は空を返す
    if not categories_set:
        return ()

    # source のカテゴリで絞る（出現順を保持）
    result = []
    for link in links:
        if categorize(link.source) in categories_set:
            result.append(link)

    return tuple(result)


# ---------------------------------------------------------------------------
# 軸 2: 派生を原本へ畳む（純粋関数）
# ---------------------------------------------------------------------------


def fold_target(target: str) -> str:
    """派生の target を原本へ畳む（契約 §5-1 軸 2）.

    `src/c3/_template/` プレフィックスを除去すると、
    派生ツリーの `src/c3/_template/.claude/X` → `.claude/X` になる。

    それ以外のパス（原本・テーブル等）は素通し。
    原本が無くても（ファイルシステムに触らずに）畳める（純粋関数）。
    """
    if target.startswith(_TEMPLATE_PREFIX):
        return target[len(_TEMPLATE_PREFIX) :]
    return target


# ---------------------------------------------------------------------------
# 軸 2 の道具: fold_links
# ---------------------------------------------------------------------------


def fold_links(links) -> tuple[refgraph.Link, ...]:
    """派生の target を原本へ畳み、7 フィールド完全一致で重複除去（契約 §5-1 軸 2）.

    **処理**:
    1. 各辺の target を `fold_target()` で畳む
    2. 7 フィールド（relation / source / source_line / context / target /
       target_exists / resolution）が同一の辺を出現順で重複除去

    **保持する**:
    - `source` は畳まない（出所が消えるため・契約 §2 原則 2）
    - `resolution` は書き換えない（「抽出時に曖昧だった」という出所の記録）
    - 入力の順序を保つ（出所を追う読み方が壊れないため）
    - 入力の `Link` を書き換えない（非破壊・`dataclasses.replace` で新規生成）

    **正の対照**: 派生の双子（同じ行の 2 候補が畳むと同一になる）は潰れる
    """
    seen = set()
    result = []

    for link in links:
        # target を畳む
        folded_target = fold_target(link.target)

        # 新しい Link を作る（入力を書き換えない）
        folded = replace(link, target=folded_target)

        # 7 フィールドのキーで重複判定
        key = (
            folded.relation,
            folded.source,
            folded.source_line,
            folded.context,
            folded.target,
            folded.target_exists,
            folded.resolution,
        )

        if key not in seen:
            seen.add(key)
            result.append(folded)

    return tuple(result)
