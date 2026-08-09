#!/usr/bin/env python3
"""`.claude/reports/*.md` を `.claude/reports/archive/` へ安全に移動する。

`/start` の Step 0（レポート整理）から `c3 run` 経由で呼ばれる。

設計（SSOT）: `.claude/reports/architecture-report-20260809-154124.md`（改訂 4）
  - INV-1: 既存ファイルを 1 バイトも変更しない（移動先の名前を排他生成で確保する）
  - INV-2: `archive/` はフラットのまま（サブディレクトリを作らない）
  - INV-3: アーカイブの実装はこの 1 箇所（SKILL.md 側にロジックを置かない）
  - INV-4: 移動先はバイト同一（読み書きはバイナリ I/O のみ・テキスト変換を経由しない）

挙動:
  - 移動先に同名が既にある場合は、移動元を
    `{stem}-archived-{移動元 mtime:YYYYMMDD-HHMMSS}{suffix}` へリネームして移動する（ADR-2）。
    その名前も埋まっていれば `-2` / `-3` … と空きを探す（上限 `MAX_CANDIDATE_NAMES`）。
    既存ファイルは決して上書きしない
  - 移動先の mtime は移動元の値へ復元する（`recall` の再構築誘発と版情報の消失を防ぐ）
  - 1 件失敗しても残りは処理し、末尾で stderr に失敗一覧を出して exit 1 にする（ロールバックしない）
  - 失敗した 1 件は移動元を消さない（残す方に倒す）

改訂 4（フェーズ E レビュー差し戻し）で加わった規定:
  - SR-V-002: `{reports-dir}/archive` が対象ディレクトリの外へ解決されるなら 1 件も処理せず exit 1
    （`mkdir` の前と `os.open` の前の 2 箇所で検査する）
  - SR-NEW-2: `src` を読む直前に `is_symlink()` を再判定し、真ならその 1 件を失敗にする
  - SR-AI-001: `--reports-dir` は環境変数 `C3_ARCHIVE_REPORTS_DIR_OK=1` がある場合のみ受け付ける
  - CR-E-005: 「コピー成功・移動元の削除だけ失敗」は `FAILED` ではなく `SOURCE_KEPT` として
    別分類で報告し、**移動先は削除しない**
  - SR-V-001: 衝突退避の候補生成に上限を設け、超過はその 1 件の失敗として扱う
  - SR-NEW-1: stdout / stderr に出す全てのファイル名・パス・失敗理由を `_safe()` に通す

改訂 5（フェーズ E 再レビュー・CR-NEW / SR-V-002 の再発）で加わった規定:
  - `archive_dir` の判定を `is_symlink()` から **realpath 封じ込め**へ置き換えた。
    `Path.is_symlink()` は NTFS ディレクトリジャンクション（`mklink /J` で管理者権限なしに
    作成できる）に `False` を返し素通りするため（実測: exit 0 の成功扱いで外部ディレクトリへ
    移動し移動元が削除された）。判定は「`os.path.realpath(archive_dir)` が
    `os.path.realpath(reports_dir)` 直下の `archive` と一致すること」（reparse の種別を問わない）。
    既存の解決パターン（`mode_line.py` の plan-path 検査）と同じ realpath 封じ込め方式に揃えた
  - `src` 側（ファイル）の `is_symlink()` 判定は据え置き（ジャンクションはディレクトリ専用で
    ファイルには張れないため影響を受けない）

Exit code（設計 §3-3。**戻り値で表し例外では表さない**）:
  0: 全対象の移動を確認できた（対象 0 件も 0）
  1: 1 件でも移動に失敗した、コピー済み・移動元が残存した、または引数が不正
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

# 設計 §3-3【改訂 4・SR-V-001】: 移動先名の候補生成は無限ではなく上限を持つ。
# 上限なしだと、退避名を大量に事前配置されたときに 1 件の移動が O(N) の `os.open` 試行を要する。
MAX_CANDIDATE_NAMES = 1000

# 設計 §3-3c【改訂 4・SR-AI-001】: `--reports-dir` を開ける env ゲート。
# `--reports-dir` は任意ディレクトリへの破壊操作（移動元の削除）を開く唯一の入口であり、
# `permissions.allow` の前方一致では塞ぎきれないため、env を実効的な関所にする。
REPORTS_DIR_OK_ENV = "C3_ARCHIVE_REPORTS_DIR_OK"
REPORTS_DIR_OK_VALUE = "1"

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
# 出力の無害化（設計 §3-3【改訂 4・SR-NEW-1】)
# ---------------------------------------------------------------------------


def _safe(value: object) -> str:
    """出力に載せる文字列から C0 制御文字と DEL を取り除く。

    stdout / stderr に出す**すべての**ファイル名・パス・失敗理由をこの helper に通す。
    `--reports-dir` は任意ディレクトリを指せるため、そこにあるファイル名は攻撃者が制御でき、
    生の ANSI エスケープが端末表示を偽装しうる。

    除去ではなく `\\x1b` 形の可視表現へ置換するのは、どの名前だったかの手掛かりを残すため。
    改行・タブも対象に含む: `FAILED` / `SOURCE_KEPT` 行は「1 件 1 行・タブ区切り」の契約であり、
    生の改行・タブが混ざると行とフィールドの構造そのものが壊れる。
    """
    text = str(value)
    return "".join(
        f"\\x{ord(char):02x}" if ord(char) < 0x20 or ord(char) == 0x7F else char
        for char in text
    )


def _one_line(exc: BaseException) -> str:
    """失敗理由を 1 行に潰す（失敗一覧は 1 件 1 行の契約のため）。

    `_safe()` が改行・タブを含む制御文字を可視表現へ置換するため、この戻り値は
    そのまま `FAILED` / `SOURCE_KEPT` 行のフィールドとして使える。
    """
    return _safe(f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# パス解決
# ---------------------------------------------------------------------------


def _archive_dir_is_contained(archive_dir: Path) -> bool:
    """`archive_dir` が対象ディレクトリ（その親）の直下の `archive` へ実際に解決されるか（設計 §3-3c 改訂 5）。

    `is_symlink()` は NTFS ディレクトリジャンクションに `False` を返し素通りするため判定に使えない
    （実測済み）。reparse point の種別を問わず塞ぐには、**実際に辿り着く先**を見るしかない。
    `archive_dir` は常に `reports_dir / ARCHIVE_DIR_NAME` として構築されるため、
    `archive_dir.parent` を対象ディレクトリとして扱える（`_move_one` は設計 §3-3b で
    引数を `src` / `archive_dir` の 2 個に固定しているため、`reports_dir` を別引数で渡さない）。

    既存の解決パターン（`mode_line.py` の plan-path 検査）と同じ `os.path.realpath` を使う。
    """
    expected = os.path.join(os.path.realpath(archive_dir.parent), ARCHIVE_DIR_NAME)
    return os.path.realpath(archive_dir) == expected


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

    【改訂 4・SR-V-001】候補は合計 `MAX_CANDIDATE_NAMES` 件で打ち切る（無限に produce しない）。
    枯渇した場合の扱いは呼び出し側（`_move_one`）が決める。
    """
    yield name
    pure = PurePath(name)
    base = f"{pure.stem}{ARCHIVED_MARKER}{_stamp(mtime)}"
    suffix = pure.suffix
    yield f"{base}{suffix}"
    for index in range(2, MAX_CANDIDATE_NAMES):
        yield f"{base}-{index}{suffix}"


