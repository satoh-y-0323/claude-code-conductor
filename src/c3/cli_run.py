"""``c3 run`` - launch distributed Python scripts through the c3 entry point.

Introduced in v2.51.0 as the cross-OS replacement for the bare ``python``
launcher used by ``.claude/`` hooks and skills scripts (architecture-report
20260718-114347 §2-1, ADR-1..3). ``c3`` is the one launcher pip guarantees on
PATH for every OS, so invoking a script via ``c3 run`` is already inside the
correct interpreter — no interpreter discovery, no machine-specific absolute
paths leaking into git-shared ``settings.json``.

Three python-parity forms are supported (all remaining tokens are forwarded
verbatim to the launched script)::

    c3 run <script.py> [args...]   # runpy.run_path
    c3 run -m <module> [args...]   # runpy.run_module(..., alter_sys=True)
    c3 run -c <code> [args...]     # exec in an isolated __main__ namespace

Execution semantics mirror ``python`` (architecture §2-1):

- ``sys.argv`` is set to ``[target, *args]`` (``["-c", *args]`` for ``-c``)
  before execution; stdin/stdout/stderr are inherited untouched (same process).
- A ``SystemExit`` raised by the script is transparent: its ``code`` is returned
  as-is (``None`` -> 0), preserving the Claude Code hook exit 0/2 vocabulary.
- An uncaught exception prints its traceback to stderr and returns exit 1 —
  never exit 2, so a crash cannot be mistaken for a hook "block" (ADR-2).
- ``c3 run``'s *own* argument errors (a missing target, ``-m``/``-c`` without a
  value) are normalized to exit 1 + stderr, never argparse's default exit 2
  (DC-AS-005). Exit 2 is reserved for a script that explicitly requests it.
"""

from __future__ import annotations

import argparse
import os
import runpy
import sys
import traceback
from typing import Callable, Sequence

# ``prefix_chars`` is set to a control character that never appears on a command
# line so the run sub-parser treats ``-m`` / ``-c`` / ``--flag`` as ordinary
# positionals rather than options in the ``c3 --help`` / ``c3 run -h`` listing.
# NOTE: this sub-parser is deliberately *off* the execution path. ``cli.main``
# special-cases ``run`` with a dedicated branch that bypasses argparse entirely
# and calls ``handle`` directly, so verbatim-forwarded tokens (notably ``--``)
# survive untouched. ``register`` therefore only shapes the help output; the real
# token handling lives in ``handle``, driven by ``_raw_argv`` from ``cli.main``.
_NEVER_A_PREFIX = "\x00"


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "run",
        help="Run a Python script/module/code through the c3 launcher (python parity)",
        prefix_chars=_NEVER_A_PREFIX,
        add_help=False,
    )
    parser.add_argument("run_args", nargs="*", metavar="<target> [args...]")
    parser.set_defaults(handler=handle)


def handle(args: argparse.Namespace) -> int:
    # ``cli.main`` is the *sole* caller and always sets ``_raw_argv`` to the full
    # argv list (``["run", *forwarded_tokens]``) before dispatching here — see the
    # dedicated "run" branch in ``cli.main`` that bypasses argparse so tokens like
    # "--" survive untouched. We read ``_raw_argv`` directly and forward every
    # token after the leading "run" verbatim.
    #
    # There is deliberately no ``sys.argv`` fallback: reaching for the
    # process-global ``sys.argv`` here would silently couple ``handle`` to it if
    # this function were ever wired to another entry point, masking a missing
    # ``_raw_argv`` instead of failing loudly (security review 20260718-143906).
    raw_argv = args._raw_argv
    run_args = list(raw_argv[1:])

    if not run_args:
        # DC-AS-005: parse failure *before* any script runs -> exit 1, not exit 2.
        print(
            "c3 run: expected a script path, '-m <module>', or '-c <code>'",
            file=sys.stderr,
        )
        return 1

    first = run_args[0]
    if first == "-m":
        module = run_args[1] if len(run_args) >= 2 else None
        if not module:
            print("c3 run: -m requires a module name", file=sys.stderr)
            return 1
        # Forward all remaining tokens (including "--") verbatim.
        return _run_module(module, run_args[2:])
    if first == "-c":
        if len(run_args) < 2:
            print("c3 run: -c requires a code string", file=sys.stderr)
            return 1
        # Forward all remaining tokens (including "--") verbatim.
        return _run_code(run_args[1], run_args[2:])
    # Forward all remaining tokens (including "--") verbatim.
    return _run_path(first, run_args[1:])


def _run_path(script: str, script_args: Sequence[str]) -> int:
    # ``python script.py`` puts the script's own directory at the front of
    # sys.path so sibling imports resolve (DC-AS-006). runpy.run_path does NOT do
    # this for a plain file, so we insert it ourselves; _execute restores sys.path.
    script_dir = os.path.dirname(os.path.abspath(script))

    def _thunk() -> None:
        sys.path.insert(0, script_dir)
        runpy.run_path(script, run_name="__main__")

    return _execute([script, *script_args], _thunk)


def _run_module(module: str, module_args: Sequence[str]) -> int:
    # alter_sys=True makes runpy set sys.argv[0] to the module's real file, matching
    # ``python -m``; we supply sys.argv[1:] ourselves. Insert "" at the front of
    # sys.path so the current directory is importable, matching ``python -m`` behavior.
    def _thunk() -> None:
        sys.path.insert(0, "")
        runpy.run_module(module, run_name="__main__", alter_sys=True)

    return _execute([module, *module_args], _thunk)


def _run_code(code: str, code_args: Sequence[str]) -> int:
    # DC-AM-001: ``python -c`` parity — execute in a fresh dict seeded only with
    # ``__name__`` so the script never sees c3's module globals (argparse/runpy/...).
    # Insert "" at the front of sys.path so the current directory is importable,
    # matching ``python -c`` behavior.
    def _thunk() -> None:
        sys.path.insert(0, "")
        namespace = {"__name__": "__main__"}
        exec(compile(code, "<string>", "exec"), namespace)

    return _execute(["-c", *code_args], _thunk)


def _execute(argv: Sequence[str], thunk: Callable[[], object]) -> int:
    """Run ``thunk`` with ``sys.argv`` set to ``argv``, mapping the outcome to an
    exit code with python semantics. ``sys.argv`` and ``sys.path`` are always
    restored so repeated in-process invocations (and the c3 CLI caller) are
    unaffected by the script's argv/path mutations. Note: ``sys.modules`` is NOT
    restored; this function is designed for single execution per process, not
    repeated in-process imports and module reloads.
    """
    saved_argv = sys.argv
    saved_path = sys.path[:]
    sys.argv = list(argv)
    try:
        thunk()
        return 0
    except SystemExit as exc:
        return _exit_code(exc.code)
    except Exception:  # noqa: BLE001 - mirror python: crash -> traceback + exit 1
        traceback.print_exc()
        return 1
    finally:
        sys.argv = saved_argv
        sys.path[:] = saved_path


def _exit_code(code: object) -> int:
    """Map a ``SystemExit.code`` to a process exit code the way python does."""
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    # A non-int, non-None code (e.g. ``sys.exit("message")``): python prints it to
    # stderr and exits 1.
    print(code, file=sys.stderr)
    return 1
