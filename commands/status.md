---
description: Show the lifecycle status of every milestone, spec and plan
allowed-tools: Bash(python:*)
---

!`python "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" status --dir docs/superpowers`

Summarise the state above: which documents sit at a human gate waiting on a
decision, and which are mid-flight. Do not act on any of them — this command
reads, it does not decide.
