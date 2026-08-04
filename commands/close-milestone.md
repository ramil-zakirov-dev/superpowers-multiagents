---
description: Close a milestone whose tracks are all complete
argument-hint: [path-to-brief]
allowed-tools: Bash(python:*)
---

!`python "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" set-status --file "$ARGUMENTS" --status MILESTONE_CLOSED`

The transition above has already been attempted; its output is included.

If it succeeded, say so in one line.

If it failed, the orchestrator refused and its message says why. The usual
reason is an open slice: every slice listed in every track must be
`VERIFIED_CLOSED` first, and the refusal names each one that is not. Report
them. Do not retry with a different status, a different file, or a direct edit
of the frontmatter.
