"""Tests for mode_line.py — moード行の有効性判定（T2 引用符対応・M-1～M-7）。

architecture-report-20260724-000131.md §2-9 / test-report-20260724-084410.md §2-2
の仕様に基づく。_extract_plan_path と _classify_mode_line を直接呼び、
引用符対応のロジックを回帰検出する。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MODE_LINE_SCRIPT = (
    REPO_ROOT / ".claude" / "skills" / "autonomous-mode" / "scripts" / "mode_line.py"
)


def _load_mode_line_module():
    if not MODE_LINE_SCRIPT.is_file():
        raise FileNotFoundError(f"mode_line.py not found: {MODE_LINE_SCRIPT}")
    spec = importlib.util.spec_from_file_location(
        "_mode_line_under_test_mode_line", MODE_LINE_SCRIPT
    )
    assert spec and spec.loader, f"could not build import spec for {MODE_LINE_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# M-1: 引用符あり・スペースなし
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not MODE_LINE_SCRIPT.is_file(),
    reason="autonomous-mode は配布元ローカル限定のため CI では skip"
)
def test_mode_line_quoted_path_without_spaces(tmp_path, monkeypatch):
    """M-1: 引用符あり・スペースなし の場合、正規化パスが返される。

    期待値: (True, "<正規化済み絶対パス>")
    """
    mode_line = _load_mode_line_module()
    allowed_root = tmp_path / "plans"
    allowed_root.mkdir()
    plan_file = allowed_root / "dazzling.md"
    plan_file.write_text("# plan", encoding="utf-8")
    monkeypatch.setattr(mode_line, "DEFAULT_ALLOWED_ROOT", str(allowed_root))

    # 引用符付きパス
    line = f'モード: 自律 plan="{plan_file}"'
    valid, detail = mode_line._classify_mode_line(line)
    assert valid is True
    assert os.path.realpath(detail) == os.path.realpath(str(plan_file))


# ---------------------------------------------------------------------------
# M-2: 引用符あり・スペースあり
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not MODE_LINE_SCRIPT.is_file(),
    reason="autonomous-mode は配布元ローカル限定のため CI では skip"
)
def test_mode_line_quoted_path_with_spaces(tmp_path, monkeypatch):
    """M-2: 引用符あり・スペースあり の場合、スペース入りパスが正規化されて返される。

    期待値: (True, "<スペース入りパスの正規化絶対パス>")
    """
    mode_line = _load_mode_line_module()
    allowed_root = tmp_path / "plans"
    allowed_root.mkdir()
    plan_file = allowed_root / "my plan.md"
    plan_file.write_text("# plan", encoding="utf-8")
    monkeypatch.setattr(mode_line, "DEFAULT_ALLOWED_ROOT", str(allowed_root))

    # スペース入りパスを引用符で囲む
    line = f'モード: 自律 plan="{plan_file}"'
    valid, detail = mode_line._classify_mode_line(line)
    assert valid is True
    assert os.path.realpath(detail) == os.path.realpath(str(plan_file))


# ---------------------------------------------------------------------------
# M-3: 引用符あり + cycles= は閉じ引用符の後
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not MODE_LINE_SCRIPT.is_file(),
    reason="autonomous-mode は配布元ローカル限定のため CI では skip"
)
def test_mode_line_quoted_path_with_cycles_after_quote(tmp_path, monkeypatch):
    """M-3: 引用符で囲まれたパスの後に cycles= がある場合、plan 値に cycles が混入しない。

    期待値: (True, "<スペース入りパスの正規化絶対パス>")（cycles は plan 値に含まれない）
    """
    mode_line = _load_mode_line_module()
    allowed_root = tmp_path / "plans"
    allowed_root.mkdir()
    plan_file = allowed_root / "my plan.md"
    plan_file.write_text("# plan", encoding="utf-8")
    monkeypatch.setattr(mode_line, "DEFAULT_ALLOWED_ROOT", str(allowed_root))

    # 引用符 + cycles= トークン
    line = f'モード: 自律 plan="{plan_file}" cycles=C-3/2,E/1'
    valid, detail = mode_line._classify_mode_line(line)
    assert valid is True
    assert os.path.realpath(detail) == os.path.realpath(str(plan_file))


# ---------------------------------------------------------------------------
# M-4: 引用符閉じ忘れ → unclosed_quote
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not MODE_LINE_SCRIPT.is_file(),
    reason="autonomous-mode は配布元ローカル限定のため CI では skip"
)
def test_mode_line_unclosed_quote(tmp_path, monkeypatch):
    """M-4: 引用符を閉じ忘れた場合、理由コード unclosed_quote が返される。

    期待値: (False, "unclosed_quote")
    """
    mode_line = _load_mode_line_module()
    allowed_root = tmp_path / "plans"
    allowed_root.mkdir()
    plan_file = allowed_root / "my plan.md"
    plan_file.write_text("# plan", encoding="utf-8")
    monkeypatch.setattr(mode_line, "DEFAULT_ALLOWED_ROOT", str(allowed_root))

    # 閉じ引用符なし
    line = f'モード: 自律 plan="{plan_file}'
    valid, detail = mode_line._classify_mode_line(line)
    assert valid is False
    assert detail == "unclosed_quote"


# ---------------------------------------------------------------------------
# M-5: 引用符内に cycles= 偽装文字列
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not MODE_LINE_SCRIPT.is_file(),
    reason="autonomous-mode は配布元ローカル限定のため CI では skip"
)
def test_mode_line_quoted_path_with_embedded_cycles_string(tmp_path, monkeypatch):
    """M-5: ファイル名自体に " cycles=" が含まれている場合、引用符優先で正しく扱われる。

    期待値: (True, "<ファイル名を含むパスの正規化絶対パス>")
    """
    mode_line = _load_mode_line_module()
    allowed_root = tmp_path / "plans"
    allowed_root.mkdir()
    # ファイル名にスペースと "cycles=" 相当の文字列を含む
    plan_file = allowed_root / "fake cycles=C-3-99.md"
    plan_file.write_text("# plan", encoding="utf-8")
    monkeypatch.setattr(mode_line, "DEFAULT_ALLOWED_ROOT", str(allowed_root))

    # 引用符で囲むことで、ファイル名内の " cycles=" が誤実装されない
    line = f'モード: 自律 plan="{plan_file}"'
    valid, detail = mode_line._classify_mode_line(line)
    assert valid is True
    assert os.path.realpath(detail) == os.path.realpath(str(plan_file))


# ---------------------------------------------------------------------------
# M-6: 従来形（引用符なし）後方互換一式・代表例
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not MODE_LINE_SCRIPT.is_file(),
    reason="autonomous-mode は配布元ローカル限定のため CI では skip"
)
def test_mode_line_unquoted_valid_path(tmp_path, monkeypatch):
    """M-6a: 従来形（引用符なし）・有効なパスは現状どおり VALID。"""
    mode_line = _load_mode_line_module()
    allowed_root = tmp_path / "plans"
    allowed_root.mkdir()
    plan_file = allowed_root / "dazzling-cooking-dusk.md"
    plan_file.write_text("# plan", encoding="utf-8")
    monkeypatch.setattr(mode_line, "DEFAULT_ALLOWED_ROOT", str(allowed_root))

    line = f"モード: 自律 plan={plan_file}"
    valid, detail = mode_line._classify_mode_line(line)
    assert valid is True
    assert os.path.realpath(detail) == os.path.realpath(str(plan_file))


@pytest.mark.skipif(
    not MODE_LINE_SCRIPT.is_file(),
    reason="autonomous-mode は配布元ローカル限定のため CI では skip"
)
def test_mode_line_unquoted_missing_plan_token(tmp_path, monkeypatch):
    """M-6b: 従来形・plan= 欠落は現状どおり no_plan_token。"""
    mode_line = _load_mode_line_module()
    allowed_root = tmp_path / "plans"
    allowed_root.mkdir()
    monkeypatch.setattr(mode_line, "DEFAULT_ALLOWED_ROOT", str(allowed_root))

    # "モード: 自律 " の後に plan= 以外のトークン
    line = "モード: 自律 cycles=C-3/1"
    valid, detail = mode_line._classify_mode_line(line)
    assert valid is False
    assert detail == "no_plan_token"


@pytest.mark.skipif(
    not MODE_LINE_SCRIPT.is_file(),
    reason="autonomous-mode は配布元ローカル限定のため CI では skip"
)
def test_mode_line_unquoted_nonexistent_plan(tmp_path, monkeypatch):
    """M-6c: 従来形・実在しないパスは現状どおり not_found。"""
    mode_line = _load_mode_line_module()
    allowed_root = tmp_path / "plans"
    allowed_root.mkdir()
    monkeypatch.setattr(mode_line, "DEFAULT_ALLOWED_ROOT", str(allowed_root))

    missing = allowed_root / "does-not-exist.md"
    line = f"モード: 自律 plan={missing}"
    valid, detail = mode_line._classify_mode_line(line)
    assert valid is False
    assert detail == "not_found"


@pytest.mark.skipif(
    not MODE_LINE_SCRIPT.is_file(),
    reason="autonomous-mode は配布元ローカル限定のため CI では skip"
)
def test_mode_line_unquoted_outside_allowed_root(tmp_path, monkeypatch):
    """M-6d: 従来形・許可ルート外は現状どおり outside_allowed_root。"""
    mode_line = _load_mode_line_module()
    allowed_root = tmp_path / "plans"
    allowed_root.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_plan = outside_dir / "sneaky.md"
    outside_plan.write_text("# plan", encoding="utf-8")
    monkeypatch.setattr(mode_line, "DEFAULT_ALLOWED_ROOT", str(allowed_root))

    line = f"モード: 自律 plan={outside_plan}"
    valid, detail = mode_line._classify_mode_line(line)
    assert valid is False
    assert detail == "outside_allowed_root"


# ---------------------------------------------------------------------------
# M-7: C-3/5+2+1 表記の非干渉
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not MODE_LINE_SCRIPT.is_file(),
    reason="autonomous-mode は配布元ローカル限定のため CI では skip"
)
def test_mode_line_cycles_c3_5_2_1_noninterference(tmp_path, monkeypatch):
    """M-7: C-3/5+2+1 表記を含むモード行は、plan 判定に影響しない（非干渉）。

    期待値: (True, "<正規化絶対パス>")
    """
    mode_line = _load_mode_line_module()
    allowed_root = tmp_path / "plans"
    allowed_root.mkdir()
    plan_file = allowed_root / "dazzling-cooking-dusk.md"
    plan_file.write_text("# plan", encoding="utf-8")
    monkeypatch.setattr(mode_line, "DEFAULT_ALLOWED_ROOT", str(allowed_root))

    # C-3/5+2+1 表記（差し戻し履歴を含む cycles）
    line = f"モード: 自律 plan={plan_file} cycles=C-3/5+2+1,E/3"
    valid, detail = mode_line._classify_mode_line(line)
    assert valid is True
    assert os.path.realpath(detail) == os.path.realpath(str(plan_file))


# ---------------------------------------------------------------------------
# CLI 呼び出し: 引用符ケースの最小検証（計画中の M-1 相当）
# ---------------------------------------------------------------------------

def _run_mode_line_cli(mode_line_text: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(MODE_LINE_SCRIPT)],
        input=mode_line_text,
        capture_output=True,
        encoding="utf-8",
        env=env,
    )


@pytest.mark.skipif(
    not MODE_LINE_SCRIPT.is_file(),
    reason="autonomous-mode は配布元ローカル限定のため CI では skip"
)
def test_mode_line_cli_quoted_path_valid(tmp_path):
    """CLI 呼び出し: 引用符付きパスは VALID + 正規化パス + exit 0。

    計画 §2-4 の「CLI 版を最低 1 件（M-1 相当）追加」の実装。
    """
    fake_home = tmp_path
    allowed_root = fake_home / ".claude" / "plans"
    allowed_root.mkdir(parents=True)
    plan_file = allowed_root / "dazzling.md"
    plan_file.write_text("# dummy plan", encoding="utf-8")

    env = dict(os.environ)
    env["USERPROFILE"] = str(fake_home)
    env["HOME"] = str(fake_home)

    # CLI に引用符付きパスを渡す
    result = _run_mode_line_cli(f'モード: 自律 plan="{plan_file}"', env=env)
    assert result.returncode == 0, result.stderr
    line = result.stdout.rstrip("\r\n")
    status, _, detail = line.partition("\t")
    assert status == "VALID"
    assert os.path.realpath(detail) == os.path.realpath(str(plan_file))
