"""Tests for .claude/hooks/statusline.py"""

import io
import sys
import time
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

# pytest の DontReadFromInput との非互換を回避
# statusline.py はモジュールレベルで sys.stdin/stdout/stderr.reconfigure を呼ぶ
sys.stdin = MagicMock()
sys.stdin.reconfigure = MagicMock()
sys.stdout.reconfigure = MagicMock()
sys.stderr.reconfigure = MagicMock()

# importlib で .claude/hooks/statusline.py を直接ロード
_spec = importlib.util.spec_from_file_location(
    "statusline",
    Path(__file__).parent.parent / ".claude" / "hooks" / "statusline.py",
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


# ---------------------------------------------------------------------------
# pct_color (4 件)
# ---------------------------------------------------------------------------

def test_pct_color_above_90_returns_red():
    """pct_color(91) は RED を返す（90 超 → RED）"""
    assert mod.pct_color(91) == mod.RED


def test_pct_color_above_75_returns_orange():
    """pct_color(76) は ORANGE を返す（75 超 → ORANGE）"""
    assert mod.pct_color(76) == mod.ORANGE


def test_pct_color_above_60_returns_yellow():
    """pct_color(61) は YELLOW を返す（60 超 → YELLOW）"""
    assert mod.pct_color(61) == mod.YELLOW


def test_pct_color_60_or_below_returns_green():
    """pct_color(50) は GREEN を返す（60 以下 → GREEN）"""
    assert mod.pct_color(50) == mod.GREEN


# ---------------------------------------------------------------------------
# build_gauge (2 件)
# ---------------------------------------------------------------------------

def test_build_gauge_100_contains_10_blocks():
    """build_gauge(100) は BLOCK 10 個を含む"""
    result = mod.build_gauge(100)
    assert mod.BLOCK * 10 in result


def test_build_gauge_0_contains_10_empty_blocks():
    """build_gauge(0) は BLOCK_EMPTY 10 個を含む"""
    result = mod.build_gauge(0)
    assert mod.BLOCK_EMPTY * 10 in result


# ---------------------------------------------------------------------------
# format_reset_time (2 件)
# ---------------------------------------------------------------------------

def test_format_reset_time_unix_future_returns_time_string():
    """未来の unix タイムスタンプを渡すと "Xm" / "Xh Ym" / "Xd Yh" 形式の文字列を返す"""
    future_unix = time.time() + 300  # 5 分後
    result = mod.format_reset_time(future_unix)
    assert any(unit in result for unit in ("m", "h", "d")), (
        f"Expected time string containing 'm', 'h', or 'd', got: {result!r}"
    )


def test_format_reset_time_iso_future_returns_time_string():
    """未来の ISO 8601 文字列を渡すと "Xm" / "Xh Ym" / "Xd Yh" 形式の文字列を返す"""
    iso_future = "2099-01-01T00:00:00+00:00"
    result = mod.format_reset_time(iso_future)
    assert any(unit in result for unit in ("m", "h", "d")), (
        f"Expected time string containing 'm', 'h', or 'd', got: {result!r}"
    )


# ---------------------------------------------------------------------------
# render_output (2 件)
# ---------------------------------------------------------------------------

def test_render_output_context_usage_contains_expected_text(capsys):
    """render_output がコンテキスト使用率を含む出力を書き出す"""
    import json
    payload = json.dumps({"context_window": {"used_percentage": 50}})
    mod.render_output(payload)
    captured = capsys.readouterr()
    output = captured.out
    assert "context" in output or "%" in output, (
        f"Expected 'context' or '%' in output, got: {output!r}"
    )


def test_render_output_rate_limit_contains_expected_text(capsys):
    """render_output が rate limit 情報を含む出力を書き出す"""
    import json
    payload = json.dumps({
        "context_window": {"used_percentage": 20},
        "rate_limits": {
            "five_hour": {
                "used_percentage": 40,
                "resets_at": None,
            }
        },
    })
    mod.render_output(payload)
    captured = capsys.readouterr()
    output = captured.out
    assert "5hour" in output or "limit" in output, (
        f"Expected '5hour' or 'limit' in output, got: {output!r}"
    )
