"""Tests for .claude/hooks/subagent_log.py (新仕様 Red フェーズ)

新仕様の record 構造:
  全イベント共通: {"ts": <ISO8601>, "payload": <stdin JSON 全体>}
  SubagentStop でペアリング成功時のみ追加: "duration_seconds", "matched_start_ts"
  トップレベルへの hook_event_name / session_id / agent_type / event の複製は無し

ペアリングロジック:
  同 session_id + 同 agent_id の Start が未対応なら最古を Stop と対応付ける
  agent_id が空文字 / 欠落の SubagentStop は duration 計算スキップ
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WORKTREE_ROOT = Path(__file__).parents[2]
HOOK_PATH = WORKTREE_ROOT / ".claude" / "hooks" / "subagent_log.py"


def _load_hook_module(path: Path) -> types.ModuleType:
    """Hook スクリプトを __main__ を実行せずにモジュールとしてロードする。"""
    spec = importlib.util.spec_from_file_location("subagent_log", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


def _run_hook(module: types.ModuleType, payload: dict, tmp_path: Path) -> None:
    """tmp_path を LOG_DIR / LOG_FILE に差し替えて main() を呼ぶ。"""
    log_dir = tmp_path
    log_file = str(tmp_path / "agent-runs.jsonl")

    module.LOG_DIR = str(log_dir)
    module.LOG_FILE = log_file

    original_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    try:
        module.main()
    finally:
        sys.stdin = original_stdin


def _read_records(tmp_path: Path) -> list[dict]:
    """agent-runs.jsonl を読んで各行を dict のリストで返す。"""
    log_file = tmp_path / "agent-runs.jsonl"
    if not log_file.exists():
        return []
    records = []
    for line in log_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def _make_start_payload(session_id: str = "sess-1", agent_id: str = "agent-aaa") -> dict:
    return {
        "session_id": session_id,
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/tmp",
        "agent_id": agent_id,
        "agent_type": "Explore",
        "hook_event_name": "SubagentStart",
    }


def _make_stop_payload(
    session_id: str = "sess-1",
    agent_id: str = "agent-aaa",
    last_message: str = "done",
) -> dict:
    return {
        "session_id": session_id,
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/tmp",
        "permission_mode": "default",
        "agent_id": agent_id,
        "agent_type": "Explore",
        "hook_event_name": "SubagentStop",
        "stop_hook_active": False,
        "agent_transcript_path": "/tmp/agent.jsonl",
        "last_assistant_message": last_message,
    }


# ---------------------------------------------------------------------------
# Test 1: SubagentStart は {"ts", "payload"} のみを持つ
# ---------------------------------------------------------------------------


class TestStartOnlyRecordsPayload:
    """test_1: SubagentStart は duration_seconds を持たず、
    トップレベルに hook_event_name / session_id / agent_type / event を複製しない。
    """

    def test_start_only_records_payload(self, tmp_path: Path) -> None:
        """SubagentStart payload を流すと agent-runs.jsonl に 1 行追記され、
        その行は {"ts", "payload"} のみを持ち duration_seconds は無い。

        [Red] 旧実装は hook_event_name / session_id / agent_type / event を
        トップレベルに複製するため、余分なキーが存在して失敗する。
        """
        module = _load_hook_module(HOOK_PATH)
        payload = _make_start_payload()

        _run_hook(module, payload, tmp_path)

        records = _read_records(tmp_path)
        assert len(records) == 1, f"期待 1 行、実際 {len(records)} 行"

        record = records[0]
        # 新仕様: トップレベルのキーは ts と payload のみ
        assert set(record.keys()) == {"ts", "payload"}, (
            f"record のキーが新仕様 {{ts, payload}} と異なる: {set(record.keys())}"
        )
        assert "duration_seconds" not in record
        assert record["payload"] == payload


# ---------------------------------------------------------------------------
# Test 2: 対応 Start が無い SubagentStop は duration_seconds が付かない
# ---------------------------------------------------------------------------


class TestStopWithoutStartRecordsNoDuration:
    """test_2: 対応 Start が無い SubagentStop は duration_seconds キーが付かない。"""

    def test_stop_without_start_records_no_duration(self, tmp_path: Path) -> None:
        """Start 無しで Stop を流した場合、duration_seconds キーが存在しないこと。

        [Red] 旧実装はトップレベルに event="stop" を持ち、新仕様の
        {"ts", "payload"} のみ構造ではないため失敗する。
        """
        module = _load_hook_module(HOOK_PATH)
        payload = _make_stop_payload()

        _run_hook(module, payload, tmp_path)

        records = _read_records(tmp_path)
        assert len(records) == 1

        record = records[0]
        assert "duration_seconds" not in record, "Start 未対応の Stop に duration_seconds が付いている"
        # 新仕様: トップレベルに event / hook_event_name 等の複製は無し
        assert set(record.keys()) == {"ts", "payload"}, (
            f"record のキーが新仕様 {{ts, payload}} と異なる: {set(record.keys())}"
        )


# ---------------------------------------------------------------------------
# Test 3: Start → Stop ペアで duration_seconds と matched_start_ts が記録される
# ---------------------------------------------------------------------------


class TestStartThenStopPairing:
    """test_3: 同 session_id + agent_id の Start → Stop ペアリングで
    duration_seconds と matched_start_ts が記録される。
    """

    def test_start_then_stop_pairing(self, tmp_path: Path) -> None:
        """Start の後に同 session_id + agent_id の Stop を流すと
        duration_seconds (>= 0) と matched_start_ts が記録される。

        [Red] 旧実装はペアリングキーに agent_id を使わず agent_type を使うため、
        また record 構造も新仕様と異なるため失敗する。
        """
        module = _load_hook_module(HOOK_PATH)
        session_id = "sess-pair"
        agent_id = "agent-xyz"

        _run_hook(module, _make_start_payload(session_id=session_id, agent_id=agent_id), tmp_path)
        _run_hook(module, _make_stop_payload(session_id=session_id, agent_id=agent_id), tmp_path)

        records = _read_records(tmp_path)
        assert len(records) == 2

        stop_record = records[1]
        # 新仕様: ペアリング成功時は duration_seconds と matched_start_ts を追加
        assert "duration_seconds" in stop_record, "ペアリング成功時に duration_seconds が無い"
        assert "matched_start_ts" in stop_record, "ペアリング成功時に matched_start_ts が無い"
        assert stop_record["duration_seconds"] >= 0
        # Start record の ts と matched_start_ts が一致すること
        start_ts = records[0]["ts"]
        assert stop_record["matched_start_ts"] == start_ts
        # 新仕様: トップレベルのキーは ts / payload / duration_seconds / matched_start_ts のみ
        allowed_keys = {"ts", "payload", "duration_seconds", "matched_start_ts"}
        assert set(stop_record.keys()) <= allowed_keys, (
            f"Stop record に余分なキーが存在: {set(stop_record.keys()) - allowed_keys}"
        )


# ---------------------------------------------------------------------------
# Test 4: 並列サブエージェントが agent_id で正しくペアリングされる
# ---------------------------------------------------------------------------


class TestParallelSubagentsPairCorrectly:
    """test_4: 同 session_id 内で agent_id が異なる 2 件を
    Start_A → Start_B → Stop_B → Stop_A の順に流すと、
    Stop_B は Start_B と、Stop_A は Start_A と正しくペアリングされる。

    FIFO (agent_type ベース) だと Stop_B が Start_A と誤ペアリングする。
    """

    def test_parallel_subagents_pair_correctly(self, tmp_path: Path) -> None:
        """[Red] 旧実装は agent_type ベースの FIFO で、Start_A → Start_B → Stop_B の
        順では Stop_B が Start_A と誤ペアリングするため失敗する。

        ペアリングの正しさは matched_start_ts の一致ではなく、
        matched_start_ts が指す Start レコードの payload.agent_id を検証する。
        これにより同秒内実行で ts が等しくなる場合でも正確に判定できる。
        """
        module = _load_hook_module(HOOK_PATH)
        session_id = "sess-parallel"
        agent_id_a = "agent-AAAA"
        agent_id_b = "agent-BBBB"

        # Start_A
        _run_hook(module, _make_start_payload(session_id=session_id, agent_id=agent_id_a), tmp_path)
        # Start_B
        _run_hook(module, _make_start_payload(session_id=session_id, agent_id=agent_id_b), tmp_path)
        # Stop_B
        _run_hook(module, _make_stop_payload(session_id=session_id, agent_id=agent_id_b), tmp_path)
        # Stop_A
        _run_hook(module, _make_stop_payload(session_id=session_id, agent_id=agent_id_a), tmp_path)

        records = _read_records(tmp_path)
        assert len(records) == 4

        start_a_record = records[0]
        start_b_record = records[1]
        stop_b_record = records[2]
        stop_a_record = records[3]

        # 新仕様: Stop record は {"ts", "payload", "duration_seconds", "matched_start_ts"} のみ持つ
        # 旧実装はトップレベルに event / hook_event_name / session_id / agent_type を複製するため失敗
        allowed_keys = {"ts", "payload", "duration_seconds", "matched_start_ts"}
        assert set(stop_b_record.keys()) <= allowed_keys, (
            f"Stop_B record に余分なキーが存在: {set(stop_b_record.keys()) - allowed_keys}"
        )
        assert set(stop_a_record.keys()) <= allowed_keys, (
            f"Stop_A record に余分なキーが存在: {set(stop_a_record.keys()) - allowed_keys}"
        )

        # Stop_B の matched_start_ts は Start_B の ts と等しいこと
        # 旧実装は agent_type ベース FIFO のため Stop_B が Start_A とペアリングされ、
        # matched_start_ts が Start_A の ts を指すことで失敗する
        assert "matched_start_ts" in stop_b_record, "Stop_B に matched_start_ts が無い"
        assert stop_b_record["matched_start_ts"] == start_b_record["ts"], (
            f"Stop_B が Start_B ではなく誤ペアリング (FIFO 誤マッチ):"
            f" expected ts={start_b_record['ts']}, got matched={stop_b_record['matched_start_ts']}"
        )

        # Stop_A の matched_start_ts は Start_A の ts と等しいこと
        assert "matched_start_ts" in stop_a_record, "Stop_A に matched_start_ts が無い"
        assert stop_a_record["matched_start_ts"] == start_a_record["ts"], (
            f"Stop_A が Start_A と正しくペアリングされていない:"
            f" expected ts={start_a_record['ts']}, got matched={stop_a_record['matched_start_ts']}"
        )


# ---------------------------------------------------------------------------
# Test 5: agent_id が空文字の SubagentStop は duration_seconds が付かない
# ---------------------------------------------------------------------------


class TestStopWithoutAgentIdSkipsDuration:
    """test_5: SubagentStop の payload で agent_id が空文字の場合は
    duration_seconds が付かない。
    """

    def test_stop_without_agent_id_skips_duration(self, tmp_path: Path) -> None:
        """agent_id が空文字の Stop は duration 計算をスキップすること。

        [Red] 旧実装はペアリングキーに agent_type を使うため、
        agent_id が空でも agent_type が一致すれば誤ってペアリングする恐れがある。
        加えて record 構造も新仕様と異なるため失敗する。
        """
        module = _load_hook_module(HOOK_PATH)
        session_id = "sess-no-id"
        agent_id = "agent-real"

        # まず正常な Start を記録
        _run_hook(module, _make_start_payload(session_id=session_id, agent_id=agent_id), tmp_path)

        # agent_id が空文字の Stop
        stop_payload = _make_stop_payload(session_id=session_id, agent_id="")
        _run_hook(module, stop_payload, tmp_path)

        records = _read_records(tmp_path)
        assert len(records) == 2

        stop_record = records[1]
        assert "duration_seconds" not in stop_record, (
            "agent_id が空文字の Stop に duration_seconds が付いている"
        )
        # 新仕様: トップレベルのキーは ts と payload のみ（ペアリング失敗時）
        assert set(stop_record.keys()) == {"ts", "payload"}, (
            f"record のキーが新仕様 {{ts, payload}} と異なる: {set(stop_record.keys())}"
        )


# ---------------------------------------------------------------------------
# Test 6: JSON 不正行が混在しても hook はクラッシュしない
# ---------------------------------------------------------------------------


class TestBrokenLogLineDoesNotCrash:
    """test_6: 既存の agent-runs.jsonl に JSON でない行が混じっていても
    hook はクラッシュせず正常に追記する。
    """

    def test_broken_log_line_does_not_crash(self, tmp_path: Path) -> None:
        """_read_log_records が JSONDecodeError を catch しているため、
        現状実装でも通る可能性がある（それで構わない）。

        リファクタ後も維持される性質であることを確認する。
        """
        module = _load_hook_module(HOOK_PATH)

        # 事前に壊れた行を含む JSONL ファイルを用意する
        log_file = tmp_path / "agent-runs.jsonl"
        log_file.write_text(
            '{"ts": "2026-01-01T00:00:00+09:00", "payload": {}}\n'
            "THIS IS NOT JSON\n"
            '{"ts": "2026-01-01T00:00:01+09:00", "payload": {}}\n',
            encoding="utf-8",
        )

        # Start を記録してからペアリング対象の Stop を流す（クラッシュしないことを確認）
        session_id = "sess-broken"
        agent_id = "agent-safe"

        _run_hook(module, _make_start_payload(session_id=session_id, agent_id=agent_id), tmp_path)
        # Stop を流してもクラッシュしないこと
        _run_hook(module, _make_stop_payload(session_id=session_id, agent_id=agent_id), tmp_path)

        # 追記されていること（元の 3 行 + Start 1 行 + Stop 1 行 = 5 行）
        lines = [
            line for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        assert len(lines) == 5, f"追記後の行数が期待と異なる: {len(lines)}"
