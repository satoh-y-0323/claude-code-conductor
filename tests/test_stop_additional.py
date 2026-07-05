"""
Additional tests for stop.py:

  TestExtractSessionPatterns
    - Valid C3:SESSION:JSON block  -> returns pattern list
    - No JSON block               -> returns []
    - Broken JSON in block        -> returns [] (no crash)

  TestAppendLastMessageTruncation
    - Message > 500 chars  -> first 500 chars kept + '…（省略）' appended
    - Message = 500 chars  -> stored as-is, no truncation marker

  TestUpdatePatternsTrustScore
    - New pattern registered      -> trust_score is calculated and stored
    - Pattern 4 days old, trust=1.0 -> promotion_candidate: true
    - Pattern 31 days old         -> excluded from patterns.json (EXPIRY_DAYS=30)

  [Regression guards (originally Red-phase)]
  TestEnsureSessionFileSingleReadWrite
    - _append_last_message + _update_facts_timestamp must complete with
      exactly 1 read + 1 write total (not 2 reads + 2 writes)

  TestLoadPatternsJsonDecodeError
    - load_patterns() must return {"patterns": []} on broken JSON, not raise

  TestAppendLastMessageEscapesCommentCloser
    - --> in last_assistant_message must be sanitized so JSON block is not broken
"""

from __future__ import annotations

import importlib.util
import json
import types
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

# conftest.py が .claude/hooks/ を sys.path.insert(0, ...) で追加するため
# session_utils をテストから直接 import できる（mypy/pyright は静的解析できない）。
from session_utils import extract_section as _extract  # type: ignore[import-not-found]

# ---------------------------------------------------------------------------
# Constants / module loader
# ---------------------------------------------------------------------------

WORKTREE_ROOT = Path(__file__).parent.parent
HOOKS_DIR = WORKTREE_ROOT / ".claude" / "hooks"
STOP_PY = HOOKS_DIR / "stop.py"

TODAY = date.today()
TODAY_STR = TODAY.strftime("%Y%m%d")


