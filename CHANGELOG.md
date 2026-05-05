# Changelog

## [0.5.1] - 2026-05-05

### Added
- New `/pattern-status` skill (read-only) that visualizes `patterns.json`:
  trust score distribution, promotion candidates, expiry-near patterns,
  and already-promoted patterns. Use it before `/promote-pattern` to
  inspect the current state without modifying the file.
- `session_utils.append_checkpoint(session_file, label, summary)` helper
  for milestone state snapshots. Safely handles non-existent and empty
  session files by writing the template before appending. Used by both
  `wave-execution` (success/skipped-failure waves) and `pre_compact.py`.
- `CLAUDE.md` "When to use /compact" guideline — decision flow for
  `/compact` vs session restart, aimed at clarifying the choice for
  power users (restart) vs casual users (`/compact`).

### Changed
- `pre_compact.py` now emits `hookSpecificOutput.additionalContext` to
  inject KEEP/DISCARD save instructions into Claude's context just
  before compaction. Previously the hook only wrote a timestamp marker
  to the session file. Claude now writes important state (remaining
  tasks, key decisions, resolved gotchas) to the session before the
  context shrinks.
- `wave-execution` Step 2-F now records a checkpoint block to the
  session file on every wave completion (success or skipped failure),
  in addition to flipping `[ ]` → `[x]`. This gives a time-stamped
  trail of milestones for `/init-session` and `/pattern-status`.
- Added `WebSearch` to `permissions.allow` in `settings.json` so
  research subagents can use it without prompting.

## [0.5.0] - 2026-05-05

### Added
- New `systematic-debugger` agent: dedicated investigation phase for
  root-cause analysis and pattern matching when `developer` gets stuck.
  Runs in a separate phase from implementation, preserving C3's
  multi-agent separation.
- `developer` agent: Stuck Signal — after 3 failed attempts at the
  same problem, write a `debug-needed` report and stop, letting the
  orchestrator dispatch `systematic-debugger`.
- `dev-workflow` D-2.5 and `worktree-tdd-workflow` Step 3.5: detect
  Stuck Signal, run systematic-debugger, re-invoke developer with the
  debug analysis injected.
- `tester` agent: Verify RED rule — before handing off to developer,
  confirm tests fail for the right reason (missing feature, not
  syntax errors), and document the verification in the test-report.
- `developer` agent: minimal code principle — Green phase writes
  only what tests require, no premature extensions or speculative
  abstractions.
- `developer` agent: lightweight verification before tester handoff
  (syntax/build check) drawn from superpowers' verification ideas
  while keeping C3's agent-separated structure.

## [0.4.0] - 2026-05-04

### Breaking Changes
- **Skill renamed**: `/review` → `/code-review` — avoids conflict with the
  official Claude Code `/review [PR]` command (which reviews pull requests).
  C3's `/code-review` runs `code-reviewer` + `security-reviewer` agents
  as dev-workflow phase E.
- **Skill renamed**: `/mcp` → `/mcp-config` — avoids conflict with the
  official Claude Code `/mcp` command (which manages live MCP connections).
  C3's `/mcp-config` manages `mcpServers` entries in `.claude/settings.json`.
- **Skill structure**: `commands/` directory migrated to `skills/` following
  the Claude Code 2026 skills standard. All skills are now under
  `.claude/skills/{name}/SKILL.md` with YAML frontmatter.

### Added
- `stop.py`: Records `last_assistant_message` from Stop hook payload into the
  session file's 事実ログ section as `- 最終応答: ...` (truncated at 500 chars).
  The next session's init-session can now read what Claude last accomplished.
- `session_utils.py`: New shared module exporting `SESSIONS_DIR`,
  `SESSION_JSON_MARKER`, `is_worktree()`, and `create_session_template()`.
  Eliminates duplicate definitions across `stop.py` and `pre_compact.py`.

### Fixed
- `settings.local.json` had a duplicate `hooks` section identical to
  `settings.json`, causing all hooks to fire twice per event. Removed.
