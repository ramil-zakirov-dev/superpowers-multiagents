---
description: Close a verified slice — merges its branch and re-syncs every milestone brief
argument-hint: [path-to-plan] [--skip-merge]
allowed-tools: Bash(python:*)
---

!`python "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" set-status --file "$1" --status VERIFIED_CLOSED $2`

The transition above has already been attempted; its output is included. It
does more than set a status: it merges the slice's feature branch and refreshes
the track checkbox in every milestone brief that lists the slice.

If it succeeded, report which briefs were refreshed, if any.

If it failed, the orchestrator refused and its message says why. Three refusals
are common: `VERIFIED_CLOSED` is legal only from `EXECUTION_COMPLETE`; it must
target the plan file rather than the design spec; and there must be a branch to
merge. Report the reason. Do not retry with a different status, a different
file, or a direct edit of the frontmatter.

The third refusal names a branch that does not exist. That is a question for
the human, not a thing to route around: either the `slice_id` is wrong, or the
slice landed fast-forward and its branch was tidied away. Only in the second
case does `--skip-merge` apply, and it applies as an assertion — nothing in the
orchestrator can confirm the work is really home. Ask before passing it.