def _load_stop_module(module_name: str) -> types.ModuleType:
    """Load stop.py as a fresh module instance without registering in sys.modules."""
    spec = importlib.util.spec_from_file_location(module_name, STOP_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# File helpers shared across test classes
# ---------------------------------------------------------------------------

def _write_session_with_patterns(
    sessions_dir: Path, date_str: str, patterns: list
) -> None:
    """Write a .tmp session file containing the given patterns in the JSON block."""
    content = (
        f"SESSION: {date_str}\n"
        f"AGENT: \n"
        f"DURATION: \n"
        f"\n"
        f"<!-- C3:SESSION:JSON\n"
        f"{{\n"
        f'  "session": "{date_str}",\n'
        f'  "patterns": {json.dumps(patterns)},\n'
        f'  "successes": [],\n'
        f'  "failures": [],\n'
        f'  "todos": []\n'
        f"}}\n"
        f"-->\n"
    )
    (sessions_dir / f"{date_str}.tmp").write_text(content, encoding="utf-8")


def _write_session_no_json(sessions_dir: Path, date_str: str) -> None:
    """Write a minimal .tmp session file WITHOUT a C3:SESSION:JSON block."""
    content = f"SESSION: {date_str}\nAGENT: \nDURATION: \n"
    (sessions_dir / f"{date_str}.tmp").write_text(content, encoding="utf-8")


def _write_session_broken_json(sessions_dir: Path, date_str: str) -> None:
    """Write a .tmp session file whose C3:SESSION:JSON block contains invalid JSON."""
    content = (
        f"SESSION: {date_str}\n"
        f"<!-- C3:SESSION:JSON\n"
        f"{{not valid json!!\n"
        f"-->\n"
    )
    (sessions_dir / f"{date_str}.tmp").write_text(content, encoding="utf-8")


def _session_file_with_timestamp(date_str: str) -> str:
    """Return minimal session file text that has '- 記録時刻:' but no '- 最終応答:'."""
    return (
        f"SESSION: {date_str}\n"
        f"## 事実ログ\n"
        f"- 記録時刻: 2026-05-05 00:00:00\n"
    )


# ---------------------------------------------------------------------------
# TestExtractSessionPatterns
# ---------------------------------------------------------------------------


class TestExtractSessionPatterns:
    """Tests for extract_session_patterns(date_str)."""

    def test_returns_patterns_when_valid_json_block_present(self, tmp_path):
        """Session file with a valid C3:SESSION:JSON block returns the pattern list."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _write_session_with_patterns(sessions_dir, TODAY_STR, [
            {"id": "p1", "description": "desc 1"},
            {"id": "p2", "description": "desc 2"},
        ])

        mod = _load_stop_module(f"_stop_ext_valid_{tmp_path.name}")
        mod.SESSIONS_DIR = str(sessions_dir)

        result = mod.extract_session_patterns(TODAY_STR)

        assert len(result) == 2
        assert result[0]["id"] == "p1"
        assert result[1]["id"] == "p2"

    def test_returns_empty_list_when_no_json_block(self, tmp_path):
        """Session file without C3:SESSION:JSON block returns an empty list."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _write_session_no_json(sessions_dir, TODAY_STR)

        mod = _load_stop_module(f"_stop_ext_noblock_{tmp_path.name}")
        mod.SESSIONS_DIR = str(sessions_dir)

        result = mod.extract_session_patterns(TODAY_STR)

        assert result == []

    def test_returns_empty_list_when_json_is_broken(self, tmp_path):
        """Session file with malformed JSON in the block returns [] without raising."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _write_session_broken_json(sessions_dir, TODAY_STR)

        mod = _load_stop_module(f"_stop_ext_broken_{tmp_path.name}")
        mod.SESSIONS_DIR = str(sessions_dir)

        result = mod.extract_session_patterns(TODAY_STR)

        assert result == []


# ---------------------------------------------------------------------------
# TestAppendLastMessageTruncation
# ---------------------------------------------------------------------------


class TestAppendLastMessageTruncation:
    """Tests for _append_last_message truncation at MAX_LAST_MSG (500) characters."""

    def test_message_over_500_chars_is_truncated(self, tmp_path):
        """Messages longer than 500 characters are truncated to first 500 + '…（省略）'."""
        session_file = tmp_path / f"{TODAY_STR}.tmp"
        session_file.write_text(
            _session_file_with_timestamp(TODAY_STR), encoding="utf-8"
        )

        long_message = "X" * 600  # 100 chars over the 500-char limit

        mod = _load_stop_module(f"_stop_trunc_long_{tmp_path.name}")
        mod._append_last_message(str(session_file), long_message)

        content = session_file.read_text(encoding="utf-8")
        assert "- 最終応答:" in content, "最終応答 line must be written to the file"

        for line in content.splitlines():
            if line.startswith("- 最終応答:"):
                stored = line[len("- 最終応答:"):].strip()
                # Full 600-char message must NOT be stored verbatim
                assert stored != long_message, (
                    "600-char message must not be stored verbatim; "
                    "it should be truncated to 500 chars"
                )
                # First 500 characters must be preserved
                assert stored.startswith("X" * 500), (
                    f"First 500 characters of the message must be kept. "
                    f"Got: {stored[:30]!r}..."
                )
                # Truncation marker must be appended
                assert "…（省略）" in stored, (
                    "Truncation marker '…（省略）' must be appended to long messages"
                )
                break
        else:
            raise AssertionError("'- 最終応答:' line not found in session file")

    def test_message_exactly_500_chars_is_not_truncated(self, tmp_path):
        """Messages of exactly 500 characters are stored as-is, without a truncation marker."""
        session_file = tmp_path / f"{TODAY_STR}.tmp"
        session_file.write_text(
            _session_file_with_timestamp(TODAY_STR), encoding="utf-8"
        )

        exact_message = "Y" * 500

        mod = _load_stop_module(f"_stop_trunc_exact_{tmp_path.name}")
        mod._append_last_message(str(session_file), exact_message)

        content = session_file.read_text(encoding="utf-8")
        assert "- 最終応答:" in content

        for line in content.splitlines():
            if line.startswith("- 最終応答:"):
                stored = line[len("- 最終応答:"):].strip()
                assert stored == exact_message, (
                    "Message of exactly 500 chars must be stored without any modification"
                )
                assert "…（省略）" not in stored, (
                    "Truncation marker must NOT appear for a 500-char message "
                    "(boundary value: len == MAX_LAST_MSG, not strictly greater)"
                )
                break
        else:
            raise AssertionError("'- 最終応答:' line not found in session file")


# ---------------------------------------------------------------------------
# TestUpdatePatternsTrustScore
# ---------------------------------------------------------------------------


class TestUpdatePatternsTrustScore:
    """Tests for trust_score, promotion_candidate, and expiry in update_patterns."""

    def _setup(self, tmp_path: Path, tag: str = "") -> tuple:
        """Create isolated temp dirs and return a fresh stop module with overridden paths."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        patterns_file = tmp_path / "patterns.json"
        mod = _load_stop_module(f"_stop_trust_{tag}_{tmp_path.name}")
        mod.SESSIONS_DIR = str(sessions_dir)
        mod.PATTERNS_FILE = str(patterns_file)
        return mod, sessions_dir, patterns_file

    def test_trust_score_is_set_after_first_registration(self, tmp_path):
        """After a new pattern is registered in one session, trust_score must be computed."""
        mod, sessions_dir, patterns_file = self._setup(tmp_path, "ts")
        patterns_file.write_text(json.dumps({"patterns": []}), encoding="utf-8")

        _write_session_with_patterns(sessions_dir, TODAY_STR, [
            {"id": "ts-pat", "description": "trust score test"}
        ])

        mod.update_patterns(TODAY_STR)

        data = json.loads(patterns_file.read_text(encoding="utf-8"))
        stored = next((p for p in data["patterns"] if p["id"] == "ts-pat"), None)
        assert stored is not None, "Pattern 'ts-pat' must be stored in patterns.json"
        assert "trust_score" in stored, "trust_score field must be present on stored pattern"
        assert isinstance(stored["trust_score"], (int, float)), (
            "trust_score must be a numeric value"
        )
        assert 0.0 < stored["trust_score"] <= 1.0, (
            f"trust_score must be in the range (0.0, 1.0], got {stored['trust_score']!r}"
        )

    def test_promotion_candidate_true_when_old_and_high_trust(self, tmp_path):
        """Pattern 4 days old with 1 obs / 1 session (trust=1.0) must get promotion_candidate=True.

        Conditions satisfied:
          days_elapsed (4) >= COOLING_DAYS (3)  ->  True
          trust_score   (1.0) >= PROMOTION_THRESHOLD (0.8)  ->  True
        """
        mod, sessions_dir, patterns_file = self._setup(tmp_path, "promo")

        registered = (TODAY - timedelta(days=4)).strftime("%Y%m%d")
        patterns_data = {
            "patterns": [
                {
                    "id": "promo-pat",
                    "description": "promotion test",
                    "registered_date": registered,
                    "trust_score": 0.1,
                    "promotion_candidate": False,
                    "observations": [{"date": registered}],
                    "last_updated": registered,
                }
            ]
        }
        patterns_file.write_text(json.dumps(patterns_data), encoding="utf-8")

        # One session file today -> sessions_total=1, obs_count=1 -> trust=1.0
        _write_session_no_json(sessions_dir, TODAY_STR)

        mod.update_patterns(TODAY_STR)

        data = json.loads(patterns_file.read_text(encoding="utf-8"))
        stored = next((p for p in data["patterns"] if p["id"] == "promo-pat"), None)
        assert stored is not None, "Pattern 'promo-pat' must still be in patterns.json"
        assert stored.get("promotion_candidate") is True, (
            f"Pattern registered 4 days ago with trust=1.0 must have "
            f"promotion_candidate=True. "
            f"Got trust_score={stored.get('trust_score')!r}, "
            f"promotion_candidate={stored.get('promotion_candidate')!r}"
        )

    def test_expired_pattern_removed_after_30_days(self, tmp_path):
        """Pattern registered 31 days ago must be removed (EXPIRY_DAYS=30)."""
        mod, sessions_dir, patterns_file = self._setup(tmp_path, "exp")

        registered = (TODAY - timedelta(days=31)).strftime("%Y%m%d")
        patterns_data = {
            "patterns": [
                {
                    "id": "expired-pat",
                    "description": "expired pattern",
                    "registered_date": registered,
                    "trust_score": 0.5,
                    "promotion_candidate": False,
                    "observations": [{"date": registered}],
                    "last_updated": registered,
                }
            ]
        }
        patterns_file.write_text(json.dumps(patterns_data), encoding="utf-8")

        # Session file exists so update_patterns can complete cleanly
        _write_session_no_json(sessions_dir, TODAY_STR)

        mod.update_patterns(TODAY_STR)

        data = json.loads(patterns_file.read_text(encoding="utf-8"))
        ids = [p["id"] for p in data["patterns"]]
        assert "expired-pat" not in ids, (
            "Pattern registered 31 days ago must be excluded from patterns.json. "
            "EXPIRY_DAYS=30, days_elapsed=31 >= 30 -> pattern must be dropped."
        )


# ---------------------------------------------------------------------------
# [Regression guard] TestEnsureSessionFileSingleReadWrite
# ---------------------------------------------------------------------------


class TestEnsureSessionFileSingleReadWrite:
    """[New] _append_last_message + _update_facts_timestamp must complete with
    1 read + 1 write total (not 2 separate read-modify-write cycles).

    Current implementation:
      _append_last_message: open(r) + open(w)  -> 1 read + 1 write
      _update_facts_timestamp: open(r) + open(w)  -> 1 read + 1 write
    Total: 2 reads + 2 writes.

    Expected after fix: combined into 1 read + 1 write.

    実装側で修正済み。本テストは退行防止のための Green 回帰防止テスト。
    """

    def test_ensure_session_file_does_single_read_write(self, tmp_path):
        """Combined _append_last_message + _update_facts_timestamp must use
        at most 1 file read and 1 file write (total across both operations).

        Verification: count open() calls with 'r' and 'w' modes during a
        simulated existing-file path through ensure_session_file().
        This test targets the scenario where FileExistsError is raised and
        both _update_facts_timestamp and _append_last_message are called.
        """
        mod = _load_stop_module(f"_stop_single_rw_{tmp_path.name}")

        # Create a session file that already has the timestamp line
        session_file = tmp_path / f"{TODAY_STR}.tmp"
        session_file.write_text(
            f"SESSION: {TODAY_STR}\n"
            f"## 事実ログ\n"
            f"- 記録時刻: 2026-05-05 00:00:00\n",
            encoding="utf-8",
        )

        read_count = [0]
        write_count = [0]
        original_open = open

        def counting_open(file, mode="r", **kwargs):
            if str(file) == str(session_file):
                if "r" in mode and "w" not in mode:
                    read_count[0] += 1
                elif "w" in mode or "a" in mode:
                    write_count[0] += 1
            return original_open(file, mode, **kwargs)

        message = "test assistant message"

        with mock.patch("builtins.open", side_effect=counting_open):
            mod._append_last_message(str(session_file), message)
            mod._update_facts_timestamp(str(session_file))

        # After fix: both operations should be combined into 1 read + 1 write
        assert read_count[0] <= 1, (
            f"[code-High-2] Combined operations must read the file at most once. "
            f"Got {read_count[0]} reads. Current implementation reads twice "
            f"(once in _append_last_message, once in _update_facts_timestamp)."
        )
        assert write_count[0] <= 1, (
            f"[code-High-2] Combined operations must write the file at most once. "
            f"Got {write_count[0]} writes. Current implementation writes twice."
        )


# ---------------------------------------------------------------------------
# [Regression guard] TestLoadPatternsJsonDecodeError
# ---------------------------------------------------------------------------


class TestLoadPatternsJsonDecodeError:
    """[New] load_patterns() must handle broken JSON gracefully.

    Current implementation:
        def load_patterns() -> dict:
            if os.path.exists(PATTERNS_FILE):
                with open(PATTERNS_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)  # raises JSONDecodeError on broken JSON
            return {"patterns": []}

    Expected after fix: catch JSONDecodeError and return {"patterns": []}.

    実装側で修正済み。本テストは退行防止のための Green 回帰防止テスト。
    """

    def test_load_patterns_handles_json_decode_error(self, tmp_path):
        """load_patterns() on a broken patterns.json must return {"patterns": []}
        without raising an exception.

        実装側で修正済み（json.JSONDecodeError raised）。本テストは Green 回帰防止テスト。
        """
        broken_patterns_file = tmp_path / "patterns.json"
        broken_patterns_file.write_text(
            "{ this is definitely not valid JSON !!!",
            encoding="utf-8",
        )

        mod = _load_stop_module(f"_stop_load_err_{tmp_path.name}")
        mod.PATTERNS_FILE = str(broken_patterns_file)

        # Should not raise — must return {"patterns": []} instead
        try:
            result = mod.load_patterns()
        except json.JSONDecodeError as exc:
            raise AssertionError(
                "[code-Medium-5] load_patterns() raised json.JSONDecodeError on broken "
                f"patterns.json. Expected it to catch the error and return "
                f'{{"patterns": []}}. Error: {exc}'
            ) from exc

        assert result == {"patterns": []}, (
            f"[code-Medium-5] load_patterns() on broken JSON must return "
            f'{{"patterns": []}}, got {result!r}'
        )


# ---------------------------------------------------------------------------
# [Regression guard] TestAppendLastMessageEscapesCommentCloser
# ---------------------------------------------------------------------------


class TestAppendLastMessageEscapesCommentCloser:
    """[New] --> in last_assistant_message must be sanitized.

    The session file uses <!-- C3:SESSION:JSON ... --> blocks. If
    last_assistant_message contains '-->', the block comment is prematurely
    closed, corrupting the JSON block.

    Current implementation:
        # No sanitization of '-->' in the message.

    Expected after fix:
        Replace '-->' with '-- >' (or similar) before writing.

    実装側で修正済み。本テストは退行防止のための Green 回帰防止テスト。
    """

    def _make_session_with_json_block(self, session_file: Path, date_str: str) -> None:
        """Write a session file with a valid C3:SESSION:JSON block."""
        content = (
            f"SESSION: {date_str}\n"
            f"## 事実ログ\n"
            f"- 記録時刻: 2026-05-05 00:00:00\n"
            f"\n"
            f"<!-- C3:SESSION:JSON\n"
            f"{{\n"
            f'  "session": "{date_str}",\n'
            f'  "patterns": [],\n'
            f'  "successes": [],\n'
            f'  "failures": [],\n'
            f'  "todos": []\n'
            f"}}\n"
            f"-->\n"
        )
        session_file.write_text(content, encoding="utf-8")

    def test_append_last_message_escapes_comment_closer(self, tmp_path):
        """When last_assistant_message contains '-->', the session file's JSON block
        must remain intact (not be broken by an unescaped comment closer).

        本テストは Green 回帰防止テスト（実装側修正済み）。修正前は '-->' is written
        verbatim, which closes the HTML comment block prematurely.
        """
        session_file = tmp_path / f"{TODAY_STR}.tmp"
        self._make_session_with_json_block(session_file, TODAY_STR)

        mod = _load_stop_module(f"_stop_comment_{tmp_path.name}")

        # A message with --> that would prematurely close the JSON block
        dangerous_message = "See the comparison: a --> b means 'a leads to b'"
        mod._append_last_message(str(session_file), dangerous_message)

        content = session_file.read_text(encoding="utf-8")

        # The JSON block marker/closing must still be present and intact
        assert "<!-- C3:SESSION:JSON" in content, (
            "The C3:SESSION:JSON opening comment must remain in the session file"
        )

        # Find the JSON block and verify it's not broken by the --> in the message
        import re
        json_block_match = re.search(
            r"<!-- C3:SESSION:JSON\s*(.*?)-->",
            content,
            re.DOTALL,
        )
        assert json_block_match is not None, (
            "[sec-Medium] The C3:SESSION:JSON block was broken by '-->' in the message. "
            "The block comment was prematurely closed, corrupting the JSON block.\n"
            "Expected: '-->' to be sanitized (e.g. replaced with '-- >') before writing.\n"
            f"File content:\n{content}"
        )

        # The matched JSON block must be valid JSON
        try:
            block_content = json_block_match.group(1).strip()
            json.loads(block_content)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                "[sec-Medium] The JSON inside C3:SESSION:JSON block is invalid after "
                f"writing a message with '-->'. Error: {exc}\n"
                f"Block content: {block_content!r}"
            ) from exc

        # The written message must not contain literal --> (it must be sanitized)
        response_line = next(
            (line for line in content.splitlines() if line.startswith("- 最終応答:")),
            None,
        )
        assert response_line is not None, "'- 最終応答:' line must be written"
        assert "-->" not in response_line, (
            "[sec-Medium] The '- 最終応答:' line must not contain literal '-->' "
            "as it would break the JSON comment block. "
            f"Got: {response_line!r}"
        )