- Hook commands now use `"$CLAUDE_PROJECT_DIR/.claude/hooks/…"` (absolute
  path via env var) so hooks remain findable even after `cd` changes CWD.
  The `cd` block in `pre_tool.py` has been removed as it is no longer needed.
- `UserPromptSubmit` hook for `statusline.py` removed — the hook input has no
  `context_window` field, so it always displayed `0%`. The `statusLine`
  setting handles display correctly on its own.
- `stop.py`: Reads and respects `stop_hook_active` flag — skips processing
  on re-entrant Stop calls to prevent duplicate session updates.
- `pre_compact.py`: Uses `__file__`-based paths instead of `os.getcwd()` so
  the session file is always found regardless of working directory.
- `pre_compact.py`: Records `trigger` (manual/auto) and `context_items_before`
  in checkpoint output for richer context.
- `stop.py`: Sanitizes surrogate characters (`\udc80`–`\udcff`) in
  `last_assistant_message` before writing to avoid `UnicodeEncodeError`.
- `settings.json`: Added missing `Write`/`Edit` permissions for
  `.claude/reports/archive/**`, `.claude/rules/**`, `.claude/settings.json`,
  `Edit(.claude/memory/**)`, `Edit(.claude/rules/**)`, `Edit(.claude/skills/**)`.
- Bash permissions for hook scripts now include both relative-path and
  `$CLAUDE_PROJECT_DIR`-prefixed forms for full coverage.

## [0.3.4] - 2026-05-02

### Security
- `pre_tool.py`: Hardened `rm -rf` detection — flags are now collected
  only from tokens immediately following the `rm` command, preventing
  false-negatives when earlier commands in a pipeline carry `-r`/`-f`
  flags (e.g. `grep -rf … && rm file`). Also added detection of
  `--recursive --force` long-option combinations.
- `pre_tool.py`: Extended `cd` block to cover subshell `$()`, backtick,
  newline, and `eval "cd …"` bypass paths that the previous regex missed.
- `stop.py`: Field whitelist on `patterns.json` writes — only
  allow-listed keys are written and `promoted` can never be injected
  via a session JSON block. Added `MAX_ID_LENGTH = 64` and
  `MAX_DESCRIPTION_LENGTH = 500` guards.
- `manifest.py`: `writes`, `agent`, and `concurrency_group` values in
  generated wave manifests are now passed through `_yaml_quote` to
  prevent newline injection into the ephemeral YAML.

### Fixed
- `run.py`: Replaced `assert process.stderr is not None` (silently
  removed by `-O` optimised bytecode) with an explicit
  `if … is None: raise RuntimeError(…)` guard.
- `pre_compact.py`: Replaced `os.path.exists()` + `open('w')` TOCTOU
  with `open('x')` + `except FileExistsError` — matches the pattern
  already used in `stop.py`.
- `stop.py`: `update_patterns` called `os.listdir` inside the pattern
  loop, causing O(N×M) file-system reads. A single `_build_sessions_by_date`
  call outside the loop reduces this to O(N+M).
- `manifest.py`: Removed dead branch `rest is None` (always `False`
  for `str.partition` return values). Double-quoted YAML scalars now
  handle `\\`, `\"`, `\n`, `\t`, and `\r` escape sequences.
- `cli_po.py`: `run-wave` temp manifest now uses `tempfile.NamedTemporaryFile`
  (unpredictable name) and is deleted in a `try/finally` block regardless
  of outcome.
- `cli_list.py`: `OSError` when reading a file in `_summary` is caught
  and returns `"(unreadable)"` instead of propagating and breaking the
  entire listing.
- `run.py`: Replaced `__import__("sys").stderr` idiom with `sys.stderr`.
- `manifest.py`: `validate_manifest` local `version` renamed to
  `plan_version` to avoid shadowing a potential future import.
  `build_wave_manifest_text` accepts an optional `waves` argument to
  avoid recomputing the wave graph when the caller already has it.

### Changed
- `pre_compact.py` / `stop.py`: `SESSION_JSON_MARKER = 'C3:SESSION:JSON'`
  constant is now defined in both files — eliminates the hard-coded
  string in `pre_compact.py` and makes the two files consistent.
