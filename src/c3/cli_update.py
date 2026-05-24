"""``c3 update`` - bring the project's ``.claude/`` up to date with the package template."""

from __future__ import annotations

import argparse
import filecmp
import re
import shutil
import sys
from pathlib import Path, PurePosixPath
from typing import Any

_ANSI_ESCAPE_RE = re.compile(r'\x1b\[[\x20-\x3f]*[\x40-\x7e]')

from c3._excludes import should_skip
from c3._terminal import supports_color
from c3.adapters import print_adapter_actions, scaffold_adapters
from c3.paths import templates_dir
from c3.platforms import PLATFORM_CHOICES, expand_platforms


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
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip the deletion confirmation prompt (for CI/automated workflows). No effect with --dry-run.",
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

        # === 新規: 削除処理 ===
        entries, parse_warnings = _load_deletions(template)
        result = _apply_deletions(
            entries,
            claude_root=dest.resolve(),
            dry_run=args.dry_run,
            assume_yes=args.yes,
        )
        # parse_warnings を warnings に統合
        result["warnings"] = parse_warnings + result["warnings"]
        report = _format_deletion_report(result, dry_run=args.dry_run, assume_yes=args.yes)
        if report:
            print(report)
        # 削除件数を全体カウンタに反映
        if not args.dry_run:
            changed += len(result["deleted"])

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
    return 0


def _color(text: str, ansi: str) -> str:
    """ANSI カラーコードを付与する private ヘルパー。supports_color() が False なら素テキストを返す。"""
    if supports_color():
        return f"{ansi}{text}\033[0m"
    return text


def _load_deletions(template_dir: Path) -> tuple[list[str], list[str]]:
    """`deletions.txt` を読み込みパス候補と無視警告を返す。

    Returns:
        (entries, warnings):
          entries:  正常にパース・前 validate を通過した `.claude/` 相対 POSIX パス文字列のリスト
                    （順序保持 dedupe 済み、絶対パス・空行・コメントを除外）
          warnings: パース時点で検出した「不正行・無視理由」の人間可読メッセージ
                    （`.claude/` プレフィックス禁止 / BOM 検出 / 文字列レベル不正など）
    Notes:
        - ファイル不在時は ([], []) を返す（後方互換: deletions.txt 未配布の wheel でも動く）
        - 読み込み失敗（OSError）は ([], [warning]) を返し処理続行
        - 本関数はファイルシステムを変更しない（読み取り専用）
    """
    deletions_file = template_dir / "deletions.txt"
    if not deletions_file.exists():
        return [], []

    try:
        raw = deletions_file.read_bytes()
    except OSError as exc:
        return [], [f"deletions.txt: failed to read: {exc}"]

    # BOM チェック
    if raw.startswith(b"\xef\xbb\xbf"):
        return [], ["deletions.txt: UTF-8 BOM detected; entire file ignored (re-save without BOM)"]

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return [], [f"deletions.txt: UTF-8 decode error: {exc}"]

    warnings: list[str] = []
    seen: dict[str, None] = {}

    for line in text.splitlines():
        stripped = line.strip()

        # 空行・コメント行をスキップ
        if not stripped or stripped.startswith("#"):
            continue

        # 文字列レベルの事前バリデーション（_validate_deletion_path と重複するが、
        # ロード段階で警告を明示するため再チェック）
        # 絶対パス
        if stripped.startswith("/"):
            warnings.append(f"absolute path not allowed: {stripped}")
            continue
        # home-relative
        if stripped.startswith("~"):
            warnings.append(f"home-relative path not allowed: {stripped}")
            continue
        # バックスラッシュ
        if "\\" in stripped:
            warnings.append(f"backslash in path not allowed: {stripped}")
            continue
        # Windows ドライブレター
        if re.match(r"^[A-Za-z]:", stripped):
            warnings.append(f"drive letter not allowed: {stripped}")
            continue
        # .claude/ プレフィックス禁止
        if stripped.startswith(".claude/"):
            warnings.append(f"do not prefix with .claude/: {stripped}")
            continue
        # .. / . 含み
        # step 7 (parts チェック) の前段。PurePosixPath 正規化前に文字列レベルで弾く。
        # 注意: PurePosixPath('./foo').parts は ('foo',) と正規化されるため文字列チェックを先に行う
        if stripped.startswith("./") or stripped.startswith("../"):
            warnings.append(f"relative-traversal path not allowed: {stripped}")
            continue
        if any(part in {".", ".."} for part in PurePosixPath(stripped).parts):
            warnings.append(f"relative-traversal path not allowed: {stripped}")
            continue

        # ANSI エスケープシーケンス検出 → warning + skip
        if _ANSI_ESCAPE_RE.search(stripped):
            warnings.append(f"ANSI escape sequence not allowed: {stripped!r}")
            continue

        # 順序保持 dedupe
        if stripped not in seen:
            seen[stripped] = None

    return list(seen.keys()), warnings


