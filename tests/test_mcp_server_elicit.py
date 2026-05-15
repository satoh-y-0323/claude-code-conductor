"""Tests for ``c3.mcp_server.C3MCPServer._elicit`` error-handling.

[CR-T-001] 不正な JSON 行が届いた場合にメソッドが例外で抜けず、
ログに記録してスキップし、ループ継続することを検証する。
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from c3.mcp_server import C3MCPServer


def _make_server(tmp_path: Path) -> C3MCPServer:
    """最小限のプロジェクトルートを持つサーバーインスタンスを返す。"""
    (tmp_path / ".claude").mkdir()
    server = C3MCPServer.__new__(C3MCPServer)
    server._next_id = 1
    server._client_supports_elicitation = False
    server.project_root = tmp_path
    return server


def _stdin_from_lines(*lines: str) -> io.StringIO:
    """複数行を結合して StringIO を返す。最後は EOF（readline で '' を返す）。"""
    return io.StringIO("".join(lines))


class TestElicitInvalidJson:
    """_elicit の json.JSONDecodeError ハンドリング。"""

    def test_invalid_json_line_is_skipped_and_loop_continues(self, tmp_path, capsys):
        """不正 JSON 行 → 有効な結果行 の順に届いた場合、例外が出ずに結果を返す。"""
        server = _make_server(tmp_path)
        request_id = f"c3-elicitation-{server._next_id}"

        # 有効な elicitation 応答
        valid_response = json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": {"action": "accept", "content": {"answer": "ok"}}}
        ) + "\n"

        # 不正 JSON 行を先に、次に有効な応答を配置
        stdin_data = _stdin_from_lines(
            "not valid json\n",
            valid_response,
        )

        # _send は stdout に書き出すが、ここでは副作用を無視する
        with patch.object(server, "_send"):
            with patch("sys.stdin", stdin_data):
                result = server._elicit("test message", {"type": "object", "properties": {}})

        assert result == {"action": "accept", "content": {"answer": "ok"}}
        # stderr にスキップログが出ていること
        captured = capsys.readouterr()
        assert "[c3 mcp_server] _elicit: invalid JSON skipped:" in captured.err

    def test_multiple_invalid_json_lines_are_all_skipped(self, tmp_path, capsys):
        """複数の不正 JSON 行が連続しても、最終的に有効な応答が返る。"""
        server = _make_server(tmp_path)
        request_id = f"c3-elicitation-{server._next_id}"

        valid_response = json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": {"action": "cancel"}}
        ) + "\n"

        stdin_data = _stdin_from_lines(
            "{broken\n",
            "{{also broken}}\n",
            valid_response,
        )

        with patch.object(server, "_send"):
            with patch("sys.stdin", stdin_data):
                result = server._elicit("test message", {"type": "object", "properties": {}})

        assert result == {"action": "cancel"}
        captured = capsys.readouterr()
        # 2 行分のスキップログが記録されること
        assert captured.err.count("[c3 mcp_server] _elicit: invalid JSON skipped:") == 2

    def test_eof_after_invalid_json_returns_cancel(self, tmp_path):
        """不正 JSON 行の後に EOF が来た場合は {'action': 'cancel'} を返す。"""
        server = _make_server(tmp_path)

        stdin_data = _stdin_from_lines(
            "this is not json\n",
            # EOF: StringIO はここで readline() == '' を返す
        )

        with patch.object(server, "_send"):
            with patch("sys.stdin", stdin_data):
                result = server._elicit("test message", {"type": "object", "properties": {}})

        assert result == {"action": "cancel"}

    def test_clean_eof_without_invalid_json_still_returns_cancel(self, tmp_path):
        """不正 JSON なしで即 EOF の場合も cancel を返す（既存動作の回帰テスト）。"""
        server = _make_server(tmp_path)

        stdin_data = _stdin_from_lines()  # 空 = 即 EOF

        with patch.object(server, "_send"):
            with patch("sys.stdin", stdin_data):
                result = server._elicit("test message", {"type": "object", "properties": {}})

        assert result == {"action": "cancel"}
