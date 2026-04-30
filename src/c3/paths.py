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

    1. Dev source: walk up from this file looking for a sibling ``.claude/``
       next to a ``pyproject.toml``. This makes editable installs (``pip install
       -e .``) reflect live edits to the source ``.claude/`` without rebuilding.
       In a wheel-installed environment the ``.py`` files live under
       ``site-packages/`` and this lookup naturally returns no match.
    2. Installed location: ``importlib.resources.files("c3") / "_template" / ".claude"``.
       This is the path produced by hatchling's build hook + ``force-include``
       during ``pip install``.

    Raises ``FileNotFoundError`` if neither location exists. This usually means
    the package was built without the template (manually copied source tree) -
    reinstall via pip to fix.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / ".claude"
        if candidate.is_dir() and (parent / "pyproject.toml").is_file():
            return candidate

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