# ---------------------------------------------------------------------------
# 1 件分の移動
# ---------------------------------------------------------------------------


class _SourceKeptError(Exception):
    """コピーは成功したが移動元の削除だけが失敗した（設計 §3-3【改訂 4・CR-E-005】）。

    この経路を通常の失敗（`FAILED`）に丸めると、**移動先には正しい内容が既に存在する**という
    実態を報告できず、再実行のたびに同一内容が別名で `archive/` に積み上がる。
    移動先は削除せず（バイト同一のコピーを捨てない）、`SOURCE_KEPT` として別分類で報告する。
    """

    def __init__(self, dst: Path, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.dst = dst
        self.cause = cause


def _move_one(src: Path, archive_dir: Path) -> Path:
    """`src` を `archive_dir` へ移動し、実際に書いた移動先パスを返す。

    設計 §3-3b は引数 2 個（`src` / `archive_dir`）を契約として固定している。戻り値は
    同 §3-3b では `None` と書かれているが、§3-3 の stdout 契約（リネーム時に旧名→新名を出す）を
    命名ロジックの重複なく満たすため移動先を返す形にした（INV-3: 命名の実装を 2 箇所に持たない）。

    失敗時は例外を送出し、**移動元を残す**（ADR-3）。中途半端に生成した移動先は削除する。
    ただしコピー完了後の `src.unlink()` だけが失敗した場合は `_SourceKeptError`（CR-E-005）。
    """
    # 【改訂 4・SR-NEW-2】列挙（`_collect_targets`）と読み取りの間に `src` が symlink へ
    # 差し替えられる窓を狭める。読む直前に再判定し、真ならこの 1 件を失敗にする。
    if src.is_symlink():
        raise RuntimeError(
            f"読み取り直前に対象がシンボリックリンクへ差し替えられた: {_safe(src.name)}"
        )

    info = src.stat()
    # INV-4: バイナリ I/O のみ。テキストモードを経由すると改行・encoding が変換される。
    data = src.read_bytes()

    # 【改訂 5・CR-NEW / SR-V-002 再発】`os.open` の前にも移動先を検査する。`O_EXCL` は
    # 最終コンポーネントにしか効かないため、親が reparse point だと移動先をすり替えたうえで
    # 移動元が削除される。`is_symlink()` は NTFS ジャンクションに素通りされるため
    # realpath 封じ込めで判定する（種別を問わない）。
    if not _archive_dir_is_contained(archive_dir):
        raise RuntimeError(
            f"アーカイブ先が対象ディレクトリの外へ解決される: {_safe(archive_dir)}"
        )

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

    if dst is None or fd < 0:
        # 【改訂 4・SR-V-001】候補は有限（`MAX_CANDIDATE_NAMES` 件）。枯渇はこの 1 件の失敗。
        # `assert` は `python -O` で無効化されるため `RuntimeError` で明示的に投げる
        # （`record_agent_outcome.py` / `review_hint_inject.py` と同じ理由）。
        raise RuntimeError(
            f"移動先の候補が上限 {MAX_CANDIDATE_NAMES} 件で尽きた: {_safe(src.name)}"
        )

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

    try:
        src.unlink()
    except OSError as exc:
        # 【改訂 4・CR-E-005】ここまでで移動先にはバイト同一のコピーが存在する。
        # 移動先は削除せず、`SOURCE_KEPT` として運用者が手で後始末できる形で報告する。
        raise _SourceKeptError(dst, exc) from exc
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


# ---------------------------------------------------------------------------
# 移動の実行と集計（CR-Q-002: `main()` は「検証 → 委譲 → 出力」に絞る）
# ---------------------------------------------------------------------------


class _ArchiveOutcome:
    """`_archive_all()` の結果。件数と、報告に必要な名前の組を持つ。

    `@dataclass` を使わないのは、本スクリプトが `importlib.util.spec_from_file_location` で
    `sys.modules` に登録せずロードされる（テスト・confirm のスタブ検査がこの形）ため。
    `from __future__ import annotations` と組み合わせると `dataclasses` が
    `sys.modules[cls.__module__]` を引けず `AttributeError` でロードごと落ちる（実測）。
    """

    def __init__(self) -> None:
        self.moved: int = 0
        self.renamed: list[tuple[str, str]] = []
        self.failures: list[tuple[str, str]] = []
        self.kept: list[tuple[str, str, str]] = []

    def has_problem(self) -> bool:
        """exit 1 にすべき状態か（失敗、またはコピー済み・移動元残存）。"""
        return bool(self.failures or self.kept)


def _archive_all(targets: list[Path], archive_dir: Path) -> _ArchiveOutcome:
    """対象を 1 件ずつ移動して集計する。

    ADR-4「1 件ごとの確認」／設計 §3-3「1 件失敗しても残りの対象は処理を続ける」。
    ロールバックはしない。
    """
    outcome = _ArchiveOutcome()
    for src in targets:
        try:
            dst = _move_one(src, archive_dir)
        except _SourceKeptError as exc:
            # CR-E-005: コピーは成功している。`FAILED` に丸めない。
            outcome.kept.append((src.name, exc.dst.name, _one_line(exc.cause)))
            continue
        except Exception as exc:  # noqa: BLE001 - 1 件の失敗で残りを打ち切らない（ADR-4）
            outcome.failures.append((src.name, _one_line(exc)))
            continue
        outcome.moved += 1
        if dst.name != src.name:
            outcome.renamed.append((src.name, dst.name))
    return outcome


def _report(outcome: _ArchiveOutcome, archive_dir: Path, skipped_links: list[Path]) -> None:
    """集計を stdout へ、失敗一覧を stderr へ出す（設計 §3-3）。

    出力に載る名前・パス・理由は例外なく `_safe()` を通す（SR-NEW-1）。
    """
    print(f"アーカイブ先: {_safe(archive_dir)}")
    print(
        f"移動: {outcome.moved} 件 / リネーム: {len(outcome.renamed)} 件"
        f" / 失敗: {len(outcome.failures)} 件"
        f" / コピー済み・移動元が残存: {len(outcome.kept)} 件"
    )
    for old_name, new_name in outcome.renamed:
        print(f"  リネーム: {_safe(old_name)} -> {_safe(new_name)}")
    for link in skipped_links:
        print(f"  スキップ（シンボリックリンク）: {_safe(link.name)}")

    for name, reason in outcome.failures:
        print(f"FAILED\t{_safe(name)}\t{reason}", file=sys.stderr)
    for name, dst_name, reason in outcome.kept:
        print(f"SOURCE_KEPT\t{_safe(name)}\t{_safe(dst_name)}\t{reason}", file=sys.stderr)


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
        help=(
            "reports directory (archive dir is always <PATH>/archive)."
            f" requires {REPORTS_DIR_OK_ENV}={REPORTS_DIR_OK_VALUE}"
        ),
    )
    return parser


