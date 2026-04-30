# Changelog

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
