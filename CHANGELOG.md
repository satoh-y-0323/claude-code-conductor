# Changelog

## [0.2.2] - 2026-05-01

### Fixed
- `c3 po run` no longer crashes on Windows when parallel-orchestra emits UTF-8
  characters on stderr. The `subprocess.Popen` call previously paired
  `text=True` with no explicit `encoding`, so Python decoded the pipe with the
  platform's locale (cp932 on JP Windows) and raised `UnicodeDecodeError` on
  the first non-ASCII byte. The Popen now passes `encoding="utf-8",
  errors="replace"` so PO's output decodes regardless of locale and a stray
  byte cannot tear down the stream mid-run.

### Added
- `tests/test_po_run.py::test_run_manifest_decodes_stderr_as_utf8` —
  regression test that asserts the Popen kwargs include `encoding="utf-8"`
  and `errors="replace"`.

## [0.2.1] - 2026-05-01

### Fixed
- `c3 init` no longer copies personal/working files when run against the live
  development tree. Two regressions in 0.2.0 caused this:
  1. `templates_dir()` walked up from `__file__` looking for any ancestor with
     `.claude/` + `pyproject.toml`. A wheel install in a venv that happened to
     live inside the C3 source tree (e.g. `claude-code-conductor/.venv/...`)
     therefore resolved to the dirty live `.claude/` instead of the bundled
     `_template/`. The dev fallback is now anchored to `<root>/src/c3/` ancestry
     so site-packages-loaded copies always use `importlib.resources`.
  2. `_copytree` did not apply the same exclusion rules as the build hook,
     so even legitimate editable installs (which intentionally serve the live
     `.claude/`) could leak personal files. `cli_init` and `cli_update` now
     share `c3._excludes` with `hatch_build.py`.

### Added
- `src/c3/_excludes.py` — single source of truth for excluded paths
  (reports/, memory/sessions/, memory/patterns.json, docs/decisions.md, etc.).
- Regression tests:
  - `tests/test_paths.py` — `_resolve_dev_template` rejects site-packages paths.
  - `tests/test_excludes.py` — KEEP_PATTERNS override EXCLUDE_PATTERNS.
  - `tests/test_cli_init.py::test_init_excludes_personal_files` — init does not
    leak personal files even when given a "dirty" template tree.

## [0.2.0] - 2026-05-01

### Added
- PyPI distribution as `claude-code-conductor` (`pip install claude-code-conductor`)
- `c3` command-line interface with subcommands:
  - `c3 init` — scaffold `.claude/` into a project (refuses to overwrite without `--force`)
  - `c3 update` — refresh framework files; preserves user-managed files (reports/, memory/sessions/, founding docs)
  - `c3 list-agents` / `list-skills` / `list-commands` — inspect installed assets
  - `c3 doctor` — diagnose `.claude/`, `settings.json`, claude binary, parallel-orchestra availability
  - `c3 po dry-run <plan-report>` / `c3 po run <plan-report>` — invoke parallel-orchestra via subprocess
- Optional `parallel-orchestra` integration (loose coupling; PO is *not* in dependencies):
  - Runtime detection via `shutil.which` + `importlib.metadata`
  - `.claude/skills/parallel-execution.md` skill orchestrates D-0 → preflight → user approval → run → report
  - `planner` agent now emits required YAML frontmatter on plan-reports per `.claude/docs/parallel-orchestra-manifest.md`
  - `/develop` Phase D adds **D-0: 実行モード選択** (TDD 逐次 vs PO 並列)

### Changed
- Recommended install path is now `pip install claude-code-conductor` + `c3 init`. Manual `cp -r .claude/` still documented as an alternative.
- `worktree_guard.py` docstring: `C3_WORKTREE_GUARD` → `PO_WORKTREE_GUARD` (matches the implementation).

### Internal
- `src/c3/` package layout (hatchling build backend)
- Hatch custom build hook stages distributable subset of `.claude/` into `src/c3/_template/.claude/`
- Test suite under `tests/` (28 tests including loose-coupling guards and an opt-in `parallel-orchestra --dry-run` smoke)

## [0.1.0] - 2026-04-29

### Added
- Initial Claude Code Conductor (C3) framework structure
- Multi-agent orchestration with parent-Claude-persona pattern
- Structured approval flow using `AskUserQuestion` tool
- `/init-session` — session initialization and state restoration
- `/start` — development workflow entry (interviewing → design → planning)
- `/develop` — implementation phase with TDD (tester → developer → tester)
- `/review` — review phase (code-reviewer + security-reviewer)
- `/promote-pattern` — promote candidate patterns to rules/skills
- `/doc` — architecture diagram and documentation generation
- `/mcp` — MCP server management (add / list / remove)
- `/extract-lib` — cross-project common code extraction and library design
- Code review checklist (`rules/code-review-checklist.md`)
- Security review checklist (`rules/security-review-checklist.md`)
- Hooks: `pre_tool.py`, `stop.py`, `log_agent.py`, `validate_skill_change.py`, `pre_compact.py`, `statusline.py`
- Session memory system with pattern trust scoring

### Fixed
- Force UTF-8 encoding on stdout/stderr for all hooks (Windows compatibility)
- Block `cd` commands in `pre_tool` hook to prevent CWD drift that breaks hook resolution
- Exclude all report/tmp file types from git tracking
