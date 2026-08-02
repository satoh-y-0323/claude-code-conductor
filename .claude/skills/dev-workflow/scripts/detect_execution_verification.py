#!/usr/bin/env python3
"""フェーズ E の実行検証判定（E-0）の検出器。

requirements-report-20260802-185228.md および
architecture-report-20260802-190003.md（改訂 1〜5）に従い実装。

Usage:
    c3 run .claude/skills/dev-workflow/scripts/detect_execution_verification.py [--base <ref>]

出力:
    stdout（第 1 行のみ・必ず出力）:
        NEEDS_VERIFY<TAB>{件数}<TAB>{file1}\0{file2}\0...  （--print0 指定時のみ NUL 区切り）
        NOT_NEEDED<TAB>0
        UNKNOWN<TAB>{理由コード}
    stderr（既定）:
        対象ファイルの一覧（1 行 1 ファイル）

設計の要点（ADR-*):
- ADR-3F/ADR-3G: 語彙は strong（1 種で発火・大小文字無視の部分一致）/
  weak（2 種で発火・一部は単語境界）の二段
- ADR-2R/ADR-2U/ADR-2E/ADR-2V/ADR-2W/ADR-2X: 走査対象は git diff + untracked、
  untracked の失敗は該当ファイルのみスキップ、判定順序は語彙ヒット >
  tracked 0 件 > その他、git は -c core.quotePath=false 前置
- ADR-4R: 出力は stdout 1 行（NUL なし既定）+ stderr 一覧
- ADR-8H: seam は detect / _run_git / resolve_base / collect_untracked
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

try:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="")
    sys.stderr.reconfigure(encoding="utf-8", newline="")
except AttributeError:
    pass


# 語彙定義（ADR-3F / ADR-3G）
_STRONG_LITERALS = {
    "escape",
    "unescape",
    "encode",
    "decode",
    "sanitize",
    "quote",
    "unquote",
    "tokenize",
    "lexer",
    "parser",
    "serialize",
    "deserialize",
    "re.compile",
    "new RegExp",
    "MustCompile",
    "Regex(",
    "replace(/",
    "match(/",
    "split(/",
    "search(/",
    r"\x00",
    r"\u00",
    "[[:cntrl:]]",
    "transition",
    "STATE_",
}

# weak は 2 種類の部分一致語と 4 種類の単語境界語に分け、マッチング方式を区別
_WEAK_SUBSTRING = {
    ".replace(",
    ".split(",
    ".join(",
    ".strip(",
    ".trim(",
    "startswith",
    "endswith",
    "token",
    "buffer",
}

_WEAK_WORD_BOUNDARY = {
    "state",
    "match",
    "switch",
    "mode",
}


def _run_git(args: list[str]) -> tuple[int, str]:
    """git を実行し (exit_code, stdout) を返す。

    ADR-2X: 全 git 呼び出しに -c core.quotePath=false を前置する。
    タイムアウト 10 秒（ADR-2X 改訂 4 [DC-GP-002]）。

    git の出力は UTF-8 固定のため encoding="utf-8" を明示する。
    text=True / universal_newlines=True はロケール既定（Windows では cp932 等）を
    使うため、日本語を含む diff・ファイル名でリーダースレッドが UnicodeDecodeError
    で死に、正常時でも GIT_FAILED になる。errors="replace" でデコード不能バイト
    （非 UTF-8 のファイル内容が diff 本文に混ざる場合）でも例外にしない。
    """
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotePath=false", *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            shell=False,
        )
        return (result.returncode, result.stdout)
    except subprocess.TimeoutExpired:
        return (124, "")  # タイムアウト（git の rc 慣例ではないが、GIT_FAILED 扱い）
    except Exception:
        return (127, "")  # git 不在等


def resolve_base(explicit: str | None) -> str | None:
    """ベースコミットを解決する。

    ADR-2E: 明示 --base は rev-parse --verify のみ（連鎖なし）。
    未指定: merge-base HEAD @{u} → origin/HEAD → main → master の順。
    """
    if explicit is not None:
        rc, stdout = _run_git(["rev-parse", "--verify", explicit])
        if rc == 0:
            return stdout.strip()
        # 明示 --base が失敗したら連鎖に入らない（ADR-2E）
        return None

    # 未指定時の自動解決順序
    candidates = [
        ["merge-base", "HEAD", "@{u}"],
        ["merge-base", "HEAD", "origin/HEAD"],
        ["merge-base", "HEAD", "main"],
        ["merge-base", "HEAD", "master"],
    ]

    for cmd in candidates:
        rc, stdout = _run_git(cmd)
        if rc == 0:
            return stdout.strip()

    return None


def collect_untracked() -> list[tuple[str, str]]:
    """untracked ファイル一覧を収集し、(相対パス, 内容) の列を返す。

    ADR-2U: git ls-files --others --exclude-standard で取得。
    ADR-2V: 読み取り失敗（UnicodeDecodeError / PermissionError 等）でもスキップして継続。
    先頭 8KB に NUL バイトを含むファイルは走査対象から外す（バイナリ判定）。
    SR-V-002: symlink 封じ込め確認・リポジトリ外の実体は読み取り対象から除外。
    SR-NEW: ファイルサイズ上限を 1MB に設定。超過は読み取りスキップ。
    """
    rc, stdout = _run_git(["ls-files", "--others", "--exclude-standard"])
    if rc != 0:
        return []

    files = stdout.strip().split("\n") if stdout.strip() else []
    result: list[tuple[str, str]] = []
    repo_root = Path.cwd()
    repo_root_resolved = repo_root.resolve()  # ループ外で 1 回のみ計算
    max_file_size = 1024 * 1024  # 1MB

    for file_path in files:
        if not file_path:
            continue

        try:
            full_path = repo_root / file_path

            # SR-V-002: symlink チェック・リポジトリ外の実体を封じ込める
            # 注: is_symlink() は Windows ジャンクションを捕捉しないため、
            # 実質的な封じ込めは 2 番目の resolve() ベース判定に依存している。
            if full_path.is_symlink():
                print(f"Warning: skipping {file_path}: symlink", file=sys.stderr)
                continue

            resolved = full_path.resolve()
            # resolved がリポジトリ配下に収まることを確認
            # SR-V-002: check-then-use（is_symlink 判定と read 実行の間に TOCTOU の窓が
            # 理論上存在）だが、本ツールの脅威モデル（ローカル単発実行・攻撃者が同時に
            # ファイルシステムを制御する想定なし）から実害は見込まれない。
            if not (resolved == repo_root_resolved or repo_root_resolved in resolved.parents):
                print(f"Warning: skipping {file_path}: outside repository", file=sys.stderr)
                continue

            # SR-NEW: ファイルサイズ上限チェック
            try:
                file_size = full_path.stat().st_size
                if file_size > max_file_size:
                    print(f"Warning: skipping {file_path}: exceeds size limit (1MB)", file=sys.stderr)
                    continue
            except OSError:
                pass  # stat 取得失敗時は read で失敗時に catch する

            content = full_path.read_text(encoding="utf-8")

            # 先頭 8KB でNUL チェック（ADR-2V）
            head_8kb = content[:8192]
            if "\x00" in head_8kb:
                continue  # バイナリ判定・走査対象から外す

            result.append((file_path, content))
        except (UnicodeDecodeError, PermissionError, FileNotFoundError, OSError) as e:
            # stderr に警告を出して継続（ADR-2V）
            print(f"Warning: skipping {file_path}: {type(e).__name__}", file=sys.stderr)
            continue

    return result


def detect(
    diff_text: str, untracked: list[tuple[str, str]] | None = None
) -> tuple[str, list[str]]:
    """実行検証が必要かを判定する（純関数）。

    戻り値:
        (判定トークン, 対象ファイル一覧)
        判定トークン: "NEEDS_VERIFY" / "NOT_NEEDED" / "UNKNOWN"（理由コード無し）
        UNKNOWN は diff_text/untracked からファイルが 0 件の場合のみ。

    ADR-3F/ADR-3G: strong 1 種 or weak 2 種で発火。
    ADR-2R: ファイル単位で weak 「種類数」を通算（出現回数ではない）。
    ADR-2W: tracked 側（diff）が 0 件なら UNKNOWN（untracked は分母に入れない）。
    ADR-8G: ファイル名は +++ b/<path> からのみ抽出。+++ /dev/null は除外。
    """
    if untracked is None:
        untracked = []

    # 走査対象の統合（ファイル単位で重複排除・POSIX 形式に正規化）
    all_files: dict[str, str] = {}

    # diff からのファイル + 追加行（ADR-8G と tracked_count の両方を get）
    diff_files = _extract_added_lines_from_diff(diff_text)
    for name, added_lines in diff_files.items():
        all_files[name] = added_lines

    # untracked ファイルはすべての内容を「追加行」とみなす（ADR-2U）
    for path, content in untracked:
        normalized = _normalize_path(path)
        if normalized not in all_files:
            all_files[normalized] = content

    # 判定順序（ADR-2W）
    # 1. 語彙ヒットがあれば NEEDS_VERIFY
    firing_files = set()
    for file_name, content in all_files.items():
        if _has_firing_vocabulary(content):
            firing_files.add(file_name)

    if firing_files:
        return ("NEEDS_VERIFY", sorted(list(firing_files)))

    # 2. tracked 側（diff）が 0 件なら UNKNOWN（ADR-2W）
    tracked_count = len(diff_files)
    if tracked_count == 0:
        return ("UNKNOWN", [])

    # 3. それ以外は NOT_NEEDED
    return ("NOT_NEEDED", [])


def _normalize_path(path: str) -> str:
    """パスを POSIX 形式の相対パスに正規化する（ADR-2R [DC-AM-003]）。"""
    return path.replace("\\", "/").lstrip("/")


def _extract_added_lines_from_diff(diff_text: str) -> dict[str, str]:
    """diff から各ファイルの追加行を集約する。

    同一ファイルに複数のセクション（base 差分と HEAD 差分等）がある場合は
    追加行を統合し、重複排除する（ADR-2R）。

    返り値:
        {ファイル名: 追加行の連結テキスト}
    """
    current_file: str | None = None
    added_lines: dict[str, set[str]] = {}  # ファイル名 -> 追加行の集合（重複排除）

    for line in diff_text.split("\n"):
        # ファイル名の変更検出
        if line.startswith("+++ b/"):
            path = line[6:]
            if path != "/dev/null" and path != "\\dev\\null":
                current_file = _normalize_path(path)
                if current_file not in added_lines:
                    added_lines[current_file] = set()

        # 追加行の抽出（+++ ヘッダを除く）
        elif current_file and line.startswith("+") and not line.startswith("+++"):
            added_lines[current_file].add(line[1:])  # 先頭の + を削除

        # 特殊行の処理
        elif line.startswith("Binary files"):
            current_file = None
        elif line.startswith("\\ No newline at end of file"):
            # ファイル名の候補でないため何もしない
            pass

    # 集合を連結してテキストに戻す
    # nul-boundary: allow(dedupe_aggregation)  # NUL 境界不適用・\n は同一ファイル内の行結合
    result = {name: "\n".join(sorted(lines)) for name, lines in added_lines.items()}
    return result


def _has_firing_vocabulary(content: str) -> bool:
    """コンテンツが検出語彙を含むかを判定する。

    ADR-3F/ADR-3G: strong は 1 種で発火、weak は 2 種で発火。
    strong は大小文字無視の部分一致、weak の一部は単語境界を課す。
    """
    strong_count = _count_vocabulary_matches(content, _STRONG_LITERALS, word_boundary=False)
    if strong_count > 0:
        return True

    weak_count = (
        _count_vocabulary_matches(content, _WEAK_SUBSTRING, word_boundary=False)
        + _count_vocabulary_matches(content, _WEAK_WORD_BOUNDARY, word_boundary=True)
    )
    return weak_count >= 2


def _count_vocabulary_matches(
    content: str, vocab: set[str], word_boundary: bool
) -> int:
    """コンテンツから語彙の「種類数」を数える（出現回数ではない）。

    word_boundary=True の場合は単語境界を課す。
    """
    count = 0
    for word in vocab:
        if word_boundary:
            # 単語境界を課す場合は正規表現で検索
            pattern = r"\b" + re.escape(word.lower()) + r"\b"
            if re.search(pattern, content.lower()):
                count += 1
        else:
            # 大小文字無視の部分一致
            if word.lower() in content.lower():
                count += 1
    return count


def _collect_diffs(base: str | None) -> tuple[str, bool]:
    """git diff でコミット済み変更と作業ツリー変更を収集する。

    ADR-2R: <base> が明示されていれば差分を取得。未指定でも HEAD 差分を試す。
    CR-M-002: 明示 base が失敗した場合は HEAD 試行を スキップし fail-safe する（情報損失は
    UNKNOWN で補完）。

    戻り値: (統合 diff テキスト, git_failed フラグ)
    """
    diffs: list[str] = []
    git_failed = False

    if base:
        rc, stdout = _run_git(["diff", base])
        if rc == 0:
            diffs.append(stdout)
        else:
            git_failed = True

    # base が失敗していなければ HEAD 側も試す
    if not git_failed:
        rc, stdout = _run_git(["diff", "HEAD"])
        if rc == 0:
            diffs.append(stdout)
        else:
            git_failed = True

    return ("".join(diffs), git_failed)


def _format_output(token: str, files: list[str], print0: bool) -> None:
    """判定結果と対象ファイル一覧を出力する。

    ADR-4R に従い stdout に判定結果を出力、stderr に対象ファイルを出力する。
    print0=True の場合は stdout にファイル一覧を NUL 区切りで追加出力。

    副作用: stdout/stderr へ直接出力。戻り値なし。
    """
    if token == "UNKNOWN":
        # detect が UNKNOWN を返したのは EMPTY_DIFF
        print("UNKNOWN\tEMPTY_DIFF")
    elif token == "NEEDS_VERIFY":
        print(f"NEEDS_VERIFY\t{len(files)}", end="")
        if print0:
            # NUL 区切りで stdout に追加出力（ADR-4R・セパレータは NUL のため lint 対象外）
            print("\t" + "\x00".join(files), end="")
        print()
    elif token == "NOT_NEEDED":
        print("NOT_NEEDED\t0")

    # stderr に人間可読の一覧を出力（既定）
    for file_name in sorted(files):
        print(file_name, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。ADR-4R / ADR-2X に従い exit 0 を返す（全経路）。"""
    parser = argparse.ArgumentParser(
        description="フェーズ E の実行検証判定（E-0）"
    )
    parser.add_argument(
        "--base",
        default=None,
        help="ベースコミット（明示指定時は連鎖なし・ADR-2E）",
    )
    parser.add_argument(
        "--print0",
        action="store_true",
        help="stdout にファイル一覧を NUL 区切りで追加出力する",
    )

    args = parser.parse_args(argv)

    try:
        # ベース解決（ADR-2E）
        base = resolve_base(args.base)
        if args.base is not None and base is None:
            # 明示 --base が解決不能 → UNKNOWN GIT_FAILED
            print("UNKNOWN\tGIT_FAILED")
            return 0

        # git diff <base> / git diff HEAD で追加行を取得（ADR-2R）
        diff_text, git_failed = _collect_diffs(base)
        if git_failed:
            print("UNKNOWN\tGIT_FAILED")
            return 0

        # untracked 収集（ADR-2U）
        untracked = collect_untracked()

        # 判定実行
        token, files = detect(diff_text, untracked)

        # 出力（ADR-4R）
        _format_output(token, files, args.print0)

        return 0

    except Exception as e:
        # すべてのエラーを exit 0 で通す（ADR-5 / N-3）
        # SR-R-001: 例外メッセージ本文を出力しない（内部エラーで decoding 不能バイト漏洩回避）
        print("UNKNOWN\tGIT_FAILED", file=sys.stdout)
        print(f"Error: {type(e).__name__}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
