---
slice_id: "slice-09-config-can-say-off"
title: "A project can say which roles it does not delegate, and an unspoken gate stops meaning wide open"
status: VERIFIED_CLOSED
target_version: "2.20.0"
depends_on: []
lenses:
- wondelai/pragmatic-programmer#design-by-contract@6800c2f4dceb
- wondelai/design-everyday-things#constraints@82db4899450a
---

# Slice 09 — Absence is not permission

## 1. Problem

Two issues, one seam. A project wanted to stop delegating one level of the
hierarchy. There was no way to write that down, and the two spellings it
reached for did the opposite of what they say.

### 1.1 An empty entry gate opens the gate (#31)

`orchestrator.py:614`:

```python
allowed_statuses = agent_config.get("allowed_statuses") or []
if allowed_statuses and current_status not in allowed_statuses:
```

An empty list is the natural way to write *this role accepts no document*.
The guard reads it as *this role has no gate* and dispatches from **any**
status — `VERIFIED_CLOSED` included, and a status another role's supervisor
currently owns included.

Reproduced against `main` @ `e57e661`. All three spellings resolve to the same
wide-open gate, and `validate_config` accepts all three:

| written | resolved | dispatch at `VERIFIED_CLOSED` refused? |
|---|---|---|
| `allowed_statuses: []` | `[]` | no |
| `allowed_statuses:` (key present, no value) | `None` → `[]` | no |
| key omitted on a custom role | missing → `[]` | no |

The third is the one that will bite a stranger. `docs/configuration.md`
("Adding Custom Agents") tells the reader to declare `success_status` and
`in_progress_status`; nothing there says the entry gate is mandatory, and the
omission is silent.

### 1.2 A role cannot be switched off (#32)

**Omitting it restores it.** `deep_merge` merges mappings key by key and
`DEFAULT_CONFIG` always carries `planner`, so an `agents:` block naming only
`executor` still resolves a full planner — at `kimi-k3` on `opencode-go`,
which is the exact route the project had decided against:

```
deep_merge(DEFAULT_CONFIG, {"agents": {"executor": {}}})["agents"]  -> ['executor', 'planner']
                                        ...["planner"]["model"]     -> 'kimi-k3'
```

**Nulling it crashes.** `planner: null` is the other obvious spelling:

```
validate_config      -> TypeError: 'NoneType' object is not iterable
in_progress_statuses -> AttributeError: 'NoneType' object has no attribute 'get'
```

Not a `ConfigError`, and not at dispatch — a bare traceback from every command
that touches the agents map, naming neither the file nor the role. (#32 said
this reaches `validate_config` from `status` too. It does not: `cmd_status`
deliberately skips validation, "a report is a read". It crashes anyway, one
layer down and with a different exception, which is worse — the read path was
built to survive an unusable config and does not.)

**And a third spelling nobody reported.** `planner: "kimi-k3"` — writing the
model where the mapping goes — is not a crash but a confidently wrong message,
because `set(agent)` on a string iterates its characters:

```
ConfigError: agent 'planner': unknown key(s) ['-', '3', 'i', 'k', 'm'].
```

This is the same root as the `None` case: `validate_config` assumes an agent
entry is a mapping and never checks. It is worth fixing on its own terms — a
config typo should never surface as a `TypeError`, and it should certainly
never surface as a fluent sentence about five keys that do not exist.

So today the only workable answer is a convention: leave the role at the
plugin's defaults, write a YAML comment saying this project never dispatches
it, and rely on nobody typing the command. That is exactly the class of thing
this pipeline exists to take out of a human's head.

### 1.3 What the two have in common

In both, **absence is read as permission**. A gate nobody declared admits
everything; a role nobody mentioned is dispatched at the plugin's default
model. The notation offers no way to say *no* — and the two shapes that look
like *no* to a reader mean *anything* to the orchestrator.

## 2. Lenses

### `pragmatic-programmer#design-by-contract`

> Use assertions for things that should never happen; error handling for
> things that might. Crash early: a dead program does far less damage than a
> crippled one.

This settles the open question of *where* to refuse a gateless role. A role
with no entry gate is not a runtime condition to be handled — it is a
violated precondition of the orchestrator itself, and no document can make it
true later. So it belongs in `validate_config`, loudly, on every command that
validates, and not in a branch at the dispatch site that only fires when
someone happens to dispatch that role.