- `stop.py`: Import block reordered to comply with PEP 8 (all imports
  before module-level statements).
- `validate_skill_change.py`: Early-exit paths changed from
  `sys.exit(0)` to `return`; `__main__` block uses
  `sys.exit(main() or 0)` pattern, consistent with `pre_tool.py`.
- `clear_file_history.py`: Added `os.path.islink` pre-check so
  symbolic links are removed with `os.unlink` rather than
  `shutil.rmtree`, preventing accidental recursive deletion of a
  symlink target on some platforms.
- `worktree_guard.py`: Removed noisy `stderr` log on every tool call
  when `PO_WORKTREE_GUARD` is unset; the hook now exits silently when
  the guard is disabled.
- Template sync: all seven files under `src/c3/_template/.claude/hooks/`
  are now identical to their counterparts under `.claude/hooks/`, so
  `c3 init` / `c3 update` distribute the corrected implementations.

## [0.3.3] - 2026-05-01

### Fixed
- `__pycache__/` and `.pyc`/`.pyo` artefacts no longer ship in the
  wheel and no longer leak into user projects via `c3 init` /
  `c3 update`. Previous releases shipped Python bytecode caches at
  any path under `.claude/` whenever the dev had run hooks before
  the build (notably `.claude/hooks/__pycache__/*.pyc`). The
  `should_skip` predicate in both `c3._excludes` and `hatch_build.py`
  now short-circuits on any path component named `__pycache__` or
  any `.pyc` / `.pyo` suffix.
- `tests/test_excludes.py`: regression test
  `test_excludes_pycache_at_any_depth` asserts the new behaviour at
  multiple directory depths and confirms `.py` source files remain
  framework files.

## [0.3.2] - 2026-05-01

### Fixed
- `c3 update` and `c3 init` no longer overwrite the user's
  `.claude/settings.local.json`. This file is per-machine permission
  state that Claude Code edits when granting tool permissions; the
  bundled template should never replace it. `settings.local.json`
  is now in `EXCLUDE_PATTERNS` in both `c3._excludes` (used at
  runtime by `c3 init` / `c3 update`) and `hatch_build.py` (used at
  wheel-staging time so the file no longer ships in the wheel at
  all). The companion `settings.json` (project-shared permissions)
  remains a framework file and continues to be updated by
  `c3 update`.
- `tests/test_excludes.py`: regression test asserting the new
  exclusion.

## [0.3.1] - 2026-05-01

