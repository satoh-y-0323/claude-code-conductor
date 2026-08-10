"""
Tests for .claude/hooks/stop.py — agent-memory の injection 予算接近を検知する warn.

背景:
  サブエージェントの MEMORY.md は起動時に **先頭 200 行 / 25KB まで**しか system prompt に
  載らない（公式 sub-agents.md「Enable persistent memory」）。超過分は読まれないため、
  過去にユーザーと合意した許容例外が window 外へ落ちて再指摘される
  （＝合意の実効的な巻き戻り）。

  patterns.json には肥大検知 warn（DESCRIPTION_WARN_LENGTH）があるが、
  agent-memory には機械防御が一切なかった（2026-07-28 security-review [SR-R-004]）。
  実測時点で security-reviewer/MEMORY.md は 21,479B ＝ 25KB 予算の 84% に達していた。

方針:
  - 超過後ではなく **80% 到達で警告**する（超過時点では既に巻き戻りが起きているため）
  - 削除・切り詰めはしない（警告のみ・read-only）＝「境界は硬く・中は柔らかく」
  - Stop hook を止めない（例外は握る）
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import stat
import sys
import tempfile
import types
from pathlib import Path

import pytest

WORKTREE_ROOT = Path(__file__).parent.parent
STOP_PY = WORKTREE_ROOT / ".claude" / "hooks" / "stop.py"


def _load_stop_module(module_name: str) -> types.ModuleType:
    """Load stop.py as a fresh module instance without registering in sys.modules."""
    spec = importlib.util.spec_from_file_location(module_name, STOP_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def stop_mod(tmp_path):
    """agent-memory の探索先を tmp_path に差し替えた stop モジュール."""
    mod = _load_stop_module("stop_for_agent_memory_warn")
    mod.AGENT_MEMORY_DIR = str(tmp_path / "agent-memory")
    return mod


def _write_memory(mod, agent: str, *, line_count: int, line_body: str) -> Path:
    """agent-memory/<agent>/MEMORY.md を任意の行数・行長で作る."""
    d = Path(mod.AGENT_MEMORY_DIR) / agent
    d.mkdir(parents=True, exist_ok=True)
    p = d / "MEMORY.md"
    p.write_text("\n".join(line_body for _ in range(line_count)), encoding="utf-8")
    return p


class TestThresholdConstants:
    """公式の injection 予算（200 行 / 25KB）と警告比率が定数化されていること."""

    def test_limits_match_official_spec(self, stop_mod):
        assert stop_mod.AGENT_MEMORY_LIMIT_BYTES == 25 * 1024
        assert stop_mod.AGENT_MEMORY_LIMIT_LINES == 200

    def test_warn_ratio_is_80_percent(self, stop_mod):
        assert stop_mod.AGENT_MEMORY_WARN_RATIO == 0.8


class TestWarnOversizedAgentMemory:
    def test_silent_below_threshold(self, stop_mod, capsys):
        # 100 行 × 10 バイト = 約 1KB・50 行 → 予算の数 % で静か
        _write_memory(stop_mod, "developer", line_count=50, line_body="x" * 10)
        stop_mod._warn_oversized_agent_memory()
        assert capsys.readouterr().err == ""

    def test_warns_when_bytes_reach_80_percent(self, stop_mod, capsys):
        # 21,000 バイト超（= 25KB の 82%）だが行数はわずか 21 行
        _write_memory(stop_mod, "security-reviewer", line_count=21, line_body="x" * 1000)
        stop_mod._warn_oversized_agent_memory()
        err = capsys.readouterr().err
        assert "security-reviewer" in err
        assert "MEMORY.md" in err

    def test_warns_when_lines_reach_80_percent(self, stop_mod, capsys):
        # 161 行（= 200 行の 80% 超）だがバイト数は 1KB 未満
        _write_memory(stop_mod, "tester", line_count=161, line_body="x")
        stop_mod._warn_oversized_agent_memory()
        err = capsys.readouterr().err
        assert "tester" in err

    def test_bytes_just_below_threshold_is_silent(self, stop_mod, capsys):
        # 20,000 バイト = 25KB の 78%（閾値 20,480B 未満）
        _write_memory(stop_mod, "code-reviewer", line_count=20, line_body="x" * 999)
        stop_mod._warn_oversized_agent_memory()
        assert capsys.readouterr().err == ""

    def test_reports_actual_measurements(self, stop_mod, capsys):
        """警告には実測値（バイト数・行数）が入り、何をどれだけ削ればよいか分かること."""
        _write_memory(stop_mod, "security-reviewer", line_count=21, line_body="x" * 1000)
        stop_mod._warn_oversized_agent_memory()
        err = capsys.readouterr().err
        assert "21" in err  # 行数または実バイト数の一部
        assert str(stop_mod.AGENT_MEMORY_LIMIT_BYTES) in err or "25" in err

    def test_each_oversized_file_is_one_line(self, stop_mod, capsys):
        _write_memory(stop_mod, "security-reviewer", line_count=21, line_body="x" * 1000)
        _write_memory(stop_mod, "tester", line_count=21, line_body="x" * 1000)
        stop_mod._warn_oversized_agent_memory()
        err_lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
        assert len(err_lines) == 2


class TestDefensiveBehaviour:
    def test_missing_agent_memory_dir_is_silent(self, stop_mod, capsys):
        # ディレクトリ自体が無い（利用先の初回起動時など）
        stop_mod._warn_oversized_agent_memory()
        assert capsys.readouterr().err == ""

    def test_subdir_without_memory_md_is_skipped(self, stop_mod, capsys):
        # wt_developer / wt_tester のように MEMORY.md 未生成のディレクトリがある
        (Path(stop_mod.AGENT_MEMORY_DIR) / "wt_developer").mkdir(parents=True)
        stop_mod._warn_oversized_agent_memory()
        assert capsys.readouterr().err == ""

    def test_stray_file_at_top_level_is_skipped(self, stop_mod, capsys):
        d = Path(stop_mod.AGENT_MEMORY_DIR)
        d.mkdir(parents=True)
        (d / ".gitkeep").write_text("", encoding="utf-8")
        stop_mod._warn_oversized_agent_memory()
        assert capsys.readouterr().err == ""

    def test_agent_name_is_sanitized_before_output(self, stop_mod, capsys, monkeypatch):
        """ディレクトリ名は表示前にサニタイズする（制御文字によるターミナル汚染防止）.

        Windows では制御文字を含むディレクトリを実際に作れないため listdir を差し替える。
        """
        real_dir = Path(stop_mod.AGENT_MEMORY_DIR) / "evil"
        real_dir.mkdir(parents=True)
        (real_dir / "MEMORY.md").write_text("x" * 21000, encoding="utf-8")

        real_join = stop_mod.os.path.join
        monkeypatch.setattr(stop_mod.os, "listdir", lambda _p: ["evil\x1b[31m\x00"])
        monkeypatch.setattr(
            stop_mod.os.path,
            "join",
            lambda *parts: real_join(*[p.replace("\x1b[31m\x00", "") for p in parts]),
        )

        stop_mod._warn_oversized_agent_memory()
        err = capsys.readouterr().err
        assert "\x1b" not in err
        assert "\x00" not in err

    def test_never_raises_on_unreadable_entry(self, stop_mod, monkeypatch):
        Path(stop_mod.AGENT_MEMORY_DIR).mkdir(parents=True)
        monkeypatch.setattr(stop_mod.os, "listdir", lambda _p: ["broken"])

        def _boom(*_a, **_k):
            raise OSError("unreadable")

        monkeypatch.setattr(stop_mod.os.path, "getsize", _boom)
        # 例外を送出しないこと（Stop hook を止めない）
        stop_mod._warn_oversized_agent_memory()


class TestBoundaryValues:
    """境界値（ちょうど 80%）が非発火であることを固定する [CR-T-001]."""

    def test_exactly_at_byte_threshold_is_silent(self, stop_mod, capsys):
        # ちょうど 20,480B（= 25KB の 80%）。strict greater なので鳴らない
        d = Path(stop_mod.AGENT_MEMORY_DIR) / "code-reviewer"
        d.mkdir(parents=True)
        (d / "MEMORY.md").write_text("x" * 20480, encoding="utf-8")
        stop_mod._warn_oversized_agent_memory()
        assert capsys.readouterr().err == ""

    def test_one_byte_over_threshold_warns(self, stop_mod, capsys):
        d = Path(stop_mod.AGENT_MEMORY_DIR) / "code-reviewer"
        d.mkdir(parents=True)
        (d / "MEMORY.md").write_text("x" * 20481, encoding="utf-8")
        stop_mod._warn_oversized_agent_memory()
        assert "code-reviewer" in capsys.readouterr().err

    def test_exactly_at_line_threshold_is_silent(self, stop_mod, capsys):
        # ちょうど 160 行（= 200 行の 80%）
        _write_memory(stop_mod, "tester", line_count=160, line_body="x")
        stop_mod._warn_oversized_agent_memory()
        assert capsys.readouterr().err == ""


class TestLineCounting:
    """行数カウントの正確さ [CR-NEW: off-by-one]."""

    def test_trailing_newline_does_not_inflate_count(self, stop_mod, capsys):
        """末尾改行ありのファイルで実際より 1 行多く数えないこと.

        実測（security-reviewer/MEMORY.md）で 153 カウント vs 実 152 行のズレがあった。
        全 agent-memory ファイルが末尾改行ありのため通常経路で常に踏む。
        """
        d = Path(stop_mod.AGENT_MEMORY_DIR) / "tester"
        d.mkdir(parents=True)
        # 実 161 行（末尾に改行あり）
        (d / "MEMORY.md").write_text("x\n" * 161, encoding="utf-8")
        stop_mod._warn_oversized_agent_memory()
        err = capsys.readouterr().err
        assert "161行" in err
        assert "162行" not in err

    def test_no_trailing_newline_counts_last_line(self, stop_mod, capsys):
        d = Path(stop_mod.AGENT_MEMORY_DIR) / "tester"
        d.mkdir(parents=True)
        # 実 161 行（末尾に改行なし）
        (d / "MEMORY.md").write_text("\n".join("x" for _ in range(161)), encoding="utf-8")
        stop_mod._warn_oversized_agent_memory()
        assert "161行" in capsys.readouterr().err


class TestReadCap:
    """行数カウントのための読み込みに上限があること [SR-NEW]."""

    def test_read_cap_constant_exists(self, stop_mod):
        assert stop_mod.AGENT_MEMORY_READ_CAP_BYTES >= stop_mod.AGENT_MEMORY_LIMIT_BYTES

    def test_huge_file_is_not_fully_read(self, stop_mod, capsys, monkeypatch):
        d = Path(stop_mod.AGENT_MEMORY_DIR) / "security-reviewer"
        d.mkdir(parents=True)
        (d / "MEMORY.md").write_text("x" * 200_000, encoding="utf-8")

        real_open = stop_mod.open if hasattr(stop_mod, "open") else open
        read_sizes = []

        class _SpyFile:
            def __init__(self, inner):
                self._inner = inner

            def read(self, n=-1):
                read_sizes.append(n)
                return self._inner.read(n)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._inner.close()
                return False

        def _spy_open(path, mode="r", *a, **k):
            return _SpyFile(real_open(path, mode, *a, **k))

        monkeypatch.setitem(stop_mod.__builtins__, "open", _spy_open) if isinstance(
            stop_mod.__builtins__, dict
        ) else monkeypatch.setattr(stop_mod.__builtins__, "open", _spy_open)

        stop_mod._warn_oversized_agent_memory()

        # 無条件 read()（引数なし = -1）で全体を読んでいないこと
        assert read_sizes, "open().read() が呼ばれていない"
        assert all(n is not None and n > 0 for n in read_sizes), f"読み込み上限なし: {read_sizes}"

    def test_truncated_file_reports_line_count_as_lower_bound(self, stop_mod, capsys):
        d = Path(stop_mod.AGENT_MEMORY_DIR) / "security-reviewer"
        d.mkdir(parents=True)
        (d / "MEMORY.md").write_text("x" * 200_000, encoding="utf-8")
        stop_mod._warn_oversized_agent_memory()
        err = capsys.readouterr().err
        assert "以上" in err  # 実数でなく下限であることが分かる表記
        assert "200000B" in err  # 実サイズ自体は getsize で正確に出す


class TestDisplaySanitize:
    """stderr へ出す値のサニタイズ [SR-V-001]."""

    def test_display_regex_removes_lf_and_cr(self, stop_mod):
        cleaned = stop_mod._DISPLAY_SANITIZE_RE.sub("", "a\nb\rc")
        assert cleaned == "abc"

    def test_display_regex_removes_bidi_and_zero_width(self, stop_mod):
        raw = "a‮b​c﻿d"
        cleaned = stop_mod._DISPLAY_SANITIZE_RE.sub("", raw)
        assert cleaned == "abcd"

    def test_warning_stays_single_line_with_newline_in_dir_name(
        self, stop_mod, capsys, monkeypatch
    ):
        """改行入りディレクトリ名でも 1 ファイル 1 行を維持すること."""
        real_dir = Path(stop_mod.AGENT_MEMORY_DIR) / "evil"
        real_dir.mkdir(parents=True)
        (real_dir / "MEMORY.md").write_text("x" * 21000, encoding="utf-8")

        real_join = stop_mod.os.path.join
        monkeypatch.setattr(stop_mod.os, "listdir", lambda _p: ["evil\n[Stop] 偽の警告"])
        monkeypatch.setattr(
            stop_mod.os.path,
            "join",
            lambda *parts: real_join(*[p.replace("\n[Stop] 偽の警告", "") for p in parts]),
        )

        stop_mod._warn_oversized_agent_memory()
        err_lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
        assert len(err_lines) == 1

    def test_patterns_json_pid_uses_display_sanitize(self, stop_mod, capsys):
        """patterns.json 側の pid 表示も同じ穴を持たないこと（SR 指摘の横展開）."""
        stop_mod._warn_oversized_descriptions(
            [{"id": "evil\n[Stop] 偽の警告", "description": "d" * (stop_mod.DESCRIPTION_WARN_LENGTH + 1)}]
        )
        err_lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
        assert len(err_lines) == 1


class TestWiredIntoRun:
    """run() から呼ばれていること（実装が孤立していないことの担保）."""

    def test_run_invokes_agent_memory_warn(self, stop_mod, monkeypatch, tmp_path):
        called = {"n": 0}

        def _spy():
            called["n"] += 1

        monkeypatch.setattr(stop_mod, "_warn_oversized_agent_memory", _spy)
        monkeypatch.setattr(stop_mod, "ensure_session_file", lambda _d: None)
        monkeypatch.setattr(stop_mod, "update_patterns", lambda _d: None)
        monkeypatch.setattr(stop_mod, "is_worktree", lambda _c: False)

        assert stop_mod.run({}) == 0
        assert called["n"] == 1

    def test_run_survives_warn_failure(self, stop_mod, monkeypatch):
        def _boom():
            raise RuntimeError("boom")

        monkeypatch.setattr(stop_mod, "_warn_oversized_agent_memory", _boom)
        monkeypatch.setattr(stop_mod, "ensure_session_file", lambda _d: None)
        monkeypatch.setattr(stop_mod, "update_patterns", lambda _d: None)
        monkeypatch.setattr(stop_mod, "is_worktree", lambda _c: False)

        # 警告処理が壊れても Stop は 0 で返る
        assert stop_mod.run({}) == 0


# ---------------------------------------------------------------------------
# S3 ⑦ agent-memory 肥大ガード拡張（2 しきい値制）
#
# 設計契約:
#   - MEMORY.md（各 agent 直下の索引）: 25KB / 200 行・injection 予算文言。挙動変更なし。
#   - 非 MEMORY.md（個別メモリファイル・topics/ 等サブディレクトリ含む再帰）:
#     新設定数 100KB **超過のみ** 警告。文言は Read / 保守コスト観点。
#
# 索引（MEMORY.md）は起動時に system prompt へ注入されるため予算が硬く 25KB だが、
# 個別メモリファイルは必要時に Read されるだけで注入されない。したがって「注入予算」
# ではなく「1 回の Read で払うトークン・保守コスト」が制約になり、しきい値は分離する。
# ---------------------------------------------------------------------------

# 期待する新設定数の値（実装側の定数名は AGENT_MEMORY_FILE_LIMIT_BYTES）
EXPECTED_FILE_LIMIT_BYTES = 100 * 1024


def _write_individual(mod, agent: str, rel: str, size: int) -> Path:
    """agent-memory/<agent>/<rel> に任意サイズの個別メモリファイルを作る.

    rel には "topics/foo.md" のようにサブディレクトリを含められる。
    """
    p = Path(mod.AGENT_MEMORY_DIR) / agent / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x" * size, encoding="utf-8")
    # 作成に失敗していると「機能未実装」と区別がつかない赤になるため、ここで確定させる
    assert p.stat().st_size == size
    return p


def _err_lines(capsys) -> list[str]:
    return [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]


def _only_line(capsys) -> str:
    """警告が 1 行だけ出ていることを確かめてその行を返す.

    素の [0] だと未実装時に IndexError になり「なぜ赤いのか」が読み取れないため、
    Red の失敗理由を明示するアサーションを噛ませる。
    """
    lines = _err_lines(capsys)
    assert len(lines) == 1, f"警告が 1 行だけ出ることを期待したが {len(lines)} 行だった: {lines}"
    return lines[0]


@pytest.fixture
def short_root_stop_mod():
    """ルートを短い一時ディレクトリに寄せた stop モジュール.

    pytest の tmp_path は 90 文字を超えることがあり、64 文字超のパス要素を
    2 つ重ねると Windows の MAX_PATH (260) にぶつかる。そのまま使うと
    「機能未実装」ではなく「テストがファイルを作れない」で赤くなるため、
    長い要素名を扱うテストだけルートを短く取り直す。
    """
    root = tempfile.mkdtemp(prefix="c3am")
    mod = _load_stop_module("stop_for_agent_memory_warn_short")
    mod.AGENT_MEMORY_DIR = os.path.join(root, "am")
    try:
        yield mod
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _rel_path_in(line: str) -> list[str]:
    """警告行から agent-memory 配下の相対パスを取り出し、要素リストに分解する.

    区切りは実装が os.sep を使う可能性があるため / と \\ の両方を許容する。
    """
    m = re.search(r"agent-memory[/\\](\S+)", line)
    assert m, f"警告行に agent-memory 配下のパスが含まれていない: {line!r}"
    return m.group(1).replace("\\", "/").split("/")


# --- Red 群 ---------------------------------------------------------------


class TestIndividualFileLimitConstant:
    """個別メモリファイル用の新しきい値が定数化されていること（Red）."""

    def test_individual_file_limit_is_100kb(self, stop_mod):
        assert stop_mod.AGENT_MEMORY_FILE_LIMIT_BYTES == EXPECTED_FILE_LIMIT_BYTES

    def test_individual_limit_is_separate_from_index_limit(self, stop_mod):
        """索引（MEMORY.md）の 25KB と混同されていないこと."""
        assert (
            stop_mod.AGENT_MEMORY_FILE_LIMIT_BYTES > stop_mod.AGENT_MEMORY_LIMIT_BYTES
        )


class TestIndividualFileWarn:
    """(1) 非 MEMORY.md が 100KB を超えたら警告する（Red）."""

    def test_warns_for_oversized_file_directly_under_agent_dir(self, stop_mod, capsys):
        _write_individual(
            stop_mod, "security-reviewer", "sr_checklist_notes.md",
            EXPECTED_FILE_LIMIT_BYTES + 1,
        )
        stop_mod._warn_oversized_agent_memory()
        err = capsys.readouterr().err
        assert "security-reviewer" in err
        assert "sr_checklist_notes" in err

    def test_warns_for_oversized_file_in_subdirectory(self, stop_mod, capsys):
        """topics/ のようなサブディレクトリ配下も走査対象であること."""
        _write_individual(
            stop_mod, "developer", "topics/big_topic.md",
            EXPECTED_FILE_LIMIT_BYTES + 1,
        )
        stop_mod._warn_oversized_agent_memory()
        err = capsys.readouterr().err
        assert "developer" in err
        assert "big_topic" in err

    def test_warns_for_deeply_nested_file(self, stop_mod, capsys):
        """再帰であること（1 階層だけの走査では通らない）."""
        _write_individual(
            stop_mod, "tester", "topics/2026/08/deep_note.md",
            EXPECTED_FILE_LIMIT_BYTES + 1,
        )
        stop_mod._warn_oversized_agent_memory()
        assert "deep_note" in capsys.readouterr().err

    def test_one_byte_over_limit_warns(self, stop_mod, capsys):
        _write_individual(
            stop_mod, "code-reviewer", "notes.md", EXPECTED_FILE_LIMIT_BYTES + 1
        )
        stop_mod._warn_oversized_agent_memory()
        assert "notes" in capsys.readouterr().err

    def test_each_oversized_individual_file_is_one_line(self, stop_mod, capsys):
        _write_individual(
            stop_mod, "developer", "alpha_note.md", EXPECTED_FILE_LIMIT_BYTES + 1
        )
        _write_individual(
            stop_mod, "developer", "topics/beta_note.md",
            EXPECTED_FILE_LIMIT_BYTES + 1,
        )
        stop_mod._warn_oversized_agent_memory()
        lines = _err_lines(capsys)
        assert len(lines) == 2
        assert sum("alpha_note" in ln for ln in lines) == 1
        assert sum("beta_note" in ln for ln in lines) == 1


class TestIndividualFileWording:
    """(2) 個別ファイル用の文言が MEMORY.md 用と異なること（Red）."""

    def test_wording_differs_from_index_warning(self, stop_mod, capsys):
        # 索引は injection 予算に接近（21KB）、個別ファイルは 100KB 超過
        _write_memory(stop_mod, "security-reviewer", line_count=21, line_body="x" * 1000)
        _write_individual(
            stop_mod, "security-reviewer", "sr_long_note.md",
            EXPECTED_FILE_LIMIT_BYTES + 1,
        )
        stop_mod._warn_oversized_agent_memory()
        lines = _err_lines(capsys)
        assert len(lines) == 2, f"索引と個別ファイルの 2 行が出ること: {lines}"

        index_line = next(ln for ln in lines if "MEMORY.md" in ln)
        file_line = next(ln for ln in lines if "sr_long_note" in ln)
        assert index_line != file_line

    def test_individual_warning_has_no_injection_budget_wording(self, stop_mod, capsys):
        """個別ファイルは system prompt へ注入されないため injection 予算の話をしない."""
        _write_individual(
            stop_mod, "developer", "long_note.md", EXPECTED_FILE_LIMIT_BYTES + 1
        )
        stop_mod._warn_oversized_agent_memory()
        line = _only_line(capsys)
        assert "injection" not in line
        assert "予算" not in line

    def test_individual_warning_mentions_read_or_maintenance_cost(self, stop_mod, capsys):
        _write_individual(
            stop_mod, "developer", "long_note.md", EXPECTED_FILE_LIMIT_BYTES + 1
        )
        stop_mod._warn_oversized_agent_memory()
        line = _only_line(capsys)
        assert ("Read" in line) or ("保守" in line), f"Read / 保守コスト観点の文言がない: {line!r}"

    def test_individual_warning_reports_size_and_limit(self, stop_mod, capsys):
        """実測値と上限が出て、どれだけ削ればよいか分かること."""
        size = EXPECTED_FILE_LIMIT_BYTES + 1234
        _write_individual(stop_mod, "developer", "long_note.md", size)
        stop_mod._warn_oversized_agent_memory()
        line = _only_line(capsys)
        assert str(size) in line
        assert str(EXPECTED_FILE_LIMIT_BYTES) in line or "100" in line


class TestIndividualFileNameSanitize:
    """(3) 100KB 超ファイル名の表示偽装文字が無害化されること（Red・複合 assert）.

    U+202E (RIGHT-TO-LEFT OVERRIDE) / U+200B (ZERO WIDTH SPACE) は NTFS で
    ファイル名に使用でき、かつ _DISPLAY_SANITIZE_RE の射程内。
    C0 制御文字は NTFS で作成できないため題材にしない（listdir 差し替えは
    既存の TestDefensiveBehaviour が担当）。
    """

    def test_bidi_and_zero_width_in_filename_are_stripped(self, stop_mod, capsys):
        name = "evil‮dm​_note.md"
        _write_individual(stop_mod, "developer", name, EXPECTED_FILE_LIMIT_BYTES + 1)

        stop_mod._warn_oversized_agent_memory()
        err = capsys.readouterr().err

        # 警告が出ること（サニタイズによって検知自体が落ちないこと）
        assert "evil" in err, f"100KB 超ファイルの警告が出ていない: {err!r}"
        # かつ生の表示偽装文字が出力に残らないこと
        assert "‮" not in err
        assert "​" not in err

    def test_bidi_in_subdirectory_name_is_stripped(self, stop_mod, capsys):
        """サブディレクトリ名も同じ経路で無害化されること."""
        _write_individual(
            stop_mod, "developer", "top‮ics​/inner_note.md",
            EXPECTED_FILE_LIMIT_BYTES + 1,
        )
        stop_mod._warn_oversized_agent_memory()
        err = capsys.readouterr().err
        assert "inner_note" in err
        assert "‮" not in err
        assert "​" not in err


class TestPathElementTruncation:
    """(4) 長い要素名が切り詰められても対象ファイルが一意に特定できること（Red）.

    サニタイズと長さ上限は「連結後のパス全体」ではなく「連結前の各パス要素ごと」に
    適用する設計。全体を切り詰めるとファイル名要素が丸ごと消え、どのファイルの
    警告か分からなくなる（＝要素境界が壊れる）。
    """

    def test_each_path_element_is_truncated_independently(self, short_root_stop_mod, capsys):
        mod = short_root_stop_mod
        # MAX_ID_LENGTH (64) を超えるが、Windows の MAX_PATH には収まる長さ
        subdir = "sub_" + "a" * 66
        filename = "file_" + "b" * 65 + ".md"
        _write_individual(
            mod, "developer", f"{subdir}/{filename}", EXPECTED_FILE_LIMIT_BYTES + 1
        )
        mod._warn_oversized_agent_memory()
        parts = _rel_path_in(_only_line(capsys))

        assert len(parts) == 3, f"要素境界が壊れている（agent/subdir/file の 3 要素でない）: {parts}"
        assert parts[0] == "developer"
        assert parts[1].startswith("sub_aaaa")
        assert parts[2].startswith("file_bbbb"), "ファイル名要素が切り詰めで消えている"
        for part in parts:
            assert len(part) <= mod.MAX_ID_LENGTH, f"要素が長さ上限を超えている: {part!r}"

    def test_two_long_named_files_remain_distinguishable(self, short_root_stop_mod, capsys):
        mod = short_root_stop_mod
        long_a = "alpha_" + "z" * 70 + ".md"
        long_b = "bravo_" + "z" * 70 + ".md"
        _write_individual(mod, "developer", long_a, EXPECTED_FILE_LIMIT_BYTES + 1)
        _write_individual(mod, "developer", long_b, EXPECTED_FILE_LIMIT_BYTES + 1)

        mod._warn_oversized_agent_memory()
        lines = _err_lines(capsys)
        assert len(lines) == 2, f"2 件とも警告されること: {lines}"
        shown = {_rel_path_in(ln)[-1] for ln in lines}
        assert len(shown) == 2, f"切り詰め後に区別できなくなっている: {shown}"
        assert any(s.startswith("alpha_") for s in shown)
        assert any(s.startswith("bravo_") for s in shown)


# --- 回帰ガード群 ---------------------------------------------------------
# 以下は是正前から緑である想定。「最初から Pass なら修正する」規範は適用しない。


class TestThresholdSeparation:
    """(5) 100KB 未満の個別ファイルは（25KB を超えても）警告しない."""

    def test_file_below_individual_limit_is_silent(self, stop_mod, capsys):
        # 30KB = 索引の 25KB 予算は超えるが、個別ファイルの 100KB には届かない
        _write_individual(stop_mod, "developer", "mid_note.md", 30 * 1024)
        stop_mod._warn_oversized_agent_memory()
        assert capsys.readouterr().err == ""

    def test_file_exactly_at_individual_limit_is_silent(self, stop_mod, capsys):
        """「超過のみ」＝ strict greater（既存の境界値規約と同じ）."""
        _write_individual(
            stop_mod, "developer", "edge_note.md", EXPECTED_FILE_LIMIT_BYTES
        )
        stop_mod._warn_oversized_agent_memory()
        assert capsys.readouterr().err == ""

    def test_many_line_individual_file_is_silent(self, stop_mod, capsys):
        """個別ファイルは行数（200 行）しきい値の対象外."""
        p = Path(stop_mod.AGENT_MEMORY_DIR) / "developer" / "many_lines.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n" * 500, encoding="utf-8")
        stop_mod._warn_oversized_agent_memory()
        assert capsys.readouterr().err == ""


class TestIndexWarningUnchanged:
    """(6) 既存の MEMORY.md 向け警告の緑維持."""

    def test_index_warning_still_fires_with_injection_wording(self, stop_mod, capsys):
        _write_memory(stop_mod, "security-reviewer", line_count=21, line_body="x" * 1000)
        stop_mod._warn_oversized_agent_memory()
        line = _err_lines(capsys)[0]
        assert "agent-memory/security-reviewer/MEMORY.md" in line
        assert "injection" in line
        assert str(stop_mod.AGENT_MEMORY_LIMIT_BYTES) in line

    def test_huge_index_is_reported_once_with_index_wording(self, stop_mod, capsys):
        """100KB 超の MEMORY.md でも索引扱いのまま。個別ファイル警告と二重に出さない."""
        d = Path(stop_mod.AGENT_MEMORY_DIR) / "security-reviewer"
        d.mkdir(parents=True)
        (d / "MEMORY.md").write_text("x" * (EXPECTED_FILE_LIMIT_BYTES + 1), encoding="utf-8")
        stop_mod._warn_oversized_agent_memory()
        lines = _err_lines(capsys)
        assert len(lines) == 1, f"MEMORY.md が二重報告されている: {lines}"
        assert "injection" in lines[0]

    def test_index_and_individual_file_do_not_interfere(self, stop_mod, capsys):
        """索引が予算内・個別ファイルのみ肥大しているケースで索引の緑を保つ."""
        _write_memory(stop_mod, "developer", line_count=10, line_body="x" * 10)
        _write_individual(
            stop_mod, "developer", "topics/huge.md", EXPECTED_FILE_LIMIT_BYTES + 1
        )
        stop_mod._warn_oversized_agent_memory()
        lines = _err_lines(capsys)
        assert all("MEMORY.md" not in ln for ln in lines), f"索引が誤検知されている: {lines}"


class TestScanFailOpen:
    """(7) 走査エラーで Stop が止まらない（fail-open）."""

    def test_scan_error_is_silent_and_does_not_raise(self, stop_mod, capsys, monkeypatch):
        _write_individual(
            stop_mod, "developer", "topics/huge.md", EXPECTED_FILE_LIMIT_BYTES + 1
        )

        def _boom(*_a, **_k):
            raise PermissionError("scan denied")

        # 実装が listdir / walk / scandir（pathlib.rglob 含む）のどれを使っても
        # 走査が失敗する状態にする
        monkeypatch.setattr(stop_mod.os, "listdir", _boom)
        monkeypatch.setattr(stop_mod.os, "walk", _boom)
        monkeypatch.setattr(stop_mod.os, "scandir", _boom)

        stop_mod._warn_oversized_agent_memory()  # 例外を送出しないこと
        assert capsys.readouterr().err == ""

    def test_unreadable_individual_file_does_not_block_index_warning(
        self, stop_mod, capsys, monkeypatch
    ):
        _write_memory(stop_mod, "security-reviewer", line_count=21, line_body="x" * 1000)
        _write_individual(
            stop_mod, "security-reviewer", "broken_note.md",
            EXPECTED_FILE_LIMIT_BYTES + 1,
        )

        real_getsize = stop_mod.os.path.getsize

        def _selective(path, *a, **k):
            if "broken" in str(path):
                raise OSError("unreadable")
            return real_getsize(path, *a, **k)

        monkeypatch.setattr(stop_mod.os.path, "getsize", _selective)

        stop_mod._warn_oversized_agent_memory()
        err = capsys.readouterr().err
        assert "MEMORY.md" in err, "読めないファイル 1 件で走査全体が止まっている"

    def test_run_returns_zero_when_scan_raises_unexpectedly(self, stop_mod, monkeypatch):
        def _boom(*_a, **_k):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(stop_mod.os, "listdir", _boom)
        monkeypatch.setattr(stop_mod.os, "walk", _boom)
        monkeypatch.setattr(stop_mod.os, "scandir", _boom)
        monkeypatch.setattr(stop_mod, "ensure_session_file", lambda _d: None)
        monkeypatch.setattr(stop_mod, "update_patterns", lambda _d: None)
        monkeypatch.setattr(stop_mod, "is_worktree", lambda _c: False)

        assert stop_mod.run({}) == 0


def _make_link(target: Path, link: Path) -> None:
    """走査対象ディレクトリの内側に、外部ディレクトリへのリンクを作る.

    Windows は junction（_winapi.CreateJunction・管理者権限不要）、
    POSIX は os.symlink を使う。作成に失敗した場合は skip せず fail させる
    （skip すると「リンクを辿らない」という契約が全 OS で無検証になりうる）。
    """
    if sys.platform == "win32":
        try:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        except Exception as exc:  # pragma: no cover - 環境不備時のみ
            pytest.fail(f"junction の作成に失敗した（skip せず fail させる契約）: {exc!r}")
    else:
        try:
            os.symlink(str(target), str(link), target_is_directory=True)
        except OSError as exc:  # pragma: no cover - 環境不備時のみ
            pytest.fail(f"symlink の作成に失敗した（skip せず fail させる契約）: {exc!r}")


def _patch_lstat_as_reparse_point(mod, monkeypatch, target: Path) -> None:
    """`target` に対してのみ os.lstat が reparse point 属性を返すようにする.

    Windows の非特権環境では**ファイル**の symlink を作れず、junction は
    ディレクトリ専用のため、MEMORY.md 自体をリンクにする構成が実ファイルでは
    作れない。属性を差し替えることで `_is_link_like` の判定経路
    （FILE_ATTRIBUTE_REPARSE_POINT）はそのまま通す（判定関数自体は差し替えない）。
    """
    real_lstat = mod.os.lstat
    key = os.path.normcase(os.path.abspath(str(target)))
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

    def _fake_lstat(path, *args, **kwargs):
        st = real_lstat(path, *args, **kwargs)
        try:
            same = os.path.normcase(os.path.abspath(str(path))) == key
        except (TypeError, ValueError):  # pragma: no cover - fd 引数など
            same = False
        if not same:
            return st
        return types.SimpleNamespace(
            st_mode=st.st_mode,
            st_size=st.st_size,
            st_file_attributes=getattr(st, "st_file_attributes", 0) | reparse_flag,
        )

    monkeypatch.setattr(mod.os, "lstat", _fake_lstat)


class TestLinkIsNotFollowed:
    """(8) symlink / junction 非追跡.

    リンクは agent-memory/<agent>/ 配下（走査対象ディレクトリの内側）に置き、
    リンク先（repo 外＝tmp_path 直下）に 100KB 超の *.md を置いても
    警告対象に載らないことを assert する。

    是正前の期待値: 現行実装は <agent>/MEMORY.md 決め打ちのため
    サブディレクトリ内リンクは辿られず沈黙＝是正前後とも警告なし（回帰ガード）。

    注意: Windows の junction は os.path.islink() が False を返し、
    os.walk(followlinks=False) でも追跡されてしまう。再帰実装では
    os.lstat().st_reparse_tag などによる明示的な除外が必要になる。
    """

    def test_link_to_outside_dir_is_not_followed(self, stop_mod, capsys, tmp_path):
        outside = tmp_path / "outside-repo"
        outside.mkdir()
        (outside / "linked_huge.md").write_text(
            "x" * (EXPECTED_FILE_LIMIT_BYTES + 1), encoding="utf-8"
        )

        agent_dir = Path(stop_mod.AGENT_MEMORY_DIR) / "developer"
        agent_dir.mkdir(parents=True)
        _make_link(outside, agent_dir / "linked")

        # リンクが実際に張れており、辿れば 100KB 超ファイルに到達できる状態
        assert (agent_dir / "linked" / "linked_huge.md").exists()

        stop_mod._warn_oversized_agent_memory()
        err = capsys.readouterr().err
        assert "linked_huge" not in err, f"リンク先を辿っている: {err!r}"
        assert err == "", f"リンク経由の誤検知: {err!r}"

    def test_real_file_beside_link_is_still_detected(self, stop_mod, capsys, tmp_path):
        """リンク除外が「サブディレクトリ走査ごと止める」実装になっていないこと.

        是正前は個別ファイル警告自体が無いため赤（Red 群相当）。
        """
        outside = tmp_path / "outside-repo"
        outside.mkdir()
        (outside / "linked_huge.md").write_text(
            "x" * (EXPECTED_FILE_LIMIT_BYTES + 1), encoding="utf-8"
        )

        agent_dir = Path(stop_mod.AGENT_MEMORY_DIR) / "developer"
        agent_dir.mkdir(parents=True)
        _make_link(outside, agent_dir / "linked")
        (agent_dir / "real_huge.md").write_text(
            "x" * (EXPECTED_FILE_LIMIT_BYTES + 1), encoding="utf-8"
        )

        stop_mod._warn_oversized_agent_memory()
        err = capsys.readouterr().err
        assert "real_huge" in err
        assert "linked_huge" not in err

    def test_link_like_index_file_is_skipped(
        self, stop_mod, capsys, tmp_path, monkeypatch
    ):
        """[CR-NEW] MEMORY.md 自体がリンクなら索引警告経路もスキップする.

        `_warn_index_file` は `<agent>/MEMORY.md` を直接パスで開くため
        `_iter_relative_md_files` のリンク除外を通らない。是正前は索引経路だけ
        リンクを辿り、リンク先（走査対象外）のサイズ・行数を警告に出していた。

        対照群（リンクでない実体の MEMORY.md）を同時に置き、警告経路そのものが
        死んでいないことを担保する（空振りの緑を防ぐ）。
        """
        outside = tmp_path / "outside-repo"
        outside.mkdir()
        real_index = outside / "MEMORY.md"
        real_index.write_text("x" * 21000, encoding="utf-8")

        # 対照群: 実体の索引は従来どおり警告される
        _write_memory(stop_mod, "control", line_count=21, line_body="x" * 1000)

        agent_dir = Path(stop_mod.AGENT_MEMORY_DIR) / "developer"
        agent_dir.mkdir(parents=True)
        index_path = agent_dir / "MEMORY.md"
        try:
            os.symlink(str(real_index), str(index_path))
        except (OSError, NotImplementedError, AttributeError):
            # Windows の非特権環境ではファイル symlink を作れないため、実体を置いた
            # うえで lstat の属性のみリンク相当にする（skip では逃さない）
            shutil.copyfile(real_index, index_path)
            _patch_lstat_as_reparse_point(stop_mod, monkeypatch, index_path)

        # 辿れば警告対象サイズに到達できる状態であること
        assert index_path.stat().st_size == 21000

        stop_mod._warn_oversized_agent_memory()
        err = capsys.readouterr().err
        assert "control" in err, f"対照群の索引警告が出ていない（空振りの緑）: {err!r}"
        assert "developer" not in err, f"リンクの索引を辿っている: {err!r}"
