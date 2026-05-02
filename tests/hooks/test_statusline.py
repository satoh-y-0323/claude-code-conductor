"""Tests for .claude/hooks/statusline.py

Covers:
- [Sec Low-1] stdin MAX_INPUT overrun: joined chunks must not exceed MAX_INPUT bytes
  after breaking out of the read loop when total_size > MAX_INPUT.
- Normal input (below MAX_INPUT) must be processed without truncation.
- statusline display output (context gauge) must be produced for valid JSON input.
"""

import importlib.util
import io
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Load statusline module from the hook path (not installed as a package).
# The module calls sys.stdin.reconfigure() at import time, which fails under
# pytest (pytest replaces stdin with DontReadFromInput).  We patch the three
# stream reconfigure calls away before exec_module runs.
# ---------------------------------------------------------------------------
STATUSLINE_PATH = (
    Path(__file__).parents[2]
    / ".claude"
    / "hooks"
    / "statusline.py"
)


def load_statusline() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("statusline", STATUSLINE_PATH)
    mod = importlib.util.module_from_spec(spec)

    # Provide real stream objects so reconfigure() succeeds at module level
    fake_stdin = io.TextIOWrapper(io.BytesIO(b""), encoding="utf-8")
    fake_stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    fake_stderr = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")

    with (
        patch.object(sys, "stdin", fake_stdin),
        patch.object(sys, "stdout", fake_stdout),
        patch.object(sys, "stderr", fake_stderr),
    ):
        spec.loader.exec_module(mod)
    return mod


statusline = load_statusline()

MAX_INPUT = statusline.MAX_INPUT  # 64 * 1024 = 65536


# ---------------------------------------------------------------------------
# Helper: run main() with controlled stdin, capture stdout
# ---------------------------------------------------------------------------
def run_main_with_input(input_text: str) -> str:
    """Run statusline.main() with the given string as stdin, return stdout."""
    fake_stdin = io.StringIO(input_text)
    fake_stdout = io.StringIO()

    with (
        patch.object(sys, "stdin", fake_stdin),
        patch.object(sys, "stdout", fake_stdout),
    ):
        statusline.main()

    return fake_stdout.getvalue()


# ---------------------------------------------------------------------------
# Sec Low-1: overrun test – joined chunks must not exceed MAX_INPUT
# ---------------------------------------------------------------------------
class TestMaxInputEnforcement:
    def test_chunks_not_exceed_max_input_when_overrun(self):
        """[Sec Low-1] When stdin exceeds MAX_INPUT, joined chunks <= MAX_INPUT bytes."""
        # Build input that is clearly larger than MAX_INPUT.
        # Use lines of 1 KB each so total > 64 KB.
        line = "A" * 1023 + "\n"  # 1024 bytes per line
        num_lines = 100  # 100 KB total – well over 64 KB
        oversized_input = line * num_lines

        assert len(oversized_input.encode("utf-8")) > MAX_INPUT, (
            "Pre-condition: input must be larger than MAX_INPUT"
        )

        # Patch render_output to capture what chunks were joined
        captured_raw: list[str] = []
        original_render = statusline.render_output

        def spy_render(raw: str) -> None:
            captured_raw.append(raw)
            original_render(raw)

        with patch.object(statusline, "render_output", side_effect=spy_render):
            fake_stdin = io.StringIO(oversized_input)
            fake_stdout = io.StringIO()
            with (
                patch.object(sys, "stdin", fake_stdin),
                patch.object(sys, "stdout", fake_stdout),
            ):
                statusline.main()

        assert len(captured_raw) == 1, "render_output should be called exactly once"
        joined = captured_raw[0]
        assert len(joined.encode("utf-8")) <= MAX_INPUT, (
            f"Joined chunks length {len(joined.encode('utf-8'))} exceeds "
            f"MAX_INPUT {MAX_INPUT}. The overrun fix is not applied."
        )

    def test_chunks_not_exceed_max_input_exact_boundary(self):
        """Boundary: input of exactly MAX_INPUT+1 bytes must still fit within MAX_INPUT."""
        # Create a single large line just over MAX_INPUT
        line_size = MAX_INPUT + 1
        oversized_input = "B" * line_size + "\n"

        captured_raw: list[str] = []
        original_render = statusline.render_output

        def spy_render(raw: str) -> None:
            captured_raw.append(raw)
            original_render(raw)

        with patch.object(statusline, "render_output", side_effect=spy_render):
            fake_stdin = io.StringIO(oversized_input)
            fake_stdout = io.StringIO()
            with (
                patch.object(sys, "stdin", fake_stdin),
                patch.object(sys, "stdout", fake_stdout),
            ):
                statusline.main()

        assert len(captured_raw) == 1
        joined = captured_raw[0]
        assert len(joined.encode("utf-8")) <= MAX_INPUT, (
            f"Boundary overrun: joined length {len(joined.encode('utf-8'))} > {MAX_INPUT}"
        )


# ---------------------------------------------------------------------------
# Normal input (below MAX_INPUT) must not be truncated
# ---------------------------------------------------------------------------
class TestNormalInput:
    def test_small_valid_json_is_not_truncated(self):
        """Normal JSON input smaller than MAX_INPUT must reach render_output intact."""
        payload = '{"context_window": {"used_percentage": 42}}\n'
        assert len(payload.encode("utf-8")) < MAX_INPUT

        captured_raw: list[str] = []
        original_render = statusline.render_output

        def spy_render(raw: str) -> None:
            captured_raw.append(raw)
            original_render(raw)

        with patch.object(statusline, "render_output", side_effect=spy_render):
            fake_stdin = io.StringIO(payload)
            fake_stdout = io.StringIO()
            with (
                patch.object(sys, "stdin", fake_stdin),
                patch.object(sys, "stdout", fake_stdout),
            ):
                statusline.main()

        assert len(captured_raw) == 1
        # Content must not be altered for normal-sized input
        assert captured_raw[0] == payload

    def test_empty_input_does_not_crash(self):
        """Empty stdin must not raise an exception; output should still be produced."""
        output = run_main_with_input("")
        # Should produce some output (the gauge line)
        assert len(output) > 0


# ---------------------------------------------------------------------------
# Display / output correctness
# ---------------------------------------------------------------------------
class TestDisplayOutput:
    def test_context_gauge_appears_in_output(self):
        """Context gauge label must appear in stdout for valid JSON input."""
        payload = '{"context_window": {"used_percentage": 55}}\n'
        output = run_main_with_input(payload)
        assert "context usage:" in output

    def test_invalid_json_still_produces_output(self):
        """Even if stdin is not valid JSON, a gauge line must be printed."""
        output = run_main_with_input("not json at all\n")
        assert "context usage:" in output

    def test_rate_limit_section_appears_when_provided(self):
        """Rate limit gauge must appear when five_hour data is in JSON."""
        payload = (
            '{"context_window": {"used_percentage": 10},'
            ' "rate_limits": {"five_hour": {"used_percentage": 80, "resets_at": null}}}\n'
        )
        output = run_main_with_input(payload)
        assert "5hour limits:" in output