### Docs
- Operational rules captured from a 17-tasks / 7-stages C3+PO verification
  run in `c3_pip_test`:
  - `.claude/skills/wave-execution.md`: new **Step 0-pre** that requires a
    clean working tree before invoking PO (PO's auto-merge re-creates
    same-named files in worktrees and conflicts on dirty main — most
    commonly via `.claude/settings.local.json`, which Claude Code auto-edits
    when granting permissions). Adds an explicit **"do not git
    add/commit/push"** rule to case A-2 Agent-tool prompts (a developer
    sub-agent was committing implementation files while leaving Red tests
    and test-reports untracked). Adds an **auto-merge conflict (exit code
    3) recovery** sub-section under case B with a selective-checkout
    procedure that rescues only declared `writes` and discards worktree-
    side edits to surrounding files. Adds a per-wave commit reminder under
    Step 2-F. Notes PO's hardcoded 15-minute per-task timeout
    (`_INTERNAL_TIMEOUT_SEC = 900`, no manifest-level override) so the
    parent Claude can route exit-code-1 timeouts back to planner sizing
    rather than agent debugging.
  - `.claude/agents/planner.md`: documents the `depends_on: []` pitfall
    (`c3 po dry-run` rejects empty arrays — omit the field instead) and
    the `writes` collision detection. Adds a per-task time budget rule
    (≤15 min, matching PO's internal timeout) with a self-check item.
    Adds an **"alternating parallel/serial pattern"** section that
    authorises ordering `depends_on` between stages when the user
    explicitly wants intermediate review/sync points, while preserving
    in-stage parallelism ≥ 2.
  - `.claude/docs/parallel-orchestra-manifest.md`: adds an "alternating
    parallel/serial pattern" section describing the structure with a
    pointer to the planner rule.

No code changes — `c3 update` after `pip install -U claude-code-conductor`
brings these into existing projects.

## [0.3.0] - 2026-04-30

### Changed (breaking)
- `/develop` now auto-detects YAML frontmatter on the latest plan-report and
  switches between two modes:
  - **frontmatter present** → new "C3 main + PO spot" workflow. C3 walks the
    DAG wave-by-wave, asks for user approval before each wave, and dispatches
    each wave to the right runner: solo waves run on the C3 host (Agent-tool
    spawn for `code-reviewer` / `developer` / `tester`, parent-Claude persona
    adoption for `tdd-develop` to avoid the depth-1 nested-spawn limit), and
    multi-task waves are delegated to parallel-orchestra via an ephemeral
    wave-only manifest under `.claude/tmp/`.
  - **no frontmatter** → legacy D-1〜D-5 sequential TDD ceremony, unchanged.
- The previous "PO 全委譲" model (D-0 two-choice prompt) and
  `.claude/skills/parallel-execution.md` are removed. The new flow is
  documented in `.claude/skills/wave-execution.md`.

### Added
- `c3 po waves <plan-report>` — prints the topological wave decomposition of
  a manifest as JSON. Used by `wave-execution.md` to drive the per-wave loop.
- `c3 po run-wave <plan-report> --wave-index N` — generates a wave-only
  ephemeral manifest under `.claude/tmp/po-manifest-wave-{N}-{ts}.md` and
  hands it to parallel-orchestra.
- `c3.po.manifest.compute_waves(frontmatter)` — Kahn's-algorithm topological
  wave decomposition. Detects cycles, unknown dependency ids, and duplicate
  task ids.
- `c3.po.manifest.build_wave_manifest_text(frontmatter, wave_index)` —
  emits a parseable plan-report Markdown for one wave, dropping `depends_on`
  and webhook fields and decorating the manifest name with ` - wave N`.
- `tests/test_po_waves.py` (16 tests) and `tests/test_cli_po.py` (5 tests)
  covering wave decomposition, ephemeral-manifest generation, CLI exit
  codes, and frontmatter round-trip.

### Notes
- The persona-adoption pattern for `tdd-develop` in solo waves is the direct
  consequence of Claude Code's depth-1 nested-spawn limit (a sub-agent
  spawned via the Agent tool cannot itself spawn another sub-agent). For
  agents that internally spawn sub-agents (today: `tdd-develop`), the parent
  Claude reads the agent definition and adopts its persona instead. Other
  agents (`code-reviewer`, `security-reviewer`, `developer`, `tester`) keep
  using the standard Agent-tool spawn path.

## [0.2.4] - 2026-05-01

### Changed
- `planner` agent now produces plan-reports designed for actual parallel
  execution. Previously the agent only knew "emit YAML frontmatter"; without
  rules for how to design the dependency graph it tended to write conservative
  serial chains where every task depended on the previous one, defeating the
  point of parallel-orchestra. Added a "並列実行のための設計指針" section
  with eight concrete rules:
  - depends_on only for true dependencies (not "just to be safe")
  - serialization self-check: chain length ≦ tasks/2
  - reviews go to the end via depends_on covering all dev tasks
  - decompose at file/module boundaries, not function-level or module-level
  - 1 TDD task = test + production + correction loop (do not split)
  - default granularity: file / feature
  - `writes` field is mandatory for collision detection
  - duplicate writes must be merged, sequenced via depends_on, or grouped
- `.claude/docs/parallel-orchestra-manifest.md`: example expanded to three
  dev tasks + a depends-on-all reviewer (showing real parallelism), plus
  inline comments and an "アンチパターン" section that calls out
  serialized chains, splitting TDD into separate tester/developer tasks,
  and empty/duplicate `writes` fields.

## [0.2.3] - 2026-05-01

### Added
- `name` field on every agent definition under `.claude/agents/*.md`. The
  `description` field was already present on all agents, so this fills the
  remaining frontmatter gap. Values match the file stem (e.g. `architect`,
  `tdd-develop`).

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
