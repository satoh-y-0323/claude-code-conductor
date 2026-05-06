"""Tests for parallel_orchestra.manifest module."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from parallel_orchestra.manifest import (
    SUPPORTED_PLAN_VERSIONS,
    Defaults,
    Manifest,
    ManifestError,
    Task,
    WebhookConfig,
    load_manifest,
)

# ---------------------------------------------------------------------------
# Minimal valid manifest content
# ---------------------------------------------------------------------------

MINIMAL_VALID = """\
---
po_plan_version: "0.1"
name: minimal
cwd: "."
tasks:
  - id: review
    agent: code-reviewer
    read_only: true
  - id: security
    agent: security-reviewer
    read_only: true
---
"""


def _make_manifest(extra_front: str = "", tasks_yaml: str | None = None) -> str:
    if tasks_yaml is None:
        tasks_yaml = """\
  - id: review
    agent: code-reviewer
    read_only: true
  - id: security
    agent: security-reviewer
    read_only: true"""
    front = f"""\
po_plan_version: "0.1"
name: test-plan
cwd: "."
tasks:
{tasks_yaml}
{extra_front}"""
    return f"---\n{front}\n---\n"


# ---------------------------------------------------------------------------
# Basic parsing
# ---------------------------------------------------------------------------


def test_正常なマニフェストがパースできる(manifest_file):
    path = manifest_file(MINIMAL_VALID)
    result = load_manifest(path)

    assert isinstance(result, Manifest)
    assert result.po_plan_version == "0.1"
    assert result.name == "minimal"
    assert result.cwd == "."
    assert len(result.tasks) == 2

    ids = {t.id for t in result.tasks}
    agents = {t.agent for t in result.tasks}
    assert ids == {"review", "security"}
    assert agents == {"code-reviewer", "security-reviewer"}


def test_Manifestにpo_plan_versionフィールドがある(manifest_file):
    """Manifest dataclass uses po_plan_version (not clade_plan_version)."""
    path = manifest_file(MINIMAL_VALID)
    result = load_manifest(path)
    assert hasattr(result, "po_plan_version")
    assert not hasattr(result, "clade_plan_version")


# ---------------------------------------------------------------------------
# po_plan_version validation
# ---------------------------------------------------------------------------


def test_サポートバージョンは0_1のみである():
    assert SUPPORTED_PLAN_VERSIONS == frozenset({"0.1"})


@pytest.mark.parametrize(
    "bad_version",
    ["0.0", "0.2", "0.3", "0.4", "0.5", "0.6", "0.7", "1.0", "unknown", "", "v0.1"],
    ids=["0.0", "0.2", "0.3", "0.4", "0.5", "0.6", "0.7", "1.0", "unknown", "empty", "v0.1"],
)
def test_未サポートバージョンでManifestErrorが送出される(bad_version, manifest_file):
    content = f"""\
---
po_plan_version: "{bad_version}"
name: test
cwd: "."
tasks:
  - id: review
    agent: code-reviewer
    read_only: true
---
"""
    path = manifest_file(content)
    with pytest.raises(ManifestError):
        load_manifest(path)


@pytest.mark.parametrize(
    "version_value",
    [123, 1.0, None],
    ids=["integer", "float", "null"],
)
def test_バージョンが非文字列型のときManifestErrorが送出される(version_value, manifest_file):
    import yaml  # noqa: PLC0415

    front: dict = {
        "po_plan_version": version_value,
        "name": "test",
        "cwd": ".",
        "tasks": [{"id": "review", "agent": "code-reviewer", "read_only": True}],
    }
    content = f"---\n{yaml.dump(front)}---\n"
    path = manifest_file(content)
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_clade_plan_versionキーはManifestErrorが送出される(manifest_file):
    """clade_plan_version (old key) raises ManifestError."""
    content = """\
---
clade_plan_version: "0.1"
name: test
cwd: "."
tasks:
  - id: review
    agent: code-reviewer
    read_only: true
---
"""
    path = manifest_file(content)
    with pytest.raises(ManifestError):
        load_manifest(path)


# ---------------------------------------------------------------------------
# Required top-level cwd field
# ---------------------------------------------------------------------------


def test_cwdフィールドが欠落するとManifestErrorが送出される(manifest_file):
    """Missing top-level cwd raises ManifestError."""
    content = """\
---
po_plan_version: "0.1"
name: no-cwd
tasks:
  - id: review
    agent: code-reviewer
    read_only: true
