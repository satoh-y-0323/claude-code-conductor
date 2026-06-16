#!/usr/bin/env python3
"""Stop hook: automatically rebuild the recall index when it is stale.

Mode 1 (default / Stop hook):
  Checks if the recall index exists and is stale, then spawns this same
  script as a detached background worker (Mode 2) and exits immediately
  (exit 0, no blocking).

Mode 2 (--rebuild-worker):
  Invoked by Mode 1.  Runs ``c3 recall rebuild --target <repo_root>``
  via subprocess, then releases the lock in a finally block.

The hook never outputs ``{"decision": "block"}`` and never blocks the
Claude turn from completing (ADR-5).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# CLAUDE.md §9-3: reconfigure stdout/stderr to UTF-8 on Windows.
try:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.stderr and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DISABLE_ENV_VAR = "C3_RECALL_AUTOREBUILD_DISABLE"

# Lock TTL in seconds.  rebuild normally finishes in tens of seconds;
# 600 s gives a generous safety margin (ADR-3).
_LOCK_TTL = 600

# Worker subprocess timeout (seconds).
# Intentionally equal to _LOCK_TTL: the lock becomes reclaimable as stale
# immediately after the worker timeout fires, preventing a stuck lock state.
_WORKER_TIMEOUT = 600

# Stale source globs — keep in sync with recall_inject._STALE_SOURCE_GLOBS.
# CR-L-02: these must mirror c3.recall_index.collect_sources source kinds.
_STALE_SOURCE_GLOBS = (
    (Path(".claude") / "memory" / "sessions", "*.tmp"),
    (Path(".claude") / "agent-memory", "*.md"),
    (Path(".claude") / "reports" / "archive", "*.md"),
)
_STALE_PATTERNS_JSON = Path(".claude") / "memory" / "patterns.json"


# ---------------------------------------------------------------------------
# Pure helper functions (testable without side effects)
# ---------------------------------------------------------------------------


def find_repo_root() -> Path | None:
    """Return the directory containing ``.claude/``, or None.

    When CLAUDE_PROJECT_DIR is set, only that exact directory is checked
    (no ancestor traversal).  This prevents false positives when running
    inside a temp directory whose parent happens to contain ``.claude``.

    When CLAUDE_PROJECT_DIR is not set, searches from cwd upward.

    Note: this intentionally differs from ``recall_inject.find_repo_root``.
    ``recall_inject`` always walks upward from cwd regardless of
    CLAUDE_PROJECT_DIR; this hook skips ancestor traversal when the env var
    is set to avoid false positives in hook context (temp-dir invocations).
    Cross-reference: .claude/hooks/recall_inject.py ``find_repo_root``.
    """
    env_dir = os.getenv("CLAUDE_PROJECT_DIR")
    if env_dir:
        try:
            candidate = Path(env_dir).resolve()
        except (OSError, ValueError):
            # L-04 [SR-V-001]: null バイト等の不正パスで resolve() が ValueError を投げる場合
            # 例外を伝播させず None を返す（ADR-6）。
            return None
        if (candidate / ".claude").is_dir():
            return candidate
        return None
    # No env override: walk upward from cwd.
    here = Path.cwd().resolve()
    for candidate in [here, *here.parents]:
        if (candidate / ".claude").is_dir():
            return candidate
    return None


def index_exists(repo_root: Path) -> bool:
    """Return True if the recall index file (``recall.hnsw``) exists.

    歴史的経緯でファイル名は ``.hnsw`` を維持しているが、中身は numpy ndarray
    (``.npy`` ペイロード, shape (N, dim) float32) であり HNSW ではない。

    Only ``recall.hnsw`` is checked — not ``recall_meta.json`` — because
    this hook's purpose is to trigger a rebuild when the index is stale.
    If the index file is present (even without a metadata sidecar),
    the stale check in ``index_is_stale_fast`` is meaningful and we should
    proceed.  ``recall_inject`` checks both files because it needs a valid
    meta to run a search; we do not run a search here.
    """
    index = repo_root / ".claude" / "state" / "recall.hnsw"
    return index.exists()


def index_is_stale_fast(repo_root: Path) -> bool:
    """Return True if any recall source file is newer than the index.

    Uses only stat() calls — never reads file contents — to stay fast
    enough for every-Stop invocation (ADR-1).

    Stale source globs are kept in sync with recall_inject._STALE_SOURCE_GLOBS
    (CR-L-02: mirror c3.recall_index.collect_sources when adding/removing
    source kinds).

    Symlinks are skipped to avoid reading mtime of files outside the
    C3 source tree.
    """
    index_path = repo_root / ".claude" / "state" / "recall.hnsw"
    if not index_path.exists():
        # Defensive guard for independent callers: main() already checks
        # index_exists() before calling this function, but callers outside
        # main() benefit from this early return rather than silently
        # returning False with no indication of the missing index.
        return False
    try:
        index_mtime = index_path.stat().st_mtime
    except OSError:
        return False

    for rel_dir, pattern in _STALE_SOURCE_GLOBS:
        absolute = repo_root / rel_dir
        if not absolute.is_dir():
            continue
        for path in absolute.rglob(pattern):
            # Skip symlinks (Cycle2-L-1 guard), non-files, and .gitkeep
            # (parity with recall_inject.index_is_stale: .gitkeep files are
            # placeholder markers, not actual recall source content).
            if path.is_symlink() or not path.is_file() or path.name == ".gitkeep":
                continue
            try:
                if path.stat().st_mtime > index_mtime:
                    return True
            except OSError:
                continue

    patterns_path = repo_root / _STALE_PATTERNS_JSON
    if patterns_path.is_file() and not patterns_path.is_symlink():
        try:
            if patterns_path.stat().st_mtime > index_mtime:
                return True
        except OSError:
            pass

    return False


def is_worktree(repo_root: Path) -> bool:
    """Return True if repo_root is a git worktree (i.e. .git is a file).

    In a normal repository, .git is a directory.
    In a git worktree, .git is a file containing a ``gitdir:`` pointer.
    We skip rebuild in worktrees to avoid spurious multi-index rebuilds
    during parallel-agents runs (ADR-4).
    """
    dot_git = repo_root / ".git"
    return dot_git.is_file()


def _lock_path_for(repo_root: Path) -> Path:
    """Return the canonical lock file path for the given repo root."""
    return repo_root / ".claude" / "state" / "recall_rebuild.lock"


def acquire_lock(lock_path: Path, ttl: int, now: float | None = None) -> Path | None:
    """Attempt to acquire the rebuild lock atomically.

    Uses os.open(O_CREAT|O_EXCL) for cross-platform atomic creation.

    - If the lock does not exist: create it and return a truthy token.
    - If the lock exists and mtime + ttl > now (fresh): return None (failed).
    - If the lock exists and mtime + ttl <= now (stale): remove it,
      then create a new lock and return a truthy token (ADR-3).

    TOCTOU note: between the stale-lock deletion and the O_EXCL creation,
    another process may race to create the lock first.  In that case
    ``os.open`` raises ``FileExistsError`` and this function returns ``None``.
    Returning ``None`` here is correct and expected behaviour — the other
    process won the race and will perform the rebuild.

    ``now`` is injectable for deterministic testing.
    """
    if now is None:
        now = time.time()

    # Check for existing lock.
    if lock_path.exists():
        try:
            mtime = lock_path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if mtime + ttl > now:
            # Fresh lock held by another process — refuse acquisition.
            return None
        # Stale lock — remove and fall through to create a new one.
        try:
            lock_path.unlink()
        except OSError:
            pass

    # Atomic create via O_CREAT|O_EXCL.
    # L-01 [SR-NEW]: 0o600 を明示してロックファイルを所有者のみ読み書き可能に制限する。
    # Windows は mode 引数を無視するが、指定しても無害。
    try:
        fd = os.open(
            str(lock_path),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError:
        # Another process won the race.
        return None
    except OSError:
        return None

    try:
        content = json.dumps({"pid": os.getpid(), "started": now}).encode()
        os.write(fd, content)
    finally:
        os.close(fd)

    return lock_path  # Truthy token.


def release_lock(lock_path: Path) -> None:
    """Remove the lock file.  Safe to call even if the file does not exist."""
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def build_worker_argv(self_path: Path, repo_root: Path) -> list[str]:
    """Return the argv list to launch this script as a rebuild worker.

    The resulting command is:
      python <self_path> --rebuild-worker --target <repo_root>
    """
    return [
        sys.executable,
        str(self_path),
        "--rebuild-worker",
        "--target",
        str(repo_root),
    ]


def spawn_detached(repo_root: Path) -> None:
    """Launch this script in worker mode as a detached background process.

    Platform specifics (ADR-2):
    - Windows: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    - POSIX: start_new_session=True
    - Common: close_fds=True, stdin/stdout/stderr=DEVNULL
    """
    self_path = Path(__file__).resolve()
    argv = build_worker_argv(self_path, repo_root)
    devnull = subprocess.DEVNULL

    # L-02 [SR-K-003]: ANTHROPIC_/CLAUDE_/OPENAI_ プレフィックスの変数を除外した最小 env を構成する。
    # Note: CLAUDE_PROJECT_DIR も CLAUDE_ プレフィックスで除外されるが、worker は
    # --target 引数で repo_root を受け取るため CLAUDE_PROJECT_DIR 除外の影響を受けない。
    _EXCLUDED_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "OPENAI_")
    env = {
        k: v
        for k, v in os.environ.items()
        if not any(k.upper().startswith(p) for p in _EXCLUDED_PREFIXES)
    }

    kwargs: dict = {
        "close_fds": True,
        "stdin": devnull,
        "stdout": devnull,
        "stderr": devnull,
        "env": env,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen(argv, **kwargs)


def run_rebuild_worker(repo_root: Path) -> None:
    """Mode 2: run ``c3 recall rebuild --target <repo_root>`` synchronously.

    Releases the lock in a finally block regardless of success or failure
    (ADR-3).
    """
    # M-01 [SR-V-002]: --target パスの .claude/ 存在を検証し、なければ rebuild しない。
    # 不正なターゲット（シンボリックアタック等）による意図しない rebuild を防止する。
    if not (repo_root / ".claude").is_dir():
        return

    # L-03 [SR-R-004]: C3_RECALL_AUTOREBUILD_DEBUG=1 のとき子プロセスの stderr を親に継承させる。
    # None を渡すと subprocess.run のデフォルト（親 stderr 継承）になる。
    debug = os.environ.get("C3_RECALL_AUTOREBUILD_DEBUG") == "1"
    stderr_target = None if debug else subprocess.DEVNULL

    # Windows: 孫プロセス（c3 recall rebuild = console app の python.exe）が
    # 新しいコンソールウィンドウを確保して一瞬表示されるのを防ぐ。worker 自身は
    # DETACHED_PROCESS でコンソールを持たないため、明示しないと孫が新規確保する。
    run_kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": stderr_target,
        "timeout": _WORKER_TIMEOUT,
    }
    if sys.platform == "win32":
        run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    lock_path = _lock_path_for(repo_root)
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "c3.cli",
                "recall",
                "rebuild",
                "--target",
                str(repo_root),
            ],
            **run_kwargs,
        )
    except Exception as e:
        sys.stderr.write(f"[recall_autorebuild] worker error: {type(e).__name__}\n")
    finally:
        release_lock(lock_path)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Stop hook entry point.  Always returns 0 (never blocks Claude turn).

    Flow (ADR-5, ADR-6):
      1. Check disable env var.
      2. Read Stop payload from stdin (errors are swallowed).
      3. Resolve repo root; skip if unknown.
      4. Skip if inside a git worktree (ADR-4).
      5. Skip if recall index does not exist.
      6. Skip if index is not stale (ADR-1).
      7. Acquire rebuild lock (skip on contention / ADR-3).
      8. Spawn worker; release lock on spawn failure.
    """
    # 1. Disable env var.
    if os.environ.get(_DISABLE_ENV_VAR) == "1":
        return 0

    # 2. Read stdin (Stop payload).  Errors are harmless — we don't use
    #    the payload for anything meaningful here, just consume it.
    try:
        sys.stdin.read()
    except Exception:
        pass

    try:
        # 3. Repo root.
        repo_root = find_repo_root()
        if repo_root is None:
            return 0

        # 4. Worktree guard.
        if is_worktree(repo_root):
            return 0

        # 5. Index existence.
        if not index_exists(repo_root):
            return 0

        # 6. Stale check (stat only — ADR-1).
        if not index_is_stale_fast(repo_root):
            return 0

        # 7. Acquire lock.
        lock_path = _lock_path_for(repo_root)
        token = acquire_lock(lock_path, _LOCK_TTL)
        if token is None:
            return 0

        # 8. Spawn detached worker.
        try:
            spawn_detached(repo_root)
        except Exception:
            release_lock(lock_path)

    except Exception:
        pass

    return 0


# ---------------------------------------------------------------------------
# Worker mode dispatch
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--rebuild-worker" in sys.argv:
        # Mode 2: resolve --target and run rebuild worker.
        try:
            idx = sys.argv.index("--target")
            repo_root = Path(sys.argv[idx + 1]).resolve()
        except (ValueError, IndexError):
            sys.exit(0)
        run_rebuild_worker(repo_root)
        sys.exit(0)
    else:
        # Mode 1: Stop hook.
        sys.exit(main())