The same lens condemns the `or []` idiom in the guard. `or []` turns *I was
not told* into *there is nothing*, and the code then limps on in an invalid
state instead of stopping at the point of the problem. Both readings of the
line are wrong in the same direction: toward continuing.

It also draws the boundary the other way, and that boundary is what §4.5
rests on. `cmd_status` is a **read**. A read has no precondition to violate —
it neither dispatches nor writes — so a malformed role there is a "might
happen" to be handled, not a "should never happen" to assert. The two paths
diverge deliberately: the validating path names the bad role, the reporting
path steps over it and still says what is on disk.

### `design-everyday-things#constraints`

> Every constraint you add is one less error the user can make — make wrong
> actions impossible rather than punishing them.

This changed the design, not just its wording. The obvious answer to #32 is a
`dispatch: manual` key: the role stays, and `dispatch <role>` grows a guard
that refuses. In Norman's taxonomy that is a warning — the wrong action is
still available, and the system punishes you at the moment you take it. Worse,
the role's `allowed_statuses`, `produced_status` and `model` stay in the file
meaning nothing, which is a second thing the reader has to know is inert.

Removing the role is a **lockout**: there is no role, so there is nothing to
dispatch, and the refusal comes from the mechanism that already exists
(`resolve_agent`) rather than from a new one bolted alongside it. That is the
design in §4.1. The cost is that `null` carries no reason — but the reason
belongs to the reader, in a YAML comment, and the orchestrator's refusal needs
only to distinguish *you typed it wrong* from *this project removed it*, which
§4.4 does from a fact it can observe.

The same rule rejects `allowed_statuses: "*"`, floated in #31 as the explicit
spelling for a wide-open role. No role wants it — both shipped roles have
gates, and a custom one can list its statuses. Adding the spelling would
restore, as a supported feature, precisely the configuration this slice exists
to make unwritable.

## 3. What this slice is not

- **Not a change to what `deep_merge` does with a value it is given.** Scalars
  and lists still replace wholesale; mappings still merge key by key. Only
  `null` acquires a meaning, and it acquires the one it already has in
  RFC 7386 JSON Merge Patch: remove this key.
- **Not a lifecycle change.** #32 asks, in passing, whether a human-held role
  should have `produced_status` in the schema, since a foreground session
  writes its document in one shot and the drafting window has no referent.
  With the role removed there is no schema entry to answer for, and the
  document is simply born at the status its author writes. Nothing to state.
- **Not a rename.** `allowed_statuses` keeps its name and its type.

## 4. Design

### 4.1 `agents.<role>: null` removes the role

`deep_merge` treats a `None` override as a deletion (RFC 7386). Verified
behaviour-preserving for every other section: every reader of a top-level
section already spells it `config.get(X) or {}`, so *key absent* and *key
present but `None`* resolve identically today. The two direct subscripts
(`config["state_machine"]`, at `orchestrator.py:550` and `runner.py:502`) are
both downstream of a `validate_config` that already refuses a state machine
with no `valid_statuses`, by either spelling.

`planner: {}` remains the way to say *this role, all defaults*.

### 4.2 An entry gate is mandatory

`validate_config` refuses a role whose `allowed_statuses` is missing, `null`
or empty, naming the role and where the list belongs.

**Amended after running it.** The draft also promised a second refusal at
`orchestrator.py:614`, with its own wording, on the reasoning that a gate is
worth two locks. It is not reachable: `cmd_dispatch_agent` calls
`validate_config` at line 543, before the guard, so after this change no empty
list can arrive there. Shipping a bespoke message down a branch no test can
enter is the thing the `approve-plan` gate exists to catch, so the branch is
not written.

What the guard does get is the removal of `and allowed_statuses` from its
condition. The truthiness test is what made the line read as though empty were
a meaningful value; without it the guard says only *the document's status must
be in the list*, and a config that somehow reached dispatch without one fails
closed instead of open. The invariant that makes this safe is asserted where
it lives — in the validator — not restated here.

### 4.3 A non-mapping agent entry is a `ConfigError`

Named, with the type it got and the shape expected. After §4.1 this no longer
sees `None`; it exists for the string, the list, and whatever else lands
there.

