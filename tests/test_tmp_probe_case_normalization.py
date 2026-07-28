"""
一時調査テスト（マージ後に削除する）— 大小無視 FS でのパス正準化の実挙動を CI で実測する.

## 背景

`_validate_deletion_path` step 13 の init-only 保護は
`resolved.relative_to(claude_root).as_posix()` を `is_init_only()` に渡すことで
表記ゆれ耐性を得ている。Windows では大小違い（`.GITIGNORE`）でも保護されるが、
2026-07-29 の 3 OS CI（run 30384425768）で **macOS では保護が素通りして削除される**
ことが判明した（`deletions: 2 deleted, 0 warning(s)`）。

原因は「`Path.resolve()` が実在ファイルの on-disk ケースを返す」という前提が
Windows 固有（`nt._getfinalpathname`）であることと推測されるが、**macOS 環境が手元に無いため
推測のまま修正案を書かない**。本テストは候補実装それぞれの実挙動を CI ログへ出力させ、
どれが OS 非依存に効くかを実測で確定させるためのもの。

design-critic DC-AS-004 が「この前提はコードにも文書にも残らない」と警告した箇所であり、
親が docstring への記載だけで対応し **どの OS で成立するかを検証しなかった** ことが
今回の見落としの直接原因。

## 使い方

CI（3 OS）で実行し、`-s` 無しでも見えるよう assert メッセージではなく
`pytest` の PASSED 出力に載せるため warning 経由で出力する。
結果を確認したらこのファイルごと削除する。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


def _is_case_insensitive(tmp_path: Path) -> bool:
    probe = tmp_path / "probe_lower.txt"
    probe.write_text("x", encoding="utf-8")
    return (tmp_path / "PROBE_LOWER.TXT").exists()


def _report(lines: list[str]) -> None:
    """CI ログへ確実に出すため stderr へ書く（pytest は失敗時以外 stdout を畳む）."""
    sys.stderr.write("\n===== CASE-NORMALIZATION PROBE =====\n")
    for ln in lines:
        sys.stderr.write(ln + "\n")
    sys.stderr.write("===== END PROBE =====\n")
    sys.stderr.flush()


def test_probe_case_normalization_behaviour(tmp_path):
    """候補 3 実装が大小違い入力をどう解決するかを OS ごとに実測する（常に PASS）."""
    claude_root = (tmp_path / ".claude").resolve()
    target_dir = claude_root / "rules" / "promoted"
    target_dir.mkdir(parents=True)
    (target_dir / "index.md").write_text("# Promoted Rules\n", encoding="utf-8")

    variant = "RULES/PROMOTED/INDEX.MD"
    canonical = "rules/promoted/index.md"

    lines = [
        f"platform            = {sys.platform}",
        f"case_insensitive_fs = {_is_case_insensitive(tmp_path)}",
        f"variant             = {variant!r}",
        f"canonical           = {canonical!r}",
    ]

    # --- 候補 A: 現行実装（resolve() の実体ケース正準化に依存） ---
    resolved = (claude_root / variant).resolve()
    try:
        rel_a = resolved.relative_to(claude_root).as_posix()
    except ValueError as exc:
        rel_a = f"<ValueError: {exc}>"
    lines.append(f"A resolve().relative_to = {rel_a!r}  -> match={rel_a == canonical}")

    # --- 候補 B: os.path.normcase（POSIX では恒等関数の可能性） ---
    norm_b = os.path.normcase(variant)
    lines.append(f"B os.path.normcase      = {norm_b!r}  -> match={norm_b == canonical}")

    # --- 候補 C: 実ファイル列挙で on-disk の実体名へ突合 ---
    def _resolve_by_listing(root: Path, rel_posix: str) -> str | None:
        cur = root
        parts = [p for p in rel_posix.split("/") if p not in ("", ".")]
        out: list[str] = []
        for part in parts:
            if not cur.is_dir():
                return None
            hit = None
            for entry in cur.iterdir():
                if entry.name == part:
                    hit = entry.name
                    break
                if entry.name.lower() == part.lower():
                    hit = entry.name
            if hit is None:
                return None
            out.append(hit)
            cur = cur / hit
        return "/".join(out)

    rel_c = _resolve_by_listing(claude_root, variant)
    lines.append(f"C listing-based         = {rel_c!r}  -> match={rel_c == canonical}")

    # --- 参考: exists() が大小違いで通るか（保護が必要かの前提） ---
    lines.append(f"exists(variant)         = {(claude_root / variant).exists()}")

    _report(lines)

    # 本テストは調査用のため常に成功させる（CI を赤くしない）
    assert True
