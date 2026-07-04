"""
Tests for .claude/hooks/pre_compact.py

  TestContextItemsBeforeNoneShowsNA (Low-2)
    - context_items_before キーが存在しない場合、summary に N/A が含まれること

  TestSaveInstruction (AC-7)
    - SAVE_INSTRUCTION が新仕様（「現在地」更新指示と「- [x]」チェックリスト更新を含む）を含む

  TestLastPrecompactCheckpointDt (T1)
    - 純粋関数 `_last_precompact_checkpoint_dt` の単体テスト（ファイル無し・
      checkpoint 無し・parse 成功/失敗・複数行からの最新行選択・非 PreCompact
      ラベルの除外・naive datetime 拒否）

  TestDebounceWindowSecondsConstant (T1)
    - モジュール定数 `DEBOUNCE_WINDOW_SECONDS` が 10 であること

  TestMainDebounce (T1)
    - main() の in-process 統合テスト。直近 DEBOUNCE_WINDOW_SECONDS 秒以内の
      checkpoint がある場合は追記・stdout 出力ともにスキップされ、窓外・
      初回起動・timestamp 破損時は従来どおり追記・出力されることを検証する
      （ちょうど 10 秒境界での `<` 厳密比較の固定を含む）

  TestLastPrecompactCheckpointDtLineSeparatorSanitization (FA1 / CR-NEW)
    - checkpoint body 中の特殊行区切り文字（\\x85 / U+2028 / U+2029）が
      偽の checkpoint 行として誤検出されないこと

  TestLastPrecompactCheckpointDtStderrDiagnostics (FA1 / CR-L-003)
    - timestamp parse 失敗時のみ stderr に診断ログが 1 行出力され、
      ファイル無し時は無音のままである非対称仕様

  TestLastPrecompactCheckpointDtReDoSGuard (FB1 / SR-NEW, fix-cycle-2)
    - `]` で閉じない長大な checkpoint 風の行でも `_last_precompact_checkpoint_dt`
      が短時間で None を返すこと（ReDoS 根絶）、および
      `MAX_CHECKPOINT_LINE_LEN` による行長ガード・行末空白許容の維持を検証する

  TestLastPrecompactCheckpointDtFutureDateGuard (FB3 / SR-V-001, fix-cycle-2)
    - `_last_precompact_checkpoint_dt(session_file, now=...)` が、許容スキューを
      超える未来日時の checkpoint を異常値として None（fail-open）で棄却し、
      許容スキュー内は従来どおり aware datetime を返すことを検証する

  TestMainFutureCheckpointDebounceGuard (FB3 / SR-V-001, fix-cycle-2)
    - 未来日時の偽 checkpoint が存在しても main() のデバウンスが恒久停止せず、
      checkpoint 追記・additionalContext 出力が継続されることを検証する

  TestLastPrecompactCheckpointDtDiagnosticTruncation (FB5 / SR-R-001, fix-cycle-2)
    - parse 失敗時の stderr 診断ログに出す捕捉テキストが固定長（64文字）に
      切り詰められ、全文が出力されないことを検証する

実装は 'N/A' 出力・新 SAVE_INSTRUCTION 文面・PreCompact checkpoint デバウンス機能に
修正済み。本テスト群は将来の退行を防ぐ回帰防止テスト。
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests._pre_compact_helpers import (
    _load_pre_compact_module,
    _run_main_in_process,
)

# Paths / Helpers はいずれも tests/_pre_compact_helpers.py（CR-M-001 対応の共通モジュール）
# から import する。tests/test_precompact_additional.py と重複定義しない。

# fix-cycle-3 FC2 (CR-M-002): `_last_precompact_checkpoint_dt` にハードコードされた
# 絶対 UTC 時刻フィクスチャ（例: "2026-07-04T06:50:52.123456+00:00"）を検証する際に
# 使う固定 `now`。`now=` を省略すると関数内部で `datetime.now(timezone.utc)`（実際の
# 壁時計）にフォールバックし、FUTURE_SKEW_TOLERANCE_SECONDS=60 に短縮された結果、
# テスト作成時点のフィクスチャ時刻が実行時点の壁時計より「未来」と判定され棄却される
# （壁時計依存によるフレーク）。フィクスチャの ts より後の固定 `now` を明示的に渡す
# ことで、壁時計に依存せず決定的に PASS させる。
_FIXED_NOW_FOR_HARDCODED_TS_FIXTURES = datetime(2026, 7, 4, 7, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# TestContextItemsBeforeNoneShowsNA (Low-2)
# ---------------------------------------------------------------------------


class TestContextItemsBeforeNoneShowsNA:
    """[Red Round 5] context_items_before キー不在時のサマリ出力を検証する。

    Current implementation:
        context_items_before = payload.get('context_items_before', 0)

    Problem:
        - Key absent  -> value = 0 -> summary shows "- context_items_before: 0"
        - Key present with value 0 -> value = 0 -> summary shows "- context_items_before: 0"
        These two situations are indistinguishable. You can't tell if it was
        "actually 0" or "key was missing (unknown)".

    Expected after fix:
        Use payload.get('context_items_before') (default None) and then:
        - If None  -> output "- context_items_before: N/A"
        - If 0     -> output "- context_items_before: 0"

    実装は 'N/A' 出力で修正済み。本テストは将来 '0' 出力に退行しないかを守る Green 回帰防止テスト。

    [T3] architecture-report §8-1 案 A に基づき、in-process 隔離方式へ移行済み
    （旧: subprocess で実際の .claude/memory/sessions/ に書き込み・読み取りしていた）。
    """

    def test_context_items_before_none_shows_na(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """[Low-2] context_items_before キーが payload に存在しない場合、
        セッションファイルへ書き込まれるサマリに 'N/A' が含まれること。

        検証方法: pre_compact.py の main() を in-process で実行し、
        tmp_path 配下のセッションファイルの内容を確認する（実 state 非参照）。
        キーが存在しない payload を渡したとき、書き込まれたサマリに 'N/A' が含まれるか確認する。
        """
        # payload に context_items_before キーを含めない
        payload_without_key = {"trigger": "manual"}

        module, sessions_dir, fake_stdout = _run_main_in_process(
            monkeypatch, tmp_path, payload_without_key
        )

        # stdout は JSON 形式のフック出力
        stdout_text = fake_stdout.getvalue().strip()
        assert stdout_text, (
            "pre_compact.py の stdout が空。worktree として検出されていないか確認が必要。"
        )

        # stdout の JSON に additionalContext が含まれていることを確認
        try:
            output = json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            pytest.fail(
                f"stdout が JSON でない: {exc}\n"
                f"stdout: {stdout_text!r}"
            )

        assert "hookSpecificOutput" in output, (
            f"stdout JSON に hookSpecificOutput がない。keys: {list(output.keys())}"
        )

        # tmp セッションディレクトリに書き込まれたサマリを確認する
        today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        session_file = sessions_dir / f"{today_str}.tmp"

        assert session_file.exists(), (
            f"セッションファイルが存在しない: {session_file}\n"
            f"pre_compact.py がセッションファイルを作成したはず。"
        )

        content = session_file.read_text(encoding="utf-8")

        # context_items_before: N/A が含まれるか確認
        assert "context_items_before: N/A" in content, (
            "[Low-2] context_items_before キーが payload に存在しない場合、"
            "サマリに 'N/A' が含まれるべき。\n"
            "現在の実装: `payload.get('context_items_before', 0)` は\n"
            "  - キー不在のとき 0 を返す\n"
            "  - '0' と 'N/A' を区別できない\n"
            "期待する修正: `payload.get('context_items_before')` を使い、\n"
            "  値が None の場合は 'N/A' を出力する。\n"
            f"実際のセッションファイル内容（最後の200文字）:\n"
            f"{content[-200:]!r}"
        )

    def test_context_items_before_zero_shows_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """[Low-2] context_items_before キーが 0 の場合、サマリに '0' が含まれること。

        キーが存在して値が 0 の場合と、キーが不在の場合（N/A）を区別できるように、
        値が 0 のときは '0' がそのまま出力されること。

        [T3] in-process 移行により各テストが fresh tmp sessions を使うため、
        旧実装にあった「前のテストの実行順序に依存する pytest.skip」は不要になった。
        """
        payload_with_zero = {"trigger": "manual", "context_items_before": 0}

        module, sessions_dir, fake_stdout = _run_main_in_process(
            monkeypatch, tmp_path, payload_with_zero
        )

        # tmp セッションファイルの内容を確認
        today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        session_file = sessions_dir / f"{today_str}.tmp"

        assert session_file.exists(), (
            f"セッションファイルが存在しない: {session_file}\n"
            f"pre_compact.py がセッションファイルを作成したはず。"
        )

        content = session_file.read_text(encoding="utf-8")

        # context_items_before: 0 が含まれるか確認（N/A ではなく 0）
        assert "context_items_before: 0" in content, (
            "[Low-2] context_items_before=0 のとき、サマリに '0' が含まれるべき。\n"
            "N/A ではなく実際の値 '0' が出力されること。\n"
            f"実際のセッションファイル内容（最後の200文字）:\n"
            f"{content[-200:]!r}"
        )


# ---------------------------------------------------------------------------
# TestSaveInstructionNewSpec (AC-7)
# ---------------------------------------------------------------------------


class TestSaveInstruction:
    """[AC-7] SAVE_INSTRUCTION が新仕様（「更新」志向）の文面を含むこと。

    architecture §5.2 に従い、SAVE_INSTRUCTION は以下の2点を必ず含む:
    1. 「現在地:」を現フェーズ名に更新する指示（「現在地」というキーワード）
    2. 「## 残タスク」をチェックリストとして更新する指示（「- [x]」によるチェック化指示）

    旧文面（「書き出してください」という無上限追記指示）とは異なり、
    「更新」を促す文面であること。「無上限追記」を促す文面でないこと。
    """

    def test_save_instruction_contains_genba_update_keyword(self) -> None:
        """SAVE_INSTRUCTION に「現在地」の更新指示が含まれる（AC-7）。

        architecture §5.2 で確定した新文面: 「現在地:」行を現フェーズ名に更新することを
        明示的に促す文言が含まれること。
        """
        module = _load_pre_compact_module()
        instruction = module.SAVE_INSTRUCTION
        assert "現在地" in instruction, (
            "[AC-7] SAVE_INSTRUCTION に「現在地」更新指示が含まれていない。\n"
            "architecture §5.2 の新文面: 「現在地:」行を現フェーズ名に更新することを\n"
            "明示的に含む文面に変更すること。\n"
            f"現在の SAVE_INSTRUCTION:\n{instruction!r}"
        )

    def test_save_instruction_contains_checklist_update_keyword(self) -> None:
        """SAVE_INSTRUCTION に「- [x]」チェックリスト更新指示が含まれる（AC-7）。

        architecture §5.2 で確定した新文面: 完了タスクを「- [x]」でチェック化することを
        明示的に促す文言（「- [x]」という文字列）が含まれること。
        """
        module = _load_pre_compact_module()
        instruction = module.SAVE_INSTRUCTION
        assert "- [x]" in instruction, (
            "[AC-7] SAVE_INSTRUCTION に「- [x]」チェックリスト更新指示が含まれていない。\n"
            "architecture §5.2 の新文面: 完了タスクを「- [x]」化することを\n"
            "明示的に含む文面に変更すること。\n"
            f"現在の SAVE_INSTRUCTION:\n{instruction!r}"
        )

    def test_save_instruction_does_not_promote_unlimited_append(self) -> None:
        """SAVE_INSTRUCTION が「無上限追記」を促す旧文面でないこと（AC-7）。

        旧文面は「書き出してください」という追記指示だった。
        新文面は「更新」であり、追記を促さないこと（「書き出してください」が含まれないこと）。
        """
        module = _load_pre_compact_module()
        instruction = module.SAVE_INSTRUCTION
        assert "書き出してください" not in instruction, (
            "[AC-7] SAVE_INSTRUCTION に旧文面「書き出してください」が残っている。\n"
            "無上限追記を促す文面から「更新」を促す文面への転換が未完了。\n"
            "architecture §5.2 の新文面に置き換えること。\n"
            f"現在の SAVE_INSTRUCTION:\n{instruction!r}"
        )


# ---------------------------------------------------------------------------
# T1: PreCompact checkpoint デバウンス機能
#
# architecture-report-20260704-065052.md §4.2 準拠。in-process 方式のみを用い、
# subprocess は使わない。実 .claude/memory/sessions/ には一切触れない。
#
#   - TestLastPrecompactCheckpointDt: 純粋関数 `_last_precompact_checkpoint_dt`
#     の単体テスト（要件④パターン3・4 + valid parse + naive datetime 防御）。
#     ファイル無し・checkpoint 無し・parse 失敗・naive datetime のいずれも
#     None を返す fail-open が中核仕様であることを固定する。
#   - TestDebounceWindowSecondsConstant: モジュール定数 `DEBOUNCE_WINDOW_SECONDS`
#     が 10 であることを固定する。
#   - TestMainDebounce: main() の in-process 統合テスト（要件④パターン1・2、
#     および3・4の main() 経由確認）。パターン1（窓内・ちょうど10秒境界含む）は
#     追記・stdout 出力がスキップされ、パターン2・3・4（窓外・初回起動・
#     timestamp 破損）は従来どおり追記・出力されることを固定する。
# ---------------------------------------------------------------------------


def _write_precompact_checkpoint_line(session_file: Path, ts_text: str, label: str = "PreCompact: manual") -> None:
    """session_file に PreCompact checkpoint 行を直接書く（append_checkpoint は使わない）。

    フォーマットは architecture §1.2 の実フォーマットに一致させる:
        ## [Checkpoint: {label} - {ts}]
        {body}
    """
    session_file.parent.mkdir(parents=True, exist_ok=True)
    with open(session_file, "a", encoding="utf-8") as f:
        f.write(f"\n## [Checkpoint: {label} - {ts_text}]\nbody\n")


class TestLastPrecompactCheckpointDt:
    """[T1] 純粋関数 `_last_precompact_checkpoint_dt(session_file)` の単体テスト。

    architecture §3.1〜3.4 準拠。実 state に一切触れず、tmp_path 配下のみ読み書きする。
    """

    def test_returns_none_when_file_does_not_exist(self, tmp_path: Path) -> None:
        """[要件④パターン3] ファイルが存在しない場合は None（fail-open の中核）。"""
        module = _load_pre_compact_module()
        missing = tmp_path / "20260704.tmp"
        assert not missing.exists()

        result = module._last_precompact_checkpoint_dt(str(missing))

        assert result is None

    def test_returns_none_when_no_precompact_checkpoint_line(self, tmp_path: Path) -> None:
        """[要件④パターン3] PreCompact checkpoint 行が存在しない場合は None。"""
        module = _load_pre_compact_module()
        session_file = tmp_path / "20260704.tmp"
        session_file.write_text("SESSION: 20260704\n現在地: \n", encoding="utf-8")

        result = module._last_precompact_checkpoint_dt(str(session_file))

        assert result is None

    def test_returns_aware_datetime_for_valid_checkpoint(self, tmp_path: Path) -> None:
        """有効な PreCompact checkpoint 行から aware datetime を parse できる。"""
        module = _load_pre_compact_module()
        session_file = tmp_path / "20260704.tmp"
        ts_text = "2026-07-04T06:50:52.123456+00:00"
        _write_precompact_checkpoint_line(session_file, ts_text)

        # [CR-M-002] now= を明示指定し壁時計依存を除去する（フィクスチャ ts の後の固定時刻）。
        result = module._last_precompact_checkpoint_dt(
            str(session_file), now=_FIXED_NOW_FOR_HARDCODED_TS_FIXTURES
        )

        assert result is not None
        assert result.tzinfo is not None, "戻り値は aware datetime でなければならない"
        assert result.isoformat() == ts_text

    def test_picks_last_precompact_line_when_multiple_present(self, tmp_path: Path) -> None:
        """複数の PreCompact checkpoint がある場合、最後の行の timestamp を採用する。"""
        module = _load_pre_compact_module()
        session_file = tmp_path / "20260704.tmp"
        older_ts = "2026-07-04T06:00:00.000000+00:00"
        newer_ts = "2026-07-04T06:50:52.123456+00:00"
        _write_precompact_checkpoint_line(session_file, older_ts)
        _write_precompact_checkpoint_line(session_file, newer_ts)

        # [CR-M-002] now= を明示指定し壁時計依存を除去する。
        result = module._last_precompact_checkpoint_dt(
            str(session_file), now=_FIXED_NOW_FOR_HARDCODED_TS_FIXTURES
        )

        assert result is not None
        assert result.isoformat() == newer_ts

    def test_returns_none_for_broken_timestamp(self, tmp_path: Path) -> None:
        """[要件④パターン4] 壊れた timestamp（parse 不能）の場合は None（fail-open）。"""
        module = _load_pre_compact_module()
        session_file = tmp_path / "20260704.tmp"
        _write_precompact_checkpoint_line(session_file, "not-a-timestamp")

        result = module._last_precompact_checkpoint_dt(str(session_file))

        assert result is None

    def test_returns_none_for_naive_timestamp(self, tmp_path: Path) -> None:
        """[要件④パターン4 派生] tz なし（naive）の timestamp は None（比較不能=安全側）。"""
        module = _load_pre_compact_module()
        session_file = tmp_path / "20260704.tmp"
        _write_precompact_checkpoint_line(session_file, "2026-07-04T06:50:52.123456")

        result = module._last_precompact_checkpoint_dt(str(session_file))

        assert result is None

    def test_ignores_non_precompact_checkpoint_labels(self, tmp_path: Path) -> None:
        """PreCompact 以外のラベル（例: Wave checkpoint）は無視する。"""
        module = _load_pre_compact_module()
        session_file = tmp_path / "20260704.tmp"
        _write_precompact_checkpoint_line(
            session_file, "2026-07-04T06:50:52.123456+00:00", label="Wave 2 success"
        )

        result = module._last_precompact_checkpoint_dt(str(session_file))

        assert result is None


class TestDebounceWindowSecondsConstant:
    """[T1] モジュール定数 `DEBOUNCE_WINDOW_SECONDS` の検証（architecture §3.5）。"""

    def test_debounce_window_seconds_is_ten(self) -> None:
        module = _load_pre_compact_module()
        assert module.DEBOUNCE_WINDOW_SECONDS == 10


class TestMainDebounce:
    """[T1] main() の in-process 統合テスト（要件④パターン1・2、および3・4の main() 経由確認）。

    architecture §4.2 の in-process 方式:
      - `mod.SESSIONS_DIR` を tmp override
      - `sys.stdin` を `io.StringIO(json.dumps(payload))` に差し替え
      - `sys.stdout` を明示的に `io.StringIO()` に差し替えて捕捉する
        （capsys は使わない。pre_compact.py は import 時に
        `sys.stdout.reconfigure(encoding='utf-8')` を呼ぶため、
        capsys のキャプチャストリームでは reconfigure に失敗し得る。
        tests/test_execution_and_hook_patterns.md の I-01 パターンに準拠）
      - `os.getcwd` を tmp_path に固定して非 worktree 経路を保証
    実 `.claude/memory/sessions/` には一切触れない。
    """

    def _setup_module(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict) -> tuple[types.ModuleType, Path]:
        module = _load_pre_compact_module()
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(module, "SESSIONS_DIR", str(sessions_dir))
        monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path))
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        return module, sessions_dir

    def _session_file_for_today(self, sessions_dir: Path) -> Path:
        today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        return sessions_dir / f"{today_str}.tmp"

    def _checkpoint_block_count(self, session_file: Path) -> int:
        if not session_file.exists():
            return 0
        return session_file.read_text(encoding="utf-8").count("## [Checkpoint: PreCompact:")

    def test_within_window_skips_append_and_stdout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """[要件④パターン1] 10秒以内の直近 checkpoint がある場合、
        追記ブロックは増えず・stdout に additionalContext も出ない。
        """
        module, sessions_dir = self._setup_module(
            monkeypatch, tmp_path, {"trigger": "manual", "context_items_before": 5}
        )
        session_file = self._session_file_for_today(sessions_dir)
        recent_ts = (datetime.now(timezone.utc) - timedelta(seconds=3)).isoformat()
        _write_precompact_checkpoint_line(session_file, recent_ts)
        block_count_before = self._checkpoint_block_count(session_file)

        fake_stdout = io.StringIO()
        monkeypatch.setattr(sys, "stdout", fake_stdout)
        module.main()

        block_count_after = self._checkpoint_block_count(session_file)
        assert block_count_after == block_count_before, (
            "10秒以内の直近 checkpoint がある場合、新たな追記は発生しないはず"
        )
        assert fake_stdout.getvalue().strip() == "", (
            "10秒以内の直近 checkpoint がある場合、stdout は空であるはず"
        )

    def test_boundary_exactly_ten_seconds_still_appends(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """[CR-T-001] デバウンス窓ちょうど（`now - last == 10.0` 秒）は
        `<` 厳密比較により追記される（スキップされない）ことを固定する。

        実時間の揺らぎなしに `now - last` をちょうど 10.0 秒に固定するため、
        main() 内部で参照される `module.datetime.now()` を固定値に差し替える
        （`module.datetime` を fixed-now を返すサブクラスに置き換える。
        `fromisoformat` 等の他メソッドは通常の継承でそのまま動作する）。
        将来 `now - last < DEBOUNCE_WINDOW_SECONDS` が `<=` に誤って
        変更された場合、この境界で追記がスキップされるようになり検知できる。
        """
        module, sessions_dir = self._setup_module(
            monkeypatch, tmp_path, {"trigger": "manual", "context_items_before": 5}
        )
        fixed_now = datetime.now(timezone.utc)
        boundary_ts = (fixed_now - timedelta(seconds=10)).isoformat()

        session_file = self._session_file_for_today(sessions_dir)
        _write_precompact_checkpoint_line(session_file, boundary_ts)
        block_count_before = self._checkpoint_block_count(session_file)

        class _FixedNowDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now

        monkeypatch.setattr(module, "datetime", _FixedNowDatetime)

        fake_stdout = io.StringIO()
        monkeypatch.setattr(sys, "stdout", fake_stdout)
        module.main()

        block_count_after = self._checkpoint_block_count(session_file)
        assert block_count_after == block_count_before + 1, (
            "デバウンス窓ちょうど10秒（now - last == 10.0）は `<` 比較により"
            "追記されるはず。`<=` へ変更されるとこの境界でスキップされてしまう。"
        )
        output = json.loads(fake_stdout.getvalue().strip())
        assert output["hookSpecificOutput"]["additionalContext"]

    def test_outside_window_appends_and_outputs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """[要件④パターン2] 10秒超前の checkpoint の場合、従来どおり追記・出力される。"""
        module, sessions_dir = self._setup_module(
            monkeypatch, tmp_path, {"trigger": "manual", "context_items_before": 5}
        )
        session_file = self._session_file_for_today(sessions_dir)
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=15)).isoformat()
        _write_precompact_checkpoint_line(session_file, old_ts)
        block_count_before = self._checkpoint_block_count(session_file)

        fake_stdout = io.StringIO()
        monkeypatch.setattr(sys, "stdout", fake_stdout)
        module.main()

        block_count_after = self._checkpoint_block_count(session_file)
        assert block_count_after == block_count_before + 1
        output = json.loads(fake_stdout.getvalue().strip())
        assert output["hookSpecificOutput"]["additionalContext"]

    def test_first_run_no_checkpoint_appends(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """[要件④パターン3] 初回起動（ファイル無し・checkpoint 無し）→ fail-open で追記される。"""
        module, sessions_dir = self._setup_module(
            monkeypatch, tmp_path, {"trigger": "manual", "context_items_before": 5}
        )
        session_file = self._session_file_for_today(sessions_dir)
        assert not session_file.exists()

        fake_stdout = io.StringIO()
        monkeypatch.setattr(sys, "stdout", fake_stdout)
        module.main()

        assert session_file.exists()
        assert self._checkpoint_block_count(session_file) == 1
        output = json.loads(fake_stdout.getvalue().strip())
        assert output["hookSpecificOutput"]["additionalContext"]

    def test_broken_timestamp_fails_open_and_appends(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """[要件④パターン4] 壊れた timestamp の checkpoint → fail-open で追記される。"""
        module, sessions_dir = self._setup_module(
            monkeypatch, tmp_path, {"trigger": "manual", "context_items_before": 5}
        )
        session_file = self._session_file_for_today(sessions_dir)
        _write_precompact_checkpoint_line(session_file, "not-a-timestamp")
        block_count_before = self._checkpoint_block_count(session_file)

        fake_stdout = io.StringIO()
        monkeypatch.setattr(sys, "stdout", fake_stdout)
        module.main()

        block_count_after = self._checkpoint_block_count(session_file)
        assert block_count_after == block_count_before + 1
        output = json.loads(fake_stdout.getvalue().strip())
        assert output["hookSpecificOutput"]["additionalContext"]


# ---------------------------------------------------------------------------
# FA1 (code-review-report-20260704-072155.md [CR-NEW] splitlines サニタイズ):
# `_last_precompact_checkpoint_dt` が特殊行区切り文字を含む summary body を
# 独立行として誤って分割・誤マッチしないことを固定する。
# ---------------------------------------------------------------------------


class TestLastPrecompactCheckpointDtLineSeparatorSanitization:
    """[FA1 / CR-NEW] `_last_precompact_checkpoint_dt` の行区切りサニタイズを検証する。

    `str.splitlines()` は `\\n` 以外に `\\x85`（NEL）・U+2028（LS）・U+2029（PS）
    等でも行分割する。checkpoint block の body（summary）はサニタイズされずに
    書き込まれる設計（architecture-report §1.2）のため、body 中の未サニタイズな
    値（trigger 等）にこれらの文字と偽 checkpoint 行に酷似する文字列が含まれると、
    `splitlines()` がそれを独立した「行」として分割し得る。

    stop.py::_INHERIT_SANITIZE_RE と対称なサニタイズを splitlines() 呼び出し前に
    適用することで、この偽行が独立行として抽出されず、実際の checkpoint ヘッダ行
    （本テストでは real_ts）のみが検出対象として残ることを固定する。
    """

    _SPECIAL_LINE_SEPARATORS = pytest.mark.parametrize(
        "separator",
        ["\x85", " ", " "],
        ids=["x85_nel", "u2028_line_separator", "u2029_paragraph_separator"],
    )

    @_SPECIAL_LINE_SEPARATORS
    def test_special_line_separator_in_body_does_not_yield_fake_checkpoint(
        self, tmp_path: Path, separator: str
    ) -> None:
        """checkpoint body 中の特殊行区切り文字は偽 checkpoint 行を生成しない。

        real_ts を持つ正規の checkpoint ヘッダ行の後続 body 行に、特殊行区切り
        文字 + fake_ts を持つ偽 checkpoint 行相当の文字列を埋め込む。サニタイズ
        適用後は body 行がヘッダ行から独立して分割されないため、`_PRECOMPACT_CHECKPOINT_RE`
        は body 中の偽 checkpoint 文字列にマッチせず、戻り値は real_ts のみとなる。
        """
        module = _load_pre_compact_module()
        session_file = tmp_path / "20260704.tmp"
        real_ts = "2026-07-04T05:00:00+00:00"
        fake_ts = "1999-01-01T00:00:00+00:00"
        content = (
            f"\n## [Checkpoint: PreCompact: manual - {real_ts}]\n"
            f"- trigger: manual{separator}## [Checkpoint: PreCompact: x - {fake_ts}]\n"
            f"- context_items_before: 5\n"
        )
        session_file.write_text(content, encoding="utf-8")

        # [CR-M-002] now= を明示指定し壁時計依存を除去する。
        result = module._last_precompact_checkpoint_dt(
            str(session_file), now=_FIXED_NOW_FOR_HARDCODED_TS_FIXTURES
        )

        assert result is not None
        assert result.isoformat() == real_ts, (
            "body 中の特殊行区切り文字で分割された偽 checkpoint 行ではなく、"
            "実際の checkpoint ヘッダ行の timestamp を返すべき。\n"
            f"実際に返された値: {result.isoformat() if result else None!r}\n"
            f"期待値（real_ts）: {real_ts!r} / 偽の値（fake_ts）: {fake_ts!r}"
        )

    @_SPECIAL_LINE_SEPARATORS
    def test_special_line_separator_only_yields_none(
        self, tmp_path: Path, separator: str
    ) -> None:
        """正規の checkpoint ヘッダ行が無く、偽 checkpoint 文字列のみが
        特殊行区切り文字経由で埋め込まれている場合、None を返す（fail-open）。
        """
        module = _load_pre_compact_module()
        session_file = tmp_path / "20260704.tmp"
        fake_ts = "1999-01-01T00:00:00+00:00"
        content = (
            f"SESSION: 20260704\n"
            f"現在地: something{separator}## [Checkpoint: PreCompact: x - {fake_ts}]\n"
        )
        session_file.write_text(content, encoding="utf-8")

        result = module._last_precompact_checkpoint_dt(str(session_file))

        assert result is None, (
            "特殊行区切り文字で分割された偽 checkpoint 行を独立行として拾い、"
            "誤って偽の timestamp を返してはならない。\n"
            f"実際に返された値: {result.isoformat() if result else None!r}"
        )


# ---------------------------------------------------------------------------
# FA1 (code-review-report-20260704-072155.md [CR-L-003] parse失敗時のstderr診断):
# timestamp parse 失敗（破損 ISO 文字列）に限り stderr へ診断ログ 1 行を出す。
# 「ファイル無し」の通常ケースでは無音のままである非対称仕様を固定する。
# ---------------------------------------------------------------------------


class TestLastPrecompactCheckpointDtStderrDiagnostics:
    """[FA1 / CR-L-003] parse 失敗パスの stderr 診断ログを検証する。

    `_last_precompact_checkpoint_dt` は「ファイル無し」（初回起動時の通常ケース）
    と「timestamp が壊れている」（想定外の破損ケース）を同じ `return None` で
    扱うが、後者に限り stderr へ診断ログを 1 行出す。ファイル無しケースで
    ログを出すとほぼ毎日 1 回のスパムになるため、その非対称は維持する。
    """

    def test_broken_timestamp_emits_one_stderr_diagnostic_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """壊れた timestamp（parse 不能）の checkpoint 行を読んだ場合、
        stderr に診断ログが 1 行出力される。戻り値は引き続き None（fail-open）。
        """
        module = _load_pre_compact_module()
        session_file = tmp_path / "20260704.tmp"
        _write_precompact_checkpoint_line(session_file, "not-a-timestamp")
        fake_stderr = io.StringIO()
        monkeypatch.setattr(sys, "stderr", fake_stderr)

        result = module._last_precompact_checkpoint_dt(str(session_file))

        assert result is None
        stderr_lines = [
            line for line in fake_stderr.getvalue().splitlines() if line.strip()
        ]
        assert len(stderr_lines) == 1, (
            "timestamp parse 失敗時は stderr に診断ログが 1 行出力されるべき。\n"
            f"実際の stderr 出力: {fake_stderr.getvalue()!r}"
        )

    def test_missing_file_stays_silent_on_stderr(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ファイルが存在しない通常ケースでは、stderr に何も出力されない。

        parse 失敗ケース（診断ログあり）との非対称仕様を固定する回帰防止テスト。
        """
        module = _load_pre_compact_module()
        missing = tmp_path / "20260704.tmp"
        assert not missing.exists()
        fake_stderr = io.StringIO()
        monkeypatch.setattr(sys, "stderr", fake_stderr)

        result = module._last_precompact_checkpoint_dt(str(missing))

        assert result is None
        assert fake_stderr.getvalue() == "", (
            "ファイル無しの通常ケースでは stderr が無音のままであるべき。\n"
            f"実際の stderr 出力: {fake_stderr.getvalue()!r}"
        )


