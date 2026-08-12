---
description: Dispatch a configured agent role against a document
argument-hint: [role] [path-to-file]
allowed-tools: Bash(python:*), Bash(echo:*), Bash(cut:*)
---

!`python "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" dispatch-agent --role "$(echo "$ARGUMENTS" | cut -d' ' -f1)" --file "$(echo "$ARGUMENTS" | cut -s -d' ' -f2-)"`

Which roles exist is the project's `.superpowers/agents.yaml` to say, not this
file's — a name that is wrong, or removed with `<role>: null`, is refused above
by an error naming every role the project really defines.

The dispatch above has already been attempted; its output is included. A
successful dispatch launches a supervised background agent. The supervisor, not
this session, sets the resulting status when that agent exits.

If it failed, report the reason. Do not retry against a different file or role,
and do not set the target status by hand.

Branches are the dispatcher's, not yours: it derives `feat/<slice_id>` and
`.worktrees/<slice_id>` from the document's frontmatter. Never create a slice
branch by hand — `feature/<slice_id>` is a different branch that `close-slice`
will not merge.

An isolated role's worktree is created from HEAD, so the document must be
committed on the branch checked out in the main working tree before dispatching.
A refusal naming an uncommitted file means exactly that: commit it there and
dispatch again.

A `[Provision Gate]` refusal is about `worktree.copy` — the untracked files, a
`.env` typically, that the project declares an isolated agent needs. Report what
it says and stop. Do not copy the file into the worktree yourself, do not drop
the entry to get past the gate, and do not dispatch a non-isolated role instead:
each of those hands the agent an environment nobody checked, which is the
failure the gate exists to prevent.
