---
description: Close a verified slice — merges its branch and re-syncs every milestone brief
argument-hint: [path-to-plan]
allowed-tools: Bash(python:*)
---

!`python "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" set-status --file "$1" --status VERIFIED_CLOSED`

The transition above has already been attempted; its output is included. It
does more than set a status: it merges the slice's feature branch and refreshes
the track checkbox in every milestone brief that lists the slice.

If it succeeded, report which briefs were refreshed, if any.

If it failed, the orchestrator refused and its message says why. Two refusals
are common: `VERIFIED_CLOSED` is legal only from `EXECUTION_COMPLETE`, and it
must target the plan file rather than the design spec. Report the reason. Do
not retry with a different status, a different file, or a direct edit of the
frontmatter.
