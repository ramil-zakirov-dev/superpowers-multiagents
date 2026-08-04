---
description: Show the lifecycle status of every milestone, spec and plan
argument-hint: [--all]
allowed-tools: Bash(python:*)
---

!`python "${CLAUDE_PLUGIN_ROOT}/scripts/orchestrator.py" status --dir docs/superpowers $ARGUMENTS`

Summarise the state above: which documents sit at a human gate waiting on a
decision, and which are mid-flight. Do not act on any of them — this command
reads, it does not decide.

A row marked `INVALID` is not a state. It is a document naming a status or a
`kind` the machine does not have, so nothing will ever move it. Report those
separately, saying what each one claims: they need a human to correct the
frontmatter, and no gate will do it for them.

A trailing count of documents "not adopted into the pipeline" is not a backlog.
Those documents predate the pipeline or were never meant to enter it. Do not
propose backfilling frontmatter into them. `--all` lists them if asked.
