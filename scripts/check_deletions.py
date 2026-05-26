"""scripts/check_deletions.py

直近の v タグ以降に削除された .claude/ 配下のファイルを列挙し、
`.claude/deletions.txt` に未記載の配布対象削除がないかチェックするスクリプト。

使い方:
  python scripts/check_deletions.py          未記載があれば追記サジェストを表示（exit 0）
  python scripts/check_deletions.py --check   未記載があれば exit 1（CI/リリース前用）

配布対象外（scripts/ は wheel に含まれない）。リリース前の人間 + CI 確認用。

設計判断:
  - should_skip() で配布除外対象を判定し、配布対象の削除のみを検出する
  - deletions.txt のパーサは _load_deletions を再利用（SSOT: BOM/コメント/バリデーション仕様の二重管理を避ける）
  - git 連携は薄く保ち、タグ取得・削除列挙のみ subprocess で行う
  - タグが無い/git 失敗時は exit 0（CI で初回タグ前でも壊さない）
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from c3._excludes import should_skip
from c3.cli_update import _load_deletions


# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

# プロジェクトルートからの相対パス（このスクリプトは scripts/ に置かれる）
_REPO_ROOT = Path(__file__).resolve().parent.parent
_CLAUDE_DIR = _REPO_ROOT / ".claude"


# ---------------------------------------------------------------------------
# 純粋関数（テスト対象）
# ---------------------------------------------------------------------------

def find_unrecorded_deletions(
    deleted_rel_paths: list[str],
    recorded_paths: set[str],
) -> list[str]:
    """削除された .claude/ 相対パスのうち、配布対象かつ deletions.txt 未記載のものを返す。

    Args:
        deleted_rel_paths: git diff で得られた .claude/ 相対 POSIX パスのリスト
                           （例: ["agents/legacy.md", "reports/x.md"]）
        recorded_paths:    deletions.txt に記載済みのパスの集合

    Returns:
        入力順保持・重複除去済みの未記載パスリスト。
        should_skip(rel) が True のもの（配布除外対象）は含まれない。
    """
    seen: dict[str, None] = {}
    for rel in deleted_rel_paths:
        if should_skip(rel):
            continue
        if rel in recorded_paths:
            continue
        if rel not in seen:
            seen[rel] = None
    return list(seen.keys())


# ---------------------------------------------------------------------------
# git 連携
# ---------------------------------------------------------------------------

def _get_latest_vtag() -> str | None:
    """直近の v タグを返す。タグが無い/git 失敗時は None を返す。"""
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--match", "v*", "--abbrev=0"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"警告: git describe の実行に失敗しました: {exc}", file=sys.stderr)
        return None

    if result.returncode != 0:
        print(
            f"警告: git describe が失敗しました（タグが存在しない可能性があります）: "
            f"{result.stderr.strip()}",
            file=sys.stderr,
        )
        return None

    return result.stdout.strip()


def _get_deleted_paths_since(tag: str) -> list[str] | None:
    """<tag>..HEAD 間で .claude/ 配下から削除されたファイルの .claude/ 相対パスを返す。

    status が "D" の行と、リネーム "R..." 行の旧パス（2 列目）を削除として扱う。
    git 失敗時は None を返す。
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-status", f"{tag}..HEAD", "--", ".claude/"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"警告: git diff の実行に失敗しました: {exc}", file=sys.stderr)
        return None

    if result.returncode != 0:
        print(
            f"警告: git diff が失敗しました: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return None

    deleted: list[str] = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0]

        if status == "D" and len(parts) >= 2:
            # 通常削除: D\t.claude/path/to/file
            repo_rel = parts[1].replace("\\", "/")
            claude_rel = _strip_claude_prefix(repo_rel)
            if claude_rel is not None:
                deleted.append(claude_rel)

        elif status.startswith("R") and len(parts) >= 3:
            # リネーム: R100\t.claude/old/path\t.claude/new/path
            # 旧パスを削除として扱う
            old_path = parts[1].replace("\\", "/")
            claude_rel = _strip_claude_prefix(old_path)
            if claude_rel is not None:
                deleted.append(claude_rel)

    return deleted


def _strip_claude_prefix(repo_rel: str) -> str | None:
    """リポジトリ相対パスから ".claude/" プレフィックスを除去して .claude/ 相対パスを返す。

    ".claude/" で始まらないパスは None を返す。
    """
    prefix = ".claude/"
    if repo_rel.startswith(prefix):
        return repo_rel[len(prefix):]
    return None


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------

def main() -> int:
    """スクリプトのエントリポイント。

    Returns:
        終了コード: 0 = 成功 / 1 = 未記載あり (--check)
    """
    parser = argparse.ArgumentParser(
        description="直近 v タグ以降の .claude/ 削除が deletions.txt に記載済みかチェックする",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="未記載の削除があれば exit 1 で終了する（CI/リリース前用）",
    )
    args = parser.parse_args()

    # deletions.txt の記載集合を取得（SSOT: _load_deletions に委譲）
    entries, warnings = _load_deletions(_CLAUDE_DIR)
    if warnings:
        for w in warnings:
            print(f"警告 [deletions.txt]: {w}", file=sys.stderr)
    recorded_paths: set[str] = set(entries)

    # 直近 v タグを取得
    tag = _get_latest_vtag()
    if tag is None:
        print("チェック対象のタグが見つかりませんでした。スキップします。", file=sys.stderr)
        return 0

    # git diff で削除ファイルを取得
    deleted = _get_deleted_paths_since(tag)
    if deleted is None:
        print("git diff の実行に失敗しました。スキップします。", file=sys.stderr)
        return 0

    # 未記載の配布対象削除を検出
    unrecorded = find_unrecorded_deletions(deleted, recorded_paths)

    if not unrecorded:
        print(f"全ての配布対象削除が deletions.txt に記載されています（{tag} 以降）。")
        return 0

    # 未記載あり
    if args.check:
        print(
            f"未記載の配布対象削除が {len(unrecorded)} 件あります（{tag} 以降）:",
            file=sys.stderr,
        )
        for path in unrecorded:
            print(f"  - {path}", file=sys.stderr)
        print(
            "\n.claude/deletions.txt への追記が必要です。"
            "引数なしで実行すると追記コメント付き雛形を表示します。",
            file=sys.stderr,
        )
        return 1

    # 引数なし: 追記サジェストを表示
    print(f"未記載の配布対象削除が {len(unrecorded)} 件あります（{tag} 以降）。")
    print()
    print("以下を .claude/deletions.txt に追記してください:")
    print()
    print(f"# {tag} 以降で削除された配布対象ファイル")
    for path in unrecorded:
        print(path)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
