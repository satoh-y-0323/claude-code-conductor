"""Tests for runner._summary_loop / _format_summary_line / _resolve_summary_interval (v1.5.1).

非 TTY 環境で wave 全体の進捗を 30 秒ごとに 1 行にまとめる機能の検証。
"""

from __future__ import annotations

import io
import threading
import time
from typing import cast

import pytest

from parallel_orchestra.runner import (
    _DEFAULT_SUMMARY_INTERVAL_SEC,
    _Dashboard,
    _format_summary_line,
    _resolve_summary_interval,
    _summary_loop,
    _TaskDisplayState,
)


# ---------------------------------------------------------------------------
# _format_summary_line
# ---------------------------------------------------------------------------


class TestFormatSummaryLine:
    """サマリ行のフォーマット仕様。"""

    def _state(self, task_id: str, status: str, *, start_offset: float = 0.0) -> _TaskDisplayState:
        # start_ts は monotonic 基準なので now を直接渡せるように
        s = _TaskDisplayState(task_id=task_id)
        s.status = status  # type: ignore[assignment]
        s.start_ts = start_offset
        return s

    def test_total_count_and_running_detail(self) -> None:
        """running task の id:Xs 詳細が含まれる。"""
        now = 10000.0
        states = [
            self._state("be-calc", "running", start_offset=now - 120),
            self._state("be-currency", "running", start_offset=now - 90),
            self._state("fe-base", "starting_up", start_offset=now - 5),
        ]
        line = _format_summary_line(states, now=now)

        assert "[summary]" in line
        assert "3 tasks" in line
        assert "2 running" in line
        assert "be-calc:120s" in line
        assert "be-currency:90s" in line
        assert "1 starting" in line

    def test_more_marker_when_running_exceeds_detail_limit(self) -> None:
        """running が 4 件以上のときは先頭 3 件 + '+N more'。"""
        now = 10000.0
        states = [
            self._state(f"task-{i}", "running", start_offset=now - 30 - i)
            for i in range(5)
        ]
        line = _format_summary_line(states, now=now)

        assert "5 running" in line
        assert "task-0:" in line
        assert "task-1:" in line
        assert "task-2:" in line
        # 4 件目以降は省略
        assert "task-3:" not in line
        assert "+2 more" in line

    def test_completed_and_failed_counts(self) -> None:
        """complete / skipped / failed の集計。"""
        now = 100.0
        states = [
            self._state("a", "complete"),
            self._state("b", "skipped"),
            self._state("c", "failed"),
            self._state("d", "running", start_offset=now - 10),
        ]
        line = _format_summary_line(states, now=now)

        assert "4 tasks" in line
        assert "2 completed" in line  # complete + skipped
        assert "1 failed" in line
        assert "1 running" in line

    def test_no_running_no_detail_block(self) -> None:
        """running が 0 件のときは詳細なし、0 件もカウントだけ表示。"""
        now = 100.0
        states = [self._state("a", "complete"), self._state("b", "complete")]
        line = _format_summary_line(states, now=now)

        assert "0 running" in line
        assert "(" not in line  # 詳細ブロックなし
        assert "2 completed" in line


# ---------------------------------------------------------------------------
# _resolve_summary_interval
# ---------------------------------------------------------------------------


class TestResolveSummaryInterval:

    def test_default_when_env_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("C3_PO_SUMMARY_INTERVAL_SEC", raising=False)
        assert _resolve_summary_interval() == _DEFAULT_SUMMARY_INTERVAL_SEC

    def test_env_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("C3_PO_SUMMARY_INTERVAL_SEC", "10")
        assert _resolve_summary_interval() == 10.0

    def test_invalid_value_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("C3_PO_SUMMARY_INTERVAL_SEC", "not-a-number")
        assert _resolve_summary_interval() == _DEFAULT_SUMMARY_INTERVAL_SEC

    def test_zero_or_negative_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """0 / 負値は busy loop を防ぐためデフォルトに戻す。"""
        monkeypatch.setenv("C3_PO_SUMMARY_INTERVAL_SEC", "0")
        assert _resolve_summary_interval() == _DEFAULT_SUMMARY_INTERVAL_SEC
        monkeypatch.setenv("C3_PO_SUMMARY_INTERVAL_SEC", "-5")
        assert _resolve_summary_interval() == _DEFAULT_SUMMARY_INTERVAL_SEC


# ---------------------------------------------------------------------------
# _summary_loop
# ---------------------------------------------------------------------------


class TestSummaryLoop:

    def test_emits_summary_line_when_active_tasks_exist(self) -> None:
        """active タスク（running）がある間は summary を出す。stop_event で抜ける。"""
        dashboard = _Dashboard(["task-1", "task-2"], enabled=False)
        dashboard.update("task-1", status="running")
        dashboard.update("task-2", status="starting_up")
        stop_event = threading.Event()
        out = io.StringIO()

        # interval を短くして 1 サイクルだけ走らせる
        thread = threading.Thread(
            target=_summary_loop,
            args=(dashboard, stop_event),
            kwargs={"interval": 0.05, "out": out},
        )
        thread.start()
        time.sleep(0.15)  # 2〜3 サイクル分
        stop_event.set()
        thread.join(timeout=2.0)

        text = out.getvalue()
        assert "[summary]" in text
        assert "2 tasks" in text
        # running と starting の両方が反映されている
        assert "running" in text and "starting" in text

    def test_skips_emission_when_no_active_tasks(self) -> None:
        """全タスクが waiting / completed / failed のときは出力しない。"""
        dashboard = _Dashboard(["task-1"], enabled=False)
        dashboard.update("task-1", status="complete")
        stop_event = threading.Event()
        out = io.StringIO()

        thread = threading.Thread(
            target=_summary_loop,
            args=(dashboard, stop_event),
            kwargs={"interval": 0.05, "out": out},
        )
        thread.start()
        time.sleep(0.15)
        stop_event.set()
        thread.join(timeout=2.0)

        # active が無いので空のまま
        assert out.getvalue() == ""

    def test_stop_event_terminates_loop(self) -> None:
        """stop_event.set() で wait から即抜ける。"""
        dashboard = _Dashboard(["task-1"], enabled=False)
        stop_event = threading.Event()
        out = io.StringIO()

        thread = threading.Thread(
            target=_summary_loop,
            args=(dashboard, stop_event),
            kwargs={"interval": 60.0, "out": out},  # 長い interval
        )
        thread.start()
        stop_event.set()
        thread.join(timeout=2.0)

        # interval を待たずに終了している
        assert not thread.is_alive()


# ---------------------------------------------------------------------------
# _Dashboard.update が enabled=False でも state を保持すること
# ---------------------------------------------------------------------------


class TestDashboardUpdateAlwaysTracksState:

    def test_update_persists_state_when_disabled(self) -> None:
        """enabled=False でも state は保持される（summary loop が読むため）。"""
        dashboard = _Dashboard(["task-1"], enabled=False)
        dashboard.update("task-1", status="running")

        snap = dashboard.snapshot_states()
        assert len(snap) == 1
        assert snap[0].task_id == "task-1"
        assert snap[0].status == "running"
        assert snap[0].start_ts > 0  # waiting -> running の遷移で start_ts が自動セットされる
