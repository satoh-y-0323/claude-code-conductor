# Changelog

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