# ---------------------------------------------------------------------------
# TestAppendLastMessageOverwrite
# ---------------------------------------------------------------------------


class TestAppendLastMessageOverwrite:
    """最終応答の上書き動作を検証する退行防止テスト。

    修正前は `if '- 最終応答:' not in updated` ガードにより、別 Claude セッションが
    新しい応答を出しても最初のセッションの最終応答が一日中残り続けた。本クラスは
    その挙動が再発しないことを保証する。
    """

    def test_second_call_overwrites_existing_last_response(self, tmp_path):
        """同じセッションファイルに 2 回追記すると、最後の呼び出しの応答だけが残る。"""
        session_file = tmp_path / f"{TODAY_STR}.tmp"
        session_file.write_text(
            _session_file_with_timestamp(TODAY_STR), encoding="utf-8"
        )

        mod = _load_stop_module(f"_stop_overwrite_{tmp_path.name}")
        mod._append_last_message(str(session_file), "first session response")
        # 別 Claude セッションを想定してプロセス境界をシミュレート
        mod._last_message_applied_paths.clear()
        mod._append_last_message(
            str(session_file), "second session response - the latest"
        )

        content = session_file.read_text(encoding="utf-8")
        last_response_lines = [
            ln for ln in content.splitlines() if ln.startswith("- 最終応答:")
        ]
        assert len(last_response_lines) == 1, (
            f"最終応答行は常に 1 件のみであるべき。"
            f"Got {len(last_response_lines)}: {last_response_lines!r}"
        )
        assert "second session response - the latest" in last_response_lines[0]
        assert "first session response" not in content

    def test_overwrite_preserves_json_block_and_other_sections(self, tmp_path):
        """上書き時に C3:SESSION:JSON ブロックや他セクションが破壊されない。"""
        session_file = tmp_path / f"{TODAY_STR}.tmp"
        mod = _load_stop_module(f"_stop_overwrite_json_{tmp_path.name}")
        # create_session_template でフルテンプレート初期化 + 記録時刻を埋める
        session_file.write_text(
            mod.create_session_template(TODAY_STR).replace(
                "- 記録時刻: ",
                "- 記録時刻: 2026-05-24 10:00:00",
            ),
            encoding="utf-8",
        )

        mod._append_last_message(str(session_file), "initial")
        mod._last_message_applied_paths.clear()
        mod._append_last_message(str(session_file), "updated")

        content = session_file.read_text(encoding="utf-8")
        assert "<!-- C3:SESSION:JSON" in content, (
            "JSON ブロック開始マーカーが残っているべき"
        )
        assert "-- >" in content, "JSON ブロック閉じタグ（サニタイズ済み）が残っているべき"
        assert "## うまくいったアプローチ" in content
        assert "## 残タスク" in content
        assert content.count("- 最終応答:") == 1
        assert "updated" in content
        assert "initial" not in content

    def test_idempotency_within_same_process(self, tmp_path):
        """同一プロセスで同じメッセージを 2 回呼んでも安全（冪等）。"""
        session_file = tmp_path / f"{TODAY_STR}.tmp"
        session_file.write_text(
            _session_file_with_timestamp(TODAY_STR), encoding="utf-8"
        )

        mod = _load_stop_module(f"_stop_idem_{tmp_path.name}")
        mod._append_last_message(str(session_file), "same message")
        # 別プロセス模擬でキャッシュをクリア
        mod._last_message_applied_paths.clear()
        mod._append_last_message(str(session_file), "same message")

        content = session_file.read_text(encoding="utf-8")
        assert content.count("- 最終応答: same message") == 1
        assert content.count("- 最終応答:") == 1


# ---------------------------------------------------------------------------
# TestInheritBacklogFromLatestSession
# ---------------------------------------------------------------------------