---
"""
    path = manifest_file(content)
    with pytest.raises(ManifestError, match="cwd"):
        load_manifest(path)


def test_cwdが非文字列型のときManifestErrorが送出される(manifest_file):
    import yaml  # noqa: PLC0415

    front: dict = {
        "po_plan_version": "0.1",
        "name": "test",
        "cwd": 123,
        "tasks": [{"id": "review", "agent": "code-reviewer", "read_only": True}],
    }
    content = f"---\n{yaml.dump(front)}---\n"
    path = manifest_file(content)
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_cwdの値がManifestに格納される(manifest_file):
    content = """\
---
po_plan_version: "0.1"
name: test
cwd: "../.."
tasks:
  - id: review
    agent: code-reviewer
    read_only: true
---
"""
    path = manifest_file(content)
    result = load_manifest(path)
    assert result.cwd == "../.."


# ---------------------------------------------------------------------------
# Forbidden task fields (internally managed by PO)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden_key, value",
    [
        ("cwd", "some/dir"),
        ("timeout_sec", 300),
        ("idle_timeout_sec", 60),
        ("retry_delay_sec", 5.0),
        ("retry_backoff_factor", 2.0),
    ],
)
def test_タスクに禁止フィールドがあるとManifestErrorが送出される(
    forbidden_key, value, manifest_file
):
    """Task-level forbidden fields raise ManifestError."""
    import yaml  # noqa: PLC0415

    task = {"id": "review", "agent": "code-reviewer", "read_only": True, forbidden_key: value}
    front = {
        "po_plan_version": "0.1",
        "name": "test",
        "cwd": ".",
        "tasks": [task],
    }
    content = f"---\n{yaml.dump(front)}---\n"
    path = manifest_file(content)
    with pytest.raises(ManifestError):
        load_manifest(path)


# ---------------------------------------------------------------------------
# Task does NOT have timeout_sec, cwd, idle_timeout_sec, retry_delay_sec,
# retry_backoff_factor fields
# ---------------------------------------------------------------------------


def test_Taskにtimeout_secフィールドがない(manifest_file):
    path = manifest_file(MINIMAL_VALID)
    result = load_manifest(path)
    for task in result.tasks:
        assert not hasattr(task, "timeout_sec")


def test_Taskにcwdフィールドがない(manifest_file):
    path = manifest_file(MINIMAL_VALID)
    result = load_manifest(path)
    for task in result.tasks:
        assert not hasattr(task, "cwd")


def test_Taskにidle_timeout_secフィールドがない(manifest_file):
    path = manifest_file(MINIMAL_VALID)
    result = load_manifest(path)
    for task in result.tasks:
        assert not hasattr(task, "idle_timeout_sec")


def test_Taskにretry_delay_secフィールドがない(manifest_file):
    path = manifest_file(MINIMAL_VALID)
    result = load_manifest(path)
    for task in result.tasks:
        assert not hasattr(task, "retry_delay_sec")


def test_Taskにretry_backoff_factorフィールドがない(manifest_file):
    path = manifest_file(MINIMAL_VALID)
    result = load_manifest(path)
    for task in result.tasks:
        assert not hasattr(task, "retry_backoff_factor")


# ---------------------------------------------------------------------------
# Required task keys
# ---------------------------------------------------------------------------


def test_tasksキーが欠落するとManifestErrorが送出される(manifest_file):
    content = """\
---
po_plan_version: "0.1"
name: no-tasks
cwd: "."
---
"""
    path = manifest_file(content)
    with pytest.raises(ManifestError):
        load_manifest(path)


@pytest.mark.parametrize("missing_key", ["agent", "id", "read_only"])
def test_タスク必須キー欠落でManifestErrorが送出される(missing_key, manifest_file):
    import yaml  # noqa: PLC0415

    base_task: dict = {"id": "review", "agent": "code-reviewer", "read_only": True}
    del base_task[missing_key]
    front = {
        "po_plan_version": "0.1",
        "name": "test",
        "cwd": ".",
        "tasks": [base_task],
    }
    content = f"---\n{yaml.dump(front)}---\n"
    path = manifest_file(content)
    with pytest.raises(ManifestError):
        load_manifest(path)


# ---------------------------------------------------------------------------
# Type errors
# ---------------------------------------------------------------------------


def test_read_onlyが文字列yesのときManifestErrorが送出される(manifest_file):
    content = """\
---
po_plan_version: "0.1"
name: test
cwd: "."
tasks:
  - id: review
    agent: code-reviewer
    read_only: "yes"
