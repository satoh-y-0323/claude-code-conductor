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
