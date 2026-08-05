"""参照抽出器（refgraph）— C3 のファイル間参照を種類と出所つきで抽出しファイルへ出力する.

契約は `docs/refgraph-contract.md`（配布元専用資料・wheel には収録されない）。

**この道具は判定をしない。** 削除してよいか・生きているかは、出力を読む側が決める。
したがって本モジュールはルート集合・起点定義・出所によるフィルタを一切持たない
（契約 §6 条件 5 が静的検査で機械強制している）。

収集方針（契約 §2）:

1. 落とさない — 出所（`CHANGELOG` / `_template/` / `.dev/` / `tmp/` / `reports/` / `tests/`）
   でフィルタしない。採りすぎは読む側で絞れるが、採り漏らしは気づけない
2. 出所を残す — すべての関係に `source` / `source_line` / `context` を付ける
3. 拾えなかったものを残す — 読めなかったファイルは `skipped` へ、
   実在しない参照先は `target_exists: false` の辺として出す

本モジュールは配布 wheel に収録されるため **標準ライブラリのみ**で書く。
またライブラリとして stdout / stderr へ一切書かない（print しない）。
"""

from __future__ import annotations

import ast
import bisect
import fnmatch
import json
import os
import posixpath
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

# 出力スキーマのバージョン（契約 §3）。
SCHEMA_VERSION = 1

# `context` の長さ上限（契約 §3「長さ上限で切る」）。外部由来の 1 行をそのまま
# 埋め込むと読む側の端末を壊すか JSON が肥大するため、抜粋として切る。
CONTEXT_MAX_CHARS = 300

# 走査しないディレクトリ名。
# 契約 §2 原則 1 が禁じるのは「**出所**によるフィルタ」（誰が書いた参照かで捨てること）であり、
# VCS の内部表現やバイトコードキャッシュは人が書いた参照元ではないので対象外にする。
_SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        "node_modules",
        ".venv",
        "venv",
    }
)

# 参照とみなす拡張子。これに当たらない文字列はパス参照として扱わない。
_REFERENCE_SUFFIXES = (
    "py",
    "md",
    "mdc",
    "json",
    "jsonl",
    "txt",
    "sql",
    "sh",
    "ps1",
    "toml",
    "yaml",
    "yml",
    "cfg",
    "ini",
    "flag",
)

# トークンを開始してよい ASCII の区切り文字（契約 §3「トークン境界」）。
# ここに無い ASCII（英数字・`_ . / \ - ~ + $ { } < > % ) ] & # @` など）は
# 「同じトークンの途中」とみなし、そこから新しい参照を開始しない。
_ASCII_BOUNDARY = frozenset(" \t\r\n\x0b\x0c`\"'([,;:=|")

# `*` の直前がこれらならグロブの途中（`reports/*-{ts}.md`）。
# 空白や別の `*` に続く `*` は Markdown の強調（`**stop.py**`）なので区切り扱いにする。
_GLOB_BODY_RE = re.compile(r"[/A-Za-z0-9_.~{}$+-]")

# 文字列中からパスらしいトークンを取り出す。
#   - `${CLAUDE_PROJECT_DIR}/` のような変数プレフィクスを 1 つだけ許す
#   - 本体に空白・引用符・括弧を含めない（末尾の `*)` や `,` を巻き込まない）
#   - 開始位置の妥当性は `_starts_at_boundary` が判定する（後読みでは書けないため）
_PATH_TOKEN_RE = re.compile(
    r"(?:\$\{[A-Za-z_][A-Za-z0-9_]*\}/)?"
    r"[A-Za-z0-9_.~-][A-Za-z0-9_./{}$~+-]*"
    r"\.(?:" + "|".join(_REFERENCE_SUFFIXES) + r")"  # nul-boundary: allow(正規表現の選択肢の組み立て。区切りは正規表現の文法で固定されており、機械可読な行集合ではない)
    r"(?![\w.])"
)

# md のコードスパン（改行をまたがない）。
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")