### 4.4 A removed default role says so when dispatched

`resolve_agent` already refuses an unknown role. When the missing role is one
the plugin ships and this project does not carry, the refusal says that
instead of implying a typo — the only way to reach that state is §4.1, so the
inference is sound.

### 4.5 The read path survives a malformed role

`abandonment`'s config readers skip an entry that is not a mapping, the way
`certifiable_statuses` already does. `cmd_status` is the only caller that
deliberately runs on an unvalidated config; it should not be the only caller
that crashes on one.

## 5. Contracts

| # | Given | When | Then |
|---|---|---|---|
| C1 | `agents: {planner: null}` | config is loaded | `planner` is not among the roles |
| C2 | C1 | `dispatch planner` | refused, naming the role as one this project removed |
| C3 | C1 | `validate_config` | passes |
| C4 | C1 | `status` | reports normally |
| C5 | C1 | `close-slice` on a plan | unaffected — `closure` names `plans/` itself, never via `produces` |
| C6 | `agents: {planner: {}}` | config is loaded | a full default planner, as today |
| C7 | a role with `allowed_statuses: []` | `validate_config` | `ConfigError` naming the role |
| C8 | a role with no `allowed_statuses` key | `validate_config` | `ConfigError` naming the role |
| C9 | *withdrawn* — see §4.2. `cmd_dispatch_agent` validates before it gates, so the state this contract describes cannot occur |
| C10 | `agents: {planner: "kimi-k3"}` | `validate_config` | `ConfigError` naming the role and the type |
| C11 | `agents: {planner: "kimi-k3"}` | `status` | reports; does not crash |
| C12 | both shipped roles | `validate_config(DEFAULT_CONFIG)` | passes — the defaults satisfy their own new rule |

## 6. Blast radius

Every config in the repository and its tests already declares
`allowed_statuses` on every role it defines, with one exception —
`tests/test_abandonment.py:140`, which writes a partial `executor` override
and relies on `deep_merge` to supply the rest. That is the case §4.2 must not
break, and C6 is its contract: a partial override still inherits the gate.

For a project outside this repository, §4.2 is a breaking change if and only
if it defines a custom role with no entry gate — a config that today dispatches
that role from any status, including one another supervisor owns. Breaking it
loudly is the point.

## 7. Verification

Every claim in §1 was produced by running it against `main` @ `e57e661`, not
read off the source. The table in §1.1 and the three tracebacks in §1.2 are
transcript, including the character-set message, which was not reported by
anyone and was found by running the typo.

The contracts were then run twice: as unit tests (`tests/test_role_off.py`,
16 cases, all red before implementation except the five falsifiers, which had
to be green and were), and as commands, against a throwaway project carrying
`planner: null` — because §5 makes claims about `status`, `dispatch`,
`certify` and `close-slice`, and a passing unit test is not one of those
having been typed.

| ran | result |
|---|---|
| `status` | reports; C4 |
| `dispatch-planner` | *"ships with the plugin, but this project removed it … there is nothing to dispatch"*; C2 |
| `dispatch-agent --role revieuwer` | *"is not defined in the configuration"* — the falsifier: a name nobody shipped is not described as removed |
| `set-status PLAN_GENERATED → PLAN_APPROVED` | works; a hand-written plan still passes its gate |
| `set-status → VERIFIED_CLOSED` | merged `feat/slice-99`, closed the plan **and** found and closed the spec; C5 |
| `certify` | *"(none configured)"* — degrades in a sentence, does not crash |
| any validating command, gate emptied | refuses, naming the role; C7 |
| `status`, gate emptied | still reports; the read path and the validating path diverge as §2 says they should |
| `planner: kimi-k3` | *"must be a mapping of settings, got str"*; C10 — and `status` still reports; C11 |

Full suite on the finished tree: **652 passed**.

One defect was found by this slice rather than fixed by it, and it was found
by the docs-consistency guard crashing:
`AttributeError: 'NoneType' object has no attribute 'items'` on the new
`planner: null` example. `test_documented_agent_defaults_match_the_code` held
the same assumption the slice was removing three modules away — that an agent
entry is always a mapping. Fixed there too. A guard is not exempt from the bug
it guards against.