def _resolve_reports_dir(raw: str | None) -> Path | None:
    """`--reports-dir` を解決する。ゲート未通過・不正なら `None` を返す（設計 §3-3c）。

    エラー内容は stderr へ出す。呼び出し側は `None` を exit 1 として扱う。
    """
    if raw is None:
        reports_dir = _default_reports_dir()
    else:
        # 【改訂 4・SR-AI-001】`--reports-dir` はテスト・検証専用であり、正規経路
        # （`start/SKILL.md`）は一切使わない。明示的な opt-in を関所にする。
        if os.environ.get(REPORTS_DIR_OK_ENV, "").strip() != REPORTS_DIR_OK_VALUE:
            print(
                f"--reports-dir は環境変数 {REPORTS_DIR_OK_ENV}={REPORTS_DIR_OK_VALUE} が"
                "設定されている場合のみ受け付ける（テスト・検証専用の引数）",
                file=sys.stderr,
            )
            return None
        reports_dir = Path(raw).resolve()

    # 設計 §3-3c: 存在しない / ディレクトリでない場合はエラー（作らない）。
    if not reports_dir.is_dir():
        print(
            f"対象ディレクトリがディレクトリとして存在しない: {_safe(reports_dir)}",
            file=sys.stderr,
        )
        return None
    return reports_dir


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # --help は 0、引数不正は 1（設計 §3-3）。例外のまま抜けさせない。
        return 0 if exc.code in (0, None) else 1

    # --- 検証 -------------------------------------------------------------
    phases: list[str] = list(args.phase or [])
    unknown = sorted({phase for phase in phases if phase not in PHASE_PREFIXES})
    if unknown:
        # 引数不正は検証段階で弾き、1 件も移動しない（部分適用を作らない・ADR-3）。
        # 一覧は list の repr のまま出す（人間可読の 1 行。機械可読な行集合ではない）。
        print(f"unknown --phase value(s): {_safe(unknown)}", file=sys.stderr)
        print(f"valid values: {sorted(PHASE_PREFIXES)}", file=sys.stderr)
        return 1

    reports_dir = _resolve_reports_dir(args.reports_dir)
    if reports_dir is None:
        return 1

    archive_dir = reports_dir / ARCHIVE_DIR_NAME
    # 【改訂 5・CR-NEW / SR-V-002 再発】`mkdir` の前に検査する。`mkdir(exist_ok=True)` は
    # リンク先が実在ディレクトリなら素通りするため、ここで止めないと移動先ごとすり替えられる。
    # `is_symlink()` は NTFS ジャンクションに `False` を返し素通りするため、
    # realpath 封じ込めで判定する（reparse の種別を問わない）。
    if not _archive_dir_is_contained(archive_dir):
        print(
            f"アーカイブ先が対象ディレクトリの外へ解決されるため 1 件も処理しない: {_safe(archive_dir)}",
            file=sys.stderr,
        )
        return 1

    targets, skipped_links = _collect_targets(reports_dir, _selected_prefixes(phases))

    if targets:
        # `c3 init` 直後の利用先には archive/ が無い。作るのは archive/ 自身のみ（INV-2）。
        try:
            archive_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(
                f"アーカイブ先を作成できない: {_safe(archive_dir)} ({_one_line(exc)})",
                file=sys.stderr,
            )
            return 1

    # --- 委譲 -------------------------------------------------------------
    outcome = _archive_all(targets, archive_dir)

    # --- 出力 -------------------------------------------------------------
    _report(outcome, archive_dir, skipped_links)
    return 1 if outcome.has_problem() else 0


if __name__ == "__main__":
    sys.exit(main())