# md のマークダウンリンク `[表示](パス)`。
_MD_LINK_RE = re.compile(r"\[[^\]\n]*\]\(\s*<?([^)>\s]+)")

# md 本文の `c3 run <path>`。
_C3_RUN_RE = re.compile(r"(?<![\w-])c3\s+run\s+(\S+)")

# md の `subagent_type: <name>` / `agent: <name>`（frontmatter に限らず本文中も拾う。
# C3 では起動手順が散文・コードスパンの中に書かれているため）。
_SUBAGENT_RE = re.compile(
    r"(?<![\w-])(?:subagent_type|agent)\s*[:=]\s*[\"']?([a-z][a-z0-9_-]*)"
)

# 表の区切り行（`| --- | --- |`）判定に使う文字集合。
_TABLE_RULE_CHARS = set("|-: ")

# 表セルから取り出す識別子（バッククォートは剥がしてから当てる）。
_CELL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

# ノード ID から agent / skill の名前を逆引きする（実ツリーの棚卸しから作るため、
# 名前の一覧をコードに焼き付けない）。
_AGENT_ID_RE = re.compile(r"(?:.*/)?\.claude/agents/([^/]+)\.md")
_SKILL_ID_RE = re.compile(r"(?:.*/)?\.claude/skills/([^/]+)/SKILL\.md")

# 制御文字（C0 / DEL / C1 / 行区切り）。`context` から除去する。
# エスケープ列をソースへ直書きすると実体文字に化けるため、行区切りは chr() で足す。
_CONTROL_RE = re.compile(
    "[" + chr(0) + "-" + chr(31)
    + chr(127) + "-" + chr(159)
    + chr(0x2028) + chr(0x2029) + "]"
)

# SQL 文字列中のテーブル名。
_SQL_TABLE_RE = re.compile(
    r"(?:(?<![\w.])(?:FROM|JOIN|INTO|UPDATE)\s+([A-Za-z_][A-Za-z0-9_]*))"
    r"|(?:(?<![\w.])(?:CREATE|DROP)\s+TABLE\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*))",
    re.IGNORECASE,
)

# SQL 文らしさの判定。これを含まない文字列は SQL とみなさない
# （`from x import y` を書いた docstring をテーブル参照と誤読しないため）。
_SQL_VERB_RE = re.compile(
    r"(?<![\w.])(?:SELECT|INSERT|UPDATE|DELETE|CREATE\s+TABLE|DROP\s+TABLE|"
    r"ALTER\s+TABLE|REPLACE\s+INTO)(?![\w.])",
    re.IGNORECASE,
)

# テーブル名として採らない SQL 予約語。
_SQL_STOP_WORDS = frozenset(
    {
        "select",
        "where",
        "order",
        "group",
        "having",
        "limit",
        "offset",
        "values",
        "set",
        "as",
        "on",
        "table",
        "if",
        "not",
        "exists",
        "import",
        "and",
        "or",
        "by",
        "distinct",
        "union",
        "returning",
    }
)

# 動的ロード（`_load_module("x")` / `importlib.import_module("x")`）の呼び出し名。
_DYNAMIC_LOAD_RE = re.compile(r"load_module|import_module")

# `settings*.json` のどの節をどの relation にするか（契約 §4）。
_SETTINGS_SECTIONS = (
    ("hooks", "settings_hook"),
    ("statusLine", "settings_statusline"),
    ("permissions", "settings_permission"),
)


# ---------------------------------------------------------------------------
# 出力の型（契約 §3 / §5）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Node:
    """グラフのノード。ファイル、または `sqltable:<name>` のテーブル."""

    id: str
    kind: str
    exists: bool


@dataclass(frozen=True)
class Link:
    """実在する参照関係 1 本。出所（source / source_line / context）を必ず持つ."""

    relation: str
    source: str
    source_line: int
    context: str
    target: str
    target_exists: bool
    resolution: str


