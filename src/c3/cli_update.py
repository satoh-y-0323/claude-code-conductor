"""``c3 update`` - bring the project's ``.claude/`` up to date with the package template."""

from __future__ import annotations

import argparse
import filecmp
import os
import re
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

_ANSI_ESCAPE_RE = re.compile(r'\x1b\[[\x20-\x3f]*[\x40-\x7e]')

import c3
from c3._excludes import should_skip
from c3._terminal import sanitize_terminal_text, supports_color
from c3.adapters import print_adapter_actions, scaffold_adapters
from c3.paths import templates_dir
from c3.platforms import PLATFORM_CHOICES, expand_platforms


# ---------------------------------------------------------------------------
# Breaking changes dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BreakingChange:
    """1 エントリの breaking change 情報。immutable / hashable。"""
    version: str  # "X.Y.Z" 形式（先頭 'v' は strip 済み）
    en: str       # サニタイズ済み英語サマリ
    ja: str       # サニタイズ済み日本語サマリ


# ---------------------------------------------------------------------------
# Version comparison utilities
# ---------------------------------------------------------------------------

def _semver_tuple(v: str) -> tuple[int, int, int]:
    """`_compare_versions` でバリデーション済みの version 文字列を (major, minor, patch) に変換する。

    前提: v は _compare_versions の _parse() によるバリデーション済み入力。
    責務は「バリデーション済みトリプル整数化のみ」であり、エラーメッセージ詳細化は
    _compare_versions._parse() 側が担う（責務分離）。
    """
    s = v.lstrip("v")
    p = s.split(".")
    return (int(p[0]), int(p[1]), int(p[2]))


