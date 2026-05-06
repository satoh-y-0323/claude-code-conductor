"""Tests for round-2 code-review and security-review fixes (Red phase).

Each test is written to FAIL against the current implementation and PASS
after the planned fixes described in plan-report-20260502-225741.md are applied.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MANIFEST_WITH_WEBHOOK = """\
---
po_plan_version: "0.1"
name: webhook-test
cwd: "."
tasks:
  - id: task1
    agent: reviewer
    read_only: true
on_complete:
  webhook_url: "{url}"
---
"""


# ---------------------------------------------------------------------------
# Task 6 / R-6 — _mask_sensitive_env_values must mask short values
# ---------------------------------------------------------------------------


def test_mask_threshold_masks_short_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Values shorter than 8 chars must still be masked after R-6 fix.

    Current code: ``if value and len(value) >= 8:`` — skips values with fewer
    than 8 characters, so "sk-ab" (5 chars) is NOT masked.

    After fix: ``if value and value.strip():`` — any non-blank value is masked.
    """
    from parallel_orchestra.runner import _mask_sensitive_env_values

    short_key = "sk-ab"
    monkeypatch.setenv("ANTHROPIC_API_KEY", short_key)

    text = f"The API key is {short_key} and it was used."
    result = _mask_sensitive_env_values(text)

    assert short_key not in result, (
        f"Expected {short_key!r} to be masked, but it was not. "
        f"Got: {result!r}"
    )
    assert "[MASKED]" in result, (
        f"Expected '[MASKED]' to appear in result, but it did not. "
        f"Got: {result!r}"
    )


# ---------------------------------------------------------------------------
# Task 8 / R-8 — _BLOCKED_HOSTNAMES must include localhost aliases
# ---------------------------------------------------------------------------


def _write_webhook_manifest(tmp_path: Path, url: str) -> Path:
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text(
        _MANIFEST_WITH_WEBHOOK.format(url=url), encoding="utf-8"
    )
    return manifest_path


def test_webhook_localhost_localdomain_blocked(tmp_path: Path) -> None:
    """https://localhost.localdomain must be rejected after R-8 fix.

    Current _BLOCKED_HOSTNAMES: {"localhost", "ip6-localhost", "ip6-loopback"}.
    "localhost.localdomain" is NOT in the set, so the URL is NOT rejected.

    After fix: "localhost.localdomain" is added to _BLOCKED_HOSTNAMES.
    """
    from parallel_orchestra.manifest import ManifestError, load_manifest

    manifest_path = _write_webhook_manifest(
        tmp_path, "https://localhost.localdomain/hook"
    )

    with pytest.raises(ManifestError):
        load_manifest(manifest_path)


def test_webhook_localhost4_blocked(tmp_path: Path) -> None:
    """https://localhost4 must be rejected after R-8 fix.

    Current _BLOCKED_HOSTNAMES does not contain "localhost4",
    so the URL is NOT rejected.

    After fix: "localhost4" is added to _BLOCKED_HOSTNAMES.
    """
    from parallel_orchestra.manifest import ManifestError, load_manifest

    manifest_path = _write_webhook_manifest(tmp_path, "https://localhost4/hook")

    with pytest.raises(ManifestError):
        load_manifest(manifest_path)


# ---------------------------------------------------------------------------
# Task 3 / R-3 — _write_task_logs must call chmod on every attempt
# ---------------------------------------------------------------------------


def test_chmod_called_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """chmod(0o600) must be called even on retry attempts (attempt > 0).

    Current code: ``if attempt == 0: stdout_path.chmod(0o600)``
    This means attempt=1 (first retry) skips the chmod call.

    After fix: chmod is called unconditionally after every write.
    """
    from parallel_orchestra.runner import LogConfig, _write_task_logs

    chmod_calls: list[tuple[Path, int]] = []

    original_chmod = Path.chmod

    def tracking_chmod(self: Path, mode: int, **kwargs: object) -> None:  # type: ignore[override]
        chmod_calls.append((self, mode))
        original_chmod(self, mode)

    monkeypatch.setattr(Path, "chmod", tracking_chmod)

    log_config = LogConfig(base_dir=tmp_path, enabled=True)

    _write_task_logs(
        "my-task",
        "stdout content",
        "stderr content",
        attempt=1,  # retry attempt — current code skips chmod here
        log_config=log_config,
    )

    chmod_paths = [p for p, _ in chmod_calls]
    stdout_log = tmp_path / "my-task-stdout.log"
    stderr_log = tmp_path / "my-task-stderr.log"

    assert stdout_log in chmod_paths, (
        f"Expected chmod to be called on {stdout_log} during attempt=1, "
        f"but chmod was called on: {chmod_paths}"
    )
    assert stderr_log in chmod_paths, (
        f"Expected chmod to be called on {stderr_log} during attempt=1, "
        f"but chmod was called on: {chmod_paths}"
    )