@dataclass(frozen=True)
class Skipped:
    """拾えなかったファイル（契約 §2 原則 3）."""

    path: str
    reason: str


class Graph:
    """抽出結果。判定 API は持たない（契約 §5 / §6 条件 5）."""

    def __init__(self, root, file_count, nodes, links, skipped):
        self.root = str(root)
        self.file_count = int(file_count)
        self.nodes = tuple(nodes)
        self.links = tuple(links)
        self.skipped = tuple(skipped)

    def to_dict(self) -> dict:
        """契約 §3 のスキーマの dict を返す."""
        return {
            "schema_version": SCHEMA_VERSION,
            "root": self.root,
            "generated_from": {"file_count": self.file_count},
            "nodes": [
                {"id": node.id, "kind": node.kind, "exists": node.exists}
                for node in self.nodes
            ],
            "links": [
                {
                    "relation": link.relation,
                    "source": link.source,
                    "source_line": link.source_line,
                    "context": link.context,
                    "target": link.target,
                    "target_exists": link.target_exists,
                    "resolution": link.resolution,
                }
                for link in self.links
            ],
            "skipped": [
                {"path": entry.path, "reason": entry.reason} for entry in self.skipped
            ],
        }


# ---------------------------------------------------------------------------
# 公開 API（契約 §5）
# ---------------------------------------------------------------------------
def build_graph(root) -> Graph:
    """`root` 以下を走査して参照関係を抽出する.

    引数は `root` のみ。起点集合・フィルタを外から差し込む余地は持たない
    （判定はクエリ側の責務・契約 §5）。
    """
    collector = _Collector(Path(root).resolve())
    collector.collect()
    return collector.result()


def write_graph(graph: Graph, path) -> None:
    """`Graph` を UTF-8 の JSON としてファイルへ書く（契約 §6 条件 4）."""
    out = Path(path)
    if out.parent and not out.parent.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(graph.to_dict(), ensure_ascii=False, indent=2, sort_keys=False)
    out.write_text(payload + "\n", encoding="utf-8")


