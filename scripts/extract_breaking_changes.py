"""scripts/extract_breaking_changes.py

CHANGELOG.md の「### 破壊的変更」セクションを抽出し、
`.claude/breaking-changes.txt` に未記載のエントリを追加するためのスクリプト。

使い方:
  python scripts/extract_breaking_changes.py          対話モードで追記
  python scripts/extract_breaking_changes.py --check   未記載があれば exit 1
  python scripts/extract_breaking_changes.py --dry-run 候補表示のみ（書き込まない）

配布対象外（scripts/ は wheel に含まれない）。リリース前の人間 + CI 確認用。

設計判断 (architecture-report §10-5):
  - CHANGELOG.md の各 H2 セクション (`## [X.Y.Z]`) を section delimiter として使用
  - `### 破壊的変更` で始まるサブセクションがある version を抽出対象とする
  - en サマリはユーザー入力必須（空入力はエラー、Q-09 確定）
  - atomic write (tmp + os.replace) で追記する
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from pathlib import Path


# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

# プロジェクトルートからの相対パス（このスクリプトは scripts/ に置かれる）
# L-04: resolve() で symlink / 相対パスの曖昧さを解消する
_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHANGELOG_PATH = _REPO_ROOT / "CHANGELOG.md"
_BC_PATH = _REPO_ROOT / ".claude" / "breaking-changes.txt"

# CHANGELOG の H2 セクションヘッダ: `## [X.Y.Z] - YYYY-MM-DD`
_H2_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - (\d{4}-\d{2}-\d{2})$")

# SemVer 簡易バリデーション: X.Y.Z 純粋形式のみ（pre-release / build metadata は拒否）
# cli_update._compare_versions の SemVer 解釈と一貫させる（M-05）
# 末尾 `$` を含めない代わりに使用側で .fullmatch() を呼ぶことで意図を明確化（R2-L-01）
_SEMVER_RE = re.compile(r"\d+\.\d+\.\d+")

# 破壊的変更サブセクションの開始行: `### 破壊的変更` で始まる
_BREAKING_RE = re.compile(r"^### 破壊的変更")

# 次の H2 または H3 の開始（破壊的変更セクションの終端検出用）
_NEXT_SECTION_RE = re.compile(r"^## |^### ")


# ---------------------------------------------------------------------------
# CHANGELOG パーサー
# ---------------------------------------------------------------------------

def _parse_changelog_breaking_versions(changelog_path: Path) -> list[tuple[str, str]]:
    """CHANGELOG.md から「### 破壊的変更」セクションを持つ version の一覧を返す。

    Returns:
        [(version, date), ...] のリスト（CHANGELOG 記載順）
        version は "X.Y.Z" 形式（先頭 'v' なし）
    """
    try:
        text = changelog_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"エラー: CHANGELOG.md の読み込みに失敗しました: {exc}", file=sys.stderr)
        sys.exit(1)

    results: list[tuple[str, str]] = []
    lines = text.splitlines()

    current_version: str | None = None
    current_date: str | None = None
    in_breaking_section = False
    has_breaking = False

    for line in lines:
        h2_match = _H2_RE.match(line)
        if h2_match:
            # 前の section の確認
            if current_version and has_breaking:
                results.append((current_version, current_date or ""))
            # 新しい section 開始
            current_version = h2_match.group(1)
            current_date = h2_match.group(2)
            in_breaking_section = False
            has_breaking = False
            continue

        if current_version is None:
            continue

        if _BREAKING_RE.match(line):
            in_breaking_section = True
            has_breaking = True
            continue

        # 次の H2 または H3 に到達したら破壊的変更セクション終了
        if in_breaking_section and _NEXT_SECTION_RE.match(line):
            in_breaking_section = False

    # 最後のセクション
    if current_version and has_breaking:
        results.append((current_version, current_date or ""))

    return results


# ---------------------------------------------------------------------------
# breaking-changes.txt パーサー
# ---------------------------------------------------------------------------

def _load_recorded_versions(bc_path: Path) -> set[str]:
    """breaking-changes.txt に記録済みの version セットを返す。

    Returns:
        "X.Y.Z" 形式の version 文字列セット（先頭 'v' strip 済み）
        ファイル不在時は空集合を返す
    """
    if not bc_path.exists():
        return set()

    try:
        text = bc_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"エラー: breaking-changes.txt の読み込みに失敗しました: {exc}", file=sys.stderr)
        sys.exit(1)

    seen: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split("|", maxsplit=1)
        if parts:
            version = parts[0].strip().lstrip("v")
            # SemVer 簡易バリデーション: cli_update._load_breaking_changes と挙動を揃える（M-05）
            # SemVer 不適合は黙って skip（cli_update 側で warn される）
            if version and _SEMVER_RE.fullmatch(version):
                seen.add(version)
    return seen


# ---------------------------------------------------------------------------
# 追記処理
# ---------------------------------------------------------------------------

def _append_entry(bc_path: Path, version: str, en: str, ja: str) -> None:
    """breaking-changes.txt に 1 エントリを atomic write で追記する。

    Args:
        bc_path:  書き込み先ファイルパス
        version:  "X.Y.Z" 形式のバージョン（先頭 'v' なし）
        en:       英語サマリ（必須、ユーザー入力済み）
        ja:       日本語サマリ（任意、空欄可）
    """
    # 既存内容を読み込む（新規作成も考慮）
    if bc_path.exists():
        try:
            existing = bc_path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"エラー: ファイル読み込み失敗: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        existing = "# C3 breaking changes log\n# Format: vX.Y.Z|<English summary>|<Japanese summary>\n# Lines starting with '#' are comments. Blank lines ignored.\n\n"

    # trailing newline を揃えた上で追記
    if existing and not existing.endswith("\n"):
        existing += "\n"

    new_line = f"v{version}|{en}|{ja}\n"
    new_content = existing + new_line

    # atomic write: tmp ファイルに書いて os.replace
    # R2-M-01 / SR N-01: PID + uuid4 の組み合わせで並走時の tmp パス衝突を実質ゼロにする
    # cli_update._save_version_checkpoint の F-02 修正と一貫させる
    tmp = bc_path.with_name(f"breaking-changes.txt.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        bc_path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(new_content, encoding="utf-8")
        os.replace(tmp, bc_path)
    except OSError as exc:
        print(f"エラー: ファイル書き込み失敗: {exc}", file=sys.stderr)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        sys.exit(1)


# ---------------------------------------------------------------------------
# 制御文字サニタイズ（対話入力用）
# ---------------------------------------------------------------------------

# 注: \x1b (ESC, 0x1B) は \x0e-\x1f (0x0E-0x1F) 範囲内に含まれるため、
#     本パターンで確実に除去される (security-review F-04 確認済み)。
_DISALLOWED_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_input(text: str) -> str:
    """対話入力から制御文字を除去する（newline / tab / CR は除く）。"""
    return _DISALLOWED_CTRL_RE.sub("", text)


def _contains_pipe(text: str) -> bool:
    """入力テキストに pipe 文字 ('|') が含まれるか判定する。

    F-01: pipe は breaking-changes.txt のフィールド区切り文字であるため、
    en / ja フィールドに含めると次回 split 時にフィールド意味が壊れる可能性がある。
    入力レベルで reject することで _append_entry の整合性を保証する。
    テスト可能性のため独立した純粋関数として切り出している。
    """
    return "|" in text


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------

def main() -> int:
    """スクリプトのエントリポイント。

    Returns:
        終了コード: 0 = 成功 / 1 = 未記載あり (--check) またはエラー
    """
    parser = argparse.ArgumentParser(
        description="CHANGELOG.md から破壊的変更を抽出し breaking-changes.txt に追記する",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="未記載 version があれば exit 1 で終了する（CI 用）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="候補を表示するのみで書き込まない",
    )
    args = parser.parse_args()

    # CHANGELOG の破壊的変更 version を取得
    breaking_versions = _parse_changelog_breaking_versions(_CHANGELOG_PATH)

    if not breaking_versions:
        if args.check:
            print("CHANGELOG.md に「### 破壊的変更」セクションが見つかりませんでした。", file=sys.stderr)
            return 0
        print("CHANGELOG.md に「### 破壊的変更」セクションが見つかりませんでした。")
        return 0

    # 既に記録済みの version セットを取得
    recorded = _load_recorded_versions(_BC_PATH)

    # 未記載 version を差分抽出
    missing: list[tuple[str, str]] = [
        (ver, date) for ver, date in breaking_versions if ver not in recorded
    ]

    if not missing:
        if args.check:
            print("全ての破壊的変更が breaking-changes.txt に記載されています。")
            return 0
        print("全ての破壊的変更が breaking-changes.txt に記載されています。未追記はありません。")
        return 0

    # 未記載 version がある
    if args.check:
        print(
            f"未記載の破壊的変更が {len(missing)} 件あります:",
            file=sys.stderr,
        )
        for ver, date in missing:
            print(f"  - v{ver} ({date})", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"未記載の破壊的変更が {len(missing)} 件あります（--dry-run: 書き込まない）:")
        for ver, date in missing:
            print(f"  - v{ver} ({date})")
        return 0

    # 対話モード: en サマリを入力させて追記
    print(f"未記載の破壊的変更が {len(missing)} 件あります。順番に英語サマリを入力してください。")
    print("（空 Enter はエラー、Ctrl+C で中断）")
    print()

    for ver, date in missing:
        print(f"--- v{ver} ({date}) ---")
        print(f"CHANGELOG 参照: {_CHANGELOG_PATH}")
        print()

        while True:
            try:
                en_raw = input("英語サマリ (必須): ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n中断しました。", file=sys.stderr)
                return 1

            if not en_raw:
                print("エラー: 英語サマリは必須です。空欄にできません。", file=sys.stderr)
                continue
            # F-01: pipe 文字は フィールド区切り文字のため入力レベルで reject する
            if _contains_pipe(en_raw):
                print("エラー: 英語サマリに '|' は使用できません（フィールド区切り文字）。", file=sys.stderr)
                continue
            break

        while True:
            try:
                ja_raw = input("日本語サマリ (任意、Enter でスキップ): ").strip()
            except (EOFError, KeyboardInterrupt):
                ja_raw = ""
                break
            # F-01: ja も pipe を reject する（フィールド意味の破綻防止）
            if _contains_pipe(ja_raw):
                print("エラー: 日本語サマリに '|' は使用できません（フィールド区切り文字）。", file=sys.stderr)
                continue
            break

        en = _sanitize_input(en_raw)
        ja = _sanitize_input(ja_raw)

        _append_entry(_BC_PATH, ver, en, ja)
        print(f"  追記しました: v{ver}|{en}|{ja}")
        print()

    print(f"{len(missing)} 件を breaking-changes.txt に追記しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