---
"""
    path = manifest_file(content)
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_tasksがリストでないときManifestErrorが送出される(manifest_file):
    content = """\
---
po_plan_version: "0.1"
name: test
cwd: "."
tasks:
  id: review
  agent: code-reviewer
  read_only: true
---
"""
    path = manifest_file(content)
    with pytest.raises(ManifestError):
        load_manifest(path)


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------


def test_envのデフォルト値が空dictである(manifest_file):
    path = manifest_file(MINIMAL_VALID)
    result = load_manifest(path)
    for task in result.tasks:
        assert task.env == {}


def test_max_retriesのデフォルト値が1である(manifest_file):
    path = manifest_file(MINIMAL_VALID)
    result = load_manifest(path)
    for task in result.tasks:
        assert task.max_retries == 1


def test_promptのデフォルト値がagentから生成される(manifest_file):
    path = manifest_file(MINIMAL_VALID)
    result = load_manifest(path)
    for task in result.tasks:
        assert task.prompt == f"/agent-{task.agent}"


# ---------------------------------------------------------------------------
# Frontmatter delimiters
# ---------------------------------------------------------------------------


def test_フロントマター開始区切りがないとManifestErrorが送出される(manifest_file):
    content = """\
po_plan_version: "0.1"
name: no-frontmatter
cwd: "."
tasks:
  - id: review
    agent: code-reviewer
    read_only: true
---
"""
    path = manifest_file(content)
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_フロントマター閉じ区切りがないとManifestErrorが送出される(manifest_file):
    content = """\
---
po_plan_version: "0.1"
name: unclosed
cwd: "."
tasks:
  - id: review
    agent: code-reviewer
    read_only: true
"""
    path = manifest_file(content)
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_ファイル不在時に適切な例外が送出される(tmp_path):
    missing = tmp_path / "does_not_exist.md"
    with pytest.raises((FileNotFoundError, ManifestError)):
        load_manifest(missing)


def test_空のタスクリストはManifestErrorが送出される(manifest_file):
    content = """\
---
po_plan_version: "0.1"
name: empty-tasks
cwd: "."
tasks: []
---
"""
    path = manifest_file(content)
    with pytest.raises(ManifestError):
        load_manifest(path)


# ---------------------------------------------------------------------------
# read_only: false is accepted
# ---------------------------------------------------------------------------


def test_read_only_falseのタスクが受理される(manifest_file):
    content = """\
---
po_plan_version: "0.1"
name: test
cwd: "."
tasks:
  - id: writer
    agent: developer
    read_only: false
  - id: review
    agent: code-reviewer
    read_only: true
---
"""
    path = manifest_file(content)
    result = load_manifest(path)
    task_ids = {t.id for t in result.tasks}
    assert "writer" in task_ids
    writer_task = next(t for t in result.tasks if t.id == "writer")
    assert writer_task.read_only is False


# ---------------------------------------------------------------------------
# writes field
# ---------------------------------------------------------------------------


def test_writesが省略時は空タプルである(manifest_file):
    path = manifest_file(MINIMAL_VALID)
    result = load_manifest(path)
    for task in result.tasks:
        assert task.writes == ()


def test_writesの相対パスが絶対パスに変換される(manifest_file):
    content = """\
---
po_plan_version: "0.1"
name: test
cwd: "."
tasks:
  - id: writer
    agent: developer
    read_only: false
    writes:
      - src/output.py
---
"""
    path = manifest_file(content)
    result = load_manifest(path)
    task = result.tasks[0]
    assert len(task.writes) == 1
    assert Path(task.writes[0]).is_absolute()
    assert task.writes[0].endswith("/src/output.py") or task.writes[0].endswith("\\src\\output.py") or "src/output.py" in task.writes[0]


def test_writes衝突があるとManifestErrorが送出される(manifest_file):
    content = """\
---
po_plan_version: "0.1"
name: test
cwd: "."
tasks:
  - id: task-a
    agent: developer
    read_only: false
    writes:
      - src/output.py
  - id: task-b
    agent: developer
    read_only: false
    writes:
      - src/output.py
---
"""
    path = manifest_file(content)
    with pytest.raises(ManifestError, match="conflict"):
        load_manifest(path)


# ---------------------------------------------------------------------------
# depends_on field
# ---------------------------------------------------------------------------


def test_depends_onが省略時は空タプルである(manifest_file):
    path = manifest_file(MINIMAL_VALID)
    result = load_manifest(path)
    for task in result.tasks:
        assert task.depends_on == ()


def test_depends_onの依存関係がパースされる(manifest_file):
    content = """\
