"""Reference graph extractor for reachability analysis.

Extract edges (function calls, imports, configurations) from C3 codebase
and compute reachability from defined entry points.
"""

from __future__ import annotations

import json
import re
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path as StdPath
from typing import Dict, List, Set, Tuple

# Edge / Path are defined here and nowhere else. This module is shipped in the
# wheel while tests/ is not, so it must never import from tests.
@dataclass(frozen=True)
class Edge:
    """Edge in the reference graph."""

    kind: str
    source_file: str
    source_line: int
    target_node_id: str


@dataclass(frozen=True)
class Path:
    """Path from an entry point to a node."""

    edges: tuple[Edge, ...]


class Graph:
    """Reference graph for reachability analysis."""

    def __init__(self, edges: List[Edge], root: StdPath, unreadable: tuple = ()):
        self.edges = edges
        self.root = root
        self.unreadable = unreadable  # tuple of (file_path, exception_type)
        self._build_adjacency()

    def _build_adjacency(self):
        """Build adjacency list from edges."""
        self.adjacency: Dict[str, List[Edge]] = {}
        for edge in self.edges:
            if edge.source_file not in self.adjacency:
                self.adjacency[edge.source_file] = []
            self.adjacency[edge.source_file].append(edge)

    def is_reachable(self, node_id: str) -> bool:
        """Check if node is reachable from entry points."""
        return len(self.paths_to(node_id)) > 0

    def paths_to(self, node_id: str) -> List[Path]:
        """Find all paths from entry points to node_id."""
        entry_points = _get_entry_points(self.root)
        paths = []

        for entry in entry_points:
            found_paths = self._bfs_paths(entry, node_id)
            paths.extend(found_paths)

        return paths

    def _bfs_paths(self, start: str, target: str) -> List[Path]:
        """Find all paths from start to target using BFS."""
        if start == target:
            return [Path(edges=())]

        visited_nodes = {start}  # Prevent cycles globally
        paths_to = {start: [[]]}  # node -> list of paths to that node
        queue = deque([start])
        max_iterations = 10000  # Prevent infinite loops

        iterations = 0
        while queue and iterations < max_iterations:
            iterations += 1
            current = queue.popleft()

            if current not in self.adjacency:
                continue

            for edge in self.adjacency[current]:
                target_node = edge.target_node_id

                if target_node == target:
                    # Found paths
                    if target not in paths_to:
                        paths_to[target] = []
                    for parent_path in paths_to[current]:
                        paths_to[target].append(parent_path + [edge])
                elif target_node not in visited_nodes:
                    visited_nodes.add(target_node)
                    paths_to[target_node] = []
                    for parent_path in paths_to[current]:
                        paths_to[target_node].append(parent_path + [edge])
                    queue.append(target_node)

        if target in paths_to:
            return [Path(edges=tuple(path)) for path in paths_to[target]]
        return []


def build_graph(root: StdPath) -> Graph:
    """Build reference graph from codebase."""
    root = StdPath(root).resolve()
    edges = []
    unreadable = []

    # Collect all Python and Markdown files once (avoid repeated rglob calls)
    # Skip tests/ directory as per spec §3
    py_files = []
    md_files = []

    for file_path in root.rglob("*"):
        # Skip tests/ directory (not a root per spec §3)
        if "tests" in file_path.parts:
            continue
        # Skip generated reports (not source per spec §7)
        if ".claude" in file_path.parts and "reports" in file_path.parts:
            continue
        if ".git" in file_path.parts or "dist" in file_path.parts or "__pycache__" in file_path.parts:
            continue

        if file_path.is_file():
            if file_path.suffix == ".py":
                py_files.append(file_path)
            elif file_path.suffix == ".md":
                md_files.append(file_path)

    # Extract all edge types
    edges.extend(_extract_settings_hooks(root))
    edges.extend(_extract_c3_run(root, md_files, unreadable))
    edges.extend(_extract_code_span_paths(root, md_files, unreadable))
    edges.extend(_extract_agent_variant_maps(root, md_files, unreadable))
    edges.extend(_extract_py_imports(root, py_files, unreadable))
    edges.extend(_extract_py_importlib(root, py_files, unreadable))
    edges.extend(_extract_py_subprocess_paths(root, py_files, unreadable))
    edges.extend(_extract_subagent_types(root, md_files, unreadable))
    edges.extend(_extract_sql_tables(root, py_files, unreadable))

    # The same file is walked by several extractors, so a single unreadable file
    # would otherwise be reported once per extractor. Deduplicate while keeping
    # the order of first observation.
    return Graph(edges, root, tuple(dict.fromkeys(unreadable)))


