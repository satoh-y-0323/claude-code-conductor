"""C3 AskUserQuestion-compatible question handling.

Claude Code has a native ``AskUserQuestion`` tool. Other hosts use this module
as the shared schema and fallback implementation.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Option:
    label: str
    description: str = ""


@dataclass(frozen=True)
class Question:
    question: str
    options: tuple[Option, ...]
    header: str = "C3"
    multi_select: bool = False
    required: bool = True


def load_questions(source: str | Path | dict[str, Any]) -> list[Question]:
    """Load one or more questions from a path, JSON string, or object."""
    if isinstance(source, dict):
        payload = source
    elif isinstance(source, Path):
        payload = json.loads(source.read_text(encoding="utf-8") if source.is_file() else str(source))
    elif isinstance(source, str):
        path = Path(source)
        payload = json.loads(path.read_text(encoding="utf-8") if path.is_file() else source)
    else:
        raise TypeError(f"source must be str, Path, or dict, got {type(source).__name__}")

    raw_questions = payload.get("questions") if isinstance(payload, dict) else None
    if raw_questions is None:
        raw_questions = [payload]
    if not isinstance(raw_questions, list):
        raise ValueError("questions must be a list")
    return [_parse_question(item) for item in raw_questions]


def answer_questions(
    questions: list[Question],
    *,
    response: str | None = None,
    interactive: bool | None = None,
) -> dict[str, Any]:
    """Return structured answers for the supplied questions."""
    answers = []
    responses = _split_response(response)
    for index, question in enumerate(questions):
        current_response = responses[index] if index < len(responses) else None
        if current_response is not None:
            selected = _select_from_response(question, current_response)
        else:
            use_interactive = sys.stdin.isatty() if interactive is None else interactive
            selected = _select_interactively(question) if use_interactive else _select_default(question)
        answers.append(_answer_payload(question, selected))
    return {"answers": answers}


def mcp_requested_schema(question: Question) -> dict[str, Any]:
    """Build an MCP elicitation form schema for a C3 question."""
    values = [option.label for option in question.options]
    titles = [
        f"{option.label} - {option.description}" if option.description else option.label
        for option in question.options
    ]
    properties: dict[str, Any]
    if question.multi_select:
        properties = {
            _multi_select_key(index): {
                "type": "boolean",
                "title": option.label,
                "description": option.description or question.question,
                "default": False,
            }
            for index, option in enumerate(question.options)
        }
    else:
        choice_schema: dict[str, Any] = {
            "type": "string",
            "title": question.header,
            "description": question.question,
            "enum": values,
            "enumNames": titles,
        }
        properties = {"choice": choice_schema}
    if question.multi_select and question_accepts_free_text(question):
        properties["details"] = {
            "type": "string",
            "title": "自由入力 / 補足",
            "description": (
                "「その他・自由入力」「具体的に入力してください」などを選んだ場合は、"
                "ここに内容を入力してください。"
            ),
        }
    required = ["choice"] if question.required and not question.multi_select else []
    return {"type": "object", "properties": properties, "required": required}


def mcp_free_text_schema(question: Question) -> dict[str, Any]:
    """Build a follow-up MCP schema that only asks for free text."""
    return {
        "type": "object",
        "properties": {
            "details": {
                "type": "string",
                "title": "自由入力",
                "description": f"{question.question} の補足内容を入力してください。",
            }
        },
        "required": ["details"],
    }


def normalize_mcp_answer(question: Question, content: dict[str, Any] | None) -> dict[str, Any]:
    """Convert MCP elicitation content into the C3 answer payload shape."""
    if not content or "choice" not in content:
        return _answer_payload(question, ())
    choice = content["choice"]
    if isinstance(choice, list):
        labels = [str(item) for item in choice]
    else:
        labels = [str(choice)]
    selected = tuple(
        idx
        for idx, option in enumerate(question.options)
        if option.label in labels
    )
    return _answer_payload(question, selected, free_text=_free_text_from_content(content))


def normalize_mcp_multi_select_answer(
    question: Question,
    content: dict[str, Any] | None,
) -> dict[str, Any]:
    """Normalize boolean-field multi-select content into the C3 answer shape."""
    if not content:
        return _answer_payload(question, ())
    selected = tuple(
        index
        for index, _option in enumerate(question.options)
        if content.get(_multi_select_key(index)) is True
    )
    return _answer_payload(question, selected, free_text=_free_text_from_content(content))


def question_accepts_free_text(question: Question) -> bool:
    """Return True when any option implies free-form input or follow-up detail."""
    return any(_option_accepts_free_text(option) for option in question.options)


def selected_free_text_requires_detail(question: Question, content: dict[str, Any] | None) -> bool:
    """Return True when a free-text option was selected but no text was supplied."""
    if not content or _free_text_from_content(content):
        return False
    if question.multi_select:
        return any(
            content.get(_multi_select_key(index)) is True
            and _option_accepts_free_text(option)
            for index, option in enumerate(question.options)
        )
    choice = content.get("choice")
    labels = [str(item) for item in choice] if isinstance(choice, list) else [str(choice)]
    return any(
        option.label in labels and _option_accepts_free_text(option)
        for option in question.options
    )


def no_multi_select_choice(question: Question, content: dict[str, Any] | None) -> bool:
    """Return True if a required multi-select question has no checked option."""
    return (
        question.multi_select
        and question.required
        and not any(
            content and content.get(_multi_select_key(index)) is True
            for index, _option in enumerate(question.options)
        )
    )


def _parse_question(raw: Any) -> Question:
    if not isinstance(raw, dict):
        raise ValueError("each question must be an object")
    raw_options = raw.get("options")
    if not isinstance(raw_options, list) or not raw_options:
        raise ValueError("question options must be a non-empty list")
    options = []
    for option in raw_options:
        if not isinstance(option, dict) or not option.get("label"):
            raise ValueError("each option must have a label")
        options.append(
            Option(
                label=str(option["label"]),
                description=str(option.get("description", "")),
            )
        )
    return Question(
        question=str(raw.get("question", "")),
        options=tuple(options),
        header=str(raw.get("header", "C3")),
        multi_select=bool(raw.get("multiSelect", raw.get("multi_select", False))),
        required=bool(raw.get("required", True)),
    )


def _split_response(response: str | None) -> list[str]:
    if response is None:
        return []
    return [part.strip() for part in response.split(";")]


def _select_from_response(question: Question, response: str) -> tuple[int, ...]:
    parts = [part.strip() for part in response.split(",") if part.strip()]
    if not parts:
        if question.required:
            raise ValueError("empty response for required question")
        return ()
    selected: list[int] = []
    labels = {option.label: index for index, option in enumerate(question.options)}
    for part in parts:
        if part.isdigit():
            index = int(part) - 1
            if index < 0 or index >= len(question.options):
                raise ValueError(f"option index out of range: {part}")
            selected.append(index)
        elif part in labels:
            selected.append(labels[part])
        else:
            raise ValueError(f"unknown option: {part}")
    if not question.multi_select and len(selected) > 1:
        raise ValueError("single-select question received multiple answers")
    return tuple(dict.fromkeys(selected))


def _select_default(question: Question) -> tuple[int, ...]:
    if question.required:
        return (0,)
    return ()


def _answer_payload(
    question: Question,
    selected: tuple[int, ...],
    *,
    free_text: str = "",
) -> dict[str, Any]:
    selected_options = [question.options[index] for index in selected]
    selected_payload = []
    for option in selected_options:
        item = {"label": option.label, "description": option.description}
        if free_text and _option_accepts_free_text(option):
            item["text"] = free_text
            item["value"] = f"{option.label}: {free_text}"
        selected_payload.append(item)
    payload: dict[str, Any] = {
        "question": question.question,
        "multiSelect": question.multi_select,
        "indices": [index + 1 for index in selected],
        "labels": [option.label for option in selected_options],
        "selected": selected_payload,
    }
    if free_text:
        payload["freeText"] = free_text
        payload["values"] = [
            item.get("value", item["label"])
            for item in selected_payload
        ]
    return payload


def _free_text_from_content(content: dict[str, Any]) -> str:
    for key in ("details", "freeText", "free_text", "text", "other"):
        value = content.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _option_accepts_free_text(option: Option) -> bool:
    haystack = f"{option.label}\n{option.description}".lower()
    markers = (
        "自由入力",
        "自由に入力",
        "具体的に入力",
        "詳しく教えて",
        "その他",
        "other",
        "free input",
        "custom",
    )
    return any(marker.lower() in haystack for marker in markers)


def _multi_select_key(index: int) -> str:
    return f"choice_{index + 1}"


def _select_interactively(question: Question) -> tuple[int, ...]:
    if not _raw_keyboard_supported():
        return _select_with_line_input(question)
    cursor = 0
    selected: set[int] = set()
    if not question.multi_select:
        selected.add(0)
    while True:
        _render_question(question, cursor, selected)
        key = _read_key()
        if key in {"up", "k"}:
            cursor = (cursor - 1) % len(question.options)
        elif key in {"down", "j"}:
            cursor = (cursor + 1) % len(question.options)
        elif key == "space" and question.multi_select:
            if cursor in selected:
                selected.remove(cursor)
            else:
                selected.add(cursor)
        elif key == "enter":
            if question.multi_select:
                if selected or not question.required:
                    _clear_screen()
                    return tuple(sorted(selected))
            else:
                _clear_screen()
                return (cursor,)
        elif key and key.isdigit():
            index = int(key) - 1
            if 0 <= index < len(question.options):
                if question.multi_select:
                    if index in selected:
                        selected.remove(index)
                    else:
                        selected.add(index)
                else:
                    _clear_screen()
                    return (index,)
        elif key in {"escape", "q"} and not question.required:
            _clear_screen()
            return ()


def _select_with_line_input(question: Question) -> tuple[int, ...]:
    print(question.question, file=sys.stderr)
    for index, option in enumerate(question.options, start=1):
        detail = f" - {option.description}" if option.description else ""
        print(f"  {index}. {option.label}{detail}", file=sys.stderr)
    suffix = "comma-separated numbers" if question.multi_select else "number"
    return _select_from_response(question, input(f"Select {suffix}: "))


def _render_question(question: Question, cursor: int, selected: set[int]) -> None:
    _clear_screen()
    print(f"{question.header}\n")
    print(question.question)
    hint = "Space: toggle, Enter: decide" if question.multi_select else "Enter: decide"
    print(f"{hint}\n")
    for index, option in enumerate(question.options):
        marker = ">" if index == cursor else " "
        check = "[x]" if index in selected else "[ ]"
        if not question.multi_select:
            check = "(*) " if index == cursor else "( ) "
        detail = f" - {option.description}" if option.description else ""
        print(f"{marker} {check} {index + 1}. {option.label}{detail}")


def _clear_screen() -> None:
    # パイプ・リダイレクト時に ANSI 制御シーケンスを書き出さない
    if not sys.stdout.isatty():
        return
    print("\033[2J\033[H", end="")


def _raw_keyboard_supported() -> bool:
    return os.name in ("nt", "posix")


def _read_key() -> str:
    if os.name == "nt":
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            ch2 = msvcrt.getwch()
            return {"H": "up", "P": "down", "K": "left", "M": "right"}.get(ch2, "")
        if ch in ("\r", "\n"):
            return "enter"
        if ch == " ":
            return "space"
        if ch == "\x1b":
            return "escape"
        return ch.lower()

    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            nxt = sys.stdin.read(2)
            return {"[A": "up", "[B": "down", "[C": "right", "[D": "left"}.get(nxt, "escape")
        if ch in ("\r", "\n"):
            return "enter"
        if ch == " ":
            return "space"
        return ch.lower()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