# ---------------------------------------------------------------------------
# FB1 (fix-cycle-2, security-review-report-20260704-075817.md [SR-NEW]):
# `_PRECOMPACT_CHECKPOINT_RE` の多項式時間バックトラッキング（ReDoS）根絶。
# plan-report-20260704-080808.md の方針: regex を撤去し
# startswith/endswith/rfind(' - ') による手続きパースへ置換する。
# 併せて行長ガード（新規モジュール定数 `MAX_CHECKPOINT_LINE_LEN`）を導入する。
# ---------------------------------------------------------------------------


class TestLastPrecompactCheckpointDtReDoSGuard:
    """[FB1 / SR-NEW] ReDoS 根絶（regex 撤去・手続きパース化）を固定する回帰防止テスト。

    architecture の fail-open 原則を維持しつつ、`## [Checkpoint: PreCompact: `
    で始まり `]` で閉じない長大行に対して短時間で None を返すこと、および
    新規モジュール定数 `MAX_CHECKPOINT_LINE_LEN` による行長ガードが機能する
    ことを固定する。
    """

    def test_unclosed_long_checkpoint_line_returns_none_quickly(
        self, tmp_path: Path
    ) -> None:
        """`" - "` を大量反復し `]` で閉じない長大行があっても、
        短時間（CI のブレを考慮し 2.0 秒未満）で None を返すこと。

        security-review-report-20260704-075817.md [SR-NEW] の実測では、
        `" - "` 反復 20,000 回の行で旧 regex 実装は約 2.77 秒を要した。
        本テストは同規模の入力を用い、手続きパースへの置換後は無視できる
        時間で完了することを固定する。
        """
        module = _load_pre_compact_module()
        session_file = tmp_path / "20260704.tmp"
        unclosed_line = "## [Checkpoint: PreCompact: " + (" - a" * 20000) + "\n"
        session_file.write_text(unclosed_line, encoding="utf-8")

        start = time.perf_counter()
        result = module._last_precompact_checkpoint_dt(str(session_file))
        elapsed = time.perf_counter() - start

        assert result is None, (
            "`]` で閉じない checkpoint 風の行は parse 対象にならず None を返すべき。"
        )
        assert elapsed < 2.0, (
            "ReDoS 相当のバックトラッキングが発生している疑いがある。\n"
            f"経過時間: {elapsed:.3f} 秒（上限 2.0 秒）。\n"
            "regex ベースの `.* - (.+)` パースを手続き的パース"
            "（startswith/endswith/rfind）に置換すること（SR-NEW 修正案2）。"
        )

    def test_overlong_checkpoint_line_is_skipped_by_line_length_guard(
        self, tmp_path: Path
    ) -> None:
        """`MAX_CHECKPOINT_LINE_LEN` を超える checkpoint 行はパース対象から
        除外される（belt-and-suspenders の行長ガード）。

        妥当な checkpoint 行の後に、正しく `]` で閉じてはいるが
        `MAX_CHECKPOINT_LINE_LEN` を超える checkpoint 風の行を続ける。
        超過行がスキップされ、妥当な行の timestamp が採用されることを確認する。
        """
        module = _load_pre_compact_module()
        session_file = tmp_path / "20260704.tmp"
        max_len = module.MAX_CHECKPOINT_LINE_LEN
        valid_ts = "2026-07-04T06:50:52.123456+00:00"

        overlong_label = "PreCompact: " + ("x" * (max_len + 100))
        content = (
            f"\n## [Checkpoint: PreCompact: manual - {valid_ts}]\nbody\n"
            f"\n## [Checkpoint: {overlong_label} - 2026-07-04T07:00:00+00:00]\nbody\n"
        )
        session_file.write_text(content, encoding="utf-8")

        # [CR-M-002] now= を明示指定し壁時計依存を除去する。
        result = module._last_precompact_checkpoint_dt(
            str(session_file), now=_FIXED_NOW_FOR_HARDCODED_TS_FIXTURES
        )

        assert result is not None
        assert result.isoformat() == valid_ts, (
            "MAX_CHECKPOINT_LINE_LEN を超える行はスキップされ、"
            "有効な checkpoint 行の timestamp が採用されるべき。\n"
            f"実際に返された値: {result.isoformat() if result else None!r}"
        )

    def test_trailing_whitespace_after_closing_bracket_still_parses(
        self, tmp_path: Path
    ) -> None:
        """行末に空白（現行 regex の `\\]\\s*$` 相当）があっても、
        手続きパースへの置換後も引き続き parse できること（後退禁止）。
        """
        module = _load_pre_compact_module()
        session_file = tmp_path / "20260704.tmp"
        ts_text = "2026-07-04T06:50:52.123456+00:00"
        session_file.write_text(
            f"\n## [Checkpoint: PreCompact: manual - {ts_text}]   \nbody\n",
            encoding="utf-8",
        )

        # [CR-M-002] now= を明示指定し壁時計依存を除去する。
        result = module._last_precompact_checkpoint_dt(
            str(session_file), now=_FIXED_NOW_FOR_HARDCODED_TS_FIXTURES
        )

        assert result is not None
        assert result.isoformat() == ts_text