def _get_entry_points(root: StdPath) -> List[str]:
    """Get list of entry point file paths (root relative, POSIX format)."""
    entry_points = []

    # settings.json / settings.local.json
    for fname in [".claude/settings.json", ".claude/settings.local.json"]:
        fpath = root / fname
        if fpath.exists():
            entry_points.append(fname)

    # .claude/skills/*/SKILL.md
    skills_dir = root / ".claude" / "skills"
    if skills_dir.exists():
        for skill_dir in skills_dir.iterdir():
            if skill_dir.is_dir():
                skill_md = skill_dir / "SKILL.md"
                if skill_md.exists():
                    rel_path = skill_md.relative_to(root)
                    entry_points.append(str(rel_path).replace("\\", "/"))

    # CLAUDE.md / .claude/CLAUDE.md / .claude/rules/**
    for fname in ["CLAUDE.md", ".claude/CLAUDE.md"]:
        fpath = root / fname
        if fpath.exists():
            entry_points.append(fname)

    rules_dir = root / ".claude" / "rules"
    if rules_dir.exists():
        for rule_file in rules_dir.rglob("*"):
            if rule_file.is_file():
                rel_path = rule_file.relative_to(root)
                entry_points.append(str(rel_path).replace("\\", "/"))

    # src/c3/cli.py
    cli_py = root / "src" / "c3" / "cli.py"
    if cli_py.exists():
        entry_points.append("src/c3/cli.py")

    return entry_points


def _extract_settings_hooks(root: StdPath) -> List[Edge]:
    """Extract edges from settings.json hooks and statusLine."""
    edges = []

    for settings_fname in [".claude/settings.json", ".claude/settings.local.json"]:
        settings_file = root / settings_fname
        if not settings_file.exists():
            continue

        try:
            data = json.loads(settings_file.read_text(encoding="utf-8"))
        except Exception:
            continue

        # Process hooks section
        hooks_section = data.get("hooks", {})
        line_num = 1

        if isinstance(hooks_section, dict):
            for matcher_key, matcher_config in hooks_section.items():
                if not isinstance(matcher_config, list):
                    continue

                for config_item in matcher_config:
                    if not isinstance(config_item, dict):
                        continue

                    hooks_list = config_item.get("hooks", [])
                    if not isinstance(hooks_list, list):
                        continue

                    for hook in hooks_list:
                        if isinstance(hook, dict) and hook.get("type") == "command":
                            args = hook.get("args", [])
                            if isinstance(args, list) and len(args) > 0:
                                for arg in args:
                                    if isinstance(arg, str):
                                        target_node_id = _resolve_claude_project_dir(arg, root)
                                        if target_node_id:
                                            edges.append(Edge(
                                                kind="settings_hook",
                                                source_file=settings_fname,
                                                source_line=1,  # JSON line numbers are not easily available
                                                target_node_id=target_node_id,
                                            ))

        # Process statusLine section
        status_line_section = data.get("statusLine", {})
        if isinstance(status_line_section, dict):
            for config_key, config_value in status_line_section.items():
                if isinstance(config_value, dict):
                    args = config_value.get("args", [])
                    if isinstance(args, list):
                        for arg in args:
                            if isinstance(arg, str):
                                target_node_id = _resolve_claude_project_dir(arg, root)
                                if target_node_id:
                                    edges.append(Edge(
                                        kind="settings_hook",
                                        source_file=settings_fname,
                                        source_line=1,
                                        target_node_id=target_node_id,
                                    ))

    return edges


