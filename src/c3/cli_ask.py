"""``c3 ask`` - AskUserQuestion-compatible picker for non-Claude hosts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from c3.question import answer_questions, load_questions


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "ask",
        help="Ask a C3 structured question and print the selected answer as JSON",
        description=(
            "Render the C3 AskUserQuestion-compatible schema outside Claude Code. "
            "Use --file for JSON blocks copied from C3 skills. In non-interactive "
            "contexts, pass --response with option labels or 1-based indices."
        ),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--file",
        type=Path,
        help="Path to a JSON file containing {\"questions\": [...]} or one question",
    )
    source.add_argument(
        "--json",
        dest="json_text",
        help="Inline JSON containing {\"questions\": [...]} or one question",
    )
    parser.add_argument(
        "--response",
        help=(
            "Non-interactive answer. Use labels or 1-based indices. "
            "For multiple questions, separate answers with semicolons."
        ),
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print the output JSON",
    )
    parser.set_defaults(handler=handle)


def handle(args: argparse.Namespace) -> int:
    if args.response is None and not sys.stdin.isatty():
        print("c3 ask: --response is required in non-interactive mode", file=sys.stderr)
        return 1

    try:
        if args.file is not None:
            source: Path | dict = args.file
        else:
            source = json.loads(args.json_text)
        questions = load_questions(source)
        result = answer_questions(questions, response=args.response)
    except (OSError, ValueError) as exc:
        print(f"c3 ask: {exc}", file=sys.stderr)
        return 1
    except EOFError:
        print("\nc3 ask: input aborted", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        return 130

    indent = 2 if args.pretty else None
    print(json.dumps(result, ensure_ascii=False, indent=indent))
    return 0