# ---------------------------------------------------------------------------
# FB3 (fix-cycle-2, security-review-report-20260704-075817.md [SR-V-001]):
# 未来日時の偽 checkpoint 行によるデバウンス恒久停止（fail-open 悪用）の解消。
# plan-report-20260704-080808.md の方針: `_last_precompact_checkpoint_dt` に
# `now` 引数を追加し、`dt > now + 許容スキュー` の場合は異常値として None を返す。
# ---------------------------------------------------------------------------


class TestLastPrecompactCheckpointDtFutureDateGuard:
    """[FB3 / SR-V-001] 純粋関数レベルでの未来日時ガードを固定する。

    `_last_precompact_checkpoint_dt(session_file, now=...)` は、parse に成功
    した timestamp であっても `now` を許容スキュー超過して超える場合は
    構造的異常値として None（fail-open）を返す。許容スキュー内（NTP の
    後方ステップ補正等を想定）は従来どおり aware datetime を返す。
    """

    def test_future_timestamp_beyond_skew_tolerance_returns_none(
        self, tmp_path: Path
    ) -> None:
        module = _load_pre_compact_module()
        session_file = tmp_path / "20260704.tmp"
        fixed_now = datetime(2026, 7, 4, 6, 50, 52, tzinfo=timezone.utc)
        future_ts = "9999-12-31T23:59:59+00:00"
        _write_precompact_checkpoint_line(session_file, future_ts)

        result = module._last_precompact_checkpoint_dt(str(session_file), now=fixed_now)

        assert result is None, (
            "now を大きく超える未来日時の checkpoint は異常値として"
            "None を返すべき（fail-open）。\n"
            f"実際に返された値: {result.isoformat() if result else None!r}"
        )

    def test_timestamp_within_skew_tolerance_still_returns_datetime(
        self, tmp_path: Path
    ) -> None:
        module = _load_pre_compact_module()
        session_file = tmp_path / "20260704.tmp"
        fixed_now = datetime(2026, 7, 4, 6, 50, 52, tzinfo=timezone.utc)
        near_future_ts = (fixed_now + timedelta(seconds=30)).isoformat()
        _write_precompact_checkpoint_line(session_file, near_future_ts)

        result = module._last_precompact_checkpoint_dt(str(session_file), now=fixed_now)

        assert result is not None, (
            "許容スキュー（推奨60秒）以内の未来日時は正当な checkpoint として"
            "扱われるべき（NTP の後方ステップ補正等での誤棄却を避ける）。"
        )
        assert result.isoformat() == near_future_ts


