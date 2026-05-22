"""``c3 update`` - bring the project's ``.claude/`` up to date with the package template."""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

from c3._excludes import should_skip
from c3.adapters import print_adapter_actions, scaffold_adapters
from c3.paths import templates_dir
from c3.platforms import PLATFORM_CHOICES, expand_platforms

# 廃止済み skill パス（リリース履歴）。
# `c3 update` 完了時に配布先で残存していたら警告を stderr に表示する。
# 削除はしない（`c3 update` の "削除を検出しない" 設計を尊重）。
DEPRECATED_PATHS: tuple[tuple[str, str], ...] = (
    (".claude/skills/code-review", "v2.15.1 で /review-phase にリネーム（Built-in /code-review と衝突回避）"),
)


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "update",
        help="Refresh .claude/ from the bundled template (skips local files)",
        description=(
            "Compare the project's .claude/ to the bundled template and "
            "overwrite framework files that differ. User-managed files "
            "(reports/, memory/sessions/, docs/decisions.md, etc.) are "
            "always skipped."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing anything",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=None,
        help="Destination directory (defaults to the current working directory)",
    )
    parser.add_argument(
        "--platform",
        choices=PLATFORM_CHOICES,
        default="claude",
        help="Target host adapter to update. Defaults to claude.",
    )
    parser.set_defaults(handler=handle)


def handle(args: argparse.Namespace) -> int:
    target_root: Path = (args.target or Path.cwd()).resolve()
    platforms = expand_platforms(args.platform)
    dest = target_root / ".claude"
    if not dest.is_dir():
        print(
            f"no .claude/ directory found in {target_root}. "
            "Run `c3 init` first.",
            file=sys.stderr,
        )
        return 1

    changed = 0
    if "claude" in platforms:
        template = templates_dir()
        actions = list(_walk_diff(template, dest))
        changed += len(actions)

        if args.dry_run:
            if actions:
                print(f"{len(actions)} file(s) would change:")
                for action, path in actions:
                    print(f"  {action}: {path.relative_to(dest)}")
            else:
                print("claude template up to date")
        else:
            for action, abs_path in actions:
                rel = abs_path.relative_to(dest)
                src = template / rel
                if action == "add" or action == "update":
                    abs_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, abs_path)
                    print(f"  {action}: {rel}")

    adapter_platforms = tuple(p for p in platforms if p != "claude")
    if adapter_platforms:
        try:
            adapter_actions = scaffold_adapters(
                target_root,
                adapter_platforms,
                dry_run=args.dry_run,
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"adapter update failed: {exc}", file=sys.stderr)
            return 1
        changed += len(adapter_actions)
        print_adapter_actions(adapter_actions, dry_run=args.dry_run)

    if changed == 0:
        print("up to date")
    elif not args.dry_run:
        print(f"{changed} file(s) updated")
    _warn_deprecated_paths(target_root)
    return 0


def _warn_deprecated_paths(dest_root: Path) -> None:
    """配布先で廃止済み skill パスが残っていたら stderr に警告を出力。

    DEPRECATED_PATHS の各エントリ (rel_path, reason) について
    dest_root / rel_path の存在を確認し、見つかれば warning メッセージを stderr に出す。

    本関数は読み取り専用（exists() チェックと stderr 出力のみ）で副作用がないため、
    `--dry-run` 時にも常に実行される。削除は行わない（手動クリーンアップを促すのみ）。
    `c3 update` の "削除を検出しない" 設計を尊重した実装。
    """
    found = []
    for rel_path, reason in DEPRECATED_PATHS:
        candidate = dest_root / Path(rel_path)
        if candidate.exists():
            found.append((rel_path, reason))
    if not found:
        return
    print("", file=sys.stderr)
    print("warning: deprecated path(s) detected (manual cleanup recommended):", file=sys.stderr)
    for rel_path, reason in found:
        print(f"  - {rel_path}: {reason}", file=sys.stderr)
    print("", file=sys.stderr)
    print("  c3 update does not delete files. Remove the path(s) manually.", file=sys.stderr)


def _walk_diff(template: Path, dest: Path):
    """Yield (action, absolute_dest_path) tuples for files that differ.

    Only ``add`` and ``update`` are emitted; we never delete files in dest.
    Personal/working files (per ``c3._excludes``) are skipped both as bundle
    sources and as overwrite targets.
    """
    for src_file in _iter_files(template):
        rel = src_file.relative_to(template)
        if should_skip(rel.as_posix()):
            continue
        target = dest / rel
        if not target.exists():
            yield "add", target
        elif not filecmp.cmp(src_file, target, shallow=False):
            yield "update", target


def _iter_files(root: Path):
    for entry in root.iterdir():
        if entry.is_dir():
            yield from _iter_files(entry)
        elif entry.is_file():
            yield entry
