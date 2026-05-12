"""Minimal stdio MCP server for C3 adapters.

The server exposes C3 question and skill helpers to hosts that support MCP
(Codex and Cursor). It intentionally has no third-party dependency so the C3
package remains lightweight.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from c3 import __version__
from c3.paths import claude_root_for
from c3.question import (
    load_questions,
    mcp_free_text_schema,
    mcp_requested_schema,
    no_multi_select_choice,
    normalize_mcp_multi_select_answer,
    normalize_mcp_answer,
    selected_free_text_requires_detail,
)

PROTOCOL_VERSION = "2025-11-25"


def main() -> int:
    _force_utf8_stdio()
    server = C3MCPServer()
    return server.run()


class C3MCPServer:
    def __init__(self) -> None:
        self._next_id = 1
        self._client_supports_elicitation = False
        self.project_root = _project_root()

    def run(self) -> int:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
                response = self._handle(message)
            except Exception as exc:  # pragma: no cover - defensive protocol boundary
                response = self._error(None, -32603, str(exc))
            if response is not None:
                self._send(response)
        return 0

    def _handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        msg_id = message.get("id")
        if method == "initialize":
            capabilities = message.get("params", {}).get("capabilities", {})
            self._client_supports_elicitation = (
                isinstance(capabilities, dict) and "elicitation" in capabilities
            )
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "c3", "version": __version__},
                    "instructions": (
                        "Use c3_ask_user_question whenever C3 instructions contain "
                        "AskUserQuestion JSON. The tool preserves single-select and "
                        "multiSelect answers."
                    ),
                },
            }
        if method in {"notifications/initialized", "initialized"}:
            return None
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": _tools()}}
        if method == "tools/call":
            return self._call_tool(msg_id, message.get("params", {}))
        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        if msg_id is None:
            return None
        return self._error(msg_id, -32601, f"unknown method: {method}")

    def _call_tool(self, msg_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "c3_ask_user_question":
            return self._tool_ask(msg_id, args)
        if name == "c3_list_skills":
            return self._text_result(msg_id, json.dumps(self._list_skills(), ensure_ascii=False))
        if name == "c3_read_skill":
            skill = str(args.get("name", ""))
            text = self._read_skill(skill)
            if text is None:
                return self._tool_error(msg_id, f"skill not found: {skill}")
            return self._text_result(msg_id, text)
        return self._error(msg_id, -32602, f"unknown tool: {name}")

    def _tool_ask(self, msg_id: Any, args: dict[str, Any]) -> dict[str, Any]:
        payload = args.get("payload") or args.get("question") or args
        try:
            questions = load_questions(payload)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return self._tool_error(msg_id, f"invalid question payload: {exc}")

        if not self._client_supports_elicitation:
            return self._tool_error(
                msg_id,
                "This MCP client did not advertise elicitation support. "
                "Run `c3 ask --file <json>` as the fallback.",
            )

        answers = []
        for question in questions:
            elicited = self._elicit(question.question, mcp_requested_schema(question))
            action = elicited.get("action")
            if action != "accept":
                return self._tool_error(msg_id, f"user {action or 'cancelled'} the question")
            content = elicited.get("content")
            if no_multi_select_choice(question, content):
                return self._tool_error(msg_id, "multiSelect question requires at least one option")
            if selected_free_text_requires_detail(question, content):
                follow_up = self._elicit(
                    f"{question.question}\n自由入力の内容を入力してください。",
                    mcp_free_text_schema(question),
                )
                if follow_up.get("action") != "accept":
                    return self._tool_error(
                        msg_id,
                        f"user {follow_up.get('action') or 'cancelled'} the free-text question",
                    )
                merged = dict(content or {})
                merged.update(follow_up.get("content") or {})
                content = merged
            if question.multi_select and "choice" not in (content or {}):
                answers.append(normalize_mcp_multi_select_answer(question, content))
            else:
                answers.append(normalize_mcp_answer(question, content))

        return self._text_result(
            msg_id,
            json.dumps({"answers": answers}, ensure_ascii=False),
        )

    def _elicit(self, message: str, requested_schema: dict[str, Any]) -> dict[str, Any]:
        request_id = f"c3-elicitation-{self._next_id}"
        self._next_id += 1
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "elicitation/create",
                "params": {
                    "mode": "form",
                    "message": message,
                    "requestedSchema": requested_schema,
                },
            }
        )
        while True:
            line = sys.stdin.readline()
            if not line:
                return {"action": "cancel"}
            payload = json.loads(line)
            if payload.get("id") == request_id and "result" in payload:
                return payload["result"]
            # Notifications can arrive while waiting for elicitation. Ignore them.

    def _list_skills(self) -> list[dict[str, str]]:
        skills_dir = self.project_root / ".claude" / "skills"
        if not skills_dir.is_dir():
            return []
        result = []
        for skill_dir in sorted(path for path in skills_dir.iterdir() if path.is_dir()):
            skill_file = skill_dir / "SKILL.md"
            if skill_file.is_file():
                result.append({"name": skill_dir.name, "path": str(skill_file)})
        return result

    def _read_skill(self, skill: str) -> str | None:
        if not skill or any(part in {".", ".."} for part in Path(skill).parts):
            return None
        skills_root = (self.project_root / ".claude" / "skills").resolve()
        path = skills_root / skill / "SKILL.md"
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if skills_root not in resolved.parents:
            return None
        if not resolved.is_file():
            return None
        return resolved.read_text(encoding="utf-8")

    def _text_result(self, msg_id: Any, text: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"content": [{"type": "text", "text": text}]},
        }

    def _tool_error(self, msg_id: Any, text: str) -> dict[str, Any]:
        result = self._text_result(msg_id, text)
        result["result"]["isError"] = True
        return result

    def _error(self, msg_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    def _send(self, payload: dict[str, Any]) -> None:
        print(_jsonrpc_line(payload), flush=True)


def _project_root() -> Path:
    raw = os.environ.get("C3_PROJECT_ROOT")
    start = Path(raw).resolve() if raw else Path.cwd().resolve()
    root = claude_root_for(start)
    return root or start


def _tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "c3_ask_user_question",
            "description": (
                "Render a C3 AskUserQuestion payload through MCP elicitation and "
                "return the selected labels as JSON. Supports multiSelect."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "payload": {
                        "type": "object",
                        "description": "C3 question payload, usually {questions:[...]}",
                    }
                },
                "required": ["payload"],
            },
        },
        {
            "name": "c3_list_skills",
            "description": "List C3 skills available under .claude/skills.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "c3_read_skill",
            "description": "Read a C3 skill from .claude/skills/<name>/SKILL.md.",
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    ]


def _jsonrpc_line(payload: dict[str, Any]) -> str:
    """Serialize JSON-RPC as ASCII-safe text for Windows stdio pipes."""
    return json.dumps(payload, ensure_ascii=True)


def _force_utf8_stdio() -> None:
    """MCP stdio is UTF-8; Windows Python otherwise defaults to cp932."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


if __name__ == "__main__":
    raise SystemExit(main())