def read_graph(path) -> Graph:
    """`write_graph` が書いた JSON を読み戻す."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Graph(
        root=data["root"],
        file_count=data["generated_from"]["file_count"],
        nodes=[
            Node(id=item["id"], kind=item["kind"], exists=item["exists"])
            for item in data["nodes"]
        ],
        links=[
            Link(
                relation=item["relation"],
                source=item["source"],
                source_line=item["source_line"],
                context=item["context"],
                target=item["target"],
                target_exists=item["target_exists"],
                resolution=item["resolution"],
            )
            for item in data["links"]
        ],
        skipped=[
            Skipped(path=item["path"], reason=item["reason"]) for item in data["skipped"]
        ],
    )


# ---------------------------------------------------------------------------
# 内部: テキスト中の位置解決
# ---------------------------------------------------------------------------
class _TextLocator:
    """生テキスト中で「その値が書かれている行」を求める.

    JSON は `json.loads` すると行番号が失われるため、パース済みの値を生テキストへ
    突き合わせて出所を復元する（契約 §2 原則 2。旧実装は `source_line=1` 固定だった）。
    """

    def __init__(self, text: str):
        self.text = text
        self.lines = text.split("\n")
        self.starts = []
        pos = 0
        for line in self.lines:
            self.starts.append(pos)
            pos += len(line) + 1
        self.cursor = {}

    def line_of(self, value: str):
        """`value` が現れる行番号（1 始まり）。見つからなければ None."""
        for needle in (
            json.dumps(value, ensure_ascii=False),
            json.dumps(value),
            value,
        ):
            if not needle:
                continue
            begin = self.cursor.get(needle, 0)
            found = self.text.find(needle, begin)
            if found < 0:
                found = self.text.find(needle)
                if found < 0:
                    continue
            else:
                self.cursor[needle] = found + 1
            return bisect.bisect_right(self.starts, found)
        return None

    def line_text(self, number: int) -> str:
        if 1 <= number <= len(self.lines):
            return self.lines[number - 1]
        return ""


def _starts_at_boundary(text: str, index: int) -> bool:
    """その位置から参照トークンを開始してよいか（契約 §3「トークン境界」）.

    許容集合外の文字でトークンが途切れたとき、その**続き**を独立した参照として
    採らないための判定。`doc-{名前}-{timestamp}.md` の `-{timestamp}.md` や
    `e0-$(date +%s)-$$-$RANDOM.txt` の `-$$-$RANDOM.txt` はここで落ちる。
    """
    if index <= 0:
        return True
    previous = text[index - 1]
    if previous == "*":
        # Markdown の強調（`**stop.py**`）は区切り、グロブ（`x/*-y.md`）は途中。
        return index < 2 or not _GLOB_BODY_RE.match(text[index - 2])
    if previous.isascii():
        return previous in _ASCII_BOUNDARY
    # 非 ASCII は句読点・空白だけを区切りとする（日本語の `。` `、` `（` など）。
    # 文字（漢字・かな）はトークンが途切れただけなので区切りにしない。
    return unicodedata.category(previous)[0] in ("P", "Z")


def _path_tokens(text: str):
    """境界から始まるパストークンだけを順に返す."""
    for match in _PATH_TOKEN_RE.finditer(text):
        if _starts_at_boundary(text, match.start()):
            yield match


def _sanitize_context(line: str) -> str:
    """該当行を `context` に使える形へ整える（契約 §3）."""
    text = _CONTROL_RE.sub(" ", line).strip()
    if len(text) > CONTEXT_MAX_CHARS:
        text = text[:CONTEXT_MAX_CHARS]
    text = text.strip()
    if not text:
        # 参照がある以上ここには来ないはずだが、context を空にすると
        # 読む側が出所を追えなくなるので必ず何かを残す。
        return "(no printable context)"
    return text


def _split_lines(text: str) -> list:
    """行番号を安定させるため `\\n` だけで分割する（`splitlines` は垂直タブ等でも切る）."""
    return [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]


# ---------------------------------------------------------------------------
# 内部: 抽出本体
# ---------------------------------------------------------------------------
class _Collector:
    """1 回の走査で全 relation を抽出する."""

    def __init__(self, base: Path):
        self.base = base
        self.file_paths = []
        self.file_ids = []
        self.present = set()
        self.by_basename = {}
        self.agents = {}
        self.skills = {}
        self.packages = {}
        self.links = []
        self.skipped = []
        self._skipped_paths = set()
        self.agent_pattern = None
        self.skill_pattern = None

    # -- 走査 ------------------------------------------------------------
    def collect(self) -> None:
        self._walk()
        self._index()
        for path, node_id in zip(self.file_paths, self.file_ids):
            self._process(path, node_id)

    def _walk(self) -> None:
        base = self.base
        if not base.is_dir():
            return
        for dirpath, dirnames, filenames in os.walk(str(base)):
            dirnames[:] = sorted(
                name for name in dirnames if name not in _SKIP_DIR_NAMES
            )
            current = Path(dirpath)
            for name in sorted(filenames):
                path = current / name
                try:
                    node_id = path.relative_to(base).as_posix()
                except ValueError:
                    continue
                self.file_paths.append(path)
                self.file_ids.append(node_id)

    def _index(self) -> None:
        for path, node_id in zip(self.file_paths, self.file_ids):
            self.present.add(node_id)
            self.by_basename.setdefault(path.name, []).append(node_id)
            agent = _AGENT_ID_RE.fullmatch(node_id)
            if agent:
                self.agents.setdefault(agent.group(1), []).append(node_id)
            skill = _SKILL_ID_RE.fullmatch(node_id)
            if skill:
                self.skills.setdefault(skill.group(1), []).append(node_id)
            if path.name == "__init__.py":
                self.packages.setdefault(path.parent.name, []).append(path.parent)
        self.agent_pattern = _name_pattern(self.agents)
        self.skill_pattern = _name_pattern(self.skills, slash_prefix=True)

    def _process(self, path: Path, node_id: str) -> None:
        suffix = path.suffix.lower()
        is_settings = fnmatch.fnmatchcase(path.name, "settings*.json")
        if suffix not in (".md", ".py", ".sql") and not is_settings:
            return

        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            self._skip(node_id, type(exc).__name__)
            return

        if is_settings:
            self._from_settings(node_id, text)
        if suffix == ".md":
            self._from_markdown(node_id, text)
        elif suffix == ".py":
            self._from_python(path, node_id, text)
        elif suffix == ".sql":
            self._from_sql_text(node_id, text, _split_lines(text), 0)

    def _skip(self, node_id: str, reason: str) -> None:
        if node_id in self._skipped_paths:
            return
        self._skipped_paths.add(node_id)
        self.skipped.append(Skipped(path=node_id, reason=reason))

    # -- 辺の登録 --------------------------------------------------------
    def _emit(self, relation, source, line_number, line_text, reference):
        """パス参照 1 件を解決して辺にする（候補が複数なら 1 本ずつ出す）."""
        for target, resolution, exists in self._resolve(reference, source):
            self._add(relation, source, line_number, line_text, target, resolution, exists)

    def _add(self, relation, source, line_number, line_text, target, resolution, exists):
        if target == source:
            # 自己参照は関係として意味を持たない（自分の名前が本文に出るだけ）。
            return
        self.links.append(
            Link(
                relation=relation,
                source=source,
                source_line=max(1, int(line_number)),
                context=_sanitize_context(line_text),
                target=target,
                target_exists=bool(exists),
                resolution=resolution,
            )
        )

    def _emit_names(self, relation, source, line_number, line_text, node_ids):
        """名前解決で得た候補（実在するものだけ）を辺にする."""
        resolution = "exact" if len(node_ids) == 1 else "ambiguous"
        for target in node_ids:
            self._add(relation, source, line_number, line_text, target, resolution, True)

    # -- パス解決（契約 §3 の resolution 4 値）---------------------------
    def _resolve(self, reference: str, source: str):
        """参照文字列を (target, resolution, exists) の列へ解決する."""
        token = reference.strip().strip("`\"'")
        if not token:
            return []
        token = token.replace("\\", "/")

        source_dir = posixpath.dirname(source)
        if token.startswith("${CLAUDE_SKILL_DIR}/"):
            token = posixpath.join(source_dir, token[len("${CLAUDE_SKILL_DIR}/"):])
        elif token.startswith("${CLAUDE_PROJECT_DIR}/"):
            token = token[len("${CLAUDE_PROJECT_DIR}/"):]
        elif token.startswith("${"):
            # 解決できない変数を含む参照。どのファイルを指すか決められないので辺にしない。
            return []
        if "${" in token:
            return []

        if token.startswith("/") or re.match(r"^[A-Za-z]:", token):
            # ルート外の絶対パスはルート相対 ID で表現できない。
            return []

        if "/" in token:
            from_base = _normalize(token)
            from_here = _normalize(posixpath.join(source_dir, token))
            if from_base and from_base in self.present:
                return [(from_base, "exact", True)]
            if from_here and from_here in self.present:
                return [(from_here, "exact", True)]
            tail_hits = self._suffix_matches(from_base)
            if len(tail_hits) == 1:
                return [(tail_hits[0], "basename", True)]
            if tail_hits:
                return [(node_id, "ambiguous", True) for node_id in tail_hits]
            target = from_base or from_here
            if not target:
                return []
            return [(target, "missing", False)]

        return self._by_name(token, source_dir)

    def _suffix_matches(self, partial):
        """パス末尾が `/<partial>` に一致する実在ファイルを返す（契約 §3 解決の順序 3）.

        C3 の文書は `dev-workflow/SKILL.md` のような**部分パス**で参照を書くため、
        この段が無いと参照元の位置しだいで実在するものが `missing` になる。
        """
        if not partial:
            return []
        tail = "/" + partial
        return [
            node_id
            for node_id in self.by_basename.get(posixpath.basename(partial), ())
            if node_id.endswith(tail)
        ]

    def _by_name(self, token, source_dir):
        """ファイル名のみの参照を解決する（同ディレクトリ → 名前索引）."""
        sibling = _normalize(posixpath.join(source_dir, token))
        if sibling and sibling in self.present:
            return [(sibling, "exact", True)]
        candidates = self.by_basename.get(token)
        if not candidates:
            return [(token, "missing", False)]
        if len(candidates) == 1:
            return [(candidates[0], "basename", True)]
        return [(node_id, "ambiguous", True) for node_id in candidates]

    # -- settings*.json --------------------------------------------------
    def _from_settings(self, source: str, text: str) -> None:
        try:
            data = json.loads(text)
        except (ValueError, RecursionError) as exc:
            self._skip(source, type(exc).__name__)
            return
        if not isinstance(data, dict):
            return

        locator = _TextLocator(text)
        for key, relation in _SETTINGS_SECTIONS:
            if key not in data:
                continue
            for value in _iter_strings(data[key]):
                line_number = locator.line_of(value)
                if line_number is None:
                    continue
                line_text = locator.line_text(line_number)
                for match in _path_tokens(value):
                    self._emit(relation, source, line_number, line_text, match.group(0))

    # -- markdown --------------------------------------------------------
    def _from_markdown(self, source: str, text: str) -> None:
        lines = _split_lines(text)
        for offset, line in enumerate(lines):
            number = offset + 1
            self._md_code_spans(source, number, line)
            self._md_links(source, number, line)
            self._md_c3_run(source, number, line)
            self._md_table_row(source, number, line)
            self._md_subagent(source, number, line)
            self._md_bare_names(source, number, line)

    def _md_code_spans(self, source, number, line):
        for span in _CODE_SPAN_RE.finditer(line):
            for match in _path_tokens(span.group(1)):
                self._emit("md_code_span_path", source, number, line, match.group(0))

    def _md_links(self, source, number, line):
        for match in _MD_LINK_RE.finditer(line):
            target = match.group(1).split("#", 1)[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            resolved = self._resolve(target, source)
            if not resolved:
                continue
            if resolved[0][1] == "missing" and not _PATH_TOKEN_RE.fullmatch(target):
                # 実在せず拡張子も持たない（ディレクトリ・アンカー等）リンクは
                # どのファイルを指すか決められないので辺にしない。
                continue
            for node_id, resolution, exists in resolved:
                self._add(
                    "md_link", source, number, line, node_id, resolution, exists
                )

    def _md_c3_run(self, source, number, line):
        for match in _C3_RUN_RE.finditer(line):
            token = next(_path_tokens(match.group(1)), None)
            if token is None:
                continue
            self._emit("md_c3_run", source, number, line, token.group(0))

    def _md_table_row(self, source, number, line):
        stripped = line.strip()
        if not stripped.startswith("|"):
            return
        if set(stripped) <= _TABLE_RULE_CHARS:
            return
        for cell in stripped.strip("|").split("|"):
            name = cell.strip().strip("`*_ ").strip()
            if not _CELL_NAME_RE.fullmatch(name):
                continue
            node_ids = self.agents.get(name)
            if node_ids:
                self._emit_names(
                    "md_agent_variant_map", source, number, line, node_ids
                )

    def _md_subagent(self, source, number, line):
        for match in _SUBAGENT_RE.finditer(line):
            node_ids = self.agents.get(match.group(1))
            if node_ids:
                self._emit_names("md_subagent_type", source, number, line, node_ids)

    def _md_bare_names(self, source, number, line):
        if self.agent_pattern is not None:
            for match in self.agent_pattern.finditer(line):
                node_ids = self.agents.get(match.group(1))
                if node_ids:
                    self._emit_names(
                        "md_bare_agent_name", source, number, line, node_ids
                    )
        if self.skill_pattern is not None:
            for match in self.skill_pattern.finditer(line):
                node_ids = self.skills.get(match.group(1))
                if node_ids:
                    self._emit_names(
                        "md_bare_skill_name", source, number, line, node_ids
                    )

    # -- python ----------------------------------------------------------
    def _from_python(self, path: Path, source: str, text: str) -> None:
        lines = _split_lines(text)
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError) as exc:
            self._skip(source, type(exc).__name__)
            return

        uses_subprocess = _uses_subprocess(tree)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                self._py_import(path, source, lines, node)
            elif isinstance(node, ast.Call):
                self._py_dynamic_load(path, source, lines, node)
                if uses_subprocess:
                    self._py_script_path(source, lines, node)
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                if uses_subprocess:
                    self._py_script_path_operand(source, lines, node.right, node)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                self._py_sql(source, lines, node)

    def _py_import(self, path, source, lines, node):
        line_text = _line_at(lines, node.lineno)
        if isinstance(node, ast.Import):
            for alias in node.names:
                self._emit_modules(path, source, node.lineno, line_text, alias.name, 0)
            return
        level = node.level or 0
        if node.module:
            self._emit_modules(path, source, node.lineno, line_text, node.module, level)
        for alias in node.names:
            if alias.name == "*":
                continue
            dotted = f"{node.module}.{alias.name}" if node.module else alias.name
            self._emit_modules(path, source, node.lineno, line_text, dotted, level)

    def _emit_modules(self, path, source, number, line_text, dotted, level):
        node_ids = self._module_targets(path, dotted, level)
        for target in node_ids:
            self._add("py_import", source, number, line_text, target, "exact", True)

    def _module_targets(self, path: Path, dotted, level):
        """import 先をツリー内の実ファイルへ解決する（見つからなければ空）."""
        parts = [part for part in (dotted or "").split(".") if part]
        starts = []
        if level:
            base = path.parent
            for _ in range(level - 1):
                base = base.parent
            starts.append(base)
        elif parts:
            # 同ディレクトリの素のモジュール（hook 群は package を作らずに import する）
            starts.append(path.parent)
            for package in self.packages.get(parts[0], ()):
                starts.append(package.parent)
        else:
            return []

        found = []
        for start in starts:
            current = start
            ok = True
            for part in parts[:-1] if parts else []:
                current = current / part
                if not current.is_dir():
                    ok = False
                    break
            if not ok:
                continue
            if parts:
                options = (current / f"{parts[-1]}.py", current / parts[-1] / "__init__.py")
            else:
                options = (current / "__init__.py",)
            for option in options:
                node_id = self._node_id_of(option)
                if node_id is not None and node_id not in found:
                    found.append(node_id)
        return found

    def _node_id_of(self, path: Path):
        try:
            node_id = path.resolve().relative_to(self.base).as_posix()
        except (ValueError, OSError):
            return None
        return node_id if node_id in self.present else None

    def _py_dynamic_load(self, path, source, lines, node):
        name = _call_name(node)
        if name is None or not _DYNAMIC_LOAD_RE.search(name):
            return
        if not node.args:
            return
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            return
        for target in self._module_targets(path, first.value, 0):
            self._add(
                "py_importlib",
                source,
                node.lineno,
                _line_at(lines, node.lineno),
                target,
                "exact",
                True,
            )

    def _py_script_path(self, source, lines, node):
        """`os.path.join(DIR, "x.py")` のように組み立てたスクリプトパスを拾う."""
        if _call_name(node) != "join":
            return
        for arg in node.args:
            self._py_script_path_operand(source, lines, arg, node)

    def _py_script_path_operand(self, source, lines, operand, node):
        if not (isinstance(operand, ast.Constant) and isinstance(operand.value, str)):
            return
        if not operand.value.lower().endswith(".py"):
            return
        self._emit(
            "py_subprocess_path",
            source,
            node.lineno,
            _line_at(lines, node.lineno),
            operand.value,
        )

    def _py_sql(self, source, lines, node):
        value = node.value
        if not _SQL_VERB_RE.search(value):
            return
        self._from_sql_text(source, value, lines, node.lineno - 1)

    def _from_sql_text(self, source, text, lines, line_offset):
        """SQL 文字列からテーブル参照を取り出す（`.sql` ファイルにも使う）."""
        for match in _SQL_TABLE_RE.finditer(text):
            name = match.group(1) or match.group(2)
            if not name or name.lower() in _SQL_STOP_WORDS:
                continue
            number = line_offset + text.count("\n", 0, match.start()) + 1
            self._add(
                "py_sql_table",
                source,
                number,
                _line_at(lines, number),
                f"sqltable:{name}",
                "exact",
                True,
            )

    # -- 出力 ------------------------------------------------------------
    def result(self) -> Graph:
        links = _dedupe(self.links)
        nodes = {}
        for node_id in self.file_ids:
            nodes[node_id] = Node(id=node_id, kind="file", exists=True)
        for link in links:
            for node_id, exists in ((link.source, True), (link.target, link.target_exists)):
                if node_id in nodes:
                    continue
                if node_id.startswith("sqltable:"):
                    # テーブルの実在はツリーから確かめられないため常に true とする。
                    nodes[node_id] = Node(id=node_id, kind="table", exists=True)
                else:
                    nodes[node_id] = Node(id=node_id, kind="file", exists=exists)
        return Graph(
            root=self.base,
            file_count=len(self.file_ids),
            nodes=[nodes[key] for key in sorted(nodes)],
            links=links,
            skipped=list(self.skipped),
        )


# ---------------------------------------------------------------------------
# 内部: 小さな道具
# ---------------------------------------------------------------------------
def _normalize(token: str):
    """ルート相対 POSIX へ正規化する。ルート外へ出るものは None."""
    normalized = posixpath.normpath(token)
    if normalized in (".", ""):
        return None
    if normalized.startswith("/") or normalized == ".." or normalized.startswith("../"):
        return None
    return normalized


def _iter_strings(value):
    """入れ子の dict / list から文字列だけを順に取り出す."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item)


