"""Tests for ``c3 run`` (Red phase — src/c3/cli_run.py does not exist yet).

Design source: architecture-report-20260718-114347.md §2-1 (無印 python 起動子の
全面是正 — c3 run 呼び出し口＋ doctor 検知).

``c3 run`` replaces plain ``python`` as the launcher for hooks/skills scripts
distributed inside ``.claude/``. It must support three python-parity forms
(``c3 run <script.py> [args]`` / ``c3 run -m <module> [args]`` /
``c3 run -c <code> [args]``), transparently pass through ``SystemExit`` codes,
normalize both script crashes and ``c3 run``'s own argument-parsing errors to
exit 1 (never exit 2, which is the Claude Code hook "block" vocabulary), and
resolve sibling imports from the script's own directory ahead of any c3
submodule of the same name.

None of this exists yet (no ``src/c3/cli_run.py``, no registration in
``src/c3/cli.py``). Every test below is expected to fail for that reason —
either at collection time (``run`` not registered → argparse ``invalid
choice``) or via explicit assertions once cli_run.py is stubbed in but
incomplete. This is intentional Red-phase failure, not a broken test.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from c3 import cli, cli_run

REPO_SRC = Path(__file__).resolve().parent.parent / "src"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _find_subparsers_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError("parser has no subparsers action")


def _write_script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# (9) c3 run はサブコマンド一覧に登録されている
# ---------------------------------------------------------------------------


def test_run_registered_as_subcommand() -> None:
    parser = cli.build_parser()
    action = _find_subparsers_action(parser)
    assert "run" in action.choices


# ---------------------------------------------------------------------------
# (1) c3 run <script.py> [args] : sys.argv 設定・runpy 実行・exit code 透過
# ---------------------------------------------------------------------------


def test_run_script_sets_argv_and_returns_exit_code(tmp_path: Path) -> None:
    out_file = tmp_path / "argv.txt"
    script = _write_script(
        tmp_path,
        "record_argv.py",
        f"""\
        import sys
        from pathlib import Path
        Path({str(out_file)!r}).write_text(repr(sys.argv), encoding="utf-8")
        sys.exit(3)
        """,
    )
    rc = cli.main(["run", str(script), "foo", "bar"])
    assert rc == 3
    recorded = out_file.read_text(encoding="utf-8")
    assert recorded == repr([str(script), "foo", "bar"])


# ---------------------------------------------------------------------------
# (2) c3 run -m <module> [args] : py_compile を実材料にする
# ---------------------------------------------------------------------------


def test_run_dash_m_py_compile_valid_file_exits_zero(tmp_path: Path) -> None:
    target = _write_script(tmp_path, "ok.py", "x = 1\n")
    rc = cli.main(["run", "-m", "py_compile", str(target)])
    assert rc == 0
    pycache = tmp_path / "__pycache__"
    assert pycache.is_dir() and any(pycache.iterdir())


def test_run_dash_m_py_compile_syntax_error_exits_one_not_two(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    target = _write_script(tmp_path, "bad.py", "def f(:\n")
    rc = cli.main(["run", "-m", "py_compile", str(target)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "SyntaxError" in err


# ---------------------------------------------------------------------------
# (3) c3 run -c <code> : インライン実行
# ---------------------------------------------------------------------------


def test_run_dash_c_executes_inline_code(capsys: pytest.CaptureFixture) -> None:
    rc = cli.main(["run", "-c", "print('hello-from-dash-c')"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "hello-from-dash-c" in out


# ---------------------------------------------------------------------------
# (4) SystemExit の code 透過: 0 / 2 / None -> 0
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exit_call,expected_rc",
    [
        ("sys.exit(0)", 0),
        ("sys.exit(2)", 2),
        ("sys.exit()", 0),
        ("pass", 0),  # no sys.exit at all -> falls off the end -> exit 0
    ],
)
def test_run_script_system_exit_code_passthrough(
    tmp_path: Path, exit_call: str, expected_rc: int
) -> None:
    script = _write_script(
        tmp_path,
        "exit_variant.py",
        f"""\
        import sys
        {exit_call}
        """,
    )
    rc = cli.main(["run", str(script)])
    assert rc == expected_rc


# ---------------------------------------------------------------------------
# (5) 未捕捉例外 -> exit 1・traceback は stderr・exit 2 に合流しない
# ---------------------------------------------------------------------------


def test_run_script_uncaught_exception_exits_one_with_traceback_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    script = _write_script(
        tmp_path,
        "crash.py",
        """\
        raise RuntimeError("boom-uncaught")
        """,
    )
    rc = cli.main(["run", str(script)])
    assert rc == 1
    assert rc != 2
    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert "boom-uncaught" in err


# ---------------------------------------------------------------------------
# (6) sibling import が sys.path 先頭で通る（c3 サブモジュール名衝突ケース含む）
# ---------------------------------------------------------------------------


def test_run_script_can_import_sibling_module(tmp_path: Path) -> None:
    _write_script(tmp_path, "helper.py", "VALUE = 42\n")
    out_file = tmp_path / "sibling_result.txt"
    script = _write_script(
        tmp_path,
        "uses_helper.py",
        f"""\
        import sys
        from pathlib import Path
        import helper
        Path({str(out_file)!r}).write_text(str(helper.VALUE), encoding="utf-8")
        sys.exit(0 if helper.VALUE == 42 else 1)
        """,
    )
    rc = cli.main(["run", str(script)])
    assert rc == 0
    assert out_file.read_text(encoding="utf-8") == "42"


def test_run_script_sibling_import_wins_over_c3_submodule_name_collision(
    tmp_path: Path,
) -> None:
    """DC-AS-006: a sibling ``db.py`` next to the script must resolve ahead of
    (i.e. be unaffected by) the unrelated ``c3.db`` submodule, because ``c3``
    modules live under the ``c3.*`` namespace and never occupy the bare
    top-level name ``db`` — but this must be proven, not assumed, since
    ``c3 run`` (unlike plain ``python``) already has the ``c3`` package
    loaded in ``sys.modules`` when the script executes.
    """
    sys.modules.pop("db", None)
    try:
        _write_script(tmp_path, "db.py", 'SENTINEL = "sibling-db"\n')
        script = _write_script(
            tmp_path,
            "uses_db.py",
            """\
            import sys
            import db
            sys.exit(0 if getattr(db, "SENTINEL", None) == "sibling-db" else 9)
            """,
        )
        rc = cli.main(["run", str(script)])
        assert rc == 0
    finally:
        sys.modules.pop("db", None)


# ---------------------------------------------------------------------------
# (7) stdin パイプの透過
# ---------------------------------------------------------------------------


def test_run_script_stdin_is_passed_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO("piped-stdin-payload\n"))
    out_file = tmp_path / "stdin_result.txt"
    script = _write_script(
        tmp_path,
        "reads_stdin.py",
        f"""\
        import sys
        from pathlib import Path
        data = sys.stdin.read()
        Path({str(out_file)!r}).write_text(data, encoding="utf-8")
        """,
    )
    rc = cli.main(["run", str(script)])
    assert rc == 0
    assert out_file.read_text(encoding="utf-8") == "piped-stdin-payload\n"


# ---------------------------------------------------------------------------
# (8) PATH に無印 python が無い環境でも c3 エントリ経由で script 実行が成功する
# ---------------------------------------------------------------------------


def test_run_via_c3_entrypoint_without_python_on_path(tmp_path: Path) -> None:
    """Simulates the macOS/Linux failure mode this feature exists to fix: a
    PATH that resolves ``c3`` but not ``python``/``python3``. Invokes the
    real installed ``c3`` console-script entry point as a subprocess (not
    in-process), so this exercises the actual ``[project.scripts] c3 =
    "c3.cli:main"`` launcher — not just the importable function.

    ``PYTHONPATH`` is pinned to this worktree's ``src/`` so the subprocess
    imports the code under test here rather than whatever copy the
    editable install happened to be registered against.
    """
    c3_exe = shutil.which("c3")
    assert c3_exe is not None, "c3 console-script entry point must be on PATH for this test"

    # Build a PATH that resolves ``c3`` but NOT ``python``/``python3``. On POSIX
    # the console script and the python interpreter live side-by-side in the same
    # ``bin/`` directory, so we cannot simply put ``c3``'s own directory on PATH
    # (that would drag python in too, which broke this test on Linux CI). Instead
    # we place ONLY the c3 launcher into an isolated tmp bin directory and point
    # PATH there. The launcher still works without python on PATH because its
    # shebang / embedded interpreter path is absolute — which is exactly the
    # feature under test.
    tmpbin = tmp_path / "isolated_bin"
    tmpbin.mkdir()
    if os.name == "nt":
        # On Windows the launcher is a self-contained ``.exe`` whose embedded
        # interpreter path is absolute; copying it is sufficient.
        launcher = tmpbin / Path(c3_exe).name
        shutil.copy2(c3_exe, launcher)
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        path_entries = [str(tmpbin), os.path.join(system_root, "System32"), system_root]
        env_extra = {"SYSTEMROOT": system_root}
    else:
        # On POSIX the launcher is a text script with an absolute shebang; a
        # symlink preserves that shebang so it runs without python on PATH.
        launcher = tmpbin / "c3"
        os.symlink(c3_exe, launcher)
        path_entries = [str(tmpbin)]
        env_extra = {}
    c3_launcher = str(launcher)
    constrained_path = os.pathsep.join(path_entries)

    # Sanity-check the simulated environment truly lacks plain python before
    # trusting the subprocess result below.
    assert shutil.which("python", path=constrained_path) is None
    assert shutil.which("python3", path=constrained_path) is None

    env = {
        "PATH": constrained_path,
        "PYTHONPATH": str(REPO_SRC),
        **env_extra,
    }
    script = _write_script(tmp_path, "no_python_on_path.py", "import sys\nsys.exit(0)\n")

    result = subprocess.run(
        [c3_launcher, "run", str(script)],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# (10) c3 run 自身の引数パースエラーは exit 2 でなく exit 1 に正規化 (DC-AS-005)
# ---------------------------------------------------------------------------


def test_run_without_any_target_normalizes_to_exit_one(
    capsys: pytest.CaptureFixture,
) -> None:
    """No script / -m / -c given at all: this is a cli_run parsing failure
    that happens *before* any user script executes, so per DC-AS-005 it must
    be normalized to exit 1 with an stderr message — not fall through to
    argparse's default SystemExit(2), and not silently return 0.
    """
    rc = cli.main(["run"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.strip() != ""


# ---------------------------------------------------------------------------
# (11) -c は独立した __main__ 名前空間・sys.argv == ["-c", *args] (DC-AM-001)
# ---------------------------------------------------------------------------


def test_run_dash_c_uses_isolated_main_namespace_and_argv(
    capsys: pytest.CaptureFixture,
) -> None:
    code = textwrap.dedent(
        """\
        import sys
        g = globals()
        assert g.get("__name__") == "__main__", g.get("__name__")
        assert "argparse" not in g, sorted(g.keys())
        assert "runpy" not in g, sorted(g.keys())
        assert sys.argv == ["-c", "x", "y"], sys.argv
        print("ISOLATED-OK")
        """
    )
    rc = cli.main(["run", "-c", code, "x", "y"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ISOLATED-OK" in out


# ---------------------------------------------------------------------------
# (12) -c で cwd の sibling import が通る（sys.path に "" を insert）
# ---------------------------------------------------------------------------


def test_run_dash_c_can_import_from_cwd(tmp_path: Path) -> None:
    """With sys.path.insert(0, ""), code run via ``c3 run -c`` should be able
    to import modules from the current working directory (matching ``python -c``).
    """
    _write_script(tmp_path, "helper.py", "MAGIC = 99\n")
    code = textwrap.dedent(
        """\
        import sys
        import helper
        sys.exit(0 if helper.MAGIC == 99 else 1)
        """
    )
    # Run the command in the tmp_path directory to make helper.py importable.
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "c3", "run", "-c", code],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(REPO_SRC)},
    )
    assert result.returncode == 0, f"stderr: {result.stderr!r}"


# ---------------------------------------------------------------------------
# (13) -m で cwd のモジュールを実行し cwd の sibling import が通る
# ---------------------------------------------------------------------------


def test_run_dash_m_can_import_from_cwd(tmp_path: Path) -> None:
    """With sys.path.insert(0, ""), a module run via ``c3 run -m`` should be able
    to import sibling modules from the current working directory (matching ``python -m``).
    """
    _write_script(tmp_path, "helper.py", "MAGIC = 88\n")
    _write_script(
        tmp_path,
        "main_module.py",
        textwrap.dedent(
            """\
            import sys
            import helper
            sys.exit(0 if helper.MAGIC == 88 else 1)
            """
        ),
    )
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "c3", "run", "-m", "main_module"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(REPO_SRC)},
    )
    assert result.returncode == 0, f"stderr: {result.stderr!r}"


# ---------------------------------------------------------------------------
# (14) script -- positional で "--" が sys.argv に残る（透過テスト）
# ---------------------------------------------------------------------------


def test_run_script_dash_dash_token_is_preserved(tmp_path: Path) -> None:
    """The ``--`` token, when passed through c3 run, should be preserved in
    sys.argv verbatim (not stripped by argparse). This tests transparent
    forwarding of all tokens including ``--``.
    """
    out_file = tmp_path / "argv_with_dashes.txt"
    script = _write_script(
        tmp_path,
        "record_dashes.py",
        f"""\
        import sys
        from pathlib import Path
        Path({str(out_file)!r}).write_text(repr(sys.argv), encoding="utf-8")
        """,
    )
    # sys.argv should be [script, "--", "positional"]
    rc = cli.main(["run", str(script), "--", "positional"])
    assert rc == 0
    recorded = out_file.read_text(encoding="utf-8")
    # Verify "--" is present in sys.argv at the expected position.
    recorded_argv = eval(recorded)  # Safe here since we wrote it ourselves.
    assert recorded_argv == [str(script), "--", "positional"], f"got {recorded_argv}"


# ---------------------------------------------------------------------------
# (15) handle() は cli.main() が唯一の呼び出し経路: _raw_argv 前提を pin する
# ---------------------------------------------------------------------------


def test_handle_requires_raw_argv_no_sys_argv_fallback() -> None:
    """CR-Q-005 / SR review 20260718-143906: ``cli_run.handle`` intentionally has
    no ``sys.argv`` fallback. ``cli.main`` is the sole caller and always sets
    ``_raw_argv``; calling ``handle`` without it must fail loudly (AttributeError)
    rather than silently binding to the process-global ``sys.argv``.
    """
    args = argparse.Namespace()  # deliberately missing ``_raw_argv``
    with pytest.raises(AttributeError):
        cli_run.handle(args)
