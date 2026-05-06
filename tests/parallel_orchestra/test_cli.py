"""Tests for parallel_orchestra.cli.main."""

from __future__ import annotations

from pathlib import Path

import pytest

from parallel_orchestra.manifest import ManifestError
from parallel_orchestra.runner import RunnerError, RunResult, TaskResult


def _make_task_result(
    task_id: str = "t1",
    agent: str = "code-reviewer",
    returncode: int = 0,
    timed_out: bool = False,
    duration_sec: float = 0.1,
) -> TaskResult:
    return TaskResult(
        task_id=task_id,
        agent=agent,
        returncode=returncode,
        stdout="",
        stderr="" if returncode == 0 else "error output",
        timed_out=timed_out,
        duration_sec=duration_sec,
    )


def _make_run_result(*task_results: TaskResult) -> RunResult:
    return RunResult(results=tuple(task_results))


class TestMainExitCodes:
    def test_all_tasks_success_returns_exit_0(self, monkeypatch, tmp_path):
        from parallel_orchestra import cli

        manifest_path = tmp_path / "manifest.md"
        manifest_path.write_text("dummy", encoding="utf-8")
        run_result = _make_run_result(
            _make_task_result("t1", returncode=0),
            _make_task_result("t2", returncode=0),
        )
        monkeypatch.setattr(cli, "load_manifest", lambda *a, **kw: object())
        monkeypatch.setattr(cli, "run_manifest", lambda *a, **kw: run_result)

        assert cli.main(["run", str(manifest_path)]) == 0

    def test_partial_failure_returns_exit_1(self, monkeypatch, tmp_path):
        from parallel_orchestra import cli

        manifest_path = tmp_path / "manifest.md"
        manifest_path.write_text("dummy", encoding="utf-8")
        run_result = _make_run_result(
            _make_task_result("t1", returncode=0),
            _make_task_result("t2", returncode=1),
        )
        monkeypatch.setattr(cli, "load_manifest", lambda *a, **kw: object())
        monkeypatch.setattr(cli, "run_manifest", lambda *a, **kw: run_result)

        assert cli.main(["run", str(manifest_path)]) == 1

    def test_manifest_error_returns_exit_2_and_writes_stderr(
        self, monkeypatch, tmp_path, capsys
    ):
        from parallel_orchestra import cli

        manifest_path = tmp_path / "manifest.md"
        manifest_path.write_text("dummy", encoding="utf-8")
        monkeypatch.setattr(
            cli,
            "load_manifest",
            lambda *a, **kw: (_ for _ in ()).throw(ManifestError("bad manifest content")),
        )

        exit_code = cli.main(["run", str(manifest_path)])
        captured = capsys.readouterr()
        assert exit_code == 2
        assert "bad manifest content" in captured.err

    def test_runner_error_returns_exit_3_and_writes_stderr(
        self, monkeypatch, tmp_path, capsys
    ):
        from parallel_orchestra import cli

        manifest_path = tmp_path / "manifest.md"
        manifest_path.write_text("dummy", encoding="utf-8")
        monkeypatch.setattr(cli, "load_manifest", lambda *a, **kw: object())
        monkeypatch.setattr(
            cli,
            "run_manifest",
            lambda *a, **kw: (_ for _ in ()).throw(RunnerError("claude not found")),
        )

        exit_code = cli.main(["run", str(manifest_path)])
        captured = capsys.readouterr()
        assert exit_code == 3
        assert "claude not found" in captured.err


class TestVersionFlag:
    def test_version_flag_prints_version_and_returns_exit_0(self, capsys):
        import parallel_orchestra
        from parallel_orchestra import cli

        with pytest.raises(SystemExit) as exc_info:
            cli.main(["--version"])

        captured = capsys.readouterr()
        assert exc_info.value.code == 0
        assert parallel_orchestra.__version__ in captured.out


class TestHelpFlag:
    def test_help_flag_returns_exit_0(self):
        from parallel_orchestra import cli

        with pytest.raises(SystemExit) as exc_info:
            cli.main(["--help"])

        assert exc_info.value.code == 0


