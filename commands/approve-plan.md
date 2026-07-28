---
description: Record the human's approval of an implementation plan
argument-hint: [path-to-plan]
allowed-tools: Bash(python:*)
---

!`python "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" set-status --file "$1" --status PLAN_APPROVED`

The transition above has already been attempted; its output is included.

If it succeeded, say so in one line. What happens next is governed by the
operating procedure, not by this command.

If it failed, the orchestrator refused and its message says why. Report the
reason. Do not retry with a different status, a different file, or a direct
edit of the frontmatter.