def _resolve_claude_project_dir(path_str: str, root: StdPath) -> str | None:
    """Resolve ${CLAUDE_PROJECT_DIR} placeholder in path and return node ID."""
    if not isinstance(path_str, str):
        return None

    # Remove placeholder and strip quotes
    resolved = path_str.replace("${CLAUDE_PROJECT_DIR}", "").strip()
    if not resolved:
        return None

    # Remove leading/trailing quotes if present
    if resolved.startswith('"') or resolved.startswith("'"):
        resolved = resolved[1:]
    if resolved.endswith('"') or resolved.endswith("'"):
        resolved = resolved[:-1]

    # Normalize path separators
    resolved = resolved.replace("\\", "/")

    # Remove leading slash
    if resolved.startswith("/"):
        resolved = resolved[1:]

    if not resolved:
        return None

    # Check if file exists and return relative path
    target_path = root / resolved
    if target_path.exists():
        rel_path = target_path.relative_to(root)
        return str(rel_path).replace("\\", "/")

    return None


def _extract_c3_run(root: StdPath, md_files: List[StdPath], unreadable: list) -> List[Edge]:
    """Extract edges from 'c3 run <path>' in markdown files."""
    edges = []
    # Match: c3 run <path> where path may be quoted
    pattern = re.compile(r'c3\s+run\s+(?:"([^"]+)"|\'([^\']+)\'|([^\s\)\]`\n]+))')

    for md_file in md_files:
        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception as e:
            unreadable.append(
                (str(md_file.relative_to(root)).replace("\\", "/"), type(e).__name__)
            )
            continue

        # Find all 'c3 run <path>' patterns
        for match in pattern.finditer(content):
            # Get the path from one of the three capture groups
            path_str = match.group(1) or match.group(2) or match.group(3)
            if not path_str:
                continue
            path_str = path_str.strip()

            # Resolve placeholders
            if path_str.startswith("${CLAUDE_PROJECT_DIR}"):
                path_str = path_str.replace("${CLAUDE_PROJECT_DIR}", "").lstrip("/")
                target_path = root / path_str
            elif path_str.startswith("${CLAUDE_SKILL_DIR}"):
                # Resolve relative to the SKILL.md's directory
                skill_dir = md_file.parent
                path_str = path_str.replace("${CLAUDE_SKILL_DIR}", "").lstrip("/")
                target_path = skill_dir / path_str
            else:
                # Try to resolve as absolute path
                path_str = path_str.replace("\\", "/")
                target_path = root / path_str

            # Normalize path
            path_str = path_str.replace("\\", "/")

            # Check if it resolves to an existing file
            if target_path.exists():
                try:
                    rel_target = target_path.relative_to(root)
                    target_node_id = str(rel_target).replace("\\", "/")

                    # Find line number
                    line_num = content[:match.start()].count('\n') + 1

                    source_rel = md_file.relative_to(root)
                    source_file = str(source_rel).replace("\\", "/")

                    edges.append(Edge(
                        kind="c3_run",
                        source_file=source_file,
                        source_line=line_num,
                        target_node_id=target_node_id,
                    ))
                except ValueError:
                    # Path is outside root, skip
                    pass

    return edges


def _extract_code_span_paths(root: StdPath, md_files: List[StdPath], unreadable: list) -> List[Edge]:
    """Extract edges from bare paths in code spans in markdown."""
    edges = []
    pattern = re.compile(r'`([^\s`]+(?:\.py|\.md|\.json|\.txt)?)`')

    for md_file in md_files:
        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception as e:
            unreadable.append(
                (str(md_file.relative_to(root)).replace("\\", "/"), type(e).__name__)
            )
            continue

        source_rel = md_file.relative_to(root)
        source_file = str(source_rel).replace("\\", "/")

        # Find all code spans (backtick-delimited)
        for match in pattern.finditer(content):
            path_str = match.group(1).strip()

            # Skip if it looks like inline code (e.g., function names, variable names without path separators)
            if "/" not in path_str and "." not in path_str:
                continue

            # Resolve path
            resolved_path = _resolve_code_span_path(root, md_file, path_str)
            if resolved_path:
                line_num = content[:match.start()].count('\n') + 1
                edges.append(Edge(
                    kind="code_span_path",
                    source_file=source_file,
                    source_line=line_num,
                    target_node_id=str(resolved_path).replace("\\", "/"),
                ))

    return edges