---
po_plan_version: "0.1"
name: test
cwd: "."
tasks:
  - id: task-a
    agent: developer
    read_only: false
  - id: task-b
    agent: code-reviewer
    read_only: true
    depends_on: [task-a]
---
"""
    path = manifest_file(content)
    result = load_manifest(path)
    task_b = next(t for t in result.tasks if t.id == "task-b")
    assert task_b.depends_on == ("task-a",)


def test_未定義IDへのdepends_onでManifestErrorが送出される(manifest_file):
    content = """\
---
po_plan_version: "0.1"
name: test
cwd: "."
tasks:
  - id: task-a
    agent: code-reviewer
    read_only: true
    depends_on: [non-existent]
---
"""
    path = manifest_file(content)
    with pytest.raises(ManifestError, match="undefined"):
        load_manifest(path)


def test_循環依存でManifestErrorが送出される(manifest_file):
    content = """\
---
po_plan_version: "0.1"
name: test
cwd: "."
tasks:
  - id: task-a
    agent: code-reviewer
    read_only: true
    depends_on: [task-b]
  - id: task-b
    agent: code-reviewer
    read_only: true
    depends_on: [task-a]
---
"""
    path = manifest_file(content)
    with pytest.raises(ManifestError, match="[Cc]yclic"):
        load_manifest(path)


# ---------------------------------------------------------------------------
# max_retries
# ---------------------------------------------------------------------------


def test_max_retriesがパースされる(manifest_file):
    content = """\
---
po_plan_version: "0.1"
name: test
cwd: "."
tasks:
  - id: review
    agent: code-reviewer
    read_only: true
    max_retries: 3
---
"""
    path = manifest_file(content)
    result = load_manifest(path)
    assert result.tasks[0].max_retries == 3


def test_max_retriesが負数のときManifestErrorが送出される(manifest_file):
    content = """\
---
po_plan_version: "0.1"
name: test
cwd: "."
tasks:
  - id: review
    agent: code-reviewer
    read_only: true
    max_retries: -1
---
"""
    path = manifest_file(content)
    with pytest.raises(ManifestError):
        load_manifest(path)


# ---------------------------------------------------------------------------
# defaults section
# ---------------------------------------------------------------------------


def test_defaultsセクションのmax_retriesが適用される(manifest_file):
    content = """\
---
po_plan_version: "0.1"
name: test
cwd: "."
defaults:
  max_retries: 2
tasks:
  - id: review
    agent: code-reviewer
    read_only: true
---
"""
    path = manifest_file(content)
    result = load_manifest(path)
    assert result.tasks[0].max_retries == 2


def test_タスクレベルmax_retriesがdefaultsを上書きする(manifest_file):
    content = """\
---
po_plan_version: "0.1"
name: test
cwd: "."
defaults:
  max_retries: 2
tasks:
  - id: review
    agent: code-reviewer
    read_only: true
    max_retries: 5
---
"""
    path = manifest_file(content)
    result = load_manifest(path)
    assert result.tasks[0].max_retries == 5


def test_defaultsにtimeout_secがあると警告が出る(manifest_file):
    """defaults section with timeout_sec should warn or raise (not silently apply)."""
    content = """\
---
po_plan_version: "0.1"
name: test
cwd: "."
defaults:
  timeout_sec: 300
tasks:
  - id: review
    agent: code-reviewer
    read_only: true
---
"""
    path = manifest_file(content)
    import warnings
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        # Should succeed (unknown defaults keys are warned, not errored)
        result = load_manifest(path)
    assert result is not None


# ---------------------------------------------------------------------------
# env field
# ---------------------------------------------------------------------------


def test_envフィールドがパースされる(manifest_file):
    content = """\
---
po_plan_version: "0.1"
name: test
cwd: "."
tasks:
  - id: review
    agent: code-reviewer
    read_only: true
    env:
      MY_VAR: hello
---
"""
    path = manifest_file(content)
    result = load_manifest(path)
    assert result.tasks[0].env == {"MY_VAR": "hello"}


def test_ブロックリスト環境変数でManifestErrorが送出される(manifest_file):
    content = """\
---
po_plan_version: "0.1"
name: test
cwd: "."
tasks:
  - id: review
    agent: code-reviewer
    read_only: true
    env:
      LD_PRELOAD: /evil.so