# ---------------------------------------------------------------------------
# fix-cycle-3 FC2 (code-review-report-20260704-083159.md [CR-T-001]):
# 採用値 `FUTURE_SKEW_TOLERANCE_SECONDS`（60秒）そのものの境界を固定する。
# 定数値をハードコードせず `module.FUTURE_SKEW_TOLERANCE_SECONDS` から算出することで、
# 値が将来 60 から変更された場合（緩められた場合も厳しくされた場合も）に検知できる。
# ---------------------------------------------------------------------------


class TestFutureSkewToleranceBoundary:
    """[CR-T-001] `module.FUTURE_SKEW_TOLERANCE_SECONDS` の境界（60秒）を固定する。

    `TestLastPrecompactCheckpointDtFutureDateGuard` の 2 ケースは year 9999 と
    now+30秒という「どちらの値（60秒・86400秒等）でも同じ結果になる」ケースのみで、
    採用値そのもの（tolerance 秒ちょうど）の境界を検証していなかった
    （FB8/CR-T-001 指摘）。本クラスは `tolerance - 1` 秒（許容）と `tolerance + 1` 秒
    （棄却）の境界で採用値を固定する。
    """

    def test_just_within_tolerance_is_accepted(self, tmp_path: Path) -> None:
        """`now + (tolerance - 1)` 秒の未来 ts は許容され datetime を返す。"""
        module = _load_pre_compact_module()
        session_file = tmp_path / "20260704.tmp"
        fixed_now = datetime(2026, 7, 4, 6, 50, 52, tzinfo=timezone.utc)
        tolerance = module.FUTURE_SKEW_TOLERANCE_SECONDS
        within_ts = (fixed_now + timedelta(seconds=tolerance - 1)).isoformat()
        _write_precompact_checkpoint_line(session_file, within_ts)

        result = module._last_precompact_checkpoint_dt(str(session_file), now=fixed_now)

        assert result is not None, (
            f"FUTURE_SKEW_TOLERANCE_SECONDS={tolerance} 秒未満の未来日時は"
            "許容されるべき（fail-open の誤棄却を避ける）。"
        )
        assert result.isoformat() == within_ts

    def test_just_beyond_tolerance_is_rejected(self, tmp_path: Path) -> None:
        """`now + (tolerance + 1)` 秒の未来 ts は棄却され None を返す。"""
        module = _load_pre_compact_module()
        session_file = tmp_path / "20260704.tmp"
        fixed_now = datetime(2026, 7, 4, 6, 50, 52, tzinfo=timezone.utc)
        tolerance = module.FUTURE_SKEW_TOLERANCE_SECONDS
        beyond_ts = (fixed_now + timedelta(seconds=tolerance + 1)).isoformat()
        _write_precompact_checkpoint_line(session_file, beyond_ts)

        result = module._last_precompact_checkpoint_dt(str(session_file), now=fixed_now)

        assert result is None, (
            f"FUTURE_SKEW_TOLERANCE_SECONDS={tolerance} 秒を超える未来日時は"
            "構造的異常値として棄却されるべき（fail-open）。\n"
            f"実際に返された値: {result.isoformat() if result else None!r}"
        )


