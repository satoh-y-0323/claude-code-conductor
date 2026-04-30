"""Regression tests for ``c3.paths._resolve_dev_template``.

The dev fallback must only fire when the package is loaded from
``<root>/src/c3/``. A venv that happens to live inside the C3 source tree
must NOT make a wheel-installed copy resolve to the (dirty) live ``.claude/``.
"""

from __future__ import annotations

from pathlib import Path

from c3.paths import _resolve_dev_template


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def test_dev_fallback_matches_when_loaded_from_src(tmp_path: Path):
    project = tmp_path / "claude-code-conductor"
    here = project / "src" / "c3" / "paths.py"
    _touch(here)
    _touch(project / "pyproject.toml")
    (project / ".claude").mkdir()

    result = _resolve_dev_template(here)
    assert result == project / ".claude"


def test_dev_fallback_skips_when_loaded_from_site_packages(tmp_path: Path):
    """The bug from 0.2.0: venv inside the C3 source tree wrongly returned dev .claude/.

    Even though walking up from site-packages eventually hits a directory with
    .claude/ + pyproject.toml, we must not return it for a wheel install.
    """
    project = tmp_path / "claude-code-conductor"
    venv_pkg = project / ".venv" / "Lib" / "site-packages" / "c3" / "paths.py"
    _touch(venv_pkg)
    _touch(project / "pyproject.toml")
    (project / ".claude").mkdir()

    result = _resolve_dev_template(venv_pkg)
    assert result is None, (
        "wheel install in a venv inside the C3 source tree must NOT resolve "
        "to the dev .claude/"
    )


def test_dev_fallback_skips_when_no_claude_dir(tmp_path: Path):
    project = tmp_path / "claude-code-conductor"
    here = project / "src" / "c3" / "paths.py"
    _touch(here)
    _touch(project / "pyproject.toml")
    # no .claude/ dir
    assert _resolve_dev_template(here) is None


def test_dev_fallback_skips_when_no_pyproject(tmp_path: Path):
    project = tmp_path / "claude-code-conductor"
    here = project / "src" / "c3" / "paths.py"
    _touch(here)
    (project / ".claude").mkdir()
    # no pyproject.toml
    assert _resolve_dev_template(here) is None
