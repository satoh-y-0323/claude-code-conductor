#!/usr/bin/env python3
"""UserPromptSubmit hook: inject semantically-similar past context (α design).

The hook runs ``c3 recall search`` against the recall index (numpy cosine search) and
returns the top hits as ``additionalContext`` for the parent Claude to
consider. The preface explicitly asks Claude to evaluate the relevance
of each hit and ignore unrelated ones — i.e. *AI judges, hook does not
filter aggressively*.

Skip conditions (all silent no-ops, exit 0):
- ``C3_RECALL_HOOK_DISABLE=1`` env var set
- Prompt shorter than :data:`_MIN_PROMPT_CHARS` (default 15)
- Prompt starts with ``/`` (slash command) or ``@`` (file mention)
- No ``.claude/state/recall_meta.json`` / ``recall.hnsw`` (index not built)
- ``c3.cli`` subprocess fails or times out
- Zero hits above ``--min-score``

Output protocol:
    ``{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
        "additionalContext": "..."}}``

Performance: each invocation runs a fresh Python subprocess that loads
fastembed + onnxruntime + the MiniLM model. Cold-start is ~2-3 seconds,
warm cache ~1-2 seconds. Users can disable this hook entirely by setting
``C3_RECALL_HOOK_DISABLE=1`` in the shell or in ``.env``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

try:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


# Minimum prompt length (chars) to bother running recall. Short messages
# like "yes" / "ok" / "go" rarely benefit from semantic recall and the
# subprocess overhead is wasteful.
_MIN_PROMPT_CHARS = 15

# SR-L-1: cap the prompt passed to the subprocess to avoid passing huge
# context windows through command-line arguments (OS arg-length limits and
# unnecessary embedding overhead).
_MAX_PROMPT_CHARS = 2000

_TOP_K = 3

# Slightly stricter than the CLI default (0.3) because the parent Claude
# pays a context cost for every injected line; surfacing weak matches
# isn't worth the noise.
_MIN_SCORE = 0.4

# Generous timeout to accommodate the fastembed cold-start the first
# time a Claude session warms up the cache.
_TIMEOUT_SEC = 8

_DISABLE_ENV_VAR = "C3_RECALL_HOOK_DISABLE"


# SR-M-1: strip control characters (except \t) and newlines from fields
# that are embedded inline into the additionalContext string.  Unescaped
# newlines in path / chunk_label would allow a malicious file path or
# heading to inject extra lines (including header-like strings) into the
# context block seen by the parent LLM.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _sanitize_field(s: str) -> str:
    """Remove newlines and non-printable control chars from inline fields.

    Any text following the first newline is stripped entirely (not just the
    newline character) so that a malicious path like
    ``normal/path\\nX-Injected: evil-header`` cannot smuggle arbitrary text
    into the output after the newline is collapsed.
    """
    if not s:
        return ""
    # Take only text before the first newline.
    first_line = s.split("\n")[0].split("\r")[0]
    # Strip remaining control chars (but preserve tab for readability).
    return _CONTROL_RE.sub("", first_line)


def should_skip_prompt(prompt: str) -> bool:
    """Pure-logic check used by main() and the unit tests."""
    if not prompt:
        return True
    stripped = prompt.strip()
    if len(stripped) < _MIN_PROMPT_CHARS:
        return True
    if stripped.startswith("/"):
        return True
    if stripped.startswith("@"):
        return True
    return False


def format_additional_context(hits: Iterable[dict], *, stale: bool = False) -> str:
    """Render hits into the ``additionalContext`` block.

    The preface is the key design lever for α: it tells the parent Claude
    that these are *candidates*, not *facts*, and asks it to discard
    unrelated entries.

    When ``stale`` is True, a directive is prepended that asks the parent
    Claude to surface ``AskUserQuestion`` so the user can choose whether
    to ``c3 recall rebuild``. The hook itself cannot invoke
    ``AskUserQuestion`` — only the parent LLM can — so we route the
    decision through context injection.
    """
    lines: list[str] = []
    if stale:
        lines.extend(
            [
                "[recall] ⚠️ インデックスが古い可能性があります（最新ソースの mtime > index mtime）。",
                "次に進む前に、AskUserQuestion で以下の 3 択をユーザーに提示してください:",
                "  - 今すぐ rebuild する → Bash で `c3 recall rebuild` を実行（約 1〜2 分）",
                "  - 後で / 今は不要 → 検索結果は古いままという前提で続行",
                "  - 無視 → このまま現タスクを進める",
                "ユーザーが同一セッション中にすでに「後で」「無視」と答えた場合は再度尋ねないでください。",
                "",
            ]
        )
    lines.append(
        "[recall] 過去の類似情報の検索結果（参考のみ。**現タスクと無関係なら無視**してください）:"
    )
    for i, hit in enumerate(hits, start=1):
        score = hit.get("score")
        # SR-M-1: sanitize inline fields to prevent newline / control-char
        # injection from malicious file paths or headings.
        path = _sanitize_field(hit.get("path") or "")
        label = _sanitize_field(hit.get("chunk_label") or "")
        snippet = (hit.get("snippet") or "").strip().replace("\n", " ")
        if len(snippet) > 220:
            snippet = snippet[:220] + "..."
        score_str = f"{score:.2f}" if isinstance(score, (int, float)) else str(score)
        lines.append(f"  [{i}] score={score_str}  {path}  :: {label}")
        if snippet:
            lines.append(f"      {snippet}")
    return "\n".join(lines)  # nul-boundary: allow(LLM コンテキストへ注入する人間可読テキスト。表示専用で再パースしない)


def find_repo_root() -> Path | None:
    """Return the nearest ancestor containing ``.claude/`` (or None)."""
    here = Path(os.getenv("CLAUDE_PROJECT_DIR") or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / ".claude").is_dir():
            return candidate
    return None


def index_exists(repo_root: Path) -> bool:
    meta = repo_root / ".claude" / "state" / "recall_meta.json"
    index = repo_root / ".claude" / "state" / "recall.hnsw"
    return meta.exists() and index.exists()


# Source directories scanned to decide if the recall index is older than
# at least one of its inputs. Mirrors :mod:`c3.recall_index.collect_sources`
# but kept local to the hook so it can run without importing the c3
# package (the hook may execute in environments where ``c3`` is not yet
# importable, e.g. immediately after ``c3 init``).
# CR-L-02: Keep in sync with c3.recall_index.collect_sources when adding
# or removing source kinds.
_STALE_SOURCE_GLOBS = (
    (Path(".claude") / "memory" / "sessions", "*.tmp"),
    (Path(".claude") / "agent-memory", "*.md"),
    (Path(".claude") / "reports" / "archive", "*.md"),
)
_STALE_PATTERNS_JSON = Path(".claude") / "memory" / "patterns.json"


def index_is_stale(repo_root: Path) -> bool:
    """Return True if any recall source is newer than the index file."""
    index_path = repo_root / ".claude" / "state" / "recall.hnsw"
    if not index_path.exists():
        return False
    index_mtime = index_path.stat().st_mtime
    for rel_dir, pattern in _STALE_SOURCE_GLOBS:
        absolute = repo_root / rel_dir
        if not absolute.is_dir():
            continue
        for path in absolute.rglob(pattern):
            # Cycle2-L-1 [SR-V-002]: skip symlinks to avoid reading mtime of
            # files outside the C3 source tree (matches the analogous guard in
            # c3.recall_index._collect_markdown_glob).
            if not path.is_file() or path.name == ".gitkeep" or path.is_symlink():
                continue
            try:
                if path.stat().st_mtime > index_mtime:
                    return True
            except OSError:
                continue
    patterns_path = repo_root / _STALE_PATTERNS_JSON
    if patterns_path.is_file():
        try:
            if patterns_path.stat().st_mtime > index_mtime:
                return True
        except OSError:
            pass
    return False


def run_recall(prompt: str, repo_root: Path) -> list[dict]:
    """Invoke ``python -m c3.cli recall search`` and return ``hits`` list.

    Any error path (subprocess failure, timeout, malformed JSON) returns
    an empty list so the hook stays silent rather than surfacing errors
    to the user mid-prompt.
    """
    # SR-L-1: truncate prompt to avoid OS arg-length limits and pass only
    # the most relevant context to the embedding model.
    prompt = prompt[:_MAX_PROMPT_CHARS]
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "c3.cli",
                "recall",
                "search",
                prompt,
                "--top",
                str(_TOP_K),
                "--min-score",
                str(_MIN_SCORE),
                "--json",
                "--target",
                str(repo_root),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return []
    hits = data.get("hits") or []
    return hits if isinstance(hits, list) else []


def main() -> int:
    if os.environ.get(_DISABLE_ENV_VAR) == "1":
        return 0

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str) or should_skip_prompt(prompt):
        return 0

    repo_root = find_repo_root()
    if repo_root is None or not index_exists(repo_root):
        return 0

    hits = run_recall(prompt, repo_root)
    if not hits:
        return 0

    stale = index_is_stale(repo_root)

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": format_additional_context(hits, stale=stale),
        }
    }
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
