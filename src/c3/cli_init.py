"""``c3 init`` - scaffold ``.claude/`` into the current project."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

from c3 import gitutil
from c3._excludes import should_skip
from c3.adapters import print_adapter_actions, scaffold_adapters
from c3.paths import templates_dir
from c3.platforms import PLATFORM_CHOICES, expand_platforms

# 非 git ディレクトリで git 操作を行わない・行えないときに表示するガイダンス文言。
# 複数の分岐（--no-git / 非 TTY / ユーザー拒否 / input 例外）で共通。
_MSG_WORKTREE_HINT = (
    "worktree を使う場合は git init してください（または c3 init --git）。"
)


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "init",
        help="Scaffold a fresh .claude/ directory into the current project",
        description=(
            "Copy the bundled C3 .claude/ template into the current working "
            "directory. Refuses to overwrite an existing .claude/ unless "
            "--force is given."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite an existing .claude/ directory without confirmation. "
            "This removes the directory tree first, so user-owned init-only files "
            "(rules/promoted/index.md, .gitignore) are lost as well."
        ),
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
        help=(
            "Target host adapter to initialize. Defaults to claude. "
            "codex/cursor/all also scaffold .claude/ as the canonical C3 source."
        ),
    )
    git_group = parser.add_mutually_exclusive_group()
    git_group.add_argument(
        "--git",
        action="store_true",
        help="git 管理外のとき確認なしで git init する（CI / 非対話の明示 opt-in）",
    )
    git_group.add_argument(
        "--no-git",
        action="store_true",
        help="git init を行わない（誘導メッセージのみ出力して正常終了）",
    )
    parser.set_defaults(handler=handle)


def handle(args: argparse.Namespace) -> int:
    target_root: Path = (args.target or Path.cwd()).resolve()
    dest = target_root / ".claude"
    platforms = expand_platforms(args.platform)
    adapter_platforms = tuple(p for p in platforms if p != "claude")

    if dest.exists() and not args.force and platforms == ("claude",):
        print(
            f"refusing to overwrite existing directory: {dest}\n"
            "Pass --force to overwrite or run `c3 update` for a diff-aware merge.",
            file=sys.stderr,
        )
        return 1

    template = templates_dir()
    if dest.exists() and args.force and "claude" in platforms:
        shutil.rmtree(dest)

    target_root.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        copied = _copytree(template, dest)
        print(f"initialized {dest} ({copied} files copied)")
    elif "claude" in platforms:
        print(f"using existing {dest}")

    if adapter_platforms:
        try:
            actions = scaffold_adapters(target_root, adapter_platforms)
        except (FileNotFoundError, ValueError) as exc:
            print(f"adapter init failed: {exc}", file=sys.stderr)
            return 1
        print_adapter_actions(actions)

    _maybe_init_git(
        target_root,
        git=getattr(args, "git", False),
        no_git=getattr(args, "no_git", False),
    )
    return 0


def _maybe_init_git(
    target_root: Path, *, git: bool, no_git: bool, _input_fn: Callable[[str], str] | None = None
) -> None:
    """Detect git status and, for non-git dirs, init under the consent model.

    git の成否は ``c3 init`` の exit code に影響させない（戻り値なし）。
    すべてのメッセージは stdout に出す（scaffold 成功通知と同列）。

    ``_input_fn`` はテスト注入ポイント（デフォルト ``None`` = 呼び出し時に
    ``builtins.input`` を解決）。本番コードからは渡さない。
    """
    status = gitutil.detect_git_status(target_root)

    if status is gitutil.GitStatus.INSIDE_REPO:
        return  # 既に git 管理下。何もしない（入れ子 repo を作らない）

    if status is gitutil.GitStatus.GIT_MISSING:
        print(
            "git コマンドが見つかりません。worktree 並列実装には git が必要です。"
            "git をインストールしてから手動で git init してください。"
        )
        return

    # ここから status == NOT_A_REPO
    if no_git:
        print(_MSG_WORKTREE_HINT)
        return

    if git:
        _do_git_init(target_root)
        return

    # フラグ無し: TTY のみ同意プロンプト
    # sys.stdin が None や isatty() が bool True を返さない場合は非 TTY として扱う。
    # 標準ライブラリの isatty() は常に bool を返すため is True で安全に判定できる。
    if not (sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty() is True):
        print(
            "git 管理下にないため worktree 並列実装は利用できません。"
            "c3 init --git で git init するか、手動で git init してください。"
        )
        return

    # input は呼び出し時に解決する（import 時束縛を避けモンキーパッチと両立）。
    fn = _input_fn if _input_fn is not None else input
    try:
        answer = fn(
            "このディレクトリは git 管理下にありません。"
            "worktree 並列実装のため git init しますか? [Y/n]: "
        ).strip().lower()
    except (EOFError, OSError):
        # パイプ等の非対話 stdin で input() が EOF/OS エラーを返した場合は
        # 誘導メッセージにフォールバックする。
        print(_MSG_WORKTREE_HINT)
        return
    if answer in ("", "y", "yes"):
        _do_git_init(target_root)
    else:
        print(_MSG_WORKTREE_HINT)


def _do_git_init(target_root: Path) -> None:
    """Run git_init and print the outcome message."""
    if gitutil.git_init(target_root):
        print(f"worktree 並列実装のため git init を実行しました: {target_root}")
    else:
        print(
            "git init に失敗しました（.claude/ scaffold は完了しています）。"
            "必要なら手動で git init してください。"
        )


def _is_link_entry(entry: Path, root: Path, rel: str) -> bool:
    """FR-4 choke point: realpath 完全一致でリンク経由の entry を検出する。

    ``entry.is_symlink()`` は NTFS ディレクトリジャンクションに ``False`` を返し
    素通りする（ADR-3・既知パターンの再発 5 回）ため採用しない。判定は
    「entry の realpath」と「コピー元ルートの realpath ＋ ルートからの相対パス」の
    完全一致（``.claude/skills/start/scripts/archive_reports.py`` の
    ``_archive_dir_is_contained`` と同じ realpath 封じ込め方式）。

    比較の両辺とも realpath 済みであること。``root`` 側の realpath を怠ると、
    コピー元ルート自体がリンク経由のとき全 entry が「配下でない」と誤判定され、
    全件スキップに縮退する。
    """
    real_root = os.path.realpath(root)
    # expected は「rel の各コンポーネントをリンクとして解決せず」文字列結合した
    # 期待パス。os.path.realpath はここでは呼ばない（呼ぶと entry 側と同じ
    # リンクを辿ってしまい、判定そのものが無効化される）。
    expected = os.path.normpath(os.path.join(real_root, *rel.split("/")))
    actual = os.path.realpath(entry)
    return actual != expected


def _copytree(src: Path, dst: Path, *, root: Path | None = None) -> int:
    """Copy ``src`` -> ``dst`` recursively, skipping personal/working files.

    ``root`` defaults to ``src`` and represents the ``.claude/`` directory; the
    relative path from ``root`` is what ``should_skip`` matches against.
    Returns the number of regular files written.
    """
    if root is None:
        root = src
    dst.mkdir(parents=True, exist_ok=True)
    count = 0
    for entry in src.iterdir():
        rel = entry.relative_to(root).as_posix()
        target = dst / entry.name
        # FR-4: リンク経由の entry は is_dir()/is_file() 分岐より前にスキップする
        # （両者ともリンクを暗黙に辿るため）。EXCLUDE 対象かどうかに関わらず
        # 検査するため should_skip より前段に置く（架空の EXCLUDE リンクも警告する・
        # architecture DC-GP-003 裁定）。
        if _is_link_entry(entry, root, rel):
            print(
                f"c3 init: リンク（symlink/junction）疑いの entry をスキップしました: {rel}",
                file=sys.stderr,
            )
            continue
        if entry.is_dir():
            count += _copytree(entry, target, root=root)
            # Drop directories that ended up empty (everything inside was skipped).
            if not any(target.iterdir()):
                target.rmdir()
        elif entry.is_file():
            if should_skip(rel):
                continue
            shutil.copy2(entry, target)
            count += 1
    return count
