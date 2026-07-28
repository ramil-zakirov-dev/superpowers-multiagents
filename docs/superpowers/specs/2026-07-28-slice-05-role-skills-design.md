---
slice_id: "slice-05-role-skills"
title: "Role skills: let a project reinforce a dispatched agent with named skills, without forking the default prompt"
status: DRAFT_SPEC
target_version: "2.4.0"
depends_on: []
---

# Slice 05 — Role Skills

## 1. Problem

A project can already tell a dispatched agent to use a named skill. It costs
the whole default prompt.

`agents.yaml` is deep-merged over `DEFAULT_CONFIG`, and `deep_merge`
(`scripts/config.py:83-96`) replaces scalars wholesale — a documented, deliberate
choice. `prompt_template` is a scalar. So a project that wants to append one
sentence about skills must copy the plugin's default prompt into its own file
and own that copy forever. When the plugin improves the wording in a later
version, every project that reached for skills is silently pinned to the old
text and never learns it moved.

That is the real defect. It is not "you cannot name a skill" — you can, today,
by forking. It is that the cheapest way to add reinforcement forces a project to
take permanent ownership of something it did not want to own.

A second, smaller defect follows from the same route. A skill named in a
free-text `prompt_template` that the harness cannot see produces nothing: the
agent reads a sentence about a skill that does not exist, shrugs, and proceeds.
The dispatch reports success, the reinforcement never happened, and there is no
signal anywhere. Silence is the worst available outcome, because the human goes
on believing the configuration works.

Every claim below was read out of the tree at `0520f51` or reproduced against
the real `opencode` binary on this machine, not inferred from documentation.

## 2. Scope

### 2.1 In

- An optional `skills:` key on an agent role in `agents.yaml`.
- Its rendering into the dispatched prompt, composing with `prompt_template`
  rather than replacing it.
- A new `HarnessAdapter.list_skills()` seam, implemented for OpenCode.
- A dispatch-time warning when a named skill is not visible to the harness.
- Schema validation of the key's shape.
- `configuration.md` + `README.md`. Version `2.4.0`.

### 2.2 Out

- **The architect side.** No project-level list for the human-facing session.
  Evidence against it: eight skills vendored into `akbars-insec-agent` at
  `18b5900` became available to the Claude Code session immediately, with zero
  configuration, because the harness discovers `.claude/skills/` by directory.
  A config key would add no availability, and as attention-direction it would
  only add prose to `SKILL.md` asking the model to read a YAML file and comply —
  a guarantee that holds exactly as long as the model chooses to honour it.
  Slices 03 and 04 both shipped guards that could only ever pass; this would be
  a third.
- **Installing skills.** The plugin never fetches, writes, or vendors a skill.
  Getting them onto disk is the project's business (`npx skills`, a marketplace
  plugin, or a commit).
- **Enforcement.** Nothing restricts an agent to the listed skills. No harness
  offers such a gate, and claiming one in our docs would be a lie.
- **Any change to the operating procedure.** `SKILL.md` is untouched.

## 3. Design

### 3.1 The key

```yaml
agents:
  planner:
    skills: [clean-architecture, domain-driven-design]
```

Optional. Absent by default, and absent means *nothing changes*: no added prompt
text, no subprocess, no output. This matters because the key must not tax the
default dispatch that no one configured.

`skills` joins `KNOWN_AGENT_KEYS` (`scripts/config.py:135-146`). Without that,
`validate_config` rejects the key as unknown — the existing typo guard would
refuse the very feature we are adding.

Note the merge semantics the key inherits: `deep_merge` replaces lists wholesale,
so a role's `skills` is exactly the list written, never an addition to a default.
There is no default list, so this is consistent rather than surprising.

### 3.2 Injection — plain append, no `{skills}` token

The rendered `prompt_template` (`scripts/orchestrator.py:328-329`) gains one
trailing paragraph:

```
Use these skills where they apply: clean-architecture, domain-driven-design.
```

Rejected alternative: a `{skills}` token substituted inside `prompt_template`.
It defeats the slice's own purpose. The whole justification is that a project
should not have to fork the default prompt; a token can only be used by editing
the template, which *is* forking it. It also reintroduces the silence we are
removing: a template without the token would drop the skills without a word,
because `str.format` ignores surplus keyword arguments.

The wording is deliberately advisory. "Use these skills where they apply" lets
the agent decline a lens that does not fit the task in front of it. An
imperative would make a domain-modelling lens mandatory on a one-line bugfix.

The list is rendered in the order written, comma-separated, terminated by a
period. Order is the human's, not sorted: a project that lists
`clean-architecture` first is stating a priority. A repeated name is rendered
once, at its first position.

### 3.3 Resolution — through the adapter, never around it

New method on `HarnessAdapter` (`scripts/adapters/base.py`):

```python
def list_skills(self, agent_config: dict, cwd: Path) -> set[str] | None:
    """Names of skills the harness can see from `cwd`, or None if unknowable."""
    return None
```