class TestNoArgumentsError:
    def test_no_args_returns_nonzero(self):
        from parallel_orchestra import cli

        assert cli.main([]) != 0


class TestQuietFlag:
    def test_quiet_suppresses_progress_output(self, monkeypatch, tmp_path, capsys):
        from parallel_orchestra import cli

        manifest_path = tmp_path / "manifest.md"
        manifest_path.write_text("dummy", encoding="utf-8")
        run_result = _make_run_result(_make_task_result("t1", returncode=0))
        monkeypatch.setattr(cli, "load_manifest", lambda *a, **kw: object())
        monkeypatch.setattr(cli, "run_manifest", lambda *a, **kw: run_result)

        exit_code = cli.main(["run", str(manifest_path), "--quiet"])
        captured = capsys.readouterr()
        assert exit_code == 0
        assert "t1" not in captured.out

    def test_quiet_still_shows_failure_summary(self, monkeypatch, tmp_path, capsys):
        from parallel_orchestra import cli

        manifest_path = tmp_path / "manifest.md"
        manifest_path.write_text("dummy", encoding="utf-8")
        run_result = _make_run_result(
            _make_task_result("t1", returncode=0),
            _make_task_result("t2", returncode=1),
        )
        monkeypatch.setattr(cli, "load_manifest", lambda *a, **kw: object())
        monkeypatch.setattr(cli, "run_manifest", lambda *a, **kw: run_result)

        exit_code = cli.main(["run", str(manifest_path), "--quiet"])
        captured = capsys.readouterr()
        assert exit_code == 1
        assert "t2" in captured.out or "t2" in captured.err


class TestMaxWorkersPassthrough:
    def test_max_workers_forwarded_to_run_manifest(self, monkeypatch, tmp_path):
        from parallel_orchestra import cli

        manifest_path = tmp_path / "manifest.md"
        manifest_path.write_text("dummy", encoding="utf-8")
        run_result = _make_run_result(_make_task_result("t1", returncode=0))
        received: dict = {}

        def capturing_run_manifest(manifest, **kwargs):
            received.update(kwargs)
            return run_result

        monkeypatch.setattr(cli, "load_manifest", lambda *a, **kw: object())
        monkeypatch.setattr(cli, "run_manifest", capturing_run_manifest)

        cli.main(["run", str(manifest_path), "--max-workers", "3"])
        assert received.get("max_workers") == 3


class TestClaudeExePassthrough:
    def test_claude_exe_forwarded_to_run_manifest(self, monkeypatch, tmp_path):
        from parallel_orchestra import cli

        manifest_path = tmp_path / "manifest.md"
        manifest_path.write_text("dummy", encoding="utf-8")
        run_result = _make_run_result(_make_task_result("t1", returncode=0))
        received: dict = {}

        def capturing_run_manifest(manifest, **kwargs):
            received.update(kwargs)
            return run_result

        monkeypatch.setattr(cli, "load_manifest", lambda *a, **kw: object())
        monkeypatch.setattr(cli, "run_manifest", capturing_run_manifest)

        cli.main(["run", str(manifest_path), "--claude-exe", "/usr/local/bin/claude"])
        assert received.get("claude_executable") == "/usr/local/bin/claude"


