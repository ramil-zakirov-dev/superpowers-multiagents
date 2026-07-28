---
description: Create a milestone brief in PRD form
argument-hint: [milestone-id] [title]
allowed-tools: Bash(python:*), Bash(echo:*), Bash(cut:*)
---

!`python "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" milestone new --id "$(echo "$ARGUMENTS" | cut -d' ' -f1)" --title "$(echo "$ARGUMENTS" | cut -d' ' -f2-)"`

The brief above has already been created; its output is included. The id is the
first word of the arguments and the title is everything after it, so the title
needs no quoting.

The brief ships with eight empty PRD sections. It cannot reach
`MILESTONE_ACTIVE` until every one of them is filled — that check runs at the
next gate, not here.

If the creation failed, report the reason. Do not create the file by hand: the
section headings are a contract the section check reads.