def _validate_deletion_path(rel: str, claude_root: Path) -> tuple[Path | None, str | None]:
    """1 エントリのセーフガードを実行し、削除可能な絶対 Path を返す。

    Args:
        rel:         deletions.txt から読んだ `.claude/` 相対 POSIX パス
        claude_root: 利用先プロジェクトの `.claude/` の resolve 済み絶対パス
    Returns:
        (resolved_path, warning):
          resolved_path: 安全に削除可能と判定された絶対 Path。不正・対象外なら None
          warning: 不正検出時の理由メッセージ（人間可読、stderr 出力用）。
                   正常時は None。「ファイル不在（=既に削除済み）」は warning ではなく
                   呼び出し側で「skipped (already absent)」として処理する

    セーフガード順序（13 段）:
      1. 空文字列 / 空白のみ
      2. 文字列レベル: 先頭 /
      3. 文字列レベル: 先頭 ~
      4. 文字列レベル: バックスラッシュ含み
      5. 文字列レベル: Windows ドライブレター
      6. 文字列レベル: .claude/ プレフィックス禁止
      7. パーツレベル: . / .. 含み
      8. Path.is_symlink() で symlink 拒否（resolve 前の candidate に対して）
         ※ resolve() 後は実体先を指すため is_symlink() が常に False になる
      9. Path.resolve() で実体解決
      10. claude_root in resolved.parents で範囲確認
      11. resolved == claude_root 拒否（step 10 と統合）
      12. deletions.txt 自己削除保護
      13. Path.is_dir() でディレクトリ拒否
      14. Path.is_file() で実在確認（not file → None, None → absent として処理）
    """
    # 1. 空文字列 / 空白のみ
    if not rel.strip():
        return None, "empty path"

    # 2. 先頭 /（絶対パス）
    if rel.startswith("/"):
        return None, f"absolute path not allowed: {rel}"

    # 3. 先頭 ~（home-relative）
    if rel.startswith("~"):
        return None, f"home-relative path not allowed: {rel}"

    # 4. バックスラッシュ含み
    if "\\" in rel:
        return None, f"backslash in path not allowed: {rel}"

    # 5. Windows ドライブレター
    if re.match(r"^[A-Za-z]:", rel):
        return None, f"drive letter not allowed: {rel}"

    # 6. .claude/ プレフィックス禁止
    if rel.startswith(".claude/"):
        return None, f"do not prefix with .claude/: {rel}"

    # 7. パーツレベル: . / .. 含み
    # step 7 の正規化に依存しないための先行チェック:
    # PurePosixPath('./foo').parts は ('foo',) と正規化されるため、
    # 文字列レベルで './' / '../' を先にチェックする
    if rel.startswith("./") or rel.startswith("../"):
        return None, f"relative-traversal path not allowed: {rel}"
    try:
        parts = PurePosixPath(rel).parts
    except Exception:
        return None, f"invalid path: {rel}"
    if any(part in {".", ".."} for part in parts):
        return None, f"relative-traversal path not allowed: {rel}"

    # candidate を Path 化
    candidate = claude_root / rel

    # 8. symlink チェック（resolve() 前の candidate に対して）
    # ※ resolve() 後は実体先を指すため is_symlink() が常に False になる
    if candidate.is_symlink():
        return None, f"symlink not allowed: {rel}"

    # 9. resolve() で実体解決
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        return None, f"cannot resolve: {rel}: {exc}"

    # 10/11. claude_root 配下かつ claude_root 自身でないことを確認
    if resolved == claude_root or claude_root not in resolved.parents:
        return None, f"path escapes .claude/: {rel}"

    # 12. deletions.txt 自己削除保護
    if resolved == claude_root / "deletions.txt":
        return None, "deletions.txt itself cannot be deleted (self-referencing guard)"

    # 13. ディレクトリ拒否
    if resolved.is_dir():
        return None, f"directory deletion not supported: {rel}"

    # 14. ファイル実在確認（不在は warning なし）
    if not resolved.is_file():
        return None, None  # absent として処理

    return resolved, None


