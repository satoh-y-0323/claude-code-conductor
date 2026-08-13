"""Tests for the C3 AskUserQuestion compatibility layer."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from c3 import _terminal, cli_ask, question
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

    def _call_with_isatty(isatty_result: bool) -> bool:
        # [CR-NEW] os.name の差し替えは必ず monkeypatch.context() に閉じる。
        # 裸の setattr だと本テストが赤化した瞬間に monkeypatch fixture の
        # teardown より先に pytest 自身の repr_failure が走り、os.name="posix"
        # のまま pathlib が PosixPath を選んで
        # NotImplementedError -> INTERNALERROR でセッションごと落ちる（実測）。
        with monkeypatch.context() as patched:
            patched.setattr(os, "name", "posix")
            patched.setattr(
                "sys.stdin",
                type("_FakeStdin", (), {"isatty": staticmethod(lambda: isatty_result)})(),
            )
            return question.stdin_is_interactive_console()

    assert _call_with_isatty(False) is False

    assert _call_with_isatty(True) is True


def test_stdin_is_interactive_console_ignores_windows_isatty_nul_lie(monkeypatch):
    """Windows: sys.stdin.isatty() が NUL を誤って True と判定しても、
    実コンソール接続チェック (_windows_console_attached) の結果を優先する。
    """
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(
        "sys.stdin", type("_FakeStdin", (), {"isatty": staticmethod(lambda: True)})()
    )
    monkeypatch.setattr(_terminal, "_windows_console_attached", lambda: False)

    assert question.stdin_is_interactive_console() is False

    monkeypatch.setattr(_terminal, "_windows_console_attached", lambda: True)
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


# ---------------------------------------------------------------------------
# E-3 裁定 [対応予定] 指摘の凍結テスト (task test-e2fix1)
#
# (1) [SR-R-001] 入口ゲートの判定関数が想定外例外を送出しても契約 (exit 1) を守る
# (2) [SR-NEW]   POSIX 分岐 _read_key() が EOF 相当の空読みで有限時間に終了する
# (3) [CR-T-001] 実コンソール接続時は従来どおり _read_key() に到達する（正の双子）
#
# 注入点はいずれも移設後も生き残る名前 (cli_ask.stdin_is_interactive_console /
# question.stdin_is_interactive_console) のみを使う。判定の実装詳細
# (_windows_console_attached) には依存しない（D-4 で _terminal.py へ移設予定）。
# ---------------------------------------------------------------------------


def test_cli_ask_gate_fails_closed_when_console_check_raises(monkeypatch, capsys):
    """[SR-R-001] 判定関数が想定外例外を送出しても traceback で異常終了せず、
    docs/cli-reference.md の契約どおり exit 1 + stderr メッセージになる。

    security-review-report-20260814-014026.md [SR-R-001]:
    cli_ask.handle() の入口ゲート呼び出しは try ブロックの外側にあるため、
    _windows_console_attached() の narrow な except で取りこぼした例外型
    (ctypes.ArgumentError / ImportError 等) がそのまま伝播し、契約を破って
    未処理例外で落ちる。ここでは呼び出し元が「判定不能なら非対話扱い」で
    fail-closed することを凍結する。
    """

    def _raise_unexpected() -> bool:
        raise RuntimeError("unexpected failure inside the console detection path")

    monkeypatch.setattr(cli_ask, "stdin_is_interactive_console", _raise_unexpected)

    def _answer_questions_must_not_be_called(*_args, **_kwargs):
        raise AssertionError("answer_questions must not run when the console check fails")

    monkeypatch.setattr(cli_ask, "answer_questions", _answer_questions_must_not_be_called)

    rc = cli_ask.handle(
        argparse.Namespace(file=None, json_text=SINGLE_QUESTION_JSON, response=None, pretty=False)
    )

    assert rc == 1
    assert "non-interactive mode" in capsys.readouterr().err


def test_select_interactively_posix_read_key_raises_eof_on_empty_read(monkeypatch):
    """[SR-NEW] POSIX 分岐で stdin の read が空文字列 (EOF 相当) を返した場合、
    _read_key() は EOFError を送出し _select_interactively() のループが
    有限時間で終了する（CPU ビジーループにならない）。

    実装場所は _read_key() だが、ビジーループという性質はループを持つ
    _select_interactively() 側でしか測れないためこちらを入口にする。
    Windows 開発機でも実行できるよう termios / tty を疑似モジュールで注入し、
    os.name を "posix" に差し替える（skip しない）。
    stdin_is_interactive_console() には True を注入する。これは
    「判定が誤って True を返した」という SR-NEW の前提状況そのものであり、
    これが無いと入口ゲートで _select_with_line_input() へ迂回し _read_key() に
    到達しない。
    """
    calls = {"read": 0}

    class _EofStdin:
        @staticmethod
        def fileno() -> int:
            return 0

        @staticmethod
        def isatty() -> bool:
            return False

        @staticmethod
        def read(_size: int = -1) -> str:
            calls["read"] += 1
            if calls["read"] == 1:
                return ""
            # 2 回目に到達＝空読みでループに戻った＝ビジーループ。
            # ハングを避けるため即座に fail-fast する。
            raise AssertionError(
                "_read_key() looped on an EOF stdin: read() was called "
                f"{calls['read']} times (expected EOFError after the 1st empty read)"
            )

    fake_termios = types.ModuleType("termios")
    fake_termios.TCSADRAIN = 1
    fake_termios.tcgetattr = lambda _fd: ["saved-termios-state"]
    fake_termios.tcsetattr = lambda _fd, _when, _attrs: None
    fake_tty = types.ModuleType("tty")
    fake_tty.setraw = lambda _fd: None

    q = load_questions(SINGLE_QUESTION_JSON)[0]

    def _drive() -> tuple[int, ...]:
        # os.name の差し替えは monkeypatch.context() で必ずこのブロック内に閉じる。
        # fixture の teardown は「call フェーズのレポート生成」より後に走るため、
        # os.name="posix" のまま assert が落ちると pytest 自身の repr_failure が
        # pathlib.Path() -> PosixPath を選んで NotImplementedError となり
        # INTERNALERROR でセッションごと落ちる（実測）。
        with monkeypatch.context() as patched:
            patched.setitem(sys.modules, "termios", fake_termios)
            patched.setitem(sys.modules, "tty", fake_tty)
            patched.setattr(os, "name", "posix")
            patched.setattr("sys.stdin", _EofStdin())
            patched.setattr(question, "stdin_is_interactive_console", lambda: True)
            return question._select_interactively(q)

    with pytest.raises(EOFError):
        _drive()

    assert calls["read"] == 1


def test_select_interactively_uses_read_key_when_console_attached(monkeypatch):
    """[CR-T-001] 正の双子テスト: 実コンソール接続時 (判定 True) は従来どおり
    _read_key() に到達し、安全網 _select_with_line_input() へ迂回しない。

    code-review-report-20260814-014025.md [CR-T-001]:
    既存の回帰テスト 9 件は「非接続 → フォールバックする」負の分岐のみを
    見ており、「接続時は対話パスが保たれる」正の分岐を担保するテストが無い。
    本テストは既存挙動の凍結が目的であり、最初から緑になるのが正しい。
    """
    calls: list[str] = []

    def _record_read_key() -> str:
        calls.append("read_key")
        return "enter"

    def _line_input_must_not_be_called(_question):
        raise AssertionError(
            "_select_with_line_input must not be used when a real console is attached"
        )

    monkeypatch.setattr(question, "stdin_is_interactive_console", lambda: True)
    monkeypatch.setattr(question, "_read_key", _record_read_key)
    monkeypatch.setattr(question, "_select_with_line_input", _line_input_must_not_be_called)

    q = load_questions(SINGLE_QUESTION_JSON)[0]
    selected = question._select_interactively(q)

    assert calls == ["read_key"]
    assert selected == (0,)


# ---------------------------------------------------------------------------
# E 周回 2 裁定 [対応予定] 指摘の凍結テスト (task test-e3fix1)
#
# [SR-R-001] / [CR-E-001]
#   確定裁定: _terminal.stdin_is_interactive_console() は「関数内部で」いかなる
#   例外が起きても送出せず False を返す（関数全体の fail-closed 化）。第 1 ゲート
#   (cli_ask.handle 入口) は既に try/except Exception で包まれているが、第 2 ゲート
#   (question.answer_questions() / _raw_keyboard_supported() 経由) は無防備なため、
#   POSIX 分岐 `sys.stdin.isatty()` が AttributeError 等を送出すると exit 1 契約を
#   破って traceback 終了する。
#
# 注入点について（重要）:
#   判定関数そのものをスタブへ差し替える注入（既存の
#   test_cli_ask_gate_fails_closed_when_console_check_raises 方式）では、関数内部の
#   fail-closed 化を是正しても赤のままになり性質を測れない。ここでは「関数内部の
#   依存」（os.name / sys.stdin）にだけ失敗を注入し、実装本体を実際に走らせる。
#
# os.name の差し替えはすべて monkeypatch.context() に閉じる（[CR-NEW] 参照。
# 赤化時の pytest INTERNALERROR 防止）。実 msvcrt / 実コンソールには触れない。
# ---------------------------------------------------------------------------


TWO_QUESTION_JSON = json.dumps(
    {
        "questions": [
            {"question": "First", "options": [{"label": "A"}, {"label": "B"}]},
            {"question": "Second", "options": [{"label": "C"}, {"label": "D"}]},
        ]
    },
    ensure_ascii=False,
)


class _StdinWithoutIsatty:
    """isatty 属性を持たない stdin（pythonw.exe / --windowed ビルド相当）。"""


class _StdinIsattyRaisesRuntimeError:
    """isatty() が OSError / ValueError / AttributeError のいずれでもない型を送出する。"""

    @staticmethod
    def isatty() -> bool:
        raise RuntimeError("unexpected failure inside isatty()")


class _StdinIsattyRaisesKeyboardInterrupt:
    @staticmethod
    def isatty() -> bool:
        raise KeyboardInterrupt


def _console_check_with_broken_stdin(monkeypatch, fake_stdin):
    """POSIX 分岐を通しつつ内部依存 (sys.stdin) を壊した状態で判定関数を呼ぶ。

    os.name / sys.stdin の差し替えは monkeypatch.context() に閉じ、assert /
    pytest.raises は呼び出し側（この関数の外）に置く。
    """
    with monkeypatch.context() as patched:
        patched.setattr(os, "name", "posix")
        patched.setattr(sys, "stdin", fake_stdin)
        return _terminal.stdin_is_interactive_console()


@pytest.mark.parametrize(
    "fake_stdin",
    [None, _StdinWithoutIsatty(), _StdinIsattyRaisesRuntimeError()],
    ids=["stdin-is-none", "stdin-without-isatty", "isatty-raises-runtimeerror"],
)
def test_stdin_is_interactive_console_fails_closed_on_internal_failure(monkeypatch, fake_stdin):
    """[SR-R-001][CR-E-001] 関数内部でどんな例外が起きても送出せず False を返す。

    是正前は POSIX 分岐 (`return sys.stdin.isatty()`) に例外捕捉が一切ないため、
    AttributeError / RuntimeError がそのまま送出されて赤になる。
    凍結する性質は例外の「型に依存しない」fail-closed であるため、
    AttributeError 相当 2 種と、OSError / ValueError / AttributeError の
    いずれでもない型 (RuntimeError) の計 3 ケースで同じ結論を実測する。
    """
    assert _console_check_with_broken_stdin(monkeypatch, fake_stdin) is False


def test_stdin_is_interactive_console_propagates_keyboard_interrupt(monkeypatch):
    """KeyboardInterrupt は fail-closed の対象外＝握り潰さず伝播する。

    fail-closed 化は `except Exception` で行う想定であり、BaseException 直属の
    KeyboardInterrupt は捕捉されない。Ctrl-C が「非対話扱い (False)」へ丸め
    込まれると、ユーザーの中断がデフォルト選択の確定に化けてしまう。
    是正前も是正後も緑（追加時点で緑の回帰網）。
    """

    def _drive() -> bool:
        return _console_check_with_broken_stdin(
            monkeypatch, _StdinIsattyRaisesKeyboardInterrupt()
        )

    with pytest.raises(KeyboardInterrupt):
        _drive()


def test_cli_ask_second_gate_fails_closed_when_stdin_is_broken(monkeypatch, capsys):
    """[SR-R-001][CR-E-001] 第 2 ゲート経由でも exit code 契約が守られる。

    --response の応答数が質問数に満たない場合、余った質問は answer_questions()
    内の第 2 ゲート (stdin_is_interactive_console()) を通る。この呼び出しは
    cli_ask.handle() の try が捕捉する (OSError, ValueError) / EOFError /
    KeyboardInterrupt のいずれにも該当しない例外型では素通りするため、是正前は
    AttributeError が伝播して traceback 終了する（int を返さない＝赤）。

    exit code 契約を測れるのは handle() 経由のみのため、入口をここに置く。
    是正後は判定が False へ倒れ _select_default() が使われ、rc=0 で完走する。
    """

    def _drive() -> int:
        with monkeypatch.context() as patched:
            patched.setattr(os, "name", "posix")
            patched.setattr(sys, "stdin", None)
            return cli_ask.handle(
                argparse.Namespace(
                    file=None,
                    json_text=TWO_QUESTION_JSON,
                    response="1",
                    pretty=False,
                )
            )

    rc = _drive()

    assert isinstance(rc, int)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    # 1 問目は --response の "1"、2 問目は第 2 ゲートが False へ倒れた結果の
    # _select_default()（required なので先頭）。
    assert [answer["labels"] for answer in payload["answers"]] == [["A"], ["C"]]
