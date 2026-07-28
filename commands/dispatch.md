---
description: Dispatch a configured agent role against a document
argument-hint: [role] [path-to-file]
allowed-tools: Bash(python:*)
---

!`python "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" dispatch-agent --role "$1" --file "$2"`

Configured roles: `planner`, `executor`.

The dispatch above has already been attempted; its output is included. A
successful dispatch launches a supervised background agent. The supervisor, not
this session, sets the resulting status when that agent exits.

If it failed, report the reason. Do not retry against a different file or role,
and do not set the target status by hand.
