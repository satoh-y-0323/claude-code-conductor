"""Shared pytest fixtures for parallel_orchestra test suite."""

from __future__ import annotations

import io
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def manifest_file(tmp_path: Path):
    """Factory fixture that writes content to a tmp manifest file."""

    def _factory(content: str) -> Path:
        path = tmp_path / "manifest.md"
        path.write_text(content, encoding="utf-8")
        return path

    return _factory


@pytest.fixture
def fake_claude_runner(monkeypatch):
    """Return a factory that installs a fake subprocess.Popen for runner tests."""

    def install(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
        recorder: dict[str, Any] = {
            "calls": [],
            "thread_ids": [],
            "call_count": 0,
            "call_args": [],
        }
        call_index_lock = threading.Lock()
        call_index = [0]

        class FakePopenInstance:
            def __init__(
                self,
                cmd: list[str],
                spec: dict[str, Any],
                *,
                sleep_sec: float,
            ) -> None:
                self._cmd = cmd
                self._spec = spec
                self._sleep_sec = sleep_sec
                self.returncode: int | None = spec.get("returncode", 0)
                self.pid: int = 0
                self._killed_event = threading.Event()
                self.stdout = io.StringIO(spec.get("stdout", ""))
                self.stderr = io.StringIO(spec.get("stderr", ""))

            def wait(self) -> int | None:
                if self._spec.get("block_until_killed"):
                    self._killed_event.wait()
                    return self.returncode
                if self._sleep_sec > 0:
                    time.sleep(self._sleep_sec)
                exc = self._spec.get("exception")
                if exc is not None and not isinstance(exc, FileNotFoundError):
                    raise exc
                return self.returncode

            def kill(self) -> None:
                self._killed_event.set()

        def fake_popen(*args: Any, **kwargs: Any) -> FakePopenInstance:
            with call_index_lock:
                idx = call_index[0]
                call_index[0] += 1

            spec: dict[str, Any] = outcomes[idx] if idx < len(outcomes) else {}

            thread_ident = threading.get_ident()
            with call_index_lock:
                recorder["call_count"] += 1
                recorder["thread_ids"].append(thread_ident)
                recorder["call_args"].append((args, kwargs))
                recorder["calls"].append(spec)

            exc = spec.get("exception")
            if isinstance(exc, FileNotFoundError):
                raise exc

            cmd: list[str] = args[0] if args else kwargs.get("args", [])
            sleep_sec: float = spec.get("sleep_sec", 0.0)
            return FakePopenInstance(cmd, spec, sleep_sec=sleep_sec)

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        return recorder

    return install