def _write_past_session_with_backlog(
    sessions_dir: Path, date_str: str, backlog_lines: list
) -> None:
    """Write a past .tmp session file with the given backlog lines in '## 残タスク'."""
    backlog_block = "\n".join(backlog_lines)
    if backlog_block:
        backlog_block += "\n"
    content = (
        f"SESSION: {date_str}\n"
        f"AGENT: \n"
        f"DURATION: \n"
        f"\n"
        f"## うまくいったアプローチ\n"
        f"\n"
        f"## 試みたが失敗したアプローチ\n"
        f"\n"
        f"## 残タスク\n"
        f"{backlog_block}\n"
        f"## 事実ログ（自動生成 / stop.py）\n"
        f"- 記録時刻: 2026-05-23 23:59:59\n"
        f"- 最終応答: previous response\n"
        f"\n"
        f"<!-- C3:SESSION:JSON\n"
        f"{{\n"
        f'  "session": "{date_str}",\n'
        f'  "patterns": [],\n'
        f'  "successes": [],\n'
        f'  "failures": [],\n'
        f'  "todos": []\n'
        f"}}\n"
        f"-- >\n"
    )
    (sessions_dir / f"{date_str}.tmp").write_text(content, encoding="utf-8")


class TestInheritBacklogFromLatestSession:
    """ensure_session_file が新規ファイル作成時に直近過去セッションの未完了タスク
    （- [ ]）を自動で当日ファイルに引き継ぐことを検証する退行防止テスト。

    修正前は前日の未完了タスクが当日に引き継がれず、init-session の git log
    照合が空ファイルでは何も検出できなかった。
    """

    YESTERDAY_STR = (TODAY - timedelta(days=1)).strftime("%Y%m%d")

    def _setup(self, tmp_path: Path, tag: str) -> tuple[types.ModuleType, Path]:
        """Create isolated SESSIONS_DIR + fresh stop module pointing at it."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        mod = _load_stop_module(f"_stop_inherit_{tag}_{tmp_path.name}")
        mod.SESSIONS_DIR = str(sessions_dir)
        return mod, sessions_dir

    def test_pending_tasks_carried_over_to_new_file(self, tmp_path):
        """前日ファイルの - [ ] 行が引き継がれ、- [x] は引き継がれない。"""
        mod, sessions_dir = self._setup(tmp_path, "carry")
        _write_past_session_with_backlog(
            sessions_dir,
            self.YESTERDAY_STR,
            [
                "- [ ] tier-routing コスト統合の実装",
                "- [ ] /usage TUI 利用量計測",
                "- [x] v2.15.2 リリース完了 → done",
            ],
        )

        mod.ensure_session_file(TODAY_STR)

        new_file = sessions_dir / f"{TODAY_STR}.tmp"
        assert new_file.exists(), "新規セッションファイルが作成されているべき"
        content = new_file.read_text(encoding="utf-8")

        # ## 残タスク セクションを抽出して中身を確認
        backlog = _extract(content, "残タスク")
        pending_lines = [
            ln for ln in backlog.splitlines() if ln.lstrip().startswith("- [ ]")
        ]
        assert len(pending_lines) == 2, (
            f"未完了タスク 2 件が引き継がれているべき。Got: {pending_lines!r}"
        )
        assert any("tier-routing" in ln for ln in pending_lines)
        assert any("/usage" in ln for ln in pending_lines)
        # 完了済みタスクは引き継がない
        assert "v2.15.2 リリース完了" not in content
        assert "- [x]" not in backlog

    def test_no_past_session_does_not_fail(self, tmp_path):
        """SESSIONS_DIR が空でも ensure_session_file はエラーにならない。"""
        mod, sessions_dir = self._setup(tmp_path, "empty")

        # 例外を出さないこと、ファイルが作成されることを確認
        mod.ensure_session_file(TODAY_STR)

        new_file = sessions_dir / f"{TODAY_STR}.tmp"
        assert new_file.exists()
        content = new_file.read_text(encoding="utf-8")
        # ## 残タスク セクションは空のまま
        assert "## 残タスク" in content

    def test_existing_today_file_is_not_modified(self, tmp_path):
        """既に当日ファイルが存在する場合、引き継ぎは発動せず内容は保持される。"""
        mod, sessions_dir = self._setup(tmp_path, "exist")
        # 前日ファイルに未完了タスクあり
        _write_past_session_with_backlog(
            sessions_dir,
            self.YESTERDAY_STR,
            ["- [ ] should NOT be carried over"],
        )
        # 当日ファイルが既に存在（手動編集された状態をシミュレート）
        today_file = sessions_dir / f"{TODAY_STR}.tmp"
        existing_content = (
            f"SESSION: {TODAY_STR}\n"
            f"## 残タスク\n"
            f"- [ ] manually added today\n"
            f"## 事実ログ（自動生成 / stop.py）\n"
            f"- 記録時刻: 2026-05-24 12:00:00\n"
        )
        today_file.write_text(existing_content, encoding="utf-8")

        mod.ensure_session_file(TODAY_STR)

        content = today_file.read_text(encoding="utf-8")
        assert "should NOT be carried over" not in content, (
            "既存当日ファイルがある場合は前日からの引き継ぎを発動してはいけない"
        )
        assert "manually added today" in content

    def test_skips_when_past_section_has_no_pending_tasks(self, tmp_path):
        """前日ファイルの残タスクが全て - [x] の場合、当日の残タスクは空のまま。"""
        mod, sessions_dir = self._setup(tmp_path, "alldone")
        _write_past_session_with_backlog(
            sessions_dir,
            self.YESTERDAY_STR,
            [
                "- [x] done A",
                "- [x] done B",
            ],
        )

        mod.ensure_session_file(TODAY_STR)

        new_file = sessions_dir / f"{TODAY_STR}.tmp"
        content = new_file.read_text(encoding="utf-8")

        backlog = _extract(content, "残タスク")
        assert backlog.strip() == "", (
            f"前日に未完了がない場合、当日の残タスクは空のまま。Got: {backlog!r}"
        )
        # 完了済み行が漏れ出していないことも確認
        assert "done A" not in content
        assert "done B" not in content


# ---------------------------------------------------------------------------
# TestInheritBacklogControlCharSanitize (M-1 / SR-V-001 退行防止)
# ---------------------------------------------------------------------------


class TestInheritBacklogControlCharSanitize:
    """_inherit_backlog_from_latest_session が過去ファイルから引き継ぐ - [ ] 行に対し
    制御文字を除去することを検証する退行防止テスト。

    確認する挙動:
      - ANSI エスケープ (\x1b[31m 等) が除去される
      - C0 制御文字 (\x00, \x0b, \r) が除去される
      - U+2028 (LINE SEPARATOR) / U+2029 (PARAGRAPH SEPARATOR) が除去される
      - タブ (\t) と通常スペース ( ) は保持される

    修正前は _inherit_backlog_from_latest_session がサニタイズを行わず、
    過去セッションの改ざんによる端末インジェクションを許していた [SR-V-001]。
    """

    YESTERDAY_STR = (TODAY - timedelta(days=1)).strftime("%Y%m%d")

    def _setup(self, tmp_path: Path, tag: str) -> tuple[types.ModuleType, Path]:
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        mod = _load_stop_module(f"_stop_ctrl_sanitize_{tag}_{tmp_path.name}")
        mod.SESSIONS_DIR = str(sessions_dir)
        return mod, sessions_dir

    def test_control_chars_removed_from_inherited_backlog(self, tmp_path):
        """過去セッションの - [ ] 行に埋め込まれた制御文字が除去されて当日ファイルへ引き継がれる。

        タブ (\t) と通常スペースは保持される。
        """
        mod, sessions_dir = self._setup(tmp_path, "ctrl")

        # 制御文字を含む - [ ] 行を持つ過去セッションファイルを作成する
        ansi_escape = "\x1b[31m"          # ANSI red color escape
        null_char = "\x00"                 # NUL (C0)
        vt_char = "\x0b"                   # Vertical Tab (C0)
        cr_char = "\r"                     # Carriage Return (C0)
        ls_char = " "                 # LINE SEPARATOR
        ps_char = " "                 # PARAGRAPH SEPARATOR
        tab_char = "\t"                    # タブ（保持すべき）
        space_char = " "                   # スペース（保持すべき）

        # 制御文字を含む - [ ] 行。
        # Python の universal newlines は `\r` を `\n` に変換するため、`\r` より後の
        # 部分は別行として分離される。`\t with space` を `\r` より前に配置することで、
        # サニタイズ後にタブ・スペースが保持されることを検証できる構造にする。
        dirty_task = (
            f"- [ ] {ansi_escape}重要タスク{null_char}"
            f"\twith{space_char}tab and space"
            f"{vt_char}継続中{cr_char}{ls_char}{ps_char}"
        )
        backlog_block = f"{dirty_task}\n"
        past_content = (
            f"SESSION: {self.YESTERDAY_STR}\n"
            f"## 残タスク\n"
            f"{backlog_block}\n"
            f"## 事実ログ（自動生成 / stop.py）\n"
            f"- 記録時刻: 2026-05-23 23:59:59\n"
        )
        past_file = sessions_dir / f"{self.YESTERDAY_STR}.tmp"
        past_file.write_bytes(past_content.encode("utf-8"))

        mod.ensure_session_file(TODAY_STR)

        new_file = sessions_dir / f"{TODAY_STR}.tmp"
        assert new_file.exists(), "新規セッションファイルが作成されているべき"
        content = new_file.read_text(encoding="utf-8")

        backlog = _extract(content, "残タスク")
        pending_lines = [
            ln for ln in backlog.splitlines() if ln.lstrip().startswith("- [ ]")
        ]
        assert len(pending_lines) >= 1, (
            f"引き継ぎタスクが 1 件以上存在するべき。Got: {pending_lines!r}"
        )

        inherited_line = pending_lines[0]

        # 制御文字が除去されていること
        assert "\x1b" not in inherited_line, (
            f"[SR-V-001] ANSI エスケープ (\x1b) が除去されていない。Got: {inherited_line!r}"
        )
        assert "\x00" not in inherited_line, (
            f"[SR-V-001] NUL 文字 (\x00) が除去されていない。Got: {inherited_line!r}"
        )
        assert "\x0b" not in inherited_line, (
            f"[SR-V-001] Vertical Tab (\x0b) が除去されていない。Got: {inherited_line!r}"
        )
        assert "\r" not in inherited_line, (
            f"[SR-V-001] Carriage Return (\r) が除去されていない。Got: {inherited_line!r}"
        )
        assert " " not in inherited_line, (
            f"[SR-V-001] LINE SEPARATOR (U+2028) が除去されていない。Got: {inherited_line!r}"
        )
        assert " " not in inherited_line, (
            f"[SR-V-001] PARAGRAPH SEPARATOR (U+2029) が除去されていない。Got: {inherited_line!r}"
        )

        # タブとスペースは保持されること
        assert "\t" in inherited_line, (
            f"[SR-V-001] タブ (\t) は保持されるべき。Got: {inherited_line!r}"
        )
        assert " " in inherited_line, (
            f"[SR-V-001] スペース ( ) は保持されるべき。Got: {inherited_line!r}"
        )


# ---------------------------------------------------------------------------
# TestInheritBacklogSessionsDirArg (M-02 / CR-Q-001 退行防止)
# ---------------------------------------------------------------------------


class TestInheritBacklogSessionsDirArg:
    """_inherit_backlog_from_latest_session の sessions_dir 引数経路を検証する退行防止テスト。

    シグネチャ:
        _inherit_backlog_from_latest_session(
            new_path: str, today_str: str, sessions_dir: str | None = None
        ) -> None

    sessions_dir が None の場合はグローバル SESSIONS_DIR を使う（後方互換）。
    明示的に渡した sessions_dir が優先される。

    修正前はモジュールグローバル SESSIONS_DIR を直接参照しており、テスト時に
    グローバル差し替えが必要だった [CR-Q-001]。
    """

    YESTERDAY_STR = (TODAY - timedelta(days=1)).strftime("%Y%m%d")

    def test_sessions_dir_argument_routes_correctly(self, tmp_path):
        """sessions_dir 引数を明示的に渡すと、SESSIONS_DIR グローバルを上書きせずに
        引き継ぎが正常に動作する。

        手順:
          1. sessions_dir_a にグローバルを向ける（過去ファイルなし）
          2. sessions_dir_b に前日ファイルを置く（- [ ] あり）
          3. _inherit_backlog_from_latest_session(new_path, today_str, sessions_dir=sessions_dir_b)
             を呼び出す
          4. sessions_dir_b の過去ファイルから引き継ぎが行われることを assert する
        """
        mod = _load_stop_module(f"_stop_sdir_arg_{tmp_path.name}")

        # sessions_dir_a: グローバル設定先（過去ファイルなし）
        sessions_dir_a = tmp_path / "sessions_a"
        sessions_dir_a.mkdir()
        mod.SESSIONS_DIR = str(sessions_dir_a)

        # sessions_dir_b: 引数で渡す先（前日ファイルあり）
        sessions_dir_b = tmp_path / "sessions_b"
        sessions_dir_b.mkdir()
        _write_past_session_with_backlog(
            sessions_dir_b,
            self.YESTERDAY_STR,
            ["- [ ] sessions_dir_b からの引き継ぎタスク"],
        )

        # 新規当日ファイルを sessions_dir_b に作成
        from session_utils import create_session_template  # type: ignore

        new_file = sessions_dir_b / f"{TODAY_STR}.tmp"
        new_file.write_text(create_session_template(TODAY_STR), encoding="utf-8")

        # sessions_dir 引数を明示的に渡して呼び出す
        # （シグネチャ: new_path, today_str, sessions_dir=None）
        mod._inherit_backlog_from_latest_session(
            str(new_file), TODAY_STR, sessions_dir=str(sessions_dir_b)
        )

        content = new_file.read_text(encoding="utf-8")
        backlog = _extract(content, "残タスク")
        pending_lines = [
            ln for ln in backlog.splitlines() if ln.lstrip().startswith("- [ ]")
        ]
        assert any("sessions_dir_b からの引き継ぎタスク" in ln for ln in pending_lines), (
            "[CR-Q-001] sessions_dir 引数経由で sessions_dir_b の過去ファイルから "
            "引き継ぎが行われるべき。\n"
            f"Got pending_lines: {pending_lines!r}\n"
            f"sessions_dir_a (global) には過去ファイルなし、"
            f"sessions_dir_b (引数) には過去ファイルあり。"
        )


# ---------------------------------------------------------------------------
# TestInheritBacklogNewPathOSErrorGuard (M-01 / CR-E-001 / SR-NEW 退行防止)
# ---------------------------------------------------------------------------


class TestInheritBacklogNewPathOSErrorGuard:
    """_inherit_backlog_from_latest_session が new_path 読み込み OSError を黙って無視する
    ことを検証する退行防止テスト。

    確認する挙動:
        new_path open 時に OSError → 例外を伝播させず黙って return する。

    実装方針（テスト側）:
        builtins.open を monkeypatch し、new_path のみ OSError を送出させる。
        過去ファイル (latest_past_path) の open は本物の動作を維持することで、
        バックログが存在する状態まで処理を進めてから new_path 読み込みを失敗させる。

    修正前は new_path 読み込みの OSError が try/except なしで伝播し、
    Stop hook プロセスが異常終了するリスクがあった [CR-E-001 / SR-NEW]。
    """

    YESTERDAY_STR = (TODAY - timedelta(days=1)).strftime("%Y%m%d")

    def test_new_path_oserror_does_not_propagate(self, tmp_path):
        """new_path の open が OSError を送出しても _inherit_backlog_from_latest_session は
        例外を伝播させず黙って return する。

        設定:
          - 過去ファイル: 前日ファイルに - [ ] タスクあり（引き継ぎ前半は正常に進む）
          - new_path: open() 時に OSError を送出するようにモンキーパッチ
        """
        mod = _load_stop_module(f"_stop_newpath_err_{tmp_path.name}")

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        mod.SESSIONS_DIR = str(sessions_dir)

        # 前日ファイルに - [ ] タスクを置く（処理がバックログ抽出まで進むように）
        _write_past_session_with_backlog(
            sessions_dir,
            self.YESTERDAY_STR,
            ["- [ ] new_path OSError テスト用タスク"],
        )

        # new_path を用意する（実際には読み込み時に OSError にする）
        from session_utils import create_session_template  # type: ignore

        new_file = sessions_dir / f"{TODAY_STR}.tmp"
        new_file.write_text(create_session_template(TODAY_STR), encoding="utf-8")
        new_path_str = str(new_file)

        original_open = open

        def patched_open(file, mode="r", **kwargs):
            # new_path への 'r' モードのオープンだけ OSError を送出する
            if str(file) == new_path_str and "r" in mode and "w" not in mode:
                raise OSError(f"[Test] Simulated OSError for new_path: {file}")
            return original_open(file, mode, **kwargs)

        # 例外が伝播しないことを assert する
        try:
            with mock.patch("builtins.open", side_effect=patched_open):
                mod._inherit_backlog_from_latest_session(new_path_str, TODAY_STR)
        except OSError as exc:
            raise AssertionError(
                "[CR-E-001 / SR-NEW] _inherit_backlog_from_latest_session が "
                "new_path 読み込み時の OSError を伝播させた。\n"
                "期待: 例外を伝播させず黙って return する。\n"
                f"実際の例外: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# [Regression guards (originally Red-phase)] TestDescriptionBloatWarning
# (plan-report T1: test-stop-guard / T2: impl-stop-guard)
# ---------------------------------------------------------------------------


class TestDescriptionBloatWarning:
    """patterns.json description 肥大検知ガード（implemented; originally written
    Red-first）を固定する回帰テスト。

    plan-report-20260705-153010.md §2 T1 / architecture-report-20260705-152313.md
    §4・§ADR-1〜4 に対応。対象は `_warn_oversized_descriptions` ヘルパーと
    `DESCRIPTION_WARN_LENGTH` 定数（値 1000・strict greater・description 長のみ）。

    T2（impl-stop-guard）で実装済み。Red 定義時点では、本クラスの T1/T4/T5/T6/T7 は
    「未実装ゆえの失敗」（AssertionError: 期待する警告が出ない / AttributeError:
    _warn_oversized_descriptions が存在しない）で落ちる設計意図だった。この経緯は
    テスト設計の記録として残しているのみで、実装済みの現在は全テストが green で
    回帰防止として機能する。

    T2/T3 は「警告が出ないこと」を検証する境界・回帰テストで、strict-greater
    境界（ちょうど1000字・実測最大706字）の誤警告ゼロを担保する。
    """

    def _setup(self, tmp_path: Path, tag: str) -> tuple:
        """Create isolated temp dirs, seed a no-op session file, and return a
        fresh stop module with overridden paths.

        セッション側に新規観測は無い（_write_session_no_json）。これにより
        update_patterns は patterns.json に既に載っている既存エントリのみを
        再評価する経路を通る。これは architecture-report §1 が言う「直接 Edit
        経路（MAX_DESCRIPTION_LENGTH=500 の取り込みガードが効かない穴）」を
        シミュレートするための意図的な設計。
        """
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        patterns_file = tmp_path / "patterns.json"
        mod = _load_stop_module(f"_stop_bloat_{tag}_{tmp_path.name}")
        mod.SESSIONS_DIR = str(sessions_dir)
        mod.PATTERNS_FILE = str(patterns_file)
        _write_session_no_json(sessions_dir, TODAY_STR)
        return mod, sessions_dir, patterns_file

    @staticmethod
    def _entry(pid: str, description) -> dict:
        """既存の expiry/trust ループ（registered_date/observations 必須）を安全に
        通過できる、有効な patterns.json エントリを 1 件組み立てる。
        description のみを可変にすることで、テスト対象を description 判定に絞る。
        """
        return {
            "id": pid,
            "description": description,
            "registered_date": TODAY_STR,
            "trust_score": 0.5,
            "promotion_candidate": False,
            "observations": [{"date": TODAY_STR}],
            "last_updated": TODAY_STR,
        }

    # -- T1 -----------------------------------------------------------------

    def test_t1_warns_when_description_exceeds_threshold(self, tmp_path, capsys):
        """T1: description 1500字のエントリ -> stderr に [Stop] 接頭辞 + 該当 id +
        字数を含む警告が1行出る。"""
        mod, _sessions_dir, patterns_file = self._setup(tmp_path, "t1")
        patterns_data = {"patterns": [self._entry("bloat-pat-1", "A" * 1500)]}
        patterns_file.write_text(json.dumps(patterns_data), encoding="utf-8")

        mod.update_patterns(TODAY_STR)

        err = capsys.readouterr().err
        warning_lines = [
            ln for ln in err.splitlines() if "[Stop]" in ln and "bloat-pat-1" in ln
        ]
        assert len(warning_lines) == 1, (
            f"[T1] description 1500字のエントリに対する [Stop] 警告が1行出るべき。"
            f"Got stderr: {err!r}"
        )
        assert "1500" in warning_lines[0], (
            f"[T1] 警告メッセージに字数(1500)が含まれるべき。Got: {warning_lines[0]!r}"
        )

    # -- T2 -----------------------------------------------------------------

    def test_t2_no_warning_at_exact_threshold_boundary(self, tmp_path, capsys):
        """T2: description ちょうど1000字 -> 警告なし（strict greater の境界固定）。"""
        mod, _sessions_dir, patterns_file = self._setup(tmp_path, "t2")
        patterns_data = {"patterns": [self._entry("boundary-pat", "B" * 1000)]}
        patterns_file.write_text(json.dumps(patterns_data), encoding="utf-8")

        mod.update_patterns(TODAY_STR)

        err = capsys.readouterr().err
        assert "boundary-pat" not in err, (
            f"[T2] ちょうど1000字（strict greater 境界）では警告が出てはいけない。"
            f"Got stderr: {err!r}"
        )

    # -- T3 -----------------------------------------------------------------

    def test_t3_no_warning_for_regular_pattern_length(self, tmp_path, capsys):
        """T3: 正規パターン実測最大長706字 -> 警告なし（誤警告ゼロの回帰防止）。"""
        mod, _sessions_dir, patterns_file = self._setup(tmp_path, "t3")
        patterns_data = {"patterns": [self._entry("regular-pat-706", "C" * 706)]}
        patterns_file.write_text(json.dumps(patterns_data), encoding="utf-8")

        mod.update_patterns(TODAY_STR)

        err = capsys.readouterr().err
        assert "regular-pat-706" not in err, (
            "[T3] 実測最大長706字（learning_signal_attribution_verification 相当）"
            f"では警告が出てはいけない。Got stderr: {err!r}"
        )

    # -- T4 -----------------------------------------------------------------

    def test_t4_multiple_bloated_entries_each_get_own_warning_line(
        self, tmp_path, capsys
    ):
        """T4: 1500字 x 2件 -> 警告2行（id ごとに個別出力）。"""
        mod, _sessions_dir, patterns_file = self._setup(tmp_path, "t4")
        patterns_data = {
            "patterns": [
                self._entry("bloat-pat-a", "D" * 1500),
                self._entry("bloat-pat-b", "E" * 1500),
            ]
        }
        patterns_file.write_text(json.dumps(patterns_data), encoding="utf-8")

        mod.update_patterns(TODAY_STR)

        err = capsys.readouterr().err
        lines_a = [ln for ln in err.splitlines() if "[Stop]" in ln and "bloat-pat-a" in ln]
        lines_b = [ln for ln in err.splitlines() if "[Stop]" in ln and "bloat-pat-b" in ln]
        assert len(lines_a) == 1, (
            f"[T4] bloat-pat-a 用の警告が1行出るべき。Got stderr: {err!r}"
        )
        assert len(lines_b) == 1, (
            f"[T4] bloat-pat-b 用の警告が1行出るべき。Got stderr: {err!r}"
        )

    # -- T5 -----------------------------------------------------------------

    def test_t5_warning_only_does_not_mutate_description(self, tmp_path, capsys):
        """T5: 警告のみ・非破壊 -> update_patterns 後も対象 description が不変で残る。

        警告そのものが出ることも同時に assert する。さもないと『何もしない』実装でも
        非破壊性アサーションだけは現状既に通ってしまい、Red として機能しない。
        """
        mod, _sessions_dir, patterns_file = self._setup(tmp_path, "t5")
        original_desc = "F" * 1500
        patterns_data = {"patterns": [self._entry("nondestructive-pat", original_desc)]}
        patterns_file.write_text(json.dumps(patterns_data), encoding="utf-8")

        mod.update_patterns(TODAY_STR)

        err = capsys.readouterr().err
        assert "nondestructive-pat" in err, (
            "[T5] 肥大エントリに警告が出るべき（警告そのものが出ないと非破壊性の"
            f"検証にならない）。Got stderr: {err!r}"
        )

        data = json.loads(patterns_file.read_text(encoding="utf-8"))
        stored = next(
            (p for p in data["patterns"] if p["id"] == "nondestructive-pat"), None
        )
        assert stored is not None, "[T5] 肥大エントリは削除されず patterns.json に残るべき"
        assert stored["description"] == original_desc, (
            "[T5] 警告は description を変更・切り詰めしてはいけない（非破壊）。"
            f"Got len={len(stored['description'])}, expected len={len(original_desc)}"
        )

    # -- T6 -----------------------------------------------------------------

    def test_t6_fail_safe_survives_malformed_description_fields(
        self, tmp_path, capsys
    ):
        """T6: description キー欠損 / 非str(None・数値) が混在しても例外を伝播させず
        update_patterns が正常完了する。

        併せて、同時に混在させた正規の肥大エントリへの警告も出ることを assert する。
        さもないと『何もしない』実装でも fail-safe アサーションだけは現状既に通って
        しまい、Red として機能しない。

        非 dict エントリ（例: 文字列そのもの）は、fix-cycle-3 で ingestion マージ
        ループ（stop.py L380 付近）と expiry/trust ループ（同 L400 以降）の両経路に
        非 dict ガードが入ったことで、新規観測の有無に関わらず update_patterns
        経由でも crash しなくなった（TestDescriptionBloatWarning の T9 / T11 回帰
        テストでこの挙動を固定）。本テスト（T6）は対象を description 判定に絞る
        ための意図的な選定として、引き続き description キー欠損・非 str の
        2 パターンのみを扱う（非 dict エントリの網羅は T9 / T11 / T7 側の責務）。
        """
        mod, _sessions_dir, patterns_file = self._setup(tmp_path, "t6")
        entry_missing_desc = {
            "id": "nodesc-pat",
            # description キーそのものが無い
            "registered_date": TODAY_STR,
            "trust_score": 0.5,
            "promotion_candidate": False,
            "observations": [{"date": TODAY_STR}],
            "last_updated": TODAY_STR,
        }
        patterns_data = {
            "patterns": [
                entry_missing_desc,
                self._entry("none-desc-pat", None),
                self._entry("num-desc-pat", 12345),
                self._entry("valid-bloat-pat", "G" * 1500),
            ]
        }
        patterns_file.write_text(json.dumps(patterns_data), encoding="utf-8")

        # 例外が伝播しないこと自体が fail-safe の核心的な検証
        mod.update_patterns(TODAY_STR)

        err = capsys.readouterr().err
        assert "valid-bloat-pat" in err, (
            "[T6] 混在する不正エントリに関わらず、正規の肥大エントリには警告が出る"
            f"べき。Got stderr: {err!r}"
        )

        data = json.loads(patterns_file.read_text(encoding="utf-8"))
        ids = {p["id"] for p in data["patterns"]}
        assert {
            "nodesc-pat", "none-desc-pat", "num-desc-pat", "valid-bloat-pat"
        } <= ids, (
            "[T6] 不正な description を持つエントリも削除されず patterns.json に"
            f"残るべき（fail-safe は警告をスキップするだけで削除はしない）。"
            f"Got ids: {ids!r}"
        )

    # -- T7 -----------------------------------------------------------------

    def test_t7_helper_unit_threshold_and_stderr(self, tmp_path, capsys):
        """T7（任意）: _warn_oversized_descriptions に list を直接渡し、閾値判定と
        stderr 出力をヘルパー単体で検証する。非 dict エントリの fail-safe も
        ここで検証する（T6 のコメント参照）。
        """
        mod, _sessions_dir, _patterns_file = self._setup(tmp_path, "t7")

        patterns = [
            {"id": "helper-bloat", "description": "H" * 1500},
            {"id": "helper-ok", "description": "I" * 706},
            "not a dict",
            {"id": "helper-nodesc"},
            {"id": "helper-none-desc", "description": None},
        ]

        # 例外を出さないこと自体が fail-safe の検証（非 dict エントリを含む）
        mod._warn_oversized_descriptions(patterns)

        err = capsys.readouterr().err
        assert "helper-bloat" in err and "[Stop]" in err, (
            f"[T7] 閾値超エントリには [Stop] 警告が出るべき。Got stderr: {err!r}"
        )
        assert "helper-ok" not in err, (
            f"[T7] 706字の正規エントリには警告が出てはいけない。Got stderr: {err!r}"
        )

    # -- T8 -----------------------------------------------------------------

    def test_t8_promoted_entry_still_warns(self, tmp_path, capsys):
        """T8: promoted: true のエントリでも description 肥大なら警告される
        （architecture-report §ADR-3: `_warn_oversized_descriptions` は promoted を
        含む active 全件を一律監査し、promoted か否かで除外しないという設計判断の
        回帰固定）。promoted エントリが expiry/trust 判定をスキップして無条件で
        active に残る経路（stop.py L396-398）も本テストで自然に検証される。
        """
        mod, _sessions_dir, patterns_file = self._setup(tmp_path, "t8")
        entry = self._entry("promoted-bloat-pat", "J" * 1500)
        entry["promoted"] = True
        patterns_data = {"patterns": [entry]}
        patterns_file.write_text(json.dumps(patterns_data), encoding="utf-8")

        mod.update_patterns(TODAY_STR)

        err = capsys.readouterr().err
        warning_lines = [
            ln for ln in err.splitlines()
            if "[Stop]" in ln and "promoted-bloat-pat" in ln
        ]
        assert len(warning_lines) == 1, (
            "[T8] promoted:true でも description 肥大なら [Stop] 警告が1行出るべき"
            f"（promoted 除外禁止の設計固定）。Got stderr: {err!r}"
        )
        assert "1500" in warning_lines[0], (
            f"[T8] 警告メッセージに字数(1500)が含まれるべき。Got: {warning_lines[0]!r}"
        )

        data = json.loads(patterns_file.read_text(encoding="utf-8"))
        stored = next(
            (p for p in data["patterns"] if p["id"] == "promoted-bloat-pat"), None
        )
        assert stored is not None, (
            "[T8] promoted エントリは active に残るべき（expiry/trust 判定スキップ経路）"
        )
        assert stored.get("promoted") is True, (
            "[T8] promoted フラグ自体は変更されず維持されるべき"
        )

    # -- T9 (fix-cycle-2 / FB1, originally Red-a) ---------------------------

    def test_t9_non_dict_pattern_entries_do_not_crash_and_are_retained(
        self, tmp_path
    ):
        """T9 [SR-V-001 Medium / originally Red-a]: data['patterns'] 直下に非 dict エントリ
        （文字列・None）が混在していても update_patterns が例外を伝播させず完走し、
        正規 dict エントリの処理・save が行われることを固定する。

        plan-report-20260705-162421.md の決定（非 dict エントリの扱い = 保持）に
        従い、非 dict エントリも save 後の patterns.json に残っていることを assert
        する（stop.py L400-404 の registered_date parse 不能時の「保持して継続」
        慣行と対称）。

        fix-cycle-2 適用前の実装では expiry/trust ループ先頭の
        `pattern.get('promoted', False)` が非 dict エントリで
        AttributeError（例: 'str' object has no attribute 'get'）を送出し、
        save に到達せず update_patterns 自体が異常終了するため、originally
        Red-first で赤だった。
        """
        mod, _sessions_dir, patterns_file = self._setup(tmp_path, "t9")
        valid_entry = self._entry("valid-pat-t9", "normal description")
        patterns_data = {"patterns": ["not a dict", None, valid_entry]}
        patterns_file.write_text(json.dumps(patterns_data), encoding="utf-8")

        # 例外が伝播しないこと自体が Red-a の核心的な検証
        # （修正前の実装は 'not a dict'.get(...) の AttributeError で crash していた）。
        mod.update_patterns(TODAY_STR)

        data = json.loads(patterns_file.read_text(encoding="utf-8"))
        stored_patterns = data["patterns"]

        valid_stored = next(
            (
                p for p in stored_patterns
                if isinstance(p, dict) and p.get("id") == "valid-pat-t9"
            ),
            None,
        )
        assert valid_stored is not None, (
            "[T9] 正規 dict エントリは save 後の patterns.json に残るべき"
            f"（save 到達確認）。Got stored_patterns: {stored_patterns!r}"
        )

        assert "not a dict" in stored_patterns, (
            "[T9] 非 dict エントリ（文字列）は削除されず保持されるべき"
            "（planner 決定: registered_date parse 不能時の保持慣行と対称）。"
            f"Got stored_patterns: {stored_patterns!r}"
        )
        assert None in stored_patterns, (
            "[T9] 非 dict エントリ（None）は削除されず保持されるべき。"
            f"Got stored_patterns: {stored_patterns!r}"
        )

    # -- T10 (fix-cycle-2 / FB1, originally Red-b) ---------------------------

    def test_t10_pid_control_chars_and_overlong_id_are_sanitized_in_stderr(
        self, tmp_path, capsys
    ):
        """T10 [SR-V-001 Low / originally Red-b]: 肥大警告 (`_warn_oversized_descriptions`) が
        出力する pid（id フィールド）に ANSI エスケープ・NUL・CR（制御文字）を含み
        MAX_ID_LENGTH(64) を超える長さの場合でも、stderr 出力ではこれらが
        `_INHERIT_SANITIZE_RE` 相当でサニタイズされ、MAX_ID_LENGTH 以内に
        切り詰められていることを固定する。

        制御文字の代表として CR (\\r, 0x0D) を用いる。LF (\\n, 0x0A) は
        `_INHERIT_SANITIZE_RE`（FB2 で再利用が指示されている既存正規表現。
        stop.py L41-43）の除去対象文字クラス（\\x00-\\x08, \\x0b-\\x1f, ...）の
        範囲外であり、過去セッションの複数行バックログ引き継ぎでは意図的に
        タブ・通常スペースと同様に温存される設計になっている。この既存正規表現を
        そのまま再利用する前提（plan-report 明記）では LF は除去対象外のため、
        LF を含めると FB2 の想定実装後も解消されない「偽の Red」になってしまう。
        よってここでは確実に除去される CR を制御文字/改行系の代表として使う。

        fix-cycle-2 適用前の実装は pid をサニタイズせずそのまま f-string に
        埋め込んで出力していたため、originally Red-first で赤だった。
        """
        mod, _sessions_dir, patterns_file = self._setup(tmp_path, "t10")
        ansi_escape = "\x1b[31m"
        null_char = "\x00"
        cr_char = "\r"
        long_tail = "Q" * 100  # サニタイズ後も MAX_ID_LENGTH(64) 超の長さを保証する
        dirty_id = f"{ansi_escape}BADPID{null_char}{cr_char}{long_tail}"

        entry = self._entry(dirty_id, "L" * 1500)
        entry["promoted"] = True  # expiry/trust 判定をスキップし確実に active へ入れる
        patterns_data = {"patterns": [entry]}
        patterns_file.write_text(json.dumps(patterns_data), encoding="utf-8")

        mod.update_patterns(TODAY_STR)

        err = capsys.readouterr().err

        # 肥大検知自体は動く（[Stop] 警告 + 字数が出る）
        assert "[Stop]" in err and "1500" in err, (
            f"[T10] 肥大警告自体は出力されるべき。Got stderr: {err!r}"
        )

        # 生の ANSI エスケープ・NUL・CR が stderr に含まれない
        assert "\x1b" not in err, (
            "[T10] ANSI エスケープ (\\x1b) がサニタイズされず stderr に出力されている。"
            f"Got stderr: {err!r}"
        )
        assert "\x00" not in err, (
            "[T10] NUL 文字 (\\x00) がサニタイズされず stderr に出力されている。"
            f"Got stderr: {err!r}"
        )
        assert "\r" not in err, (
            "[T10] CR (\\r) がサニタイズされず stderr に出力されている。"
            f"Got stderr: {err!r}"
        )

        # id 部分が _INHERIT_SANITIZE_RE 相当でサニタイズ済みかつ
        # MAX_ID_LENGTH(64) 以内に切り詰められている
        expected_sanitized_id = mod._INHERIT_SANITIZE_RE.sub("", dirty_id)[
            : mod.MAX_ID_LENGTH
        ]
        assert expected_sanitized_id in err, (
            f"[T10] サニタイズ済み id ({expected_sanitized_id!r}) が stderr に"
            f"含まれるべき。Got stderr: {err!r}"
        )
        # 未サニタイズの生 pid がそのまま出力されていないこと（真の Red 確認）
        assert dirty_id not in err, (
            "[T10] 生の pid がサニタイズされず stderr にそのまま出力されている。"
            f"Got stderr: {err!r}"
        )

    # -- T11 (fix-cycle-3 / FC1) ----------------------------------------------

    def test_t11_new_observations_with_non_dict_patterns_do_not_crash(
        self, tmp_path
    ):
        """T11 [SR-V-001 / CR-E-001 Medium]: 新規観測ありセッション（new_observations
        が非空）+ patterns.json に非 dict エントリ混在という条件でも update_patterns
        が例外を伝播せず完走し、正規 dict エントリ・非 dict エントリ・新規観測の
        いずれも save 後に残ることを固定する。

        T9 は `_write_session_no_json`（new_observations=[]）を使うため、ingestion
        マージループ（stop.py L380 `existing = next((p for p in data['patterns']
        if p['id'] == pid), None)`）自体が実行されず、この経路の残存クラッシュを
        検出できない（security-review-report-20260705-164112.md [SR-V-001] /
        code-review-report-20260705-164113.md [CR-E-001] で指摘された回帰テストの
        盲点）。本テストは `_write_session_with_patterns` で新規観測を 1 件以上
        与えることで ingestion マージループを実際に通過させ、この盲点を埋める。

        fix-cycle-3 の本 Red 追加時点（stop.py 未修正）では、L380 の
        `p['id']` が非 dict エントリ（文字列・None）への添字アクセスとなるため
        `TypeError: string indices must be integers, not 'str'` を送出し、
        update_patterns 自体が異常終了していた。よって本テストは、FC2 で
        isinstance ガードが入るまで赤だった。
        """
        mod, sessions_dir, patterns_file = self._setup(tmp_path, "t11")
        valid_entry = self._entry("valid-pat-t11", "normal description")
        patterns_data = {"patterns": ["not a dict", None, valid_entry]}
        patterns_file.write_text(json.dumps(patterns_data), encoding="utf-8")

        # _setup は _write_session_no_json（new_observations=[]）でセッション
        # ファイルを作るため、同じ日付のファイルを新規観測ありで上書きし、
        # ingestion マージループ（L373 の for obs in new_observations:）を
        # 実際に通過させる。
        _write_session_with_patterns(
            sessions_dir, TODAY_STR, [
                {"id": "new-obs-pat-t11", "description": "newly observed pattern"}
            ]
        )

        # 例外が伝播しないこと自体が本テストの核心的な検証
        # （修正前の実装は ingestion マージループの p['id'] で TypeError を
        # 送出していた）。
        mod.update_patterns(TODAY_STR)

        data = json.loads(patterns_file.read_text(encoding="utf-8"))
        stored_patterns = data["patterns"]

        valid_stored = next(
            (
                p for p in stored_patterns
                if isinstance(p, dict) and p.get("id") == "valid-pat-t11"
            ),
            None,
        )
        assert valid_stored is not None, (
            "[T11] 正規 dict エントリは save 後の patterns.json に残るべき"
            f"（save 到達確認）。Got stored_patterns: {stored_patterns!r}"
        )

        new_obs_stored = next(
            (
                p for p in stored_patterns
                if isinstance(p, dict) and p.get("id") == "new-obs-pat-t11"
            ),
            None,
        )
        assert new_obs_stored is not None, (
            "[T11] 新規観測は ingestion マージループを通過し、save 後の "
            "patterns.json に新規エントリとして残るべき。"
            f"Got stored_patterns: {stored_patterns!r}"
        )

        assert "not a dict" in stored_patterns, (
            "[T11] 非 dict エントリ（文字列）は削除されず保持されるべき"
            "（T9 と同じ保持方針）。"
            f"Got stored_patterns: {stored_patterns!r}"
        )
        assert None in stored_patterns, (
            "[T11] 非 dict エントリ（None）は削除されず保持されるべき。"
            f"Got stored_patterns: {stored_patterns!r}"
        )