def _resolve_code_span_path(root: StdPath, source_md: StdPath, path_str: str) -> str | None:
    """Resolve code span path to absolute form."""
    # Normalize path separators
    path_str = path_str.replace("\\", "/")

    # Handle ${CLAUDE_SKILL_DIR} placeholder
    if path_str.startswith("${CLAUDE_SKILL_DIR}"):
        # Resolve relative to the SKILL.md's directory
        skill_dir = source_md.parent
        relative_part = path_str.replace("${CLAUDE_SKILL_DIR}", "").lstrip("/")
        target = skill_dir / relative_part
        if target.exists():
            try:
                return str(target.relative_to(root)).replace("\\", "/")
            except ValueError:
                pass
        return None

    # Handle .claude/ absolute paths
    if path_str.startswith(".claude/"):
        target = root / path_str
        if target.exists():
            try:
                return str(target.relative_to(root)).replace("\\", "/")
            except ValueError:
                pass
        return None

    # Handle src/ absolute paths
    if path_str.startswith("src/"):
        target = root / path_str
        if target.exists():
            try:
                return str(target.relative_to(root)).replace("\\", "/")
            except ValueError:
                pass
        return None

    # Handle paths relative to skill directory (e.g., scripts/xxx.py in SKILL.md)
    if "SKILL.md" in str(source_md):
        skill_dir = source_md.parent
        target = skill_dir / path_str
        if target.exists():
            try:
                return str(target.relative_to(root)).replace("\\", "/")
            except ValueError:
                pass

    # Resolve relative to source file directory
    source_dir = source_md.parent
    target = source_dir / path_str
    try:
        target = target.resolve()
        if target.exists():
            # Check if target is within root
            try:
                return str(target.relative_to(root)).replace("\\", "/")
            except ValueError:
                pass
    except (OSError, ValueError):
        # Can occur if path is invalid
        pass

    return None


def _extract_agent_variant_maps(root: StdPath, md_files: List[StdPath], unreadable: list) -> List[Edge]:
    """Extract edges from agent variant mapping tables in SKILL.md files."""
    edges = []
    backtick_pattern = re.compile(r'`([^`]+)`')

    for md_file in md_files:
        if not md_file.name == "SKILL.md":
            continue

        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception as e:
            unreadable.append(
                (str(md_file.relative_to(root)).replace("\\", "/"), type(e).__name__)
            )
            continue

        source_rel = md_file.relative_to(root)
        source_file = str(source_rel).replace("\\", "/")

        # Find all markdown tables with | delimiters
        # Look for rows that map agent names to worktree variants
        lines = content.split('\n')

        for line_num, line in enumerate(lines, start=1):
            # Skip header and separator rows
            if not line.strip().startswith('|') or '---' in line:
                continue

            # Parse table row
            cells = [cell.strip() for cell in line.split('|')[1:-1]]  # Remove first/last empty cells

            if len(cells) < 2:
                continue

            # Look for pattern: | <name> | <wt_name> | ...
            # wt_name starts with "wt_" (may be in backticks)
            for i, cell in enumerate(cells):
                # Extract agent name from backticks if present
                agent_match = backtick_pattern.search(cell)
                if agent_match:
                    agent_name = agent_match.group(1)
                    # Check if this agent name is a worktree variant
                    if agent_name.startswith("wt_"):
                        agent_file = f".claude/agents/{agent_name}.md"

                        edges.append(Edge(
                            kind="agent_variant_map",
                            source_file=source_file,
                            source_line=line_num,
                            target_node_id=agent_file,
                        ))

    return edges