def _compare_versions(a: str, b: str) -> int:
    """2 つのバージョン文字列を比較する。

    Args:
        a: バージョン文字列（先頭 'v' 任意）
        b: バージョン文字列（先頭 'v' 任意）
    Returns:
        -1 (a < b) / 0 (a == b) / 1 (a > b)
    Raises:
        ValueError: SemVer 純粋形式 (X.Y.Z) でない場合（pre-release / build metadata 含む）
    """
    def _parse(v: str) -> tuple[int, int, int]:
        s = v.lstrip("v")
        # pre-release / build metadata は未サポート
        if "-" in s or "+" in s:
            raise ValueError(f"pre-release/build metadata not supported: {v!r}")
        parts = s.split(".")
        if len(parts) != 3:
            raise ValueError(f"not a 3-part SemVer: {v!r}")
        try:
            return (int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError:
            raise ValueError(f"non-integer version component: {v!r}")

    ta = _parse(a)
    tb = _parse(b)
    if ta < tb:
        return -1
    elif ta > tb:
        return 1
    return 0


def _bump_level(
    prev: str | None,
    curr: str,
) -> Literal["initial", "major", "minor", "patch", "same", "downgrade"]:
    """prev と curr を比較してバンプレベルを返す。

    Args:
        prev: 前バージョン文字列（None なら初回）
        curr: 現バージョン文字列
    Returns:
        "initial" / "major" / "minor" / "patch" / "same" / "downgrade"
    """
    if prev is None:
        return "initial"

    try:
        cmp = _compare_versions(prev, curr)
    except ValueError:
        # 不正な version は initial フォールバック（_load_version_checkpoint で None 化済みのため通常未到達）
        return "initial"

    if cmp == 0:
        return "same"
    if cmp > 0:
        return "downgrade"

    # prev < curr: major / minor / patch を判定（_semver_tuple は _compare_versions でバリデーション済み前提）
    pa = _semver_tuple(prev)
    pb = _semver_tuple(curr)
    if pb[0] > pa[0]:
        return "major"
    if pb[1] > pa[1]:
        return "minor"
    return "patch"


# ---------------------------------------------------------------------------
# breaking-changes.txt loader
# ---------------------------------------------------------------------------

def _load_breaking_changes(
    template_dir: Path,
) -> tuple[list[BreakingChange], list[str]]:
    """`breaking-changes.txt` を読み込みエントリと警告を返す。

    Returns:
        (entries, warnings):
          entries:  パース成功した BreakingChange のリスト（重複除外済み、ファイル記載順）
          warnings: パース時点で検出した警告の人間可読メッセージリスト
    Notes:
        - ファイル不在時は ([], []) を返す（古い wheel 互換）
        - 読み込み失敗（OSError）は ([], [warning]) を返し処理続行
        - BOM 検出時はファイル全体破棄 + warning（_load_deletions と同一挙動）
        - 副作用なし（読み取り専用）
    """
    bc_file = template_dir / "breaking-changes.txt"
    if not bc_file.exists():
        return [], []

    try:
        raw = bc_file.read_bytes()
    except OSError as exc:
        return [], [f"breaking-changes.txt: failed to read: {exc}"]

    # BOM チェック
    if raw.startswith(b"\xef\xbb\xbf"):
        return [], ["breaking-changes.txt: UTF-8 BOM detected; entire file ignored (re-save without BOM)"]

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return [], [f"breaking-changes.txt: UTF-8 decode error: {exc}"]

    warnings: list[str] = []
    seen: set[str] = set()
    entries: list[BreakingChange] = []

    for line in text.splitlines():
        stripped = line.strip()

        # 空行・コメント行をスキップ
        if not stripped or stripped.startswith("#"):
            continue

        # pipe で分割（最大 3 フィールド、過剰な pipe は ja に統合）
        parts = stripped.split("|", maxsplit=2)
        if len(parts) < 3:
            warnings.append(
                f"breaking-changes.txt: insufficient pipe separators (expected 2), skipping: {stripped!r}"
            )
            continue

        version_raw, en_raw, ja_raw = parts

        # version の SemVer バリデーション (lstrip("v") 後に一度だけ実行)
        version_norm = version_raw.strip().lstrip("v")
        try:
            _compare_versions(version_norm, version_norm)
        except ValueError as exc:
            warnings.append(
                f"breaking-changes.txt: invalid SemVer version {version_raw.strip()!r}, skipping ({exc})"
            )
            continue

        # 重複 version は先勝ち
        if version_norm in seen:
            warnings.append(
                f"breaking-changes.txt: duplicate version {version_norm!r}, skipping (first entry wins)"
            )
            continue

        # 制御文字サニタイズ（strip 後にサニタイズ: L-02）
        en = sanitize_terminal_text(en_raw.strip())
        ja = sanitize_terminal_text(ja_raw.strip())

        seen.add(version_norm)
        entries.append(BreakingChange(version=version_norm, en=en, ja=ja))

    return entries, warnings


# ---------------------------------------------------------------------------
# Breaking changes range extraction
# ---------------------------------------------------------------------------

def _extract_breaking_changes_between(
    prev: str | None,
    curr: str,
    entries: list[BreakingChange],
) -> list[BreakingChange]:
    """半開区間 (prev, curr] に含まれる breaking changes を返す。

    Args:
        prev: 前バージョン（None なら全件を対象とする）
        curr: 現バージョン
        entries: _load_breaking_changes() の戻り値
    Returns:
        該当する BreakingChange のリスト（バージョン昇順）
    Notes:
        - prev >= curr (downgrade/same) の場合は [] を返す
        - prev=None の場合は curr 以下の全件を返す（initial）
    """
    if prev is not None:
        try:
            cmp = _compare_versions(prev, curr)
        except ValueError:
            return []
        if cmp >= 0:
            return []

    result: list[BreakingChange] = []
    for entry in entries:
        try:
            cmp_curr = _compare_versions(entry.version, curr)
        except ValueError:
            continue
        # curr より大きいエントリは除外
        if cmp_curr > 0:
            continue
        # prev がある場合は prev より大きいものだけ（半開区間: prev は除外）
        if prev is not None:
            try:
                cmp_prev = _compare_versions(entry.version, prev)
            except ValueError:
                continue
            if cmp_prev <= 0:
                continue
        result.append(entry)

    # バージョン昇順ソート
    result.sort(key=lambda e: tuple(int(x) for x in e.version.split(".")))
    return result


# ---------------------------------------------------------------------------
# Breaking changes printer
# ---------------------------------------------------------------------------

def _print_breaking_changes(
    relevant: list[BreakingChange],
    *,
    bump: str,
    prev: str | None,
    curr: str,
    parse_warnings: list[str],
) -> None:
    """Breaking changes を bump レベルに応じて表示する。

    Args:
        relevant:      表示対象の BreakingChange リスト
        bump:          _bump_level() の戻り値
        prev:          前バージョン（None なら初回）
        curr:          現バージョン
        parse_warnings: _load_breaking_changes() の警告リスト
    Notes:
        - bump == "same" は何も表示しない（parse_warnings がある場合のみ stderr に表示）
        - bump == "downgrade" は stderr に 1 行 warning のみ
        - initial / major は relevant が空でもヘッダを表示する（bump 発生通知のため）。
          ただし initial + relevant 空の場合は「エントリなし」のヘッダを表示する（L-03）。
        - minor / patch は count==0 で早期 return する（UX: 不要なヘッダを抑制）。
        - parse_warnings は常に stderr に出力する（M-03: CLI 慣習: 警告は stderr へ集約）。
    """
    # F-03: バージョン表示文字列を予防的にサニタイズする（ESC シーケンス等の注入防止）
    prev_disp = sanitize_terminal_text(
        f"v{prev}" if prev and not prev.startswith("v") else (prev or "(none)")
    )
    curr_disp = sanitize_terminal_text(
        f"v{curr}" if not curr.startswith("v") else curr
    )

    if bump == "downgrade":
        print(
            f"warning: downgrade detected ({prev_disp} → {curr_disp}); checkpoint will not be updated",
            file=sys.stderr,
        )
        if parse_warnings:
            for w in parse_warnings:
                print(f"breaking-changes.txt warning: {w}", file=sys.stderr)
        return

    if bump == "same":
        if parse_warnings:
            for w in parse_warnings:
                print(f"breaking-changes.txt warning: {w}", file=sys.stderr)
        return

    # initial / major / minor / patch
    if bump == "initial":
        # L-03: initial + relevant 空の場合はエントリなしを明示する
        if relevant:
            header_text = f"初回 checkpoint 作成: breaking changes を全件表示します ({curr_disp})"
        else:
            header_text = f"初回 checkpoint 作成 ({curr_disp}): breaking-changes.txt にエントリなし"
        header = _color(header_text, "\033[33m")
        print(header)
    elif bump == "major":
        header_text = (
            f"MAJOR バージョン bump が検出されました ({prev_disp} → {curr_disp})"
        )
        header = _color(header_text, "\033[31m")
        print(header)
    else:
        # minor / patch: 表示すべきエントリが無ければ早期 return
        count = len(relevant)
        if count == 0:
            # M-03: parse_warnings は stderr に統一
            if parse_warnings:
                for w in parse_warnings:
                    print(f"breaking-changes.txt warning: {w}", file=sys.stderr)
            return
        header_text = f"{count} 件の breaking changes ({prev_disp} → {curr_disp})"
        header = _color(header_text, "\033[33m")
        print(header)

    for entry in relevant:
        print(f"  - v{entry.version}:")
        print(f"      [en] {entry.en}")
        print(f"      [ja] {entry.ja}")

    # M-03: parse_warnings は stderr に統一
    if parse_warnings:
        print("breaking-changes.txt warnings:", file=sys.stderr)
        for w in parse_warnings:
            print(f"  - {w}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Version checkpoint I/O
# ---------------------------------------------------------------------------

def _load_version_checkpoint(claude_root: Path) -> str | None:
    """`.claude/state/c3_version.txt` からバージョンを読み込む。

    Returns:
        バージョン文字列（先頭 'v' strip 済み）、または None（不在・破損・SemVer 不適合）
    """
    checkpoint = claude_root / "state" / "c3_version.txt"
    if not checkpoint.exists():
        return None

    try:
        text = checkpoint.read_text(encoding="utf-8").strip()
    except OSError:
        return None

    if not text:
        return None

    version = text.lstrip("v")
    try:
        _compare_versions(version, version)
    except ValueError:
        return None

    return version


def _save_version_checkpoint(claude_root: Path, version: str) -> None:
    """`.claude/state/c3_version.txt` にバージョンを atomic write する。

    Args:
        claude_root: 利用先の `.claude/` ディレクトリパス
        version:     保存するバージョン文字列
    Notes:
        - `state/` ディレクトリが無ければ作成する
        - tmp ファイルに書き出し → os.replace で atomic write
        - OSError は再 raise せず stderr warning に変換
    """
    path = claude_root / "state" / "c3_version.txt"
    # F-02: PID + uuid4 の組み合わせで並走時の tmp パス衝突を実質ゼロにする
    tmp = path.with_name(f"c3_version.txt.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(f"{version}\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        print(f"warning: failed to save version checkpoint: {exc}", file=sys.stderr)
        # tmp が残っている場合は除去を試みる
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


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

        # === 新規: breaking changes 表示 + MAJOR 承認ブロック ===
        # add/update の前に実行することで、MAJOR cancel 時にファイル変更も防ぐ (Q-01 確定)
        prev = _load_version_checkpoint(dest)
        curr = c3.__version__
        bc_entries, bc_parse_warns = _load_breaking_changes(template)
        bump = _bump_level(prev, curr)
        relevant = _extract_breaking_changes_between(prev, curr, bc_entries)
        _print_breaking_changes(relevant, bump=bump, prev=prev, curr=curr, parse_warnings=bc_parse_warns)

        if bump == "major" and relevant:
            if args.dry_run:
                print("(dry-run: confirmation would be required for major bump)")
            elif args.yes:
                print("(--yes: skipping prompt)")
            else:
                try:
                    answer = input("Proceed with major version update? [y/N]: ").strip().lower()
                except EOFError:
                    answer = ""
                if answer not in ("y", "yes"):
                    print("major version update cancelled by user")
                    return 0
        # ===================================================

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

        # === 新規: version checkpoint 書き込み (claude block の最終ステップ) ===
        # Q-02 確定: adapter 失敗時も claude block 成功なら checkpoint 更新
        # MAJOR cancel 時は上の `return 0` で早期 return されており、ここに到達しない（暗黙的スキップ）
        # deletions cancel 時は result["cancelled"] == True でスキップ
        # downgrade 時は checkpoint を更新しない (AC-F-05)
        # dry_run 時は checkpoint を更新しない
        if not args.dry_run and bump != "downgrade" and not result.get("cancelled", False):
            _save_version_checkpoint(dest, curr)
        # ===================================================

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
