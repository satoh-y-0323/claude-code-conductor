#!/usr/bin/env python3
"""PostToolUse hook: append Agent tool invocations to agent-audit.log."""

import json
import sys
import os
from datetime import datetime

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_DIR = os.path.dirname(_HOOKS_DIR)
AUDIT_LOG = os.path.join(_CLAUDE_DIR, 'memory', 'agent-audit.log')


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    if payload.get('tool_name') != 'Agent':
        sys.exit(0)

    tool_input = payload.get('tool_input', {})
    subagent_type = tool_input.get('subagent_type', 'general-purpose')
    description = tool_input.get('description', '(no description)')
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
    with open(AUDIT_LOG, 'a', encoding='utf-8') as f:
        f.write(f'[{now}] Agent started: {subagent_type} — {description}\n')

    sys.exit(0)


if __name__ == '__main__':
    main()
