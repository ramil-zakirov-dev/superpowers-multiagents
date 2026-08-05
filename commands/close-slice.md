---
description: Close a verified slice — merges its branch and re-syncs every milestone brief
argument-hint: [path-to-plan] [--skip-merge]
allowed-tools: Bash(python:*), Bash(echo:*), Bash(cut:*)
---

!`python "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" set-status --file "$(echo "$ARGUMENTS" | cut -d' ' -f1)" --status VERIFIED_CLOSED $(echo "$ARGUMENTS" | cut -s -d' ' -f2-)`

The transition above has already been attempted; its output is included. It
does more than set a status: it merges the slice's feature branch, records the
closure on the slice's design spec as well as its plan, and refreshes the track
checkbox in every milestone brief that lists the slice.

If it succeeded, report which spec was closed and which briefs were refreshed,
if any.

If it failed, the orchestrator refused and its message says why. Three refusals
are common: `VERIFIED_CLOSED` is legal only from `EXECUTION_COMPLETE`; a spec
whose plan is not closed yet names that plan and its status, because the plan is
what to target; and there must be a branch to merge. Report the reason. Do not
retry with a different status, a different file, or a direct edit of the
frontmatter.

The third refusal names a branch that does not exist. That is a question for
the human, not a thing to route around: either the `slice_id` is wrong, or the
slice landed fast-forward and its branch was tidied away. Only in the second
case does `--skip-merge` apply, and it applies as an assertion — nothing in the
orchestrator can confirm the work is really home. Ask before passing it.

There is a fourth refusal, on a spec for a slice that has **no** plan: nothing
on disk says that slice was ever executed through the pipeline. `--skip-merge`
is the channel for that too, and it is the same kind of claim — a human stating
that the work shipped outside the pipeline. Ask before passing it there as well.
