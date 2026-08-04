---
description: Approve a written milestone brief and make it active
argument-hint: [path-to-brief]
allowed-tools: Bash(python:*)
---

!`python "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" set-status --file "$ARGUMENTS" --status MILESTONE_ACTIVE`

The transition above has already been attempted; its output is included.

If it succeeded, say so in one line. What happens next is governed by the
operating procedure, not by this command.

If it failed, the orchestrator refused and its message says why. The usual
reason is an unfilled PRD section: the brief cannot leave `MILESTONE_DRAFT`
until all eight are non-empty, and the refusal lists every one that is missing.
Report them. Do not retry with a different status, a different file, or a
direct edit of the frontmatter.
