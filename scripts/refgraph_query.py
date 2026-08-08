"""参照抽出器のクエリ層 — 網羅的な関係抽出から読む側が絞る道具.

`docs/refgraph-contract.md` §5-1「クエリ層」に基づく。
抽出器が網羅的に採った関係（36,498 辺・実リポジトリ。**C-25 適用前のレジームの値**——
`reference` 充足で `_dedupe` のキーが割れるため、適用後は総数が変わりうる）を
2 つの軸で絞り込む：

- **軸 1: 出所のカテゴリで外す** — live / history / derived / dev / tests / generated
- **軸 2: 派生の参照先を原本へ畳む** — `src/c3/_template/...` → `...`

本モジュールは `scripts/` に置かれる配布外の dev ツール。
判定（到達可能性・削除可否）を配布物に置かない
（契約 §1-2 / §5-1）ため、配布外とした。

コマンドとして実行すると 2 つの口を提供する（driver）:

    python scripts/refgraph_query.py --build <root>
        <root> 以下を抽出し <root>/graph.json へ書く

    python scripts/refgraph_query.py --target <path> [--graph <file>]
        グラフを読み、<path> を指す **live** の辺だけを JSON で stdout へ出す
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import TYPE_CHECKING

# stdout / stderr の reconfigure（§CLAUDE.md §9-3 / 契約 §5-1 の dev tool 規律）。
# 本スクリプトは stdin を読まないので stdin は reconfigure しない（§9-3 の射程どおり）。
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

# 契約 §5-1 表のカテゴリ（`generated` は改訂 6 の追加）
CATEGORIES = ("live", "history", "derived", "dev", "tests", "generated")

# カテゴリ定義（契約 §5-1 表）。
# **接頭辞は必ず `/` で終える**。これがそのまま「前方一致を成分境界で切る」の実装になる
# （`.dev/` は `.devcontainer/` に前方一致しない）。
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
# 生成物（改訂 6）。ビルド・実行で作られるので原本を直す対象ではない。
_GENERATED_PREFIXES = (
    ".claude/state/",
    ".claude/logs/",
    "site/",
)

# `sqltable:<name>` のノード（契約 §3 `nodes[].kind`）。
_TABLE_PREFIX = "sqltable:"

# `by_target_kind` が受け付ける kind。クエリ層が自分で定義する閉じた語彙。
_TARGET_KINDS = ("file", "table")

# 契約 §4 の relation 一覧・契約 §3 の resolution 4 値。
# 契約 C-9 が「未知の値（未知のカテゴリ名・relation 名・resolution 名・kind 名）を
# 渡すと `ValueError`」と定めているため、クエリ層が閉じた語彙として持つ。
# 抽出器から import しない（クエリ層は `read_graph` の結果を読むだけ・完成条件 4）。
_RELATIONS = (
    "settings_hook",
    "settings_statusline",
    "settings_permission",
    "md_code_span_path",
    "md_link",
    "md_c3_run",
    "md_agent_variant_map",
    "md_subagent_type",
    "md_bare_agent_name",
    "md_bare_skill_name",
    "py_import",
    "py_importlib",
    "py_subprocess_path",
    "py_sql_table",
    "md_prose_path",
    "md_fence_path",
    "py_string",
    "py_comment",
    "text_path",
)
_RESOLUTIONS = ("exact", "basename", "ambiguous", "missing")

# driver が読み書きするグラフファイルの既定名。
_GRAPH_FILENAME = "graph.json"

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
    - `generated`: `.claude/state/` / `.claude/logs/` / `site/` で始まる（改訂 6）
    - `live`: 上記以外

    **成分境界を意識する**:
    - `.dev/` は巻き込む（正）、`.devcontainer/` は巻き込まない（反例 1）
    - `CHANGELOG.md` は完全一致（反例 2）
    - `.claude/tmp-scratch.md` は巻き込まない
    - `site-old/page.html` は巻き込まない

    **実装**: 接頭辞が `/` で終わるので `startswith` がそのまま成分境界での前方一致になる。
    `docs/.claude/reports/x.md` のように成分の**途中**に同じ並びが現れる形は、
    先頭から見ているので一致しない。

    判定順序は互いに素な条件の中でも固定する（`src/c3/_template/CHANGELOG.md` は
    `derived`・`.dev/CHANGELOG.md` は `dev`）。
    """
    if path.startswith(_TEMPLATE_PREFIX):
        return "derived"
    if path.startswith(_DEV_PREFIX):
        return "dev"
    if path.startswith(_TESTS_PREFIX):
        return "tests"
    if path in _HISTORY_EXACT or path.startswith(_HISTORY_PREFIXES):
        return "history"
    if path.startswith(_GENERATED_PREFIXES):
        return "generated"
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
    """派生の target を原本へ畳み、8 フィールド完全一致で重複除去（契約 §5-1 軸 2）.

    **処理**:
    1. 各辺の target を `fold_target()` で畳む
    2. 8 フィールド（relation / source / source_line / context / target /
       target_exists / resolution / reference）が同一の辺を出現順で重複除去

    `reference`（辺を生んだ読みの原文断片）をキーに含めるのは、抽出器の `_dedupe`
    と同じ理由（改訂 14 §4-6 / 契約 §2 原則 2）。原文が違えば別の出所なので、
    畳んだ結果 target が同じになっても 1 本にまとめない。

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

        # 8 フィールドのキーで重複判定
        key = (
            folded.relation,
            folded.source,
            folded.source_line,
            folded.context,
            folded.target,
            folded.target_exists,
            folded.resolution,
            folded.reference,
        )

        if key not in seen:
            seen.add(key)
            result.append(folded)

    return tuple(result)


# ---------------------------------------------------------------------------
# 軸 2 の道具: settled_links
# ---------------------------------------------------------------------------


def settled_links(links) -> tuple[refgraph.Link, ...]:
    """候補が 1 つへ収束した辺だけを返す（畳んだ後に呼ぶ・契約 §5-1 軸 2）.

    グループキーは `(relation, source, reference)`（契約 C-9）。同じ読みの同じ原文から
    出た辺が 1 つのグループで、その中の **target の異なり数**が候補の数になる。

    - 候補が 1 つのグループ → 返す
    - 候補が 2 つ以上のグループ（どれを指すか決まっていない）→ 返さない

    **各辺自身の `resolution` は判定に使わない。** T-5b の追加辺（契約 C-17）は
    同じ `(relation, source, reference)` から `missing` と `exact` の 2 本を出すので、
    `ambiguous` だけを見る実装ではこの 2 本組が両方とも「収束済み」として通ってしまう。

    `resolution` は書き換えない（契約 §5-1「情報を消さない」）。曖昧だった事実は
    出所の記録として残り、本関数は**絞るだけ**である。
    """
    links = tuple(links)

    candidates = {}
    for link in links:
        key = (link.relation, link.source, link.reference)
        candidates.setdefault(key, set()).add(link.target)

    result = []
    for link in links:
        key = (link.relation, link.source, link.reference)
        if len(candidates[key]) == 1:
            result.append(link)

    return tuple(result)


# ---------------------------------------------------------------------------
# グロブ照合（純粋関数）
# ---------------------------------------------------------------------------


def _component_matches(pattern: str, text: str) -> bool:
    """1 成分ぶんのワイルドカード照合（正規表現を生成しない・線形時間）.

    `*` は `[^/]*`（`/` をまたがない）。**連続する `*` は 1 つと等価**なので `**` も
    「成分をまたぐ」意味は持たない。`*` 以外の文字はリテラルとして 1 文字ずつ突き合わせる。

    **正規表現を使わない理由**: 可変長ワイルドカードを連結した正規表現は、末尾リテラルに
    一致しない入力でバックトラックが爆発する。`*` が**連続する**形（`***ZZZ`）は畳み込みで
    消せたが、**同じリテラルで区切られて反復する**形（`("*a" * 15) + "ZZZ"` を `"a" * 40` に
    当てる）は畳み込みが効かず、実測 4.8〜9.3 秒のハングが残っていた（SR-NEW M-1'）。
    素朴な atomic group 化はマッチ結果自体を変えてしまう（SR の差分ファジングで
    20,000 組中 554 件の不一致を実証済み）ため採らない。

    代わりに古典的な greedy two-pointer 法を使う: `*` に出会ったら「最後のスター位置」と
    「その時点の入力位置」を記録して先へ進み、不一致になったらスター直後へ巻き戻して
    スターが飲む文字を 1 つ増やす。巻き戻し先は単調に前進するため
    **O(len(pattern) × len(text)) の多項式時間**で、入力依存の破滅的挙動が構造的に起きない。

    **`*` の個数に上限は設けない**（上限で `ValueError` にすると、正当なパターンを弾く
    方向の非対称な事故が起きる）。
    """
    pattern_index = 0
    text_index = 0
    star_index = -1
    star_text_index = 0
    while text_index < len(text):
        if pattern_index < len(pattern) and pattern[pattern_index] == "*":
            # スターは巻き戻し先として覚えるだけで、まず 0 文字を飲む形で進む。
            star_index = pattern_index
            star_text_index = text_index
            pattern_index += 1
        elif (
            pattern_index < len(pattern)
            and pattern[pattern_index] == text[text_index]
        ):
            pattern_index += 1
            text_index += 1
        elif star_index >= 0 and text[star_text_index] != "/":
            # 直近のスターに 1 文字余分に飲ませて再挑戦する。`/` は飲ませない
            # （`*` は 1 成分内・`[^/]*` と同じ意味論）。
            star_text_index += 1
            pattern_index = star_index + 1
            text_index = star_text_index
        else:
            return False
    while pattern_index < len(pattern) and pattern[pattern_index] == "*":
        pattern_index += 1
    return pattern_index == len(pattern)


def glob_matches(pattern: str, path: str) -> bool:
    """グロブ `pattern` がルート相対 POSIX パス `path` に一致するか（純粋関数）.

    **`*` は 1 成分内でだけ広がる**（`/` をまたがない）。したがって成分数が違えば
    一致しない:

    - `src/**/*.py` は `src/c3/refgraph.py` に一致する（正）
    - `src/**/*.py` は `src/c3/_template/.claude/hooks/stop.py` に一致しない
      （中間成分が 2 段以上・負）
    - `.claude/*.md` は `.claude/docs/note.md` に一致しない（成分数が違う・負）

    `fnmatch` は使わない。`fnmatch` の `*` は `/` をまたぐため「1 成分内」を書けない。
    ファイルシステムには触らない（実在しないパスにも当たる）。
    """
    pattern_parts = pattern.split("/")
    path_parts = path.split("/")
    if len(pattern_parts) != len(path_parts):
        return False
    for pattern_part, path_part in zip(pattern_parts, path_parts):
        if "*" not in pattern_part:
            if pattern_part != path_part:
                return False
            continue
        if not _component_matches(pattern_part, path_part):
            return False
    return True


# ---------------------------------------------------------------------------
# 絞り込みの道具（純粋関数）
# ---------------------------------------------------------------------------


def by_relation(links, relation: str) -> tuple[refgraph.Link, ...]:
    """`relation` で辺を絞る（契約 §4 の関係の種類）.

    未知の relation 名は `ValueError` で落とす（契約 C-9）。黙って空を返すと
    綴り間違いが「その relation は 0 件」に化け、読む側が「関係が無い」と誤読する。
    """
    if relation not in _RELATIONS:
        raise ValueError(f"unknown relation: {relation!r} (expected one of {_RELATIONS})")
    return tuple(link for link in links if link.relation == relation)


def by_resolution(links, resolution: str) -> tuple[refgraph.Link, ...]:
    """`resolution` で辺を絞る（契約 §3 の 4 値）.

    `by_relation` と同じく、未知の resolution 名は `ValueError` で落とす（契約 C-9）。
    """
    if resolution not in _RESOLUTIONS:
        raise ValueError(
            f"unknown resolution: {resolution!r} (expected one of {_RESOLUTIONS})"
        )
    return tuple(link for link in links if link.resolution == resolution)


def by_target_kind(links, kind: str) -> tuple[refgraph.Link, ...]:
    """target の種類（`file` / `table`）で辺を絞る.

    `sqltable:` で始まる target がテーブル、それ以外がファイル（契約 §3 `nodes[].kind`）。
    kind の語彙は**このモジュールが定義する閉じた 2 値**なので、未知値は
    `filter_links` と同じく `ValueError` で落とす（黙って空を返すと綴り間違いが
    「その kind は 0 件」に化ける）。
    """
    if kind not in _TARGET_KINDS:
        raise ValueError(f"unknown target kind: {kind!r} (expected one of {_TARGET_KINDS})")
    if kind == "table":
        return tuple(link for link in links if link.target.startswith(_TABLE_PREFIX))
    return tuple(link for link in links if not link.target.startswith(_TABLE_PREFIX))


def to_targets(links) -> frozenset:
    """辺の集合から target の集合を作る（重複なし）."""
    return frozenset(link.target for link in links)


# ---------------------------------------------------------------------------
# driver（コマンド実行の入口）
# ---------------------------------------------------------------------------


def _load_refgraph():
    """抽出器モジュールを読み込む.

    作業ツリーの `src/` を先頭へ入れる。pip 済みの `c3`（site-packages）を掴むと
    **配布済みの古い抽出器**でグラフを作ってしまい、実装と測定がずれる。
    """
    src_dir = Path(__file__).resolve().parent.parent / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    import c3.refgraph as refgraph_module

    return refgraph_module


def _points_at(target: str, wanted: str) -> bool:
    """`target` が `wanted` を指しているか（完全一致 or 成分境界での末尾一致）.

    C3 の文書は `agents/tdd-develop.md` のような**部分パス**で参照を書くため、
    完全一致だけにすると「その名前で引けない」道具になる（抽出器 `_suffix_matches`
    と同じ理由・契約 §3 解決の順序 3）。
    """
    return target == wanted or target.endswith("/" + wanted)


def _run_build(root: str) -> int:
    """`--build`: `root` 以下を抽出し `root/graph.json` へ書く."""
    refgraph_module = _load_refgraph()
    root_path = Path(root)
    graph = refgraph_module.build_graph(root_path)
    out = root_path / _GRAPH_FILENAME
    refgraph_module.write_graph(graph, out)
    print(f"wrote {out} (links={len(graph.links)} nodes={len(graph.nodes)})", file=sys.stderr)
    return 0


def _run_target(target: str, graph: str | None) -> int:
    """`--target`: グラフを読み、`target` を指す live の辺を JSON で stdout へ出す."""
    refgraph_module = _load_refgraph()
    graph_path = Path(graph) if graph else Path.cwd() / _GRAPH_FILENAME
    if not graph_path.is_file():
        # メッセージを ASCII に保つ: 読み手はパイプ越しの呼び出し側（テストの
        # `subprocess.run(text=True)` など）で、Windows では既定 cp932 で復号される。
        # 日本語を出すと復号側が落ち、肝心の理由が読めなくなる（CLAUDE.md §9）。
        print(
            f"graph file not found: {graph_path}"
            " (run --build first, or pass --graph <file>)",
            file=sys.stderr,
        )
        return 2

    loaded = refgraph_module.read_graph(graph_path)
    links = fold_links(filter_links(loaded.links, ("live",)))
    wanted = fold_target(target)
    matched = tuple(link for link in links if _points_at(link.target, wanted))

    payload = {
        "graph": str(graph_path),
        # 枠付け宣言（SR-AI-001）。抽出器の私有定数を**そのまま**参照して同値にする
        # （driver は配布外の dev ツールなので抽出器の内部を読んでよい・契約 §5-1）。
        # 文面を写経すると to_dict() 側と静かにずれる。
        "framing": refgraph_module._FRAMING,
        "target": wanted,
        "links": [asdict(link) for link in matched],
    }
    # `ensure_ascii=True`（既定）で出す。`context` は日本語を含みうるが、stdout の
    # 読み手はパイプ越しの呼び出し側で、Windows では既定 cp932 で復号される。
    # `\\uXXXX` 形なら復号側の既定エンコーディングに依らず `json.loads` で戻る
    # （ファイルへ書く `write_graph` は UTF-8 固定なのでそちらは非 ASCII のままでよい）。
    print(json.dumps(payload, indent=2))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """driver の引数定義（`--build` と `--target` の 2 口）."""
    parser = argparse.ArgumentParser(
        prog="refgraph_query.py",
        description="参照グラフの生成と、live に絞った逆引き（配布外の dev ツール）",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--build", metavar="ROOT", help="ROOT を抽出し ROOT/graph.json へ書く")
    mode.add_argument("--target", metavar="PATH", help="PATH を指す live の辺を JSON で出す")
    parser.add_argument(
        "--graph",
        metavar="FILE",
        help="--target が読むグラフファイル（既定: カレントディレクトリの graph.json）",
    )
    return parser


def main(argv=None) -> int:
    """driver の入口。終了コードを返す（0 = 正常）."""
    args = _build_parser().parse_args(argv)
    if args.build is not None:
        return _run_build(args.build)
    return _run_target(args.target, args.graph)


if __name__ == "__main__":
    sys.exit(main())