class TestMainFutureCheckpointDebounceGuard:
    """[FB3 / SR-V-001] main() の in-process 統合テスト。

    未来日時の偽 checkpoint（例: `9999-12-31T23:59:59+00:00`）が session_file に
    存在しても、main() のデバウンスが恒久停止せず、checkpoint 追記・
    additionalContext 出力が継続されること（SR-V-001 の悪用シナリオ解消）を固定する。
    """

    def _setup_module(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict
    ) -> tuple[types.ModuleType, Path]:
        module = _load_pre_compact_module()
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(module, "SESSIONS_DIR", str(sessions_dir))
        monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path))
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        return module, sessions_dir

    def test_future_dated_checkpoint_does_not_permanently_debounce(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        module, sessions_dir = self._setup_module(
            monkeypatch, tmp_path, {"trigger": "manual", "context_items_before": 5}
        )
        today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        session_file = sessions_dir / f"{today_str}.tmp"
        future_ts = "9999-12-31T23:59:59+00:00"
        _write_precompact_checkpoint_line(session_file, future_ts)
        block_count_before = session_file.read_text(encoding="utf-8").count(
            "## [Checkpoint: PreCompact:"
        )

        fake_stdout = io.StringIO()
        monkeypatch.setattr(sys, "stdout", fake_stdout)
        module.main()

        content_after = session_file.read_text(encoding="utf-8")
        block_count_after = content_after.count("## [Checkpoint: PreCompact:")
        assert block_count_after == block_count_before + 1, (
            "未来日時の偽 checkpoint が存在しても、正当な checkpoint 追記が"
            "スキップされ続けてはならない（fail-open）。\n"
            f"追記前ブロック数: {block_count_before} / 追記後ブロック数: {block_count_after}"
        )
        stdout_text = fake_stdout.getvalue().strip()
        assert stdout_text, (
            "未来日時の偽 checkpoint によりデバウンスが恒久停止し、"
            "additionalContext 出力がスキップされてはならない。"
        )
        output = json.loads(stdout_text)
        assert output["hookSpecificOutput"]["additionalContext"]


# ---------------------------------------------------------------------------
# FB5 (fix-cycle-2, security-review-report-20260704-075817.md [SR-R-001]):
# parse 失敗時の stderr 診断ログに出す捕捉テキストへの長さ上限導入。
# plan-report-20260704-080808.md の方針: `DIAGNOSTIC_MAX_LEN = 64` を定義し、
# 出力前に先頭 64 文字へ切り詰める。
# ---------------------------------------------------------------------------


class TestLastPrecompactCheckpointDtDiagnosticTruncation:
    """[FB5 / SR-R-001] parse 失敗時の stderr 診断ログの捕捉テキストが
    固定長（64文字）に切り詰められ、全文が出力されないことを固定する。
    """

    def test_stderr_diagnostic_truncates_long_broken_timestamp(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        module = _load_pre_compact_module()
        session_file = tmp_path / "20260704.tmp"
        broken_ts = "not-a-timestamp-" + ("x" * 200)
        _write_precompact_checkpoint_line(session_file, broken_ts)
        fake_stderr = io.StringIO()
        monkeypatch.setattr(sys, "stderr", fake_stderr)

        result = module._last_precompact_checkpoint_dt(str(session_file))

        assert result is None
        stderr_output = fake_stderr.getvalue()
        assert broken_ts not in stderr_output, (
            "壊れた timestamp 全文（216文字）が stderr にそのまま出力されてはならない。"
            "先頭 64 文字への切り詰めが必要（SR-R-001）。\n"
            f"実際の stderr 出力: {stderr_output!r}"
        )
        assert broken_ts[:64] in stderr_output, (
            "切り詰め後も先頭 64 文字は診断情報として残るべき。\n"
            f"実際の stderr 出力: {stderr_output!r}"
        )
        stderr_lines = [
            line for line in stderr_output.splitlines() if line.strip()
        ]
        assert len(stderr_lines) == 1, (
            "既存の非対称仕様（parse 失敗時のみ 1 行・ファイル無しは無音）は"
            "維持されるべき。\n"
            f"実際の stderr 出力: {stderr_output!r}"
        )


# ---------------------------------------------------------------------------
# fix-cycle-3 FC1/FC2 (code-review-report-20260704-083159.md [CR-NEW]):
# タイムスタンプ欄が完全空文字の checkpoint 行（例:
# `## [Checkpoint: PreCompact: manual - ]`）は、旧 greedy regex `.* - (.+)]`
# の非マッチ挙動（`(.+)` が1文字以上を要求するため非マッチ→continue）に完全準拠させ、
# より古い有効な checkpoint 行の探索を継続する（return None で打ち切らない）。
# 診断ログも出さない（破損ではなく単なる欠落のため）。
# ---------------------------------------------------------------------------


class TestLastPrecompactCheckpointDtEmptyTimestampBoundary:
    """[CR-NEW] タイムスタンプ欄が空文字の checkpoint 行の回帰テスト。

    直近行が空 timestamp（`## [Checkpoint: PreCompact: manual - ]`）で、
    その前に有効な古い PreCompact checkpoint 行がある場合、
    `_last_precompact_checkpoint_dt` は None ではなく古い有効行の datetime を
    返すこと（旧 regex 準拠の `continue` 挙動）を固定する。
    """

    def test_empty_timestamp_line_is_skipped_and_older_valid_line_is_used(
        self, tmp_path: Path
    ) -> None:
        module = _load_pre_compact_module()
        session_file = tmp_path / "20260704.tmp"
        older_valid_ts = "2026-07-04T05:00:00+00:00"
        content = (
            f"\n## [Checkpoint: PreCompact: manual - {older_valid_ts}]\nbody\n"
            f"\n## [Checkpoint: PreCompact: manual - ]\nbody\n"
        )
        session_file.write_text(content, encoding="utf-8")

        result = module._last_precompact_checkpoint_dt(
            str(session_file), now=_FIXED_NOW_FOR_HARDCODED_TS_FIXTURES
        )

        assert result is not None, (
            "空 timestamp 行は非マッチ（旧 regex 準拠）として continue し、"
            "より古い有効な checkpoint 行の datetime を返すべき。\n"
            "None が返っている場合、空文字 ts_text を parse 失敗として扱い"
            "return None で走査を打ち切っている退行の疑いがある。"
        )
        assert result.isoformat() == older_valid_ts

    def test_empty_timestamp_line_emits_no_stderr_diagnostic(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """空 timestamp は「破損」ではなく「単なる欠落」扱いのため、
        stderr 診断ログを出さない（非空の破損 timestamp とは非対称）。
        """
        module = _load_pre_compact_module()
        session_file = tmp_path / "20260704.tmp"
        older_valid_ts = "2026-07-04T05:00:00+00:00"
        content = (
            f"\n## [Checkpoint: PreCompact: manual - {older_valid_ts}]\nbody\n"
            f"\n## [Checkpoint: PreCompact: manual - ]\nbody\n"
        )
        session_file.write_text(content, encoding="utf-8")
        fake_stderr = io.StringIO()
        monkeypatch.setattr(sys, "stderr", fake_stderr)

        result = module._last_precompact_checkpoint_dt(
            str(session_file), now=_FIXED_NOW_FOR_HARDCODED_TS_FIXTURES
        )

        assert result is not None
        assert fake_stderr.getvalue() == "", (
            "空 timestamp 行は破損ケースの診断ログ（1行）を出してはならない。\n"
            f"実際の stderr 出力: {fake_stderr.getvalue()!r}"
        )