class TestSummaryOutput:
    @pytest.mark.parametrize(
        "returncode, timed_out, expected_label",
        [
            (0, False, "ok"),
            (1, False, "fail"),
            (None, True, "timeout"),
        ],
    )
    def test_summary_line_contains_expected_label(
        self, monkeypatch, tmp_path, capsys, returncode, timed_out, expected_label
    ):
        from parallel_orchestra import cli

        manifest_path = tmp_path / "manifest.md"
        manifest_path.write_text("dummy", encoding="utf-8")
        run_result = _make_run_result(
            _make_task_result("task-alpha", returncode=returncode, timed_out=timed_out)
        )
        monkeypatch.setattr(cli, "load_manifest", lambda *a, **kw: object())
        monkeypatch.setattr(cli, "run_manifest", lambda *a, **kw: run_result)

        cli.main(["run", str(manifest_path)])
        captured = capsys.readouterr()
        assert f"[{expected_label}]" in captured.out
        assert "task-alpha" in captured.out

    def test_summary_line_contains_agent_name(self, monkeypatch, tmp_path, capsys):
        from parallel_orchestra import cli

        manifest_path = tmp_path / "manifest.md"
        manifest_path.write_text("dummy", encoding="utf-8")
        run_result = _make_run_result(
            _make_task_result("t1", agent="custom-agent", returncode=0)
        )
        monkeypatch.setattr(cli, "load_manifest", lambda *a, **kw: object())
        monkeypatch.setattr(cli, "run_manifest", lambda *a, **kw: run_result)

        cli.main(["run", str(manifest_path)])
        captured = capsys.readouterr()
        assert "custom-agent" in captured.out

    def test_summary_line_contains_duration(self, monkeypatch, tmp_path, capsys):
        from parallel_orchestra import cli

        manifest_path = tmp_path / "manifest.md"
        manifest_path.write_text("dummy", encoding="utf-8")
        run_result = _make_run_result(
            _make_task_result("t1", returncode=0, duration_sec=42.0)
        )
        monkeypatch.setattr(cli, "load_manifest", lambda *a, **kw: object())
        monkeypatch.setattr(cli, "run_manifest", lambda *a, **kw: run_result)

        cli.main(["run", str(manifest_path)])
        captured = capsys.readouterr()
        assert "duration=" in captured.out


class TestLogOptions:
    def test_no_log_flag_forwarded(self, monkeypatch, tmp_path):
        from parallel_orchestra import cli

        manifest_path = tmp_path / "manifest.md"
        manifest_path.write_text("dummy", encoding="utf-8")
        run_result = _make_run_result(_make_task_result("t1", returncode=0))
        received: dict = {}

        def capturing_run_manifest(manifest, **kwargs):
            received.update(kwargs)
            return run_result

        monkeypatch.setattr(cli, "load_manifest", lambda *a, **kw: object())
        monkeypatch.setattr(cli, "run_manifest", capturing_run_manifest)

        cli.main(["run", str(manifest_path), "--no-log"])
        assert received.get("log_enabled") is False

    def test_log_dir_forwarded(self, monkeypatch, tmp_path):
        from parallel_orchestra import cli

        manifest_path = tmp_path / "manifest.md"
        manifest_path.write_text("dummy", encoding="utf-8")
        run_result = _make_run_result(_make_task_result("t1", returncode=0))
        received: dict = {}

        def capturing_run_manifest(manifest, **kwargs):
            received.update(kwargs)
            return run_result

        monkeypatch.setattr(cli, "load_manifest", lambda *a, **kw: object())
        monkeypatch.setattr(cli, "run_manifest", capturing_run_manifest)

        log_dir = tmp_path / "mylogs"
        cli.main(["run", str(manifest_path), "--log-dir", str(log_dir)])
        assert received.get("log_dir") == log_dir


class TestDryRun:
    def test_dry_run_prints_plan_and_returns_0(self, monkeypatch, tmp_path, capsys):
        from parallel_orchestra import cli
        from parallel_orchestra.manifest import Manifest, Task

        manifest_path = tmp_path / "manifest.md"
        manifest_path.write_text("dummy", encoding="utf-8")

        fake_task = Task(
            id="review",
            agent="code-reviewer",
            read_only=True,
            prompt="/agent-code-reviewer",
            env={},
        )
        fake_manifest = Manifest(
            path=manifest_path,
            po_plan_version="0.1",
            name="test-plan",
            cwd=".",
            tasks=(fake_task,),
        )

        monkeypatch.setattr(cli, "load_manifest", lambda *a, **kw: fake_manifest)

        exit_code = cli.main(["run", str(manifest_path), "--dry-run"])
        captured = capsys.readouterr()
        assert exit_code == 0
        assert "Dry run" in captured.out
        assert "review" in captured.out
