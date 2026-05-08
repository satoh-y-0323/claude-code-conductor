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


def _run_hook(module: types.ModuleType, payload: dict, tmp_path: Path) -> int:
    """tmp_path を LOG_DIR / LOG_FILE に差し替えて main() を呼ぶ。戻り値 (int) を返す。"""
    log_dir = tmp_path
    log_file = str(tmp_path / "agent-runs.jsonl")

    module.LOG_DIR = str(log_dir)
    module.LOG_FILE = log_file

    original_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    try:
        return module.main()
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
# Test 4: 並列サブエージェントが agent_id で正しくペアリングされる（強化版: code-M-1）
# ---------------------------------------------------------------------------


class TestParallelSubagentsPairCorrectly:
    """test_4: 同 session_id 内で agent_id が異なる 2 件を
    Start_A → Start_B → Stop_B → Stop_A の順に流すと、
    Stop_B は Start_B と、Stop_A は Start_A と正しくペアリングされる。

    FIFO (agent_type ベース) だと Stop_B が Start_A と誤ペアリングする。

    強化 (code-M-1): matched_start_ts の一致だけでなく、
    matched_start_ts + agent_id の複合で Start レコードの payload.agent_id も検証する。
    これにより同秒内実行で ts が等しくなっても正確に判定できる。
    """

    def test_parallel_subagents_pair_correctly(self, tmp_path: Path) -> None:
        """[Green] 現行実装は agent_id ベースのペアリングが正しく実装済み。

        強化検証 (code-M-1): matched_start_ts が指す Start レコードの
        payload.agent_id を ts + agent_id の複合で検索して突き合わせる。
        ts が同秒内で同一になる場合でも、agent_id との複合検索で正確に判定できる。
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
        allowed_keys = {"ts", "payload", "duration_seconds", "matched_start_ts"}
        assert set(stop_b_record.keys()) <= allowed_keys, (
            f"Stop_B record に余分なキーが存在: {set(stop_b_record.keys()) - allowed_keys}"
        )
        assert set(stop_a_record.keys()) <= allowed_keys, (
            f"Stop_A record に余分なキーが存在: {set(stop_a_record.keys()) - allowed_keys}"
        )

        # Stop_B の matched_start_ts は Start_B の ts と等しいこと
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

        # --- 強化 (code-M-1): matched_start_ts + agent_id の複合で Start の agent_id を検証 ---
        # ts が同秒内で同一になる場合でも agent_id で正しい Start を識別できることを確認する。
        #
        # Stop_B の matched_start_ts が指す ts において、agent_id_b を持つ Start が存在すること。
        # これにより、Stop_B が Start_B (agent_id_b) とペアリングされたことを確認できる。
        matched_start_b = next(
            (r for r in records
             if r["ts"] == stop_b_record["matched_start_ts"]
             and r.get("payload", {}).get("hook_event_name") == "SubagentStart"
             and r.get("payload", {}).get("agent_id") == agent_id_b),
            None,
        )
        assert matched_start_b is not None, (
            f"Stop_B の matched_start_ts={stop_b_record['matched_start_ts']} かつ "
            f"agent_id={agent_id_b!r} の SubagentStart レコードが見つからない。"
            f"Stop_B が誤って Start_A ({agent_id_a!r}) とペアリングされている可能性がある。"
        )

        # Stop_A の matched_start_ts が指す ts において、agent_id_a を持つ Start が存在すること
        matched_start_a = next(
            (r for r in records
             if r["ts"] == stop_a_record["matched_start_ts"]
             and r.get("payload", {}).get("hook_event_name") == "SubagentStart"
             and r.get("payload", {}).get("agent_id") == agent_id_a),
            None,
        )
        assert matched_start_a is not None, (
            f"Stop_A の matched_start_ts={stop_a_record['matched_start_ts']} かつ "
            f"agent_id={agent_id_a!r} の SubagentStart レコードが見つからない。"
            f"Stop_A が誤って Start_B ({agent_id_b!r}) とペアリングされている可能性がある。"
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


# ---------------------------------------------------------------------------
# Test 7: Start 残留テスト (code-L-3)
# ---------------------------------------------------------------------------


class TestDoubleStartThenSingleStop:
    """test_7: 同一 session_id + agent_id で SubagentStart を 2 回連続実行後に
    SubagentStop を 1 回実行した場合の挙動検証。
    """

    def test_double_start_then_single_stop(self, tmp_path: Path) -> None:
        """Start_1 → Start_2 → Stop の順に流すと:
        - ログに 3 行記録されること
        - Stop の matched_start_ts が古い方 (Start_1) の ts と一致すること
        - 新しい方 (Start_2) の Start は残留 Start としてログに残ること

        [Red] 現行実装では payload 全体が保存されるため、
        payload サニタイズ (sec-M-1) 実装後の期待値（last_assistant_message が除外）
        と異なり失敗する。
        """
        module = _load_hook_module(HOOK_PATH)
        session_id = "sess-double-start"
        agent_id = "agent-double"

        # Start_1 (古い方)
        _run_hook(module, _make_start_payload(session_id=session_id, agent_id=agent_id), tmp_path)
        records_after_start1 = _read_records(tmp_path)
        assert len(records_after_start1) == 1
        start_1_ts = records_after_start1[0]["ts"]

        # Start_2 (新しい方)
        _run_hook(module, _make_start_payload(session_id=session_id, agent_id=agent_id), tmp_path)

        # Stop_1 (1 回のみ)
        _run_hook(module, _make_stop_payload(session_id=session_id, agent_id=agent_id), tmp_path)

        records = _read_records(tmp_path)
        # 3 行記録されること (Start_1, Start_2, Stop)
        assert len(records) == 3, f"期待 3 行、実際 {len(records)} 行"

        stop_record = records[2]
        # Stop の matched_start_ts が古い方 (Start_1) の ts と一致すること
        assert "matched_start_ts" in stop_record, "Stop に matched_start_ts が無い"
        assert stop_record["matched_start_ts"] == start_1_ts, (
            f"Stop が最古の Start_1 とペアリングされていない: "
            f"expected {start_1_ts!r}, got {stop_record['matched_start_ts']!r}"
        )

        # Start_2 の record が残留 Start としてログに存在すること
        start_2_record = records[1]
        assert start_2_record["payload"]["hook_event_name"] == "SubagentStart", (
            "records[1] が SubagentStart でない"
        )

        # --- 新仕様検証: payload サニタイズ後 ---
        # payload に last_assistant_message が含まれていないこと (sec-M-1)
        # [Red] 現行実装は payload 全体を保存するため、Stop の payload に
        # last_assistant_message が残っており、このアサーションが失敗する。
        for record in records:
            assert "last_assistant_message" not in record.get("payload", {}), (
                f"payload に last_assistant_message が残っている: {record}。"
                "payload サニタイズ (sec-M-1) が未実装。"
            )


# ---------------------------------------------------------------------------
# Test 8: Stop 先着テスト (code-L-3 補足)
# ---------------------------------------------------------------------------


class TestStopBeforeStart:
    """test_8: 同一 session_id + agent_id で SubagentStop を先に実行後に
    SubagentStart を実行した場合、Stop record に duration_seconds / matched_start_ts が
    含まれないこと (FIFO で対応 Start なし)。
    """

    def test_stop_before_start(self, tmp_path: Path) -> None:
        """Stop → Start の順で実行した場合:
        - Stop record に duration_seconds / matched_start_ts が含まれないこと
        - Start record は通常通りログに記録されること
        - 合計 2 行記録されること

        [Red] 現行実装では payload 全体が保存されるため、
        payload サニタイズ (sec-M-1) 実装後の期待値と異なり失敗する。
        """
        module = _load_hook_module(HOOK_PATH)
        session_id = "sess-stop-first"
        agent_id = "agent-stop-first"

        # Stop を先に実行 (対応する Start がない)
        _run_hook(module, _make_stop_payload(session_id=session_id, agent_id=agent_id), tmp_path)
        # Start を後に実行
        _run_hook(module, _make_start_payload(session_id=session_id, agent_id=agent_id), tmp_path)

        records = _read_records(tmp_path)
        assert len(records) == 2, f"期待 2 行、実際 {len(records)} 行"

        stop_record = records[0]
        start_record = records[1]

        # Stop record に duration_seconds / matched_start_ts が含まれないこと
        assert "duration_seconds" not in stop_record, (
            "対応 Start なしの Stop に duration_seconds が付いている"
        )
        assert "matched_start_ts" not in stop_record, (
            "対応 Start なしの Stop に matched_start_ts が付いている"
        )

        # Start record は通常の {"ts", "payload"} 構造であること
        assert set(start_record.keys()) == {"ts", "payload"}, (
            f"Start record のキーが異常: {set(start_record.keys())}"
        )

        # --- 新仕様検証: payload サニタイズ後 ---
        # Stop の payload に last_assistant_message が含まれていないこと (sec-M-1)
        # [Red] 現行実装は payload 全体を保存するため、last_assistant_message が残り失敗する。
        assert "last_assistant_message" not in stop_record.get("payload", {}), (
            "Stop の payload に last_assistant_message が残っている。"
            "payload サニタイズ (sec-M-1) が未実装。"
        )


# ---------------------------------------------------------------------------
# Test 9: main() 戻り値検証 (code-L-4)
# ---------------------------------------------------------------------------


class TestMainReturnValue:
    """test_9: main() が int を返し、正常時は 0 であることを検証する。"""

    def test_main_returns_zero_on_success(self, tmp_path: Path) -> None:
        """SubagentStart payload を投入したとき main() が 0 を返すこと。

        [Green] 現行実装の main() は return 0 で終わるため通る。
        _run_hook が戻り値を返すよう更新されたことの動作確認。
        """
        module = _load_hook_module(HOOK_PATH)
        ret = _run_hook(module, _make_start_payload(), tmp_path)
        assert isinstance(ret, int), f"main() の戻り値が int でない: {type(ret)}"
        assert ret == 0, f"main() が 0 以外を返した: {ret}"

    def test_main_returns_zero_on_stop(self, tmp_path: Path) -> None:
        """SubagentStop payload を投入したとき main() が 0 を返すこと。

        [Green] 現行実装の main() は return 0 で終わるため通る。
        """
        module = _load_hook_module(HOOK_PATH)
        _run_hook(module, _make_start_payload(), tmp_path)
        ret = _run_hook(module, _make_stop_payload(), tmp_path)
        assert isinstance(ret, int), f"main() の戻り値が int でない: {type(ret)}"
        assert ret == 0, f"main() が 0 以外を返した: {ret}"

    def test_main_returns_zero_on_invalid_stdin(self, tmp_path: Path) -> None:
        """IOError が発生する stdin を模擬したとき main() が 0 を返すこと。

        [Red] 現行実装は stdin の IOError が catch されないため (json.JSONDecodeError のみ)、
        IOError が上位に伝播して異常終了する (pytest が例外を捕捉)。
        code-H-1 対応後に Exception catch が追加されることで 0 を返すようになる。
        """
        module = _load_hook_module(HOOK_PATH)
        log_file = str(tmp_path / "agent-runs.jsonl")
        module.LOG_DIR = str(tmp_path)
        module.LOG_FILE = log_file

        original_stdin = sys.stdin

        class _BrokenStdin:
            def read(self, *args):
                raise IOError("stdin broken")

        sys.stdin = _BrokenStdin()  # type: ignore[assignment]
        try:
            ret = module.main()
        except IOError:
            # 現行実装では IOError が伝播してくる = Red の期待する失敗
            pytest.fail(
                "stdin IOError 時に main() が IOError を伝播させた。"
                "Exception catch を追加して return 0 にすること (code-H-1)。"
            )
        finally:
            sys.stdin = original_stdin

        assert isinstance(ret, int), f"main() の戻り値が int でない: {type(ret)}"
        assert ret == 0, f"stdin IOError 時に main() が 0 以外を返した: {ret}"


# ---------------------------------------------------------------------------
# Test 10: U+2028/U+2029 エスケープテスト (sec-H-1)
# ---------------------------------------------------------------------------


class TestUnicodeParagraphSeparatorEscaping:
    """test_10: payload の値に U+2028 / U+2029 (Unicode 改行類似文字) が含まれる場合、
    出力された JSONL の生バイト列にこれらの文字が含まれないこと。
    """

    def test_u2028_not_in_raw_bytes(self, tmp_path: Path) -> None:
        r"""agent_type に U+2028 (LINE SEPARATOR) を含む payload を投入したとき、
        出力 JSONL の生バイト列に U+2028 / U+2029 が含まれないこと。

        [Red] 現行実装は ensure_ascii=False で json.dumps しているため、
        U+2028 がエスケープされずに出力され、このテストが失敗する。
        """
        module = _load_hook_module(HOOK_PATH)

        # U+2028 (LINE SEPARATOR) を agent_type に仕込む
        payload = _make_start_payload()
        payload["agent_type"] = "Explore Injected"
        payload["session_id"] = "sess-u2028"

        _run_hook(module, payload, tmp_path)

        log_file = tmp_path / "agent-runs.jsonl"
        assert log_file.exists(), "ログファイルが作成されていない"

        # 生バイト列を確認
        raw_bytes = log_file.read_bytes()
        u2028_bytes = " ".encode("utf-8")  # b'\xe2\x80\xa8'
        u2029_bytes = " ".encode("utf-8")  # b'\xe2\x80\xa9'

        assert u2028_bytes not in raw_bytes, (
            "JSONL の生バイト列に U+2028 (LINE SEPARATOR) が含まれている。"
            "ensure_ascii=False 下で U+2028 がエスケープされていない (sec-H-1)。"
        )
        assert u2029_bytes not in raw_bytes, (
            "JSONL の生バイト列に U+2029 (PARAGRAPH SEPARATOR) が含まれている。"
            "ensure_ascii=False 下で U+2029 がエスケープされていない (sec-H-1)。"
        )

    def test_u2028_lines_are_valid_json(self, tmp_path: Path) -> None:
        r"""U+2028 を含む payload を投入後、各行が json.loads でパースできること。

        [Red] U+2028 がエスケープされていない場合、splitlines() で行が分割されて
        json.loads がパース失敗する。
        """
        module = _load_hook_module(HOOK_PATH)

        payload = _make_start_payload()
        payload["agent_type"] = "Explore Line1 Line2"
        payload["session_id"] = "sess-u2028-parse"

        _run_hook(module, payload, tmp_path)

        log_file = tmp_path / "agent-runs.jsonl"
        raw_text = log_file.read_text(encoding="utf-8")
        for i, line in enumerate(raw_text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError as e:
                pytest.fail(
                    f"行 {i+1} が json.loads でパースできない (U+2028/U+2029 エスケープ未実施): {e}"
                )


# ---------------------------------------------------------------------------
# Test 11: stdin サイズ上限テスト (sec-M-2)
# ---------------------------------------------------------------------------


class TestStdinSizeLimit:
    """test_11: 巨大な stdin (MAX_STDIN_BYTES 超) を投入したとき、
    エラーで終了するか空 record を書かないかのいずれかの想定挙動になること。
    """

    def test_oversized_stdin_does_not_write_record(self, tmp_path: Path) -> None:
        """1.5 MB のダミー文字列を含む payload を投入したとき:
        - exit code 0 で終了すること (hook として異常終了しないこと)
        - ログに record が書き込まれないこと (MAX_STDIN_BYTES 超過で記録しない)

        [Red] 現行実装は sys.stdin.read() でサイズ制限なく読み込むため、
        巨大 payload でも record がログに書き込まれる。
        実装後: MAX_STDIN_BYTES = 1 * 1024 * 1024 を超えたら record を書かずに return 0。
        """
        module = _load_hook_module(HOOK_PATH)
        log_file = str(tmp_path / "agent-runs.jsonl")
        module.LOG_DIR = str(tmp_path)
        module.LOG_FILE = log_file

        # 1.5 MB のダミー文字列を含む payload を生成
        dummy_large_value = "x" * (1 * 1024 * 1024 + 512 * 1024)  # 1.5 MB
        large_payload = {
            "session_id": "sess-large",
            "agent_id": "agent-large",
            "agent_type": "Explore",
            "hook_event_name": "SubagentStart",
            "cwd": "/tmp",
            "transcript_path": "/tmp/transcript.jsonl",
            "last_assistant_message": dummy_large_value,
        }
        large_json = json.dumps(large_payload)
        # JSON 全体が 1 MB を超えていること
        assert len(large_json.encode("utf-8")) > 1 * 1024 * 1024, (
            "テストデータが 1 MB を超えていない (テスト設定ミス)"
        )

        original_stdin = sys.stdin
        sys.stdin = io.StringIO(large_json)
        try:
            ret = module.main()
        finally:
            sys.stdin = original_stdin

        # exit code は 0 (hook として異常終了しない)
        assert ret == 0, f"巨大 stdin 時に main() が 0 以外を返した: {ret}"

        # ログに record が書き込まれていないこと (MAX_STDIN_BYTES 超過で記録しない)
        # [Red] 現行実装は上限なしで読むため record が書き込まれ、このアサーションが失敗する。
        records = _read_records(tmp_path)
        assert len(records) == 0, (
            f"巨大 stdin (1.5 MB) 時に record が書き込まれた: {len(records)} 件。"
            "MAX_STDIN_BYTES 超過時は record を書かずに return 0 すること (sec-M-2)。"
        )


# ---------------------------------------------------------------------------
# Test 12: payload サニタイズテスト (sec-M-1)
# ---------------------------------------------------------------------------


class TestPayloadSanitization:
    """test_12: last_assistant_message を含む payload を投入したとき、
    ログに保存された record の payload には last_assistant_message が含まれないこと。
    ホワイトリスト対象フィールドは含まれていること。
    """

    SAFE_FIELDS = {
        "hook_event_name",
        "session_id",
        "agent_id",
        "agent_type",
        "cwd",
        "transcript_path",
        "stop_hook_active",
        "permission_mode",
    }

    def test_last_assistant_message_excluded(self, tmp_path: Path) -> None:
        """SubagentStop payload に last_assistant_message を含めて投入したとき、
        ログに保存された payload には last_assistant_message が含まれないこと。

        [Red] 現行実装は payload 全体をそのまま保存するため、
        last_assistant_message が残っておりこのテストが失敗する。
        """
        module = _load_hook_module(HOOK_PATH)
        payload = _make_stop_payload(
            session_id="sess-sanitize",
            agent_id="agent-sanitize",
            last_message="SECRET_CONTENT_MUST_NOT_BE_LOGGED",
        )
        assert "last_assistant_message" in payload, "テストデータに last_assistant_message が無い (設定ミス)"

        _run_hook(module, payload, tmp_path)

        records = _read_records(tmp_path)
        assert len(records) == 1

        saved_payload = records[0].get("payload", {})
        assert "last_assistant_message" not in saved_payload, (
            "payload に last_assistant_message が残っている (サニタイズ未実施: sec-M-1)"
        )

    def test_safe_fields_preserved(self, tmp_path: Path) -> None:
        """payload のホワイトリスト対象フィールドがログに保存されること。

        [Green] 現行実装は payload 全体を保存するため、現在は通る。
        サニタイズ実装後も safe_fields が保持されることを確認する。
        """
        module = _load_hook_module(HOOK_PATH)
        payload = _make_stop_payload(
            session_id="sess-safe-fields",
            agent_id="agent-safe-fields",
        )

        _run_hook(module, payload, tmp_path)

        records = _read_records(tmp_path)
        assert len(records) == 1
        saved_payload = records[0].get("payload", {})

        # ホワイトリスト対象フィールドは保存されていること
        expected_present = {
            "hook_event_name",
            "session_id",
            "agent_id",
            "agent_type",
            "cwd",
            "transcript_path",
        }
        for field in expected_present:
            assert field in saved_payload, (
                f"ホワイトリスト対象フィールド {field!r} が payload から欠落している"
            )

    def test_agent_transcript_path_excluded(self, tmp_path: Path) -> None:
        """agent_transcript_path はホワイトリスト外のため保存されないこと。

        [Red] 現行実装は payload 全体を保存するため、agent_transcript_path も
        保存されておりこのテストが失敗する。
        """
        module = _load_hook_module(HOOK_PATH)
        payload = _make_stop_payload(
            session_id="sess-transcript",
            agent_id="agent-transcript",
        )
        assert "agent_transcript_path" in payload, (
            "テストデータに agent_transcript_path が無い (設定ミス)"
        )

        _run_hook(module, payload, tmp_path)

        records = _read_records(tmp_path)
        assert len(records) == 1
        saved_payload = records[0].get("payload", {})
        assert "agent_transcript_path" not in saved_payload, (
            "payload に agent_transcript_path が残っている (サニタイズ未実施: sec-M-1)"
        )


# ---------------------------------------------------------------------------
# Test 13: _append_log の TypeError 耐性テスト (code-M-2)
# ---------------------------------------------------------------------------


class TestAppendLogTypeErrorTolerance:
    """test_13: _append_log に JSON シリアライズ不可能な値が入った場合、
    スクリプトが exit 0 で終了すること。
    """

    def test_json_dumps_typeerror_exits_zero(self, tmp_path: Path) -> None:
        """json.dumps が TypeError を上げるよう module.json を差し替えたとき、
        main() が 0 を返すこと (TypeError を catch して return 0)。

        [Red] 現行実装は _append_log の例外 catch が OSError のみのため、
        json.dumps の TypeError が上位に伝播して異常終了する。
        code-M-2 対応で Exception に広げた後にこのテストが通る。
        """
        module = _load_hook_module(HOOK_PATH)
        log_dir = str(tmp_path)
        log_file = str(tmp_path / "agent-runs.jsonl")
        module.LOG_DIR = log_dir
        module.LOG_FILE = log_file

        # module.json を TypeError を上げる json モックに差し替える
        import json as real_json

        class _MockJson:
            """_append_log 内の json.dumps だけ TypeError を上げるモック。"""
            loads = staticmethod(real_json.loads)
            JSONDecodeError = real_json.JSONDecodeError

            @staticmethod
            def dumps(*args, **kwargs):
                raise TypeError("mock: cannot serialize set")

        original_json = module.json
        module.json = _MockJson()  # type: ignore[attr-defined]

        original_stdin = sys.stdin
        sys.stdin = io.StringIO(real_json.dumps(_make_start_payload()))
        try:
            try:
                ret = module.main()
            except TypeError as e:
                pytest.fail(
                    f"json.dumps TypeError が main() から伝播した: {e}。"
                    "Exception catch を OSError → Exception に拡大すること (code-M-2)。"
                )
        finally:
            sys.stdin = original_stdin
            module.json = original_json  # 元に戻す

        assert isinstance(ret, int), f"main() の戻り値が int でない: {type(ret)}"
        assert ret == 0, (
            f"json.dumps TypeError 時に main() が 0 以外を返した: {ret}。"
            "Exception catch を OSError → Exception に拡大すること (code-M-2)。"
        )

    def test_append_log_direct_typeerror(self, tmp_path: Path) -> None:
        """_append_log を直接呼び出してシリアライズ不可能な record を渡したとき、
        TypeError が外に出ないこと。

        [Red] 現行実装の _append_log は OSError のみ catch しているため、
        json.dumps の TypeError が上位に伝播する。code-M-2 対応後に catch されること。
        """
        module = _load_hook_module(HOOK_PATH)
        module.LOG_DIR = str(tmp_path)
        module.LOG_FILE = str(tmp_path / "agent-runs.jsonl")

        record_with_unserializable = {
            "ts": "2026-01-01T00:00:00+09:00",
            "payload": {"agent_id": "test"},
            # set は JSON シリアライズ不可
            "_internal": {1, 2, 3},
        }

        # _append_log が TypeError を外に出さないこと
        # [Red] 現行実装では TypeError が OSError の catch をすり抜けて伝播する
        try:
            module._append_log(record_with_unserializable)
        except TypeError as e:
            pytest.fail(
                f"_append_log が TypeError を外に出した: {e}。"
                "Exception catch を OSError → Exception に拡大すること (code-M-2)。"
            )
        except Exception:
            # OSError / その他の例外は想定内（TypeError でなければ合格）
            pass