The base returns `None`, meaning *this harness cannot tell us*. `None` is not an
empty set, and the difference is load-bearing: an empty set means "the harness
sees no skills" and every named skill is missing; `None` means we have no
information and must stay quiet. Conflating them would make every custom adapter
emit a warning for every correctly-named skill.

`OpenCodeAdapter` implements it by running `opencode debug skill` with `cwd=cwd`
and parsing the JSON array of `{name, description, location}` objects into a set
of names. Verified against the real binary: it resolves project-level
`.claude/skills/` entries, needs no model call, and costs nothing but a few
seconds.

Any failure — binary absent, non-zero exit, unparseable output — returns `None`.
A diagnostic that can break a dispatch is worse than no diagnostic. `cwd` is
passed explicitly because project-level skills resolve relative to the working
directory, which for an isolated role is the worktree, not the project root.

The check runs **only when the role declares `skills`**. An unconfigured
dispatch spawns no subprocess.

### 3.4 Two classes of error, deliberately not merged

| Situation | Reaction | Rationale |
|---|---|---|
| `skills: "clean-architecture"` (string, not list); a non-string element; an empty or whitespace-only name | `ConfigError`, dispatch refused | The configuration cannot be read. This is the same class as an unknown status or an unknown sandbox key, and `validate_config` already fails closed on all of them |
| A name repeated in the list | Deduplicated, first occurrence wins, dispatch proceeds | Not an error. A repeat is unambiguous about intent, and our fail-closed rule is about ambiguity, not untidiness. Refusing a dispatch over it would be punitive, and the codebase has no duplicate check anywhere to be consistent with |
| Name is well-formed, harness does not report it | Warning printed, dispatch proceeds | Skills are reinforcement, not a dependency. The agent works without them; refusing would convert an optional improvement into a new way to be blocked |
| Adapter returns `None` | Nothing printed, dispatch proceeds | We do not know, and guessing produces false alarms |

The distinction is the point of the section. A malformed config is a failure to
read the human's intent; a missing skill is an intent read correctly that did
not come true. Only the first is a reason to stop.

### 3.5 Where the warning prints

Beside `_warn_if_artifacts_not_ignored` at `scripts/orchestrator.py:415-417`,
after `Dispatched …` and `Log: …`.

That helper (`scripts/orchestrator.py:39-51`) is the precedent this follows
exactly: shell out, take a set difference, print a `Hint:` line, never fail. Our
check has the same shape and the same standing — "you may have configured
something that will not do what you meant" — so it belongs in the same place and
should read the same way:

```
Hint: these skills are not visible to the harness and will have no effect: clean-architcture
```

Printing after the spawn is intentional and matches the existing hint. The
warning is advice to the human for next time, not a precondition of the run;
placing it earlier would interleave it with the gate messages, where readers are
looking for reasons a dispatch *stopped*.

## 4. Tests

No test may invoke a real harness, a real container runtime, or the network.
This constraint carries forward from slices 02 and 03 unchanged.

1. A role with `skills` produces argv whose prompt ends with the rendered
   sentence, listing the names in the order configured.
2. A role without `skills` produces argv whose prompt is exactly the rendered
   `prompt_template` with no appended paragraph, and `list_skills` is never
   called.
3. A role listing the same name twice renders it once, at its first position.
4. A named skill the fake adapter does not report produces the hint on stdout,
   and the dispatch still spawns.
5. A fake adapter returning `None` produces no skills-related output at all.
6. Each malformed shape in §3.4 row 1 raises `ConfigError` from
   `validate_config`, and the message names the offending role.
7. `skills` is accepted by `validate_config` — a guard against adding the key to
   the schema and forgetting `KNOWN_AGENT_KEYS`.
8. `OpenCodeAdapter.list_skills` parses a captured `opencode debug skill`
   payload into the expected set, with `subprocess.run` patched. Separate cases
   for non-zero exit, malformed JSON, and a missing binary, each returning
   `None`.
9. The base `HarnessAdapter.list_skills` returns `None`, so a custom adapter
   that does not implement it stays silent.

## 5. Documentation and version

`configuration.md` gains `skills` in the agent-key table and a short subsection
covering the two error classes and the `None` semantics for adapter authors.
`README.md` gains the key in its `agents.yaml` example. `plugin.json` and
`marketplace.json` go to `2.4.0`. `SKILL.md` is untouched: no procedure changes.

## 6. Risk, stated plainly

This slice guarantees that a named skill reaches the agent's prompt and that a
name the harness cannot see is reported. It does **not** establish that naming a
skill changes what the agent does. `opencode debug skill` proves visibility, not
influence.

If a given executor model ignores the named lens, this feature will be correct
and useless at the same time. That is a question about model behaviour, not
about this code, and it can only be answered by a live dispatch and a reading of
the resulting diff. The owner has been told this before implementation rather
than after.
