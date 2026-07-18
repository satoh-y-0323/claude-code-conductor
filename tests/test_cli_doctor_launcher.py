"""Tests for the ``c3 doctor`` hook launcher verification (_check_hook_launchers).

Red phase: ``_check_hook_launchers()`` does not exist yet in c3.cli_doctor.
These tests are expected to fail with AttributeError until the function is
implemented per architecture-report-20260718-114347.md §2-4.

Scope (per architecture §2-4):
  1. Extract launcher tokens (first token of the command) from every
     settings.json hooks entry AND statusLine.
  2. ``c3`` launcher: resolvable via shutil.which -> OK + a ``--version``
     subprocess probe against the resolved binary; unresolvable -> FAIL with
     a pip/PATH/venv remediation hint.
  3. ``python`` (legacy) launcher: resolvable -> WARN recommending
     ``c3 update``; unresolvable -> FAIL warning that all hooks are silent.
  4. Missing/corrupt settings.json must not crash; must WARN.
  5. Must coexist with existing doctor checks when wired into handle().
  5b. Any other unknown token: resolvable -> informational display (not
      FAIL); unresolvable -> FAIL (catch-all safety net).
  6. FAIL/WARN wording must actually be rendered in doctor's printed output
     (capsys), not just present in the returned findings tuples.
  7. The output must include a 1-line disclaimer that the verification is
     scoped to the current shell's PATH.

Judgement (shutil.which) and the ``--version`` probe (subprocess.run) must
run once per UNIQUE launcher token, not once per settings.json entry.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import c3.cli_doctor as cli_doctor
from c3.cli_doctor import _ERR, _OK, _WARN

# Literal wording pinned by architecture-report-20260718-114347.md §2-4.
_C3_UNRESOLVABLE_HINT = "pip インストール環境の PATH を確認"
_PYTHON_LEGACY_WARN = "旧形式"
_PYTHON_LEGACY_WARN_UPDATE_HINT = "c3 update"
_PYTHON_ALL_HOOKS_SILENT_FAIL = "この環境では全 hook が起動していません"
_SCOPE_DISCLAIMER = "doctor を実行している現在のシェル PATH"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_args(platform: str = "claude", quiet: bool = False) -> argparse.Namespace:
    return argparse.Namespace(platform=platform, quiet=quiet)


def _write_settings_json(
    tmp_path: Path,
    hook_commands: list[str],
    status_line_command: str | None = None,
) -> Path:
    """Write a minimal but structurally realistic .claude/settings.json.

    ``hook_commands`` are spread across a couple of distinct hook events /
    matchers so extraction must walk the full nested hooks structure, not
    just a single flat list.
    """
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)

    entries = []
    for i, cmd in enumerate(hook_commands):
        entries.append(
            {
                "type": "command",
                "command": cmd,
                "args": [f"${{CLAUDE_PROJECT_DIR}}/.claude/hooks/h{i}.py"],
            }
        )

    settings: dict = {
        "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": entries[:1]}]
            if entries
            else [],
            "PostToolUse": [{"matcher": "Write", "hooks": entries[1:]}]
            if len(entries) > 1
            else [],
        }
    }
    if status_line_command is not None:
        settings["statusLine"] = {"type": "command", "command": status_line_command}

    settings_path = claude_dir / "settings.json"
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    return settings_path


def _patch_which(monkeypatch, resolvable: dict[str, str]) -> list[str]:
    """Patch shutil.which to resolve only the given tokens; record every call."""
    calls: list[str] = []

    def fake_which(name: str) -> str | None:
        calls.append(name)
        return resolvable.get(name)

    monkeypatch.setattr(cli_doctor.shutil, "which", fake_which)
    return calls


def _patch_run_ok(monkeypatch) -> list[list[str]]:
    """Patch subprocess.run to always succeed; record every invoked argv."""
    calls: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="c3 2.51.0", stderr="")

    monkeypatch.setattr(cli_doctor.subprocess, "run", fake_run)
    return calls


# ---------------------------------------------------------------------------
# (1) extraction across hooks entries + statusLine, per-unique-token dedup
# ---------------------------------------------------------------------------


def test_extracts_and_dedupes_launcher_tokens_across_hooks_and_statusline(
    tmp_path: Path, monkeypatch
):
    """Multiple hook entries + statusLine sharing the same launcher token must
    be judged (which + --version probe) exactly once, not once per entry."""
    _write_settings_json(
        tmp_path,
        hook_commands=["c3", "c3", "c3"],
        status_line_command='c3 run "${CLAUDE_PROJECT_DIR}/.claude/hooks/statusline.py"',
    )
    monkeypatch.chdir(tmp_path)

    which_calls = _patch_which(monkeypatch, {"c3": "/usr/local/bin/c3"})
    run_calls = _patch_run_ok(monkeypatch)

    findings = cli_doctor._check_hook_launchers()

    assert which_calls.count("c3") == 1, (
        f"expected exactly one which('c3') call (per-unique-token), got: {which_calls}"
    )
    assert len(run_calls) == 1, (
        f"expected exactly one --version probe (per-unique-token), got: {run_calls}"
    )
    assert any(status == _OK for status, _label, _detail in findings), findings


# ---------------------------------------------------------------------------
# (2) c3 launcher: resolvable -> OK + --version probe; unresolvable -> FAIL
# ---------------------------------------------------------------------------


def test_c3_launcher_resolvable_probes_version_and_reports_ok(tmp_path: Path, monkeypatch):
    _write_settings_json(tmp_path, hook_commands=["c3"])
    monkeypatch.chdir(tmp_path)

    _patch_which(monkeypatch, {"c3": "/usr/local/bin/c3"})
    run_calls = _patch_run_ok(monkeypatch)

    findings = cli_doctor._check_hook_launchers()

    assert any(
        status == _OK and "c3" in label for status, label, _detail in findings
    ), f"expected OK finding for resolvable c3 launcher, got: {findings}"
    assert run_calls, "expected a --version probe subprocess call for resolvable c3"
    probed_cmd = run_calls[0]
    assert probed_cmd[0] == "/usr/local/bin/c3"
    assert "--version" in probed_cmd


def test_c3_launcher_unresolvable_fails_with_pip_path_hint(tmp_path: Path, monkeypatch):
    _write_settings_json(tmp_path, hook_commands=["c3"])
    monkeypatch.chdir(tmp_path)

    _patch_which(monkeypatch, {})  # nothing resolves
    run_calls = _patch_run_ok(monkeypatch)

    findings = cli_doctor._check_hook_launchers()

    fail_findings = [f for f in findings if f[0] == _ERR]
    assert fail_findings, f"expected a FAIL(_ERR) finding for unresolvable c3, got: {findings}"
    assert any(_C3_UNRESOLVABLE_HINT in detail for _s, _l, detail in fail_findings), (
        f"expected pip/PATH remediation hint in FAIL detail, got: {fail_findings}"
    )
    assert not run_calls, "must not attempt a --version probe when c3 is unresolvable"


def test_c3_launcher_resolvable_but_version_probe_fails_is_not_silently_ok(
    tmp_path: Path, monkeypatch
):
    """Static which() resolution alone is insufficient per architecture
    ('起動可能性まで確認'); a failing --version probe must not be reported OK."""
    _write_settings_json(tmp_path, hook_commands=["c3"])
    monkeypatch.chdir(tmp_path)

    _patch_which(monkeypatch, {"c3": "/usr/local/bin/c3"})

    def fake_run_fails(cmd, *a, **k):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="broken pipe")

    monkeypatch.setattr(cli_doctor.subprocess, "run", fake_run_fails)

    findings = cli_doctor._check_hook_launchers()

    assert not any(
        status == _OK and "c3" in label for status, label, _detail in findings
    ), f"a failing --version probe must not produce an OK c3 finding, got: {findings}"


# ---------------------------------------------------------------------------
# (3) python (legacy) launcher: resolvable -> WARN; unresolvable -> FAIL
# ---------------------------------------------------------------------------


def test_python_launcher_resolvable_warns_with_update_recommendation(
    tmp_path: Path, monkeypatch
):
    _write_settings_json(tmp_path, hook_commands=["python"])
    monkeypatch.chdir(tmp_path)

    _patch_which(monkeypatch, {"python": "C:\\Python312\\python.exe"})
    _patch_run_ok(monkeypatch)

    findings = cli_doctor._check_hook_launchers()

    warn_findings = [f for f in findings if f[0] == _WARN]
    assert warn_findings, f"expected a WARN finding for legacy python launcher, got: {findings}"
    assert any(
        _PYTHON_LEGACY_WARN in detail and _PYTHON_LEGACY_WARN_UPDATE_HINT in detail
        for _s, _l, detail in warn_findings
    ), f"expected legacy/update wording in WARN detail, got: {warn_findings}"


def test_python_launcher_unresolvable_fails_with_all_hooks_silent_warning(
    tmp_path: Path, monkeypatch
):
    _write_settings_json(tmp_path, hook_commands=["python"])
    monkeypatch.chdir(tmp_path)

    _patch_which(monkeypatch, {})
    _patch_run_ok(monkeypatch)

    findings = cli_doctor._check_hook_launchers()

    fail_findings = [f for f in findings if f[0] == _ERR]
    assert fail_findings, f"expected a FAIL(_ERR) finding for unresolvable python, got: {findings}"
    assert any(
        _PYTHON_ALL_HOOKS_SILENT_FAIL in detail for _s, _l, detail in fail_findings
    ), f"expected 'all hooks silent' wording in FAIL detail, got: {fail_findings}"


# ---------------------------------------------------------------------------
# (4) missing / corrupt settings.json must not crash; must WARN
# ---------------------------------------------------------------------------


def test_missing_settings_json_warns_without_crashing(tmp_path: Path, monkeypatch):
    (tmp_path / ".claude").mkdir()
    monkeypatch.chdir(tmp_path)

    findings = cli_doctor._check_hook_launchers()

    assert findings, "expected at least one finding even when settings.json is missing"
    assert all(status != _ERR for status, _l, _d in findings), (
        f"missing settings.json must WARN, not FAIL, got: {findings}"
    )
    assert any(status == _WARN for status, _l, _d in findings), findings


def test_corrupt_settings_json_is_err_without_crashing(tmp_path: Path, monkeypatch):
    """A genuine JSON syntax error (user-authored mistake) must surface as ERR,
    not WARN — aligned with _check_settings_json() ("stronger side").

    CR-E-001: Round 2 collateral-downgraded JSONDecodeError to WARN; a real
    JSON typo must be actionable (ERR -> non-zero doctor exit). Must still not
    crash the whole diagnostic.
    """
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text("{not valid json", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    findings = cli_doctor._check_hook_launchers()

    assert findings, "expected at least one finding for corrupt settings.json"
    err_findings = [f for f in findings if f[0] == _ERR and "hook launchers" in f[1]]
    assert err_findings, (
        f"a JSON syntax error must surface as ERR for hook launchers, got: {findings}"
    )
    assert any("invalid JSON" in detail for _s, _l, detail in err_findings), (
        f"expected 'invalid JSON' wording in ERR detail, got: {err_findings}"
    )


# ---------------------------------------------------------------------------
# (5) coexistence with existing doctor checks (must not break handle())
# ---------------------------------------------------------------------------


def test_handle_still_reports_existing_checks_alongside_hook_launchers(
    tmp_path: Path, monkeypatch, capsys
):
    """Wiring _check_hook_launchers() into handle() must not push out or break
    the pre-existing .claude/ directory / settings.json / claude binary checks."""
    _write_settings_json(tmp_path, hook_commands=["c3"])
    monkeypatch.chdir(tmp_path)

    _patch_which(monkeypatch, {"c3": "/usr/local/bin/c3", "claude": "/usr/local/bin/claude"})
    _patch_run_ok(monkeypatch)

    args = _make_args(platform="claude", quiet=False)
    cli_doctor.handle(args)

    output = capsys.readouterr().out

    assert ".claude/ directory" in output
    assert "settings.json" in output
    # New hook-launcher check must also be represented in the same run.
    assert "c3" in output


# ---------------------------------------------------------------------------
# (5b) catch-all: unknown tokens other than c3/python
# ---------------------------------------------------------------------------


def test_unknown_resolvable_launcher_is_informational_not_fail(tmp_path: Path, monkeypatch):
    _write_settings_json(tmp_path, hook_commands=["my-custom-launcher"])
    monkeypatch.chdir(tmp_path)

    _patch_which(monkeypatch, {"my-custom-launcher": "/opt/bin/my-custom-launcher"})
    _patch_run_ok(monkeypatch)

    findings = cli_doctor._check_hook_launchers()

    assert not any(status == _ERR for status, _l, _d in findings), (
        f"a resolvable unknown launcher must not FAIL, got: {findings}"
    )
    assert any(
        "my-custom-launcher" in label or "my-custom-launcher" in detail
        for _s, label, detail in findings
    ), f"expected the unknown launcher to be surfaced informationally, got: {findings}"


def test_unknown_unresolvable_launcher_fails_catch_all(tmp_path: Path, monkeypatch):
    """Mirrors the doctor's real-world FAIL reproduction case:
    a nonexistent custom launcher token (e.g. c3-nonexistent) must FAIL."""
    _write_settings_json(tmp_path, hook_commands=["c3-nonexistent"])
    monkeypatch.chdir(tmp_path)

    _patch_which(monkeypatch, {})
    _patch_run_ok(monkeypatch)

    findings = cli_doctor._check_hook_launchers()

    fail_findings = [f for f in findings if f[0] == _ERR]
    assert fail_findings, (
        f"expected FAIL(_ERR) for unresolvable unknown launcher 'c3-nonexistent', got: {findings}"
    )
    assert any("c3-nonexistent" in label or "c3-nonexistent" in detail for _s, label, detail in fail_findings)
    assert any("c3 update" in detail for _s, _l, detail in fail_findings)


# ---------------------------------------------------------------------------
# (6) FAIL/WARN wording must be rendered in the actual doctor output (capsys)
# ---------------------------------------------------------------------------


def test_doctor_output_renders_c3_unresolvable_fail_wording(tmp_path: Path, monkeypatch, capsys):
    _write_settings_json(tmp_path, hook_commands=["c3"])
    monkeypatch.chdir(tmp_path)

    _patch_which(monkeypatch, {})
    _patch_run_ok(monkeypatch)

    args = _make_args(platform="claude", quiet=False)
    cli_doctor.handle(args)

    output = capsys.readouterr().out
    assert _C3_UNRESOLVABLE_HINT in output, (
        f"expected pip/PATH FAIL wording rendered in doctor output, got:\n{output!r}"
    )


def test_doctor_output_renders_python_unresolvable_fail_wording(tmp_path: Path, monkeypatch, capsys):
    _write_settings_json(tmp_path, hook_commands=["python"])
    monkeypatch.chdir(tmp_path)

    _patch_which(monkeypatch, {})
    _patch_run_ok(monkeypatch)

    args = _make_args(platform="claude", quiet=False)
    cli_doctor.handle(args)

    output = capsys.readouterr().out
    assert _PYTHON_ALL_HOOKS_SILENT_FAIL in output, (
        f"expected 'all hooks silent' FAIL wording rendered in doctor output, got:\n{output!r}"
    )


def test_doctor_output_renders_python_resolvable_warn_wording(tmp_path: Path, monkeypatch, capsys):
    _write_settings_json(tmp_path, hook_commands=["python"])
    monkeypatch.chdir(tmp_path)

    _patch_which(monkeypatch, {"python": "C:\\Python312\\python.exe"})
    _patch_run_ok(monkeypatch)

    args = _make_args(platform="claude", quiet=False)
    cli_doctor.handle(args)

    output = capsys.readouterr().out
    assert _PYTHON_LEGACY_WARN in output and _PYTHON_LEGACY_WARN_UPDATE_HINT in output, (
        f"expected legacy/update WARN wording rendered in doctor output, got:\n{output!r}"
    )


# ---------------------------------------------------------------------------
# (7) scope-limitation disclaimer (current shell PATH, not harness spawn context)
# ---------------------------------------------------------------------------


def test_findings_include_current_shell_path_scope_disclaimer(tmp_path: Path, monkeypatch):
    _write_settings_json(tmp_path, hook_commands=["c3"])
    monkeypatch.chdir(tmp_path)

    _patch_which(monkeypatch, {"c3": "/usr/local/bin/c3"})
    _patch_run_ok(monkeypatch)

    findings = cli_doctor._check_hook_launchers()

    assert any(_SCOPE_DISCLAIMER in detail for _s, _l, detail in findings), (
        f"expected the current-shell-PATH scope disclaimer in findings, got: {findings}"
    )


def test_doctor_output_renders_scope_disclaimer(tmp_path: Path, monkeypatch, capsys):
    _write_settings_json(tmp_path, hook_commands=["c3"])
    monkeypatch.chdir(tmp_path)

    _patch_which(monkeypatch, {"c3": "/usr/local/bin/c3"})
    _patch_run_ok(monkeypatch)

    args = _make_args(platform="claude", quiet=False)
    cli_doctor.handle(args)

    output = capsys.readouterr().out
    assert _SCOPE_DISCLAIMER in output, (
        f"expected the current-shell-PATH scope disclaimer rendered in doctor output, got:\n{output!r}"
    )


# ---------------------------------------------------------------------------
# SR-V-001: JSON parsing resilience (RecursionError / MemoryError)
# ---------------------------------------------------------------------------


def test_check_hook_launchers_survives_deeply_nested_json(tmp_path: Path, monkeypatch):
    """When settings.json contains deeply nested arrays/objects that trigger
    RecursionError in json.loads(), _check_hook_launchers() must return WARN
    and continue gracefully, not crash the entire c3 doctor process.

    SR-V-001: Resilience against RecursionError.
    """
    (tmp_path / ".claude").mkdir()
    settings = tmp_path / ".claude" / "settings.json"

    # Create a deeply nested JSON that triggers RecursionError
    deeply_nested = "[" * 10000 + "]" * 10000
    settings.write_text(deeply_nested, encoding="utf-8")

    monkeypatch.chdir(tmp_path)

    # Should not crash; should return findings with WARN for hook launchers
    findings = cli_doctor._check_hook_launchers()

    # Expect at least one finding (should be WARN for JSON parse failure)
    assert findings, f"Expected findings, got empty list"

    # The hook launchers check should gracefully WARN about JSON parsing
    hook_launcher_findings = [f for f in findings if "hook launchers" in f[1]]
    assert hook_launcher_findings, (
        f"Expected 'hook launchers' finding when JSON parse fails, got: {findings}"
    )

    # The status should be WARN (graceful degradation), not ERR
    statuses = {f[0] for f in hook_launcher_findings}
    assert _WARN in statuses, (
        f"Expected WARN status for unparseable JSON, got: {statuses}"
    )


def test_check_settings_json_survives_deeply_nested_json(tmp_path: Path, monkeypatch):
    """When settings.json contains deeply nested arrays/objects,
    _check_settings_json() must return WARN (graceful), not crash.

    SR-V-001: Resilience against RecursionError in _check_settings_json().
    """
    (tmp_path / ".claude").mkdir()
    settings = tmp_path / ".claude" / "settings.json"

    # Create deeply nested JSON that triggers RecursionError
    deeply_nested = "[" * 10000 + "]" * 10000
    settings.write_text(deeply_nested, encoding="utf-8")

    monkeypatch.chdir(tmp_path)

    # Should not crash
    status, label, detail = cli_doctor._check_settings_json()

    # Should gracefully return WARN with parse-related detail
    assert status == _WARN, f"Expected WARN, got {status}"
    assert "could not parse" in detail.lower() or "recursion" in detail.lower(), (
        f"Expected parse-error hint in detail, got: {detail}"
    )


def test_check_settings_json_simple_syntax_error_is_err_and_doctor_exits_1(
    tmp_path: Path, monkeypatch
):
    """A simple JSON syntax error ({not valid json) must make _check_settings_json()
    return ERR, and c3 doctor must exit with code 1.

    CR-E-001 regression pin: Round 2 downgraded this JSONDecodeError path to WARN
    (exit 0), silently hiding the most common, most actionable corruption (a hand-
    edited JSON typo). This test prevents that regression from recurring.
    """
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text("{not valid json", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    # (i) the unit function returns ERR
    status, label, detail = cli_doctor._check_settings_json()
    assert status == _ERR, f"expected ERR for JSON syntax error, got {status}: {detail}"
    assert "invalid JSON" in detail, f"expected 'invalid JSON' wording, got: {detail}"

    # (ii) doctor's overall exit code is 1 (ERR present)
    # Make PATH lookups deterministic so unrelated checks do not add ERR noise;
    # the settings.json ERR alone must drive exit_code=1.
    _patch_which(
        monkeypatch,
        {"c3": "/usr/local/bin/c3", "claude": "/usr/local/bin/claude"},
    )
    _patch_run_ok(monkeypatch)
    exit_code = cli_doctor.handle(_make_args(platform="claude", quiet=False))
    assert exit_code == 1, (
        f"c3 doctor must exit 1 when settings.json has a JSON syntax error, got {exit_code}"
    )


def test_invalid_utf8_settings_json_warns_without_crashing_both_checks(
    tmp_path: Path, monkeypatch
):
    """settings.json containing non-UTF-8 bytes (e.g. \\xff\\xfe) must not crash:
    both _check_settings_json() and _check_hook_launchers() must return WARN.

    SR-V-001 residual: read_text(encoding="utf-8") raises UnicodeDecodeError
    (a ValueError subclass, NOT in the OSError family), which the previous
    except tuple did not catch -> uncaught crash. This pins graceful WARN.
    """
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    # Invalid UTF-8: 0xff 0xfe is not a valid UTF-8 start byte.
    (claude_dir / "settings.json").write_bytes(b"\xff\xfe{invalid utf8}")
    monkeypatch.chdir(tmp_path)

    # _check_settings_json(): graceful WARN, no crash
    status, _label, detail = cli_doctor._check_settings_json()
    assert status == _WARN, f"expected WARN for invalid UTF-8, got {status}: {detail}"
    assert "could not parse" in detail.lower(), (
        f"expected parse-skip wording in detail, got: {detail}"
    )

    # _check_hook_launchers(): graceful WARN, no crash, no ERR
    findings = cli_doctor._check_hook_launchers()
    hook_findings = [f for f in findings if "hook launchers" in f[1]]
    assert hook_findings, f"expected a 'hook launchers' finding, got: {findings}"
    assert all(status != _ERR for status, _l, _d in hook_findings), (
        f"invalid UTF-8 must WARN (not ERR/crash) for hook launchers, got: {hook_findings}"
    )
    assert any(status == _WARN for status, _l, _d in hook_findings), hook_findings


def test_check_hook_launchers_survives_memory_error(tmp_path: Path, monkeypatch):
    """When reading settings.json triggers MemoryError (e.g., gigantic file),
    _check_hook_launchers() must catch it gracefully and return WARN.

    SR-V-001: Resilience against MemoryError.
    """
    (tmp_path / ".claude").mkdir()
    monkeypatch.chdir(tmp_path)

    settings = tmp_path / ".claude" / "settings.json"

    # Mock json.loads to raise MemoryError
    original_loads = json.loads

    def mocked_loads(s: str, *args, **kwargs):
        if s.startswith("["):
            raise MemoryError("memory limit exceeded")
        return original_loads(s, *args, **kwargs)

    monkeypatch.setattr(cli_doctor.json, "loads", mocked_loads)

    # Create a dummy settings.json (content doesn't matter, mocked loads will fail)
    settings.write_text("[1,2,3]", encoding="utf-8")

    # Should gracefully handle MemoryError
    findings = cli_doctor._check_hook_launchers()

    hook_launcher_findings = [f for f in findings if "hook launchers" in f[1]]
    assert hook_launcher_findings, "Expected findings with 'hook launchers'"

    statuses = {f[0] for f in hook_launcher_findings}
    assert _WARN in statuses, (
        f"Expected WARN when MemoryError caught, got: {statuses}"
    )


# ---------------------------------------------------------------------------
# CR Medium: launcher probe severity (c3 resolvable but --version fails = ERR)
# ---------------------------------------------------------------------------


def test_c3_launcher_resolvable_but_version_probe_fails_is_err_not_warn(
    tmp_path: Path, monkeypatch
):
    """When token 'c3' is resolvable via shutil.which() but the
    `--version` probe fails (subprocess returns non-zero or times out),
    _judge_launcher_token() must return ERR, not WARN.

    CR Medium: Architecture §2-4 intends the --version probe to be
    "a step stronger than static resolution", hence failure should be ERR.
    The practical consequence is "all hooks will not start" (same as
    python-unresolvable), so severity should match.

    This is an upgrade from the previous WARN status.
    """
    _write_settings_json(tmp_path, hook_commands=["c3"])
    monkeypatch.chdir(tmp_path)

    _patch_which(monkeypatch, {"c3": "/usr/local/bin/c3"})

    def fake_run_fails(cmd, *a, **k):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="broken pipe")

    monkeypatch.setattr(cli_doctor.subprocess, "run", fake_run_fails)

    findings = cli_doctor._check_hook_launchers()

    # Must now have ERR (not WARN) for resolvable-but-probe-fails
    err_findings = [f for f in findings if f[0] == _ERR and "c3" in f[1]]
    assert err_findings, (
        f"Expected ERR for resolvable-but-probe-fails c3, got: {findings}"
    )

    # Should mention the probe failure and its practical consequence
    err_details = [detail for _s, _l, detail in err_findings]
    assert any("プローブに失敗" in d or "起動" in d for d in err_details), (
        f"Expected probe-failure message in ERR detail, got: {err_details}"
    )


# ---------------------------------------------------------------------------
# CR Low: launcher probe timeout constant separation
# ---------------------------------------------------------------------------


def test_probe_version_ok_uses_launcher_probe_timeout(monkeypatch):
    """_probe_version_ok() must use _LAUNCHER_PROBE_TIMEOUT_SEC, not
    _MCP_STARTUP_TIMEOUT_SEC, to avoid coupling unrelated timeouts.

    CR Low: Confirm that subprocess.run receives the correct timeout value.
    """
    timeout_arg_received = []

    def mocked_run(*args, **kwargs):
        timeout_arg_received.append(kwargs.get("timeout"))
        # Return a successful CompletedProcess
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr(cli_doctor.subprocess, "run", mocked_run)

    cli_doctor._probe_version_ok("/usr/bin/c3")

    assert timeout_arg_received, "Expected subprocess.run to be called"
    assert timeout_arg_received[0] == cli_doctor._LAUNCHER_PROBE_TIMEOUT_SEC, (
        f"Expected timeout={cli_doctor._LAUNCHER_PROBE_TIMEOUT_SEC}, "
        f"got {timeout_arg_received[0]}"
    )