def _name_pattern(names, slash_prefix: bool = False):
    """実ツリーにある名前だけから素の名前用の正規表現を作る（一覧を焼き付けない）."""
    if not names:
        return None
    ordered = sorted(names, key=lambda name: (-len(name), name))
    body = "|".join(re.escape(name) for name in ordered)  # nul-boundary: allow(正規表現の選択肢の組み立て。区切りは正規表現の文法で固定されており、機械可読な行集合ではない)
    prefix = "/?" if slash_prefix else ""
    return re.compile(r"(?<![\w-])" + prefix + r"(" + body + r")(?![\w-])")


def _line_at(lines, number: int) -> str:
    if 1 <= number <= len(lines):
        return lines[number - 1]
    return ""


def _call_name(node: ast.Call):
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _uses_subprocess(tree: ast.AST) -> bool:
    """このファイルが子プロセスを起動しうるか（`py_subprocess_path` の絞り込み）."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] == "subprocess" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "subprocess":
                return True
        elif isinstance(node, ast.Name) and node.id in ("Popen", "subprocess"):
            return True
        elif isinstance(node, ast.Attribute) and node.attr in ("Popen", "check_output"):
            return True
    return False


def _dedupe(links):
    """同じ (relation, source, target) は最初の 1 本だけ残す.

    同じ名前が 1 ファイル内で何十回も出るため、そのまま出すと出所が増えるだけで
    関係の種類は増えない。最初に現れた行を代表として残す。
    """
    seen = set()
    out = []
    for link in links:
        key = (link.relation, link.source, link.target)
        if key in seen:
            continue
        seen.add(key)
        out.append(link)
    out.sort(key=lambda link: (link.source, link.source_line, link.relation, link.target))
    return out
