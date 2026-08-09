#!/usr/bin/env python3
"""`.claude/reports/*.md` を `.claude/reports/archive/` へ安全に移動する。

`/start` の Step 0（レポート整理）から `c3 run` 経由で呼ばれる。

設計（SSOT）: `.claude/reports/architecture-report-20260809-154124.md`（改訂 2）
  - INV-1: 既存ファイルを 1 バイトも変更しない（移動先の名前を排他生成で確保する）
  - INV-2: `archive/` はフラットのまま（サブディレクトリを作らない）
  - INV-3: アーカイブの実装はこの 1 箇所（SKILL.md 側にロジックを置かない）
  - INV-4: 移動先はバイト同一（読み書きはバイナリ I/O のみ・テキスト変換を経由しない）

挙動:
  - 移動先に同名が既にある場合は、移動元を
    `{stem}-archived-{移動元 mtime:YYYYMMDD-HHMMSS}{suffix}` へリネームして移動する（ADR-2）。
    その名前も埋まっていれば `-2` / `-3` … と空きを探す。既存ファイルは決して上書きしない
  - 移動先の mtime は移動元の値へ復元する（`recall` の再構築誘発と版情報の消失を防ぐ）
  - 1 件失敗しても残りは処理し、末尾で stderr に失敗一覧を出して exit 1 にする（ロールバックしない）
  - 失敗した 1 件は移動元を消さない（残す方に倒す）

Exit code（設計 §3-3。**戻り値で表し例外では表さない**）:
  0: 全対象の移動を確認できた（対象 0 件も 0）
  1: 1 件でも移動に失敗した、または引数が不正
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path, PurePath

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

ARCHIVE_DIR_NAME = "archive"

# ADR-2: 衝突退避で付く接尾辞。`-archived-` の識別子を挟むことで、
# タイムスタンプ命名の正規レポート名と字面で区別できるようにする。
ARCHIVED_MARKER = "-archived-"

# ADR-2 / 未確定事項 1: 接尾辞は **ローカル時刻** で組む（既存レポート名が日本時間のため）。
STAMP_FORMAT = "%Y%m%d-%H%M%S"

# 設計 §3-2: フェーズ名 → ファイル名接頭辞（このスクリプトが SSOT）。
PHASE_PREFIXES: dict[str, tuple[str, ...]] = {
    "requirements": ("requirements-report-",),
    "architecture": ("architecture-report-",),
    "plan": ("plan-report-",),
    "review": (
        "code-review-report-",
        "security-review-report-",
        "design-review-report-",
    ),
}

# ADR-3: 名前の確保を原子的に行う。O_EXCL があれば既存を潰すことは起こりえない。
# O_BINARY は Windows のみ存在する（POSIX では 0 相当）。
_EXCL_FLAGS = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)


# ---------------------------------------------------------------------------
# パス解決
# ---------------------------------------------------------------------------


def _default_reports_dir() -> Path:
    """`--reports-dir` 省略時の対象ディレクトリ（設計 §3-1）。

    cwd 起点にしない: `c3 run` は runpy による同一プロセス実行で cwd をシェルから
    引き継ぐため、worktree cwd リーク（Issue #28017）の状況で対象を取り違える。

    **この解決は import 時ではなく呼び出し時に行う**（設計 §3-3b）。浅い一時ディレクトリへ
    スクリプトの写しを置いた場合、import 時に評価すると `IndexError` で落ちてしまう。
    """
    return Path(__file__).resolve().parents[4] / ".claude" / "reports"


# ---------------------------------------------------------------------------
# 移動先の名前
# ---------------------------------------------------------------------------


def _stamp(mtime: float) -> str:
    """mtime をローカル時刻の `YYYYMMDD-HHMMSS` に整形する。"""
    return datetime.fromtimestamp(mtime).strftime(STAMP_FORMAT)


def _candidate_names(name: str, mtime: float):
    """移動先ファイル名の候補を優先順に produce する（ADR-2）。

    1. 元の名前そのもの
    2. `{stem}-archived-{mtime}{suffix}`
    3. `{stem}-archived-{mtime}-2{suffix}` / `-3` …（同秒衝突の退避）
    """
    yield name
    pure = PurePath(name)
    base = f"{pure.stem}{ARCHIVED_MARKER}{_stamp(mtime)}"
    suffix = pure.suffix
    yield f"{base}{suffix}"
    index = 2
    while True:
        yield f"{base}-{index}{suffix}"
        index += 1


# ---------------------------------------------------------------------------
# 1 件分の移動
# ---------------------------------------------------------------------------


def _move_one(src: Path, archive_dir: Path) -> Path:
    """`src` を `archive_dir` へ移動し、実際に書いた移動先パスを返す。

    設計 §3-3b は引数 2 個（`src` / `archive_dir`）を契約として固定している。戻り値は
    同 §3-3b では `None` と書かれているが、§3-3 の stdout 契約（リネーム時に旧名→新名を出す）を
    命名ロジックの重複なく満たすため移動先を返す形にした（INV-3: 命名の実装を 2 箇所に持たない）。

    失敗時は例外を送出し、**移動元を残す**（ADR-3）。中途半端に生成した移動先は削除する。
    """
    info = src.stat()
    # INV-4: バイナリ I/O のみ。テキストモードを経由すると改行・encoding が変換される。
    data = src.read_bytes()

    dst: Path | None = None
    fd = -1
    for candidate in _candidate_names(src.name, info.st_mtime):
        target = archive_dir / candidate
        try:
            fd = os.open(str(target), _EXCL_FLAGS, 0o644)
        except FileExistsError:
            continue
        dst = target
        break

    assert dst is not None and fd >= 0  # _candidate_names は無限に候補を出す
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        # コピー＋削除では移動時刻に置き換わるため、移動元の値を復元する。
        os.utime(dst, (info.st_atime, info.st_mtime))
    except BaseException:
        try:
            os.unlink(str(dst))
        except OSError:
            pass
        raise

    src.unlink()
    return dst


# ---------------------------------------------------------------------------
# 対象の列挙
# ---------------------------------------------------------------------------


def _selected_prefixes(phases: list[str]) -> tuple[str, ...] | None:
    """`--phase` の指定を接頭辞の集合へ畳む。指定が無ければ `None`（全件）。"""
    if not phases:
        return None
    prefixes: list[str] = []
    for phase in phases:
        for prefix in PHASE_PREFIXES[phase]:
            if prefix not in prefixes:
                prefixes.append(prefix)
    return tuple(prefixes)


def _collect_targets(
    reports_dir: Path, prefixes: tuple[str, ...] | None
) -> tuple[list[Path], list[Path]]:
    """移動対象と、シンボリックリンクゆえに除外したエントリを返す（設計 §3-3c）。

    破壊操作の対象は `{reports-dir}` 直下の `*.md` に限る（再帰しない・`archive/` を見ない）。
    """
    targets: list[Path] = []
    skipped_links: list[Path] = []
    for entry in sorted(reports_dir.glob("*.md")):
        # シンボリックリンクは辿らない。`is_file()` はリンクを辿るため先に判定する。
        if entry.is_symlink():
            skipped_links.append(entry)
            continue
        if not entry.is_file():
            continue
        if prefixes is not None and not entry.name.startswith(prefixes):
            continue
        targets.append(entry)
    return targets, skipped_links


def _one_line(exc: BaseException) -> str:
    """失敗理由を 1 行に潰す（失敗一覧は 1 件 1 行の契約のため）。"""
    text = f"{type(exc).__name__}: {exc}"
    return text.replace("\r", " ").replace("\n", " ").replace("\t", " ")


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Move .claude/reports/*.md into .claude/reports/archive/ without overwriting.",
    )
    # choices= は使わない: 未知値で argparse が SystemExit(2) を送出し、
    # 「exit code は main の戻り値で表す」（設計 §3-3b）に反するため main 内で検証する。
    parser.add_argument(
        "--phase",
        action="append",
        default=None,
        metavar="NAME",
        help="target phase (repeatable). omit to target every report",
    )
    parser.add_argument(
        "--reports-dir",
        default=None,
        metavar="PATH",
        help="reports directory (archive dir is always <PATH>/archive)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # --help は 0、引数不正は 1（設計 §3-3）。例外のまま抜けさせない。
        return 0 if exc.code in (0, None) else 1

    phases: list[str] = list(args.phase or [])
    unknown = sorted({phase for phase in phases if phase not in PHASE_PREFIXES})
    if unknown:
        # 引数不正は検証段階で弾き、1 件も移動しない（部分適用を作らない・ADR-3）。
        print(f"unknown --phase value(s): {unknown}", file=sys.stderr)
        print(f"valid values: {sorted(PHASE_PREFIXES)}", file=sys.stderr)
        return 1

    if args.reports_dir is None:
        reports_dir = _default_reports_dir()
    else:
        reports_dir = Path(args.reports_dir).resolve()

    # 設計 §3-3c: 存在しない / ディレクトリでない場合はエラー（作らない）。
    if not reports_dir.is_dir():
        print(
            f"--reports-dir がディレクトリとして存在しない: {reports_dir}",
            file=sys.stderr,
        )
        return 1

    targets, skipped_links = _collect_targets(reports_dir, _selected_prefixes(phases))
    archive_dir = reports_dir / ARCHIVE_DIR_NAME

    if targets:
        # `c3 init` 直後の利用先には archive/ が無い。作るのは archive/ 自身のみ（INV-2）。
        try:
            archive_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"アーカイブ先を作成できない: {archive_dir} ({exc})", file=sys.stderr)
            return 1

    moved = 0
    renamed: list[tuple[str, str]] = []
    failures: list[tuple[str, str]] = []
    for src in targets:
        try:
            dst = _move_one(src, archive_dir)
        except Exception as exc:  # noqa: BLE001 - 1 件の失敗で残りを打ち切らない（ADR-4）
            failures.append((src.name, _one_line(exc)))
            continue
        moved += 1
        if dst is not None and Path(dst).name != src.name:
            renamed.append((src.name, Path(dst).name))

    print(f"アーカイブ先: {archive_dir}")
    print(f"移動: {moved} 件 / リネーム: {len(renamed)} 件 / 失敗: {len(failures)} 件")
    for old_name, new_name in renamed:
        print(f"  リネーム: {old_name} -> {new_name}")
    for link in skipped_links:
        print(f"  スキップ（シンボリックリンク）: {link.name}")

    if failures:
        for name, reason in failures:
            print(f"FAILED\t{name}\t{reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