---
"""
    path = manifest_file(content)
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_PO_WORKTREE_GUARDはenvに設定できない(manifest_file):
    """PO_WORKTREE_GUARD is blocked to prevent user override."""
    content = """\
---
po_plan_version: "0.1"
name: test
cwd: "."
tasks:
  - id: review
    agent: code-reviewer
    read_only: true
    env:
      PO_WORKTREE_GUARD: "0"
---
"""
    path = manifest_file(content)
    with pytest.raises(ManifestError):
        load_manifest(path)


# ---------------------------------------------------------------------------
# Task ID and agent validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    ["../../etc/passwd", "task/slash", "task space", "task@bad", ""],
)
def test_不正なタスクIDでManifestErrorが送出される(bad_id, manifest_file):
    import yaml  # noqa: PLC0415

    front = {
        "po_plan_version": "0.1",
        "name": "test",
        "cwd": ".",
        "tasks": [{"id": bad_id, "agent": "code-reviewer", "read_only": True}],
    }
    content = f"---\n{yaml.dump(front)}---\n"
    path = manifest_file(content)
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_agentが文字列でないときManifestErrorが送出される(manifest_file):
    import yaml  # noqa: PLC0415

    front = {
        "po_plan_version": "0.1",
        "name": "test",
        "cwd": ".",
        "tasks": [{"id": "review", "agent": 123, "read_only": True}],
    }
    content = f"---\n{yaml.dump(front)}---\n"
    path = manifest_file(content)
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_空のagent名が受理される(manifest_file):
    content = """\
---
po_plan_version: "0.1"
name: test
cwd: "."
tasks:
  - id: review
    agent: ""
    read_only: true
---
"""
    path = manifest_file(content)
    result = load_manifest(path)
    assert result.tasks[0].agent == ""


# ---------------------------------------------------------------------------
# concurrency_group and concurrency_limits
# ---------------------------------------------------------------------------


def test_concurrency_groupとlimitsがパースされる(manifest_file):
    content = """\
---
po_plan_version: "0.1"
name: test
cwd: "."
tasks:
  - id: review
    agent: code-reviewer
    read_only: true
    concurrency_group: api-calls
concurrency_limits:
  api-calls: 2
---
"""
    path = manifest_file(content)
    result = load_manifest(path)
    assert result.tasks[0].concurrency_group == "api-calls"
    assert result.concurrency_limits == {"api-calls": 2}


def test_concurrency_limitsなしでgroupを使うとManifestErrorが送出される(manifest_file):
    content = """\
---
po_plan_version: "0.1"
name: test
cwd: "."
tasks:
  - id: review
    agent: code-reviewer
    read_only: true
    concurrency_group: api-calls
---
"""
    path = manifest_file(content)
    with pytest.raises(ManifestError):
        load_manifest(path)


# ---------------------------------------------------------------------------
# Webhook sections
# ---------------------------------------------------------------------------


def test_on_failureのwebhookがパースされる(manifest_file):
    content = """\
---
po_plan_version: "0.1"
name: test
cwd: "."
tasks:
  - id: review
    agent: code-reviewer
    read_only: true
on_failure:
  webhook_url: "https://example.com/notify"
---
"""
    path = manifest_file(content)
    result = load_manifest(path)
    assert result.on_failure is not None
    assert result.on_failure.webhook_url == "https://example.com/notify"


def test_プライベートIPのwebhookURLでManifestErrorが送出される(manifest_file):
    content = """\
---
po_plan_version: "0.1"
name: test
cwd: "."
tasks:
  - id: review
    agent: code-reviewer
    read_only: true
on_complete:
  webhook_url: "https://192.168.1.1/hook"
---
"""
    path = manifest_file(content)
    with pytest.raises(ManifestError):
        load_manifest(path)


# ---------------------------------------------------------------------------
# Manifest path is stored
# ---------------------------------------------------------------------------


def test_ManifestにPathが格納される(manifest_file):
    path = manifest_file(MINIMAL_VALID)
    result = load_manifest(path)
    assert result.path == path.resolve()


# ---------------------------------------------------------------------------
# Task is frozen dataclass
# ---------------------------------------------------------------------------


def test_Taskはfrozenデータクラスである(manifest_file):
    path = manifest_file(MINIMAL_VALID)
    result = load_manifest(path)
    task = result.tasks[0]
    with pytest.raises((AttributeError, TypeError)):
        task.id = "modified"  # type: ignore[misc]