def _extract_py_imports(root: StdPath, py_files: List[StdPath], unreadable: list) -> List[Edge]:
    """Extract edges from Python import statements."""
    edges = []
    import_pattern = re.compile(r'\s*(?:from\s+(\S+)\s+import|import\s+(\S+))')

    for py_file in py_files:
        try:
            content = py_file.read_text(encoding="utf-8")
        except Exception as e:
            unreadable.append(
                (str(py_file.relative_to(root)).replace("\\", "/"), type(e).__name__)
            )
            continue

        source_rel = py_file.relative_to(root)
        source_file = str(source_rel).replace("\\", "/")

        # Find import statements
        lines = content.split('\n')
        for line_num, line in enumerate(lines, start=1):
            # Skip comments
            if line.strip().startswith('#'):
                continue

            # Match: import X or from X import ...
            import_match = import_pattern.match(line)
            if import_match:
                module_name = import_match.group(1) or import_match.group(2)

                # Get the first component of the module name
                module_base = module_name.split('.')[0]

                # Look for same-directory module
                sibling_py = py_file.parent / f"{module_base}.py"
                if sibling_py.exists() and sibling_py != py_file:
                    target_rel = sibling_py.relative_to(root)
                    target_node_id = str(target_rel).replace("\\", "/")

                    edges.append(Edge(
                        kind="py_import",
                        source_file=source_file,
                        source_line=line_num,
                        target_node_id=target_node_id,
                    ))

    return edges


def _extract_py_importlib(root: StdPath, py_files: List[StdPath], unreadable: list) -> List[Edge]:
    """Extract edges from _load_module() calls in Python."""
    edges = []
    load_module_pattern = re.compile(r'_load_module\s*\(\s*["\']([^"\']+)["\']\s*\)')

    for py_file in py_files:
        try:
            content = py_file.read_text(encoding="utf-8")
        except Exception as e:
            unreadable.append(
                (str(py_file.relative_to(root)).replace("\\", "/"), type(e).__name__)
            )
            continue

        source_rel = py_file.relative_to(root)
        source_file = str(source_rel).replace("\\", "/")

        # Find _load_module("name") calls
        lines = content.split('\n')
        for line_num, line in enumerate(lines, start=1):
            for match in load_module_pattern.finditer(line):
                module_name = match.group(1)

                # Look for same-directory module
                sibling_py = py_file.parent / f"{module_name}.py"
                if sibling_py.exists():
                    target_rel = sibling_py.relative_to(root)
                    target_node_id = str(target_rel).replace("\\", "/")

                    edges.append(Edge(
                        kind="py_importlib",
                        source_file=source_file,
                        source_line=line_num,
                        target_node_id=target_node_id,
                    ))

    return edges


def _extract_py_subprocess_paths(root: StdPath, py_files: List[StdPath], unreadable: list) -> List[Edge]:
    """Extract edges from subprocess calls with os.path.join patterns."""
    edges = []
    join_pattern = re.compile(r'os\.path\.join\s*\(\s*([A-Z_]+)\s*,\s*["\']([^"\']+\.py)["\']\s*\)')

    for py_file in py_files:
        try:
            content = py_file.read_text(encoding="utf-8")
        except Exception as e:
            unreadable.append(
                (str(py_file.relative_to(root)).replace("\\", "/"), type(e).__name__)
            )
            continue

        source_rel = py_file.relative_to(root)
        source_file = str(source_rel).replace("\\", "/")

        # Find os.path.join patterns with subprocess calls nearby
        lines = content.split('\n')
        for line_num, line in enumerate(lines, start=1):
            for match in join_pattern.finditer(line):
                # Check if this is inside a subprocess or similar context
                # Look at surrounding lines for subprocess keyword
                context_start = max(0, line_num - 5)
                context_end = min(len(lines), line_num + 5)
                window = lines[context_start:context_end]

                if any('subprocess' in l or 'Popen' in l or 'run(' in l for l in window):
                    dir_const = match.group(1)
                    filename = match.group(2)

                    # Try to infer the directory from the constant name
                    dir_path = _infer_dir_from_constant(py_file, dir_const)

                    if dir_path:
                        target = dir_path / filename
                        if target.exists():
                            target_rel = target.relative_to(root)
                            target_node_id = str(target_rel).replace("\\", "/")

                            edges.append(Edge(
                                kind="py_subprocess_path",
                                source_file=source_file,
                                source_line=line_num,
                                target_node_id=target_node_id,
                            ))

    return edges


