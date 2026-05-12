"""Tests for the C3 AskUserQuestion compatibility layer."""

from __future__ import annotations

import argparse
import json

from c3 import cli_ask
from c3.mcp_server import C3MCPServer, _force_utf8_stdio, _jsonrpc_line
from c3.question import load_questions, mcp_requested_schema, normalize_mcp_answer


QUESTION_JSON = json.dumps(
    {
        "questions": [
            {
                "question": "Choose phases",
                "options": [
                    {"label": "Plan", "description": "plan-report"},
                    {"label": "Review", "description": "review-report"},
                ],
                "multiSelect": True,
            }
        ]
    },
    ensure_ascii=False,
)

FREE_TEXT_QUESTION_JSON = json.dumps(
    {
        "questions": [
            {
                "question": "使用する言語を教えてください",
                "options": [
                    {"label": "Python"},
                    {"label": "その他・自由入力"},
                ],
            }
        ]
    },
    ensure_ascii=False,
)


def test_cli_ask_response_supports_multiselect(capsys):
    rc = cli_ask.handle(
        argparse.Namespace(
            file=None,
            json_text=QUESTION_JSON,
            response="1,Review",
            pretty=False,
        )
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["answers"][0]["labels"] == ["Plan", "Review"]
    assert payload["answers"][0]["multiSelect"] is True


def test_mcp_schema_uses_array_for_multiselect():
    question = load_questions(QUESTION_JSON)[0]

    schema = mcp_requested_schema(question)

    choice_1 = schema["properties"]["choice_1"]
    assert choice_1["type"] == "boolean"
    assert choice_1["title"] == "Plan"
    assert "choice" not in schema["properties"]


def test_normalize_mcp_answer_returns_c3_shape():
    question = load_questions(QUESTION_JSON)[0]

    answer = normalize_mcp_answer(question, {"choice": ["Review"]})

    assert answer["labels"] == ["Review"]
    assert answer["indices"] == [2]


def test_mcp_schema_adds_details_field_for_free_input_option():
    question = load_questions(FREE_TEXT_QUESTION_JSON)[0]

    schema = mcp_requested_schema(question)

    assert "details" not in schema["properties"]


def test_mcp_schema_adds_details_field_for_multiselect_free_input_option():
    question = load_questions(
        {
            "questions": [
                {
                    "question": "Choose",
                    "options": [
                        {"label": "A"},
                        {"label": "その他・自由入力"},
                    ],
                    "multiSelect": True,
                }
            ]
        }
    )[0]

    schema = mcp_requested_schema(question)

    assert schema["properties"]["details"]["type"] == "string"


def test_normalize_mcp_answer_preserves_free_text():
    question = load_questions(FREE_TEXT_QUESTION_JSON)[0]

    answer = normalize_mcp_answer(
        question,
        {"choice": "その他・自由入力", "details": "PowerShell 7"},
    )

    assert answer["labels"] == ["その他・自由入力"]
    assert answer["freeText"] == "PowerShell 7"
    assert answer["selected"][0]["value"] == "その他・自由入力: PowerShell 7"


def test_mcp_server_advertises_c3_question_tool():
    server = C3MCPServer()

    initialized = server._handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"capabilities": {"elicitation": {"form": {}}}},
        }
    )
    tools = server._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    assert initialized["result"]["capabilities"] == {"tools": {}}
    assert any(
        tool["name"] == "c3_ask_user_question"
        for tool in tools["result"]["tools"]
    )


def test_mcp_server_treats_empty_elicitation_capability_as_supported():
    server = C3MCPServer()

    server._handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"capabilities": {"elicitation": {}}},
        }
    )

    assert server._client_supports_elicitation is True


def test_mcp_ask_requires_elicitation_support():
    server = C3MCPServer()
    server._handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"capabilities": {}},
        }
    )

    result = server._handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "c3_ask_user_question",
                "arguments": {"payload": json.loads(QUESTION_JSON)},
            },
        }
    )

    assert result["result"]["isError"] is True
    assert "elicitation" in result["result"]["content"][0]["text"]


def test_mcp_ask_follows_up_when_free_input_choice_has_no_text():
    server = C3MCPServer()
    server._handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"capabilities": {"elicitation": {}}},
        }
    )
    responses = iter(
        [
            {"action": "accept", "content": {"choice": "その他・自由入力"}},
            {"action": "accept", "content": {"details": "PowerShell 7"}},
        ]
    )
    server._elicit = lambda _message, _schema: next(responses)  # type: ignore[method-assign]

    result = server._handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "c3_ask_user_question",
                "arguments": {"payload": json.loads(FREE_TEXT_QUESTION_JSON)},
            },
        }
    )

    text = result["result"]["content"][0]["text"]
    assert json.loads(text)["answers"][0]["freeText"] == "PowerShell 7"


def test_mcp_ask_normalizes_boolean_multiselect_fields():
    server = C3MCPServer()
    server._handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"capabilities": {"elicitation": {}}},
        }
    )
    server._elicit = lambda _message, _schema: {  # type: ignore[method-assign]
        "action": "accept",
        "content": {"choice_1": True, "choice_2": False, "choice_3": True},
    }

    result = server._handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "c3_ask_user_question",
                "arguments": {
                    "payload": {
                        "questions": [
                            {
                                "question": "Choose",
                                "options": [
                                    {"label": "A"},
                                    {"label": "B"},
                                    {"label": "C"},
                                ],
                                "multiSelect": True,
                            }
                        ]
                    }
                },
            },
        }
    )

    text = result["result"]["content"][0]["text"]
    answer = json.loads(text)["answers"][0]
    assert answer["labels"] == ["A", "C"]
    assert answer["indices"] == [1, 3]


def test_mcp_jsonrpc_line_is_ascii_safe_for_windows_stdio():
    line = _jsonrpc_line(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "日本語 — dash"}]},
        }
    )

    line.encode("cp932")
    assert "\\u65e5\\u672c\\u8a9e" in line


def test_mcp_stdio_reconfigure_helper_is_safe_to_call():
    _force_utf8_stdio()
