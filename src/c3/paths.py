"""Path resolution helpers for C3.

Two responsibilities:
- locate the user project's ``.claude/`` directory by walking upward from a cwd
- locate the bundled ``.claude/`` template (works for both regular installs and
  editable installs)
"""

from __future__ import annotations

from importlib.resources import files as _resource_files
from pathlib import Path


def claude_root_for(start: Path | str) -> Path | None:
    """Walk up from ``start`` and return the nearest directory containing ``.claude/``.

    Returns ``None`` if no ``.claude/`` is found before the filesystem root.
    """
    here = Path(start).resolve()
    candidates = [here, *here.parents]
    for candidate in candidates:
        if (candidate / ".claude").is_dir():
            return candidate
    return None


def templates_dir() -> Path:
    """Return the path to the bundled ``.claude/`` template.

    Resolution order:

    1. Editable / source install: this module is being loaded directly from
       ``<root>/src/c3/paths.py`` (i.e. via ``pip install -e .`` or
       ``PYTHONPATH=src``) AND ``<root>/.claude/`` and ``<root>/pyproject.toml``
       both exist. Use the live ``.claude/`` so dev edits are reflected
       immediately. The check is anchored on the ``src/c3/`` ancestry so that a
       venv that happens to live *inside* the C3 source tree does not trigger
       this branch when a wheel-installed copy of the package is in use.
    2. Wheel install: ``importlib.resources.files("c3") / "_template" / ".claude"``.
       This is the path produced by hatchling's build hook + ``force-include``
       during ``pip install``.

    Raises ``FileNotFoundError`` if neither location resolves.
    """
    dev = _resolve_dev_template(Path(__file__).resolve())
    if dev is not None:
        return dev

    try:
        bundled = _resource_files("c3").joinpath("_template", ".claude")
        bundled_path = Path(str(bundled))
        if bundled_path.is_dir():
            return bundled_path
    except (ModuleNotFoundError, FileNotFoundError, AttributeError):
        pass

    raise FileNotFoundError(
        "Could not locate the bundled .claude/ template. "
        "Reinstall claude-code-conductor with "
        "`pip install --force-reinstall claude-code-conductor`."
    )


def _resolve_dev_template(here: Path) -> Path | None:
    """Return the live ``.claude/`` only when ``here`` is loaded from ``<root>/src/c3/``.

    Split out so that the resolution logic is testable with synthetic paths.
    """
    if here.parent.name != "c3" or here.parent.parent.name != "src":
        return None
    project_root = here.parent.parent.parent
    candidate = project_root / ".claude"
    if candidate.is_dir() and (project_root / "pyproject.toml").is_file():
        return candidate
    return None
