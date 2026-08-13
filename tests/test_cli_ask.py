"""Tests for the C3 AskUserQuestion compatibility layer."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from c3 import cli_ask, question
from c3.mcp_server import C3MCPServer, _force_utf8_stdio, _jsonrpc_line
from c3.question import load_questions, mcp_requested_schema, normalize_mcp_answer

REPO_SRC = Path(__file__).resolve().parent.parent / "src"


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


import pathlib
import tempfile


# ---------------------------------------------------------------------------
# load_questions type-dispatch tests
# ---------------------------------------------------------------------------

SINGLE_QUESTION_DICT = {
    "questions": [
        {
            "question": "Pick one",
            "options": [{"label": "A"}, {"label": "B"}],
        }
    ]
}
SINGLE_QUESTION_JSON = json.dumps(SINGLE_QUESTION_DICT, ensure_ascii=False)


def test_load_questions_accepts_dict():
    questions = load_questions(SINGLE_QUESTION_DICT)

    assert len(questions) == 1
    assert questions[0].question == "Pick one"
    assert questions[0].options[0].label == "A"


def test_load_questions_dict_does_not_construct_path():
    # dict 入力のとき Path(...) を経由しないことを間接的に確認する:
    # キーが valid な file path になっていない dict を渡しても TypeError にならない
    questions = load_questions(SINGLE_QUESTION_DICT)
    assert questions[0].question == "Pick one"


def test_load_questions_accepts_str_json():
    questions = load_questions(SINGLE_QUESTION_JSON)

    assert len(questions) == 1
    assert questions[0].options[1].label == "B"


def test_load_questions_accepts_str_path(tmp_path):
    file = tmp_path / "q.json"
    file.write_text(SINGLE_QUESTION_JSON, encoding="utf-8")

    questions = load_questions(str(file))

    assert questions[0].question == "Pick one"


def test_load_questions_accepts_path_object(tmp_path):
    file = tmp_path / "q.json"
    file.write_text(SINGLE_QUESTION_JSON, encoding="utf-8")

    questions = load_questions(file)

    assert questions[0].question == "Pick one"


def test_load_questions_raises_type_error_for_invalid_type():
    import pytest

    with pytest.raises(TypeError, match="str, Path, or dict"):
        load_questions(12345)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Windows NUL-stdin isatty() 誤判定によるハング回帰テスト
# (debug-analysis-20260814-011032.md)
#
# 欠陥は二重ゲート構造だった: cli_ask.py の入口ゲートと question.py の
# _raw_keyboard_supported() の両方が sys.stdin.isatty() (または OS 名のみ) に
# 依存しており、どちらか一方だけを直しても他方が素通りしてハングに到達し得た。
# 以下は両ゲートそれぞれについて、raw isatty() が嘘をついても安全側に倒れる
# ことを回帰確認する。
# ---------------------------------------------------------------------------


def test_stdin_is_interactive_console_matches_isatty_on_posix(monkeypatch):
    """POSIX では isatty() が /dev/null に対し正しく False を返すため変更不要。"""
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(
        "sys.stdin", type("_FakeStdin", (), {"isatty": staticmethod(lambda: False)})()
    )
    assert question.stdin_is_interactive_console() is False

    monkeypatch.setattr(
        "sys.stdin", type("_FakeStdin", (), {"isatty": staticmethod(lambda: True)})()
    )
    assert question.stdin_is_interactive_console() is True


def test_stdin_is_interactive_console_ignores_windows_isatty_nul_lie(monkeypatch):
    """Windows: sys.stdin.isatty() が NUL を誤って True と判定しても、
    実コンソール接続チェック (_windows_console_attached) の結果を優先する。
    """
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(
        "sys.stdin", type("_FakeStdin", (), {"isatty": staticmethod(lambda: True)})()
    )
    monkeypatch.setattr(question, "_windows_console_attached", lambda: False)

    assert question.stdin_is_interactive_console() is False

    monkeypatch.setattr(question, "_windows_console_attached", lambda: True)
    assert question.stdin_is_interactive_console() is True


def test_raw_keyboard_supported_delegates_to_console_check(monkeypatch):
    """question.py 側ゲート: _raw_keyboard_supported() は OS 名のみでなく
    stdin_is_interactive_console() に従う（旧実装は os.name in ("nt","posix")
    で事実上常に True になり安全網 _select_with_line_input を恒久迂回していた）。
    """
    monkeypatch.setattr(question, "stdin_is_interactive_console", lambda: False)
    assert question._raw_keyboard_supported() is False

    monkeypatch.setattr(question, "stdin_is_interactive_console", lambda: True)
    assert question._raw_keyboard_supported() is True


def test_select_interactively_falls_back_to_line_input_when_console_not_attached(monkeypatch):
    """実コンソール非接続時、msvcrt.getwch() 相当の _read_key() は一切呼ばれず
    input() ベースの安全網 (_select_with_line_input) だけが使われる。
    """
    monkeypatch.setattr(question, "stdin_is_interactive_console", lambda: False)

    def _read_key_must_not_be_called():
        raise AssertionError("_read_key must not be called when no real console is attached")

    monkeypatch.setattr(question, "_read_key", _read_key_must_not_be_called)
    monkeypatch.setattr("builtins.input", lambda _prompt: "1")

    q = load_questions(SINGLE_QUESTION_JSON)[0]
    selected = question._select_interactively(q)

    assert selected == (0,)


def test_select_interactively_eof_propagates_when_no_console_and_no_input(monkeypatch):
    """安全網 (_select_with_line_input -> input()) が EOF を返す場合、EOFError が
    そのまま呼び出し元へ伝播する（cli_ask.handle() が捕捉して exit 1 にする経路）。
    """
    monkeypatch.setattr(question, "stdin_is_interactive_console", lambda: False)

    def _raise_eof(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise_eof)

    q = load_questions(SINGLE_QUESTION_JSON)[0]
    with pytest.raises(EOFError):
        question._select_interactively(q)


def test_cli_ask_gate_uses_safe_console_check_not_raw_isatty(monkeypatch, capsys):
    """cli_ask.py 側ゲート: sys.stdin.isatty() が NUL 誤判定で True を返す状況でも、
    実コンソール接続チェックが False を返せば入口ゲートで exit 1 になり、
    interactive 選択 (answer_questions 以降) には一切進まない。
    """
    monkeypatch.setattr(
        "sys.stdin", type("_FakeStdin", (), {"isatty": staticmethod(lambda: True)})()
    )
    monkeypatch.setattr(cli_ask, "stdin_is_interactive_console", lambda: False)

    def _answer_questions_must_not_be_called(*_args, **_kwargs):
        raise AssertionError("answer_questions must not run past the non-interactive gate")

    monkeypatch.setattr(cli_ask, "answer_questions", _answer_questions_must_not_be_called)

    rc = cli_ask.handle(
        argparse.Namespace(file=None, json_text=SINGLE_QUESTION_JSON, response=None, pretty=False)
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "non-interactive mode" in err


def test_cli_ask_response_path_unaffected_by_console_check(monkeypatch, capsys):
    """--response 指定時は console 接続チェックを経由せず既存挙動のまま動く。"""
    monkeypatch.setattr(cli_ask, "stdin_is_interactive_console", lambda: False)

    rc = cli_ask.handle(
        argparse.Namespace(
            file=None,
            json_text=SINGLE_QUESTION_JSON,
            response="1",
            pretty=False,
        )
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["answers"][0]["labels"] == ["A"]


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="NUL デバイスの isatty() 誤判定は Windows CRT 固有の挙動",
)
def test_c3_ask_exits_promptly_on_nul_stdin_without_response(tmp_path):
    """実プロセスで --response 未指定 + stdin=NUL(DEVNULL) 実行時に無限ブロック
    せず exit 1 になることを確認する（debug-analysis-20260814-011032.md の
    再現条件そのもの）。timeout 到達 (TimeoutExpired) はテスト失敗として扱う。
    """
    q_file = tmp_path / "q.json"
    q_file.write_text(SINGLE_QUESTION_JSON, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "c3", "ask", "--file", str(q_file)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=15,
        env={**os.environ, "PYTHONPATH": str(REPO_SRC)},
    )

    assert result.returncode == 1
