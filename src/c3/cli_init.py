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


def _is_link_entry(entry: Path, real_root: str, rel: str) -> bool:
    """FR-4 choke point: realpath 完全一致でリンク経由の entry を検出する。

    ``entry.is_symlink()`` は NTFS ディレクトリジャンクションに ``False`` を返し
    素通りする（ADR-3・既知パターンの再発 5 回）ため採用しない。判定は
    「entry の realpath」と「コピー元ルートの realpath ＋ ルートからの相対パス」の
    完全一致（``.claude/skills/start/scripts/archive_reports.py`` の
    ``_archive_dir_is_contained`` と同じ realpath 封じ込め方式）。

    ``real_root`` は**呼び出し側で解決済みの** コピー元ルートの realpath（文字列）を
    受け取る契約（ADR-3 追補 2・``_copytree`` が再帰境界で 1 回だけ計算して伝播する）。
    本関数は root 側の realpath を行わない。比較の両辺とも realpath 済みであることが
    判定の前提であり、その担保は呼び出し側の責務になる。

    entry 側の realpath は ``OSError``（ELOOP・EACCES 等）を送出しうる。解決できない
    entry は安全側に倒して「リンク疑い（``True``）」として扱い、呼び出し側の
    警告付きスキップに委ねる（fail-soft・ADR-3 追補 1）。
    """
    # expected は「rel の各コンポーネントをリンクとして解決せず」文字列結合した
    # 期待パス。os.path.realpath はここでは呼ばない（呼ぶと entry 側と同じ
    # リンクを辿ってしまい、判定そのものが無効化される）。
    expected = os.path.normpath(os.path.join(real_root, *rel.split("/")))
    try:
        actual = os.path.realpath(entry)
    except OSError:
        return True
    return actual != expected


def _copytree(
    src: Path, dst: Path, *, root: Path | None = None, real_root: str | None = None
) -> int:
    """Copy ``src`` -> ``dst`` recursively, skipping personal/working files.

    ``root`` defaults to ``src`` and represents the ``.claude/`` directory; the
    relative path from ``root`` is what ``should_skip`` matches against.
    Returns the number of regular files written.

    ``real_root`` は ``root`` の realpath（解決済み文字列）。``None`` の初回呼び出しで
    1 回だけ計算し、再帰には計算済みの値を渡す（ADR-3 追補 2 の hoist。entry 総数 N 回の
    重複計算を 1 回に落とす）。

    root 側 realpath の失敗（``OSError``）は捕捉せず伝播させる。root が解決不能なのは
    テンプレート根本の異常であり、全 entry を「リンク疑い」として警告付きスキップし
    「0 件コピーの exit 0」を返すより、例外で止める方が利用者に正しく伝わるため
    （per-entry の fail-soft はコピー継続に意味がある場合の設計であり、根本が解決不能なら
    継続に意味がない・ADR-3 追補 1）。

    リンク判定 ``_is_link_entry`` の適用位置はファイル経路の 2 箇所のみ:
    (i) ``should_skip`` 前段の初回判定 (ii) ``shutil.copy2`` 直前の再検証。
    ディレクトリ側に再検証は追加しない。ディレクトリ entry が判定後にリンクへ
    差し替わっても、再帰先の per-entry 検査で全子 entry の realpath が
    ``real_root`` + rel と一致しなくなり内容は漏れないため（構造的再検証・ADR-3 追補 3）。
    """
    if root is None:
        root = src
    if real_root is None:
        # root 側の realpath はここで 1 回だけ。失敗は上記のとおり伝播させる。
        real_root = os.path.realpath(root)
    dst.mkdir(parents=True, exist_ok=True)
    count = 0
    for entry in src.iterdir():
        rel = entry.relative_to(root).as_posix()
        target = dst / entry.name
        # FR-4: リンク経由の entry は is_dir()/is_file() 分岐より前にスキップする
        # （両者ともリンクを暗黙に辿るため）。EXCLUDE 対象かどうかに関わらず
        # 検査するため should_skip より前段に置く（架空の EXCLUDE リンクも警告する・
        # architecture DC-GP-003 裁定）。
        if _is_link_entry(entry, real_root, rel):
            print(
                f"c3 init: リンク（symlink/junction）疑いの entry をスキップしました: {rel}",
                file=sys.stderr,
            )
            continue
        if entry.is_dir():
            count += _copytree(entry, target, root=root, real_root=real_root)
            # Drop directories that ended up empty (everything inside was skipped).
            if not any(target.iterdir()):
                target.rmdir()
        elif entry.is_file():
            if should_skip(rel):
                continue
            # 初回判定から copy2 到達までの間にリンクへ差し替わる TOCTOU を狭めるため、
            # copy2 の直前で同じ choke point を再検証する（ADR-3 追補 3）。
            # 残余リスク: 再検証と copy2 の間の窓は閉じない。窓を閉じる ``O_NOFOLLOW`` は
            # POSIX 限定で Windows を含む配布対象では採用できないため不採用とし、
            # 脅威モデル（ローカル開発者ツール）上この残余窓は許容する（ADR-3 追補 5）。
            if _is_link_entry(entry, real_root, rel):
                print(
                    f"c3 init: リンク（symlink/junction）疑いの entry をスキップしました: {rel}",
                    file=sys.stderr,
                )
                continue
            shutil.copy2(entry, target)
            count += 1
    return count