def _apply_deletions(
    entries: list[str],
    claude_root: Path,
    *,
    dry_run: bool,
    assume_yes: bool,
) -> dict[str, Any]:
    """deletions.txt のエントリ群に基づいて削除を実施（または予告）する。

    Args:
        entries:     `_load_deletions()` が返した検証前のパスリスト
        claude_root: 利用先 `.claude/` の resolve 済み絶対パス
        dry_run:     True なら予告のみ・ファイルシステム変更なし
        assume_yes:  True ならプロンプトをスキップして削除実行
    Returns:
        {
          "to_delete":  list[str]  # 候補表示用（rel path）
          "deleted":    list[str]  # 実際に削除したファイルの rel path
          "absent":     list[str]  # 既に存在しないファイル
          "errors":     list[str]  # 削除失敗（OSError 等）
          "warnings":   list[str]  # セーフガードで弾かれたエントリの理由
          "cancelled":  bool       # プロンプトで N が選ばれた場合 True
        }
    """
    result: dict[str, Any] = {
        "to_delete": [],
        "deleted": [],
        "absent": [],
        "errors": [],
        "warnings": [],
        "cancelled": False,
    }

    candidates: list[tuple[str, Path]] = []  # (rel, resolved)

    for rel in entries:
        resolved, warning = _validate_deletion_path(rel, claude_root)
        if warning is not None:
            result["warnings"].append(warning)
        elif resolved is None:
            # is_file() が False（ファイル不在）
            result["absent"].append(rel)
        else:
            candidates.append((rel, resolved))
            result["to_delete"].append(rel)

    # dry_run: 予告のみ
    if dry_run:
        return result

    # 削除候補なし
    if not candidates:
        return result

    # プロンプト前に候補一覧を表示（キャンセル後も見えるように先行表示）
    header = _color(
        f"deletions: {len(candidates)} file(s) will be removed:",
        "\033[33m",
    )
    print(header)
    for rel, _ in candidates:
        print(f"  - {rel}")

    # プロンプト（assume_yes でなければ）
    if not assume_yes:
        try:
            answer = input("Proceed with deletion? [y/N]: ").strip().lower()
        except EOFError:
            answer = ""  # 非対話環境では N 扱い（誤削除防止）
        if answer not in ("y", "yes"):
            result["cancelled"] = True
            return result

    # 削除実行
    for rel, resolved in candidates:
        try:
            resolved.unlink()
            result["deleted"].append(rel)
        except OSError as exc:
            result["errors"].append(f"{rel}: {exc.strerror}")

    return result


def _format_deletion_report(result: dict[str, Any], *, dry_run: bool, assume_yes: bool = False) -> str:
    """`_apply_deletions()` の戻り値を人間可読な stdout テキストに整形する。

    Args:
        result:     `_apply_deletions()` の戻り値 dict
        dry_run:    True なら dry-run 出力フォーマット
        assume_yes: True かつ通常実行の場合、`(--yes: skipping prompt)` を出力
    """
    lines: list[str] = []

    to_delete = result["to_delete"]
    deleted = result["deleted"]
    absent = result["absent"]
    errors = result["errors"]
    warnings = result["warnings"]
    cancelled = result["cancelled"]

    if dry_run:
        # --- dry_run 出力 ---
        if to_delete:
            header = _color(
                f"deletions: {len(to_delete)} file(s) would be removed:",
                "\033[33m",
            )
            lines.append(header)
            for rel in to_delete:
                lines.append(f"  - {rel}")
        if absent:
            lines.append(f"deletions: {len(absent)} already absent:")
            for rel in absent:
                lines.append(f"  - {rel}")
        if warnings:
            warn_header = _color(f"deletions: {len(warnings)} warning(s):", "\033[33m")
            lines.append(warn_header)
            for w in warnings:
                lines.append(f"  - {w}")
        if not to_delete and not absent and not warnings:
            lines.append("deletions: nothing to delete")
        else:
            lines.append("(dry-run: no files were modified)")
    else:
        # --- 通常実行出力 ---
        # 候補一覧は _apply_deletions() 内でプロンプト前に表示済み。
        # assume_yes の場合はプロンプトスキップを明示する。
        # NOTE: assume_yes=True のとき _apply_deletions は input() を呼ばないため
        # cancelled は常に False。`not cancelled` 条件は意図的な防御（assume_yes 時に
        # 別経路で cancel が発生する将来拡張への保険）
        if to_delete and not cancelled and assume_yes:
            lines.append("(--yes: skipping prompt)")

        if cancelled:
            lines.append("deletions: cancelled by user (no files removed)")
        elif not to_delete:
            lines.append("deletions: nothing to delete")
        else:
            # 削除実行済み
            deleted_count = _color(f"{len(deleted)} deleted", "\033[32m")
            error_count = len(errors)
            error_str = (
                _color(f"{error_count} error(s)", "\033[31m")
                if error_count > 0
                else f"{error_count} errors"
            )
            absent_count = len(absent)
            warn_count = len(warnings)
            warn_str = (
                _color(f"{warn_count} warning(s)", "\033[33m")
                if warn_count > 0
                else f"{warn_count} warning(s)"
            )
            lines.append(
                f"deletions: {deleted_count}, {absent_count} absent,"
                f" {error_str}, {warn_str}"
            )
            if absent:
                lines.append(f"  absent:")
                for rel in absent:
                    lines.append(f"    - {rel}")
            if errors:
                lines.append("  errors:")
                for e in errors:
                    lines.append(f"    - {e}")
            if warnings:
                lines.append("  warnings:")
                for w in warnings:
                    lines.append(f"    - {w}")

    return "\n".join(lines)


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