def _infer_dir_from_constant(py_file: StdPath, const_name: str) -> StdPath | None:
    """Infer directory path from a constant name in a Python file."""
    try:
        content = py_file.read_text(encoding="utf-8")
    except Exception:
        return None

    # Look for assignment like: CONST_NAME = Path(...) / __file__.parent / etc.
    pattern = fr'{const_name}\s*=\s*([^\n]+)'
    match = re.search(pattern, content)

    if not match:
        return None

    assignment = match.group(1)

    # Handle __file__.parent pattern
    if "__file__" in assignment and "parent" in assignment:
        return py_file.parent

    # Handle hardcoded paths
    if ".parent" in assignment:
        # Could be StdPath(...).parent or similar
        return py_file.parent

    return None


def _extract_subagent_types(root: StdPath, md_files: List[StdPath], unreadable: list) -> List[Edge]:
    """Extract edges from subagent_type and agent fields in markdown."""
    edges = []
    field_pattern = re.compile(r'\s*(subagent_type|agent)\s*:\s*(.+)')

    for md_file in md_files:
        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception as e:
            unreadable.append(
                (str(md_file.relative_to(root)).replace("\\", "/"), type(e).__name__)
            )
            continue

        source_rel = md_file.relative_to(root)
        source_file = str(source_rel).replace("\\", "/")

        # Find frontmatter and fields
        lines = content.split('\n')
        in_frontmatter = False

        for line_num, line in enumerate(lines, start=1):
            # Detect frontmatter boundaries
            if line.strip() == '---':
                in_frontmatter = not in_frontmatter
                continue

            if in_frontmatter:
                # Match: subagent_type: <name> or agent: <name>
                match = field_pattern.match(line)
                if match:
                    field_name = match.group(1)
                    field_value = match.group(2).strip()

                    # Remove quotes if present
                    if field_value.startswith('"') or field_value.startswith("'"):
                        field_value = field_value[1:-1]

                    # Build agent file path
                    agent_file = f".claude/agents/{field_value}.md"

                    edges.append(Edge(
                        kind="subagent_type",
                        source_file=source_file,
                        source_line=line_num,
                        target_node_id=agent_file,
                    ))

    return edges


def _extract_sql_tables(root: StdPath, py_files: List[StdPath], unreadable: list) -> List[Edge]:
    """Extract edges from SQL table references in Python and SQL files."""
    edges = []
    patterns = [
        re.compile(r'\b(?:FROM|INTO|UPDATE|DELETE FROM)\s+([a-zA-Z_][a-zA-Z0-9_]*)', re.IGNORECASE),
        re.compile(r'\b(?:CREATE\s+TABLE|DROP\s+TABLE)\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?([a-zA-Z_][a-zA-Z0-9_]*)', re.IGNORECASE),
    ]
    sql_keywords = {'SELECT', 'WHERE', 'ORDER', 'GROUP', 'HAVING', 'LIMIT', 'OFFSET'}

    for py_file in py_files:
        try:
            content = py_file.read_text(encoding="utf-8")
        except Exception as e:
            unreadable.append(
                (str(py_file.relative_to(root)).replace("\\", "/"), type(e).__name__)
            )
            continue

        source_rel = py_file.relative_to(root)
        source_file = str(source_rel).replace("\\", "/")

        # Find SQL table references in strings
        lines = content.split('\n')
        for line_num, line in enumerate(lines, start=1):
            # Skip comments
            if line.strip().startswith('#'):
                continue

            for pattern in patterns:
                for match in pattern.finditer(line):
                    table_name = match.group(1)

                    # Filter out SQL keywords that might be matched
                    if table_name.upper() in sql_keywords:
                        continue

                    target_node_id = f"sqltable:{table_name}"

                    edges.append(Edge(
                        kind="sql_table",
                        source_file=source_file,
                        source_line=line_num,
                        target_node_id=target_node_id,
                    ))

    return edges
