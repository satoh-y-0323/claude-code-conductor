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
