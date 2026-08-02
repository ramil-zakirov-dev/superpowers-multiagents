---
slice_id: "slice-01-orchestrator-hardening"
title: "Orchestrator hardening: runnable entry point, supervised lifecycle, honest invariants"
status: PLAN_GENERATED
target_version: "2.0.0"
depends_on: []
---

# Slice 01 — Orchestrator Hardening

## 1. Problem

The orchestrator does not run. Every command documented in `README.md` and
`skills/multiagent-orchestrator/SKILL.md` fails on invocation, and the
`SessionStart` hook is dead on macOS and Linux. Below that surface, four of the
invariants the README advertises — concurrency protection, state auditing,
strict state transitions, merge automation — are present in name only.

All findings below were reproduced empirically before this spec was written.
Each is annotated with the observed evidence, not with a reading of the code.

### 1.1 The plugin cannot execute its own instructions

`scripts/orchestrator.py:16-23` uses absolute package imports (`from scripts.config import …`)
while the file is invoked as a script. `sys.path[0]` becomes `scripts/`, so the
package is not importable:

```
$ python scripts/orchestrator.py status
ModuleNotFoundError: No module named 'scripts.config'
```

Independently, the documented paths are relative. When installed as a plugin the
working directory is the *user's project*, while `orchestrator.py` lives under
`~/.claude/plugins/…`. `SKILL.md` contains no `${CLAUDE_PLUGIN_ROOT}` reference,
so the skill injected into every session as `<EXTREMELY_IMPORTANT>` consists
entirely of commands that cannot resolve.

### 1.2 SessionStart hook is dead on POSIX

`hooks/run-hook.cmd` was reduced from the upstream cross-platform polyglot to a
Windows-only batch stub. Under the shell that macOS and Linux use to run it:

```
$ bash hooks/run-hook.cmd session-start
hooks/run-hook.cmd: line 1: @echo: command not found
hooks/run-hook.cmd: line 4: syntax error: unexpected end of file
```

The upstream file, given identical treatment, emits valid JSON. The rewrite also
dropped the Git-Bash location probes and the deliberate silent `exit /b 0` when
no bash is found, so a Windows user without bash on PATH now gets a hook error on
every session start instead of graceful degradation. `hooks/hooks.json:10` also
added a `"shell": "bash"` key that upstream does not use.

### 1.3 The agent log is always empty

`scripts/orchestrator.py:172` builds `cmd.exe /c "{agent_cmd} && {hook_cmd} > {log}"`.
In `cmd`, redirection binds to the last command of an `&&` chain, not to the
chain. Verified end-to-end with a stub adapter: the log file was created at
0 bytes and the agent's output never reached it.

This removes the evidence base for the `summary` command and for the
"Opus 5 audits the execution log" workflow the README sells. The POSIX branch
(`:178`) redirects outside `bash -c` and is unaffected — the defect is
Windows-only, which is the primary development platform.

### 1.4 The slice lock protects nothing

`scripts/locks.py:36` records `pid = os.getpid()` — the orchestrator's own PID.
The orchestrator exits immediately after `Popen`, so the lock refers to a dead
process within a second and every subsequent acquisition treats it as stale:

```
lock left by dispatch -> pid: 20008 alive? False | worker_pid: 34556 alive? False
acquire_slice_lock on an ACTIVE slice succeeded -> True
```

`worker_pid` is no better: it is the PID of the wrapping shell, which exits as
soon as the console is spawned. `release_slice_lock` is imported at
`scripts/orchestrator.py:22` and never called.

### 1.5 The merge gate blocks itself

`dispatch` creates `logs/` and `.superpowers/locks/` in the project root; neither
is gitignored. `scripts/git_ops.py:68` then requires a clean tree:

```
?? .superpowers/locks/
?? logs/
working tree considered clean? False
```

`VERIFIED_CLOSED` can therefore never complete its merge. The orchestrator's own
runtime artifacts poison its own gate.

### 1.6 Dead-end states, entered before the hook runs

A crashed background agent strands the slice permanently:

```
Error: Invalid state transition from 'PLANNING' to 'SPEC_APPROVED'.
```

`PLANNING` admits only `PLAN_GENERATED`; `EXECUTING` admits only
`EXECUTION_COMPLETE` / `MERGE_CONFLICT`. There is no rollback and no failure
state. `scripts/orchestrator.py:139-147` compounds this: the status is advanced
*before* the infrastructure hook runs, and `scripts/hooks.py:54` calls `sys.exit`
on a non-zero hook. A failing `sandbox-loopback up` therefore bricks the slice.

### 1.7 MERGE_CONFLICT is unreachable where it is used

`scripts/orchestrator.py:69-79` sets `VERIFIED_CLOSED` first, then merges. On
conflict the callback at `scripts/git_ops.py:78` tries to set `MERGE_CONFLICT`
from `VERIFIED_CLOSED`, which is terminal (`transitions["VERIFIED_CLOSED"] == []`).
The transition is rejected and the conflict is recorded nowhere.

### 1.8 Configuration behaves differently from its documentation

Verified against `docs/configuration.md`:

| User action | Documented behaviour | Actual behaviour |
|---|---|---|
| Override only `state_machine.transitions` | partial configuration | `KeyError: 'valid_statuses'` — `scripts/config.py:82` replaces the section wholesale |
| Override only an agent's `model` | rest inherits defaults | `planner == {'model': 'my-model'}` — **`allowed_statuses` disappears, so the state gate is silently disabled**; `prompt_template` degrades to `"Process {file}"` |
| Set global `harness.default` | inherited by agents | ignored — `scripts/adapters/loader.py:37` reads only `agent_config["harness"]` |

The second row is the dangerous one: it does not fail, it silently removes a
guard.

### 1.9 Hook event names diverge between code, tests and consumers

The code emits `on_slice_{role}_start` (`scripts/orchestrator.py:147`). The
consuming project `downstream-project` declares `on_slice_execution_start` in its
`.superpowers/hooks.yaml`, so its `sandbox-loopback up` hook **has never fired**
and `LOOPBACK_IP` has never been captured — while that project's compose file is
fail-closed on `${LOOPBACK_IP:?}`. `tests/test_orchestrator.py:242` encodes the
same wrong name, which is the likely origin. `README.md` uses the correct names.
An unknown event name is currently an unreported no-op.

### 1.10 Secondary defects

- `scripts/adapters/opencode.py:24` never passes `provider` under default config
  (`extra_args: []`); the OpenCode CLI expects `provider/model`.
- `prompt_template` and `extra_args` from `agents.yaml` reach the shell
  unescaped; an apostrophe breaks out of `bash -c '…'` on POSIX.
- `json.dumps` doubles backslashes in Windows paths inside the prompt.
- `logs/` is resolved against CWD while the executor runs with `cwd=worktree`:
  `mkdir` and redirect target different directories.
- `check_unmet_dependencies` reports "Spec not found" when its glob matches more
  than one file, and searches the target file's directory — `plans/` for the
  executor, though `depends_on` lives in `specs/`.
- `sys.exit` inside library modules (`hooks`, `locks`, `git_ops`, `utils`).
- `.claude-plugin/plugin.json` lacks `author`/`license`/`homepage`/`repository`;
  its description hardcodes Kimi/Minimax after the configurability refactor.
- `requirements.txt` omits `pytest`; a missing `ruamel.yaml` surfaces as a traceback.
- Documentation drift: `load_agent_config` docstring promises a tuple; README
  references `on_execution_complete`; the "Production-Ready" badge.

### 1.11 Root cause

Defects 1.3, 1.4, 1.6 and 1.7 are not four independent bugs. They are one: **the
orchestrator spawns a process and forgets it.** It never learns whether the agent
is alive or how it finished, and it holds no authority to advance status after
dispatch. Hence a lock naming a process that dies immediately, a status only the
LLM can advance, a dead end on crash, and a redirect bound to the wrong command.
Fixing them separately treats symptoms.

The 36 existing tests pass, but none of them touch `cmd_dispatch_agent`, which is
where all of the above lives.

## 2. Goals

1. Every command in `README.md` and `SKILL.md` executes as written, both from a
   clone and from an installed plugin.
2. The `SessionStart` hook works on Windows, macOS and Linux.
3. Slice lifecycle is owned by the orchestrator: completion and failure are
   derived from a process exit code, not from the agent's cooperation.
4. Advertised invariants hold or are removed from the documentation. No invariant
   is described as working when it is not.
5. Partial `agents.yaml` configuration inherits defaults; invalid configuration
   fails closed with a readable message.
6. A misspelled hook event is reported, not silently ignored.

## 3. Non-goals

- No new agent roles, harnesses or adapters beyond the existing OpenCode one.
- No migration path for existing `agents.yaml` files. There are no working
  installations to preserve; the version becomes `2.0.0` and the contract changes
  freely.
- No observability work beyond the log file and status field (no metrics, no
  tracing, no dashboard).
- No change to the milestone/track/slice conceptual model or the human gates.
- No rewrite of `README.md`'s value proposition — only its false claims.

## 4. Architecture

### 4.1 New component: `scripts/runner.py`

A supervisor process that owns one dispatched agent for its whole lifetime.

**Contract**

```
python -m scripts.runner
    --role <role>
    --file <abs path to md>
    --project-root <abs path>
    --lock <abs path to lockfile>
    --log <abs path to logfile>
    -- <argv of the agent command...>
```

**Responsibilities, in order**

1. Write its own PID into the lock file (it is the real owner).
2. Spawn the agent command with `stdout` and `stderr` redirected to `--log`.
3. Block until the child exits.
4. Advance status from the exit code: `0` → the role's `success_status`;
   non-zero → `FAILED`.
5. Fire `on_{role}_complete` on success, `on_{role}_failed` on failure.
6. Release the lock in a `finally` block, on every path including exceptions.

**Contract with `cmd_dispatch_agent`**

`dispatch` spawns the runner **as an argument list with `shell=False`** and
returns immediately. Detachment: `start_new_session=True` on POSIX,
`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` on Windows.

**Dispatch ordering — normative.** The current order advances status before the
infrastructure hook runs, so a failing hook strands the slice (1.6). The new
order performs every fallible step before any mutation the user would have to
undo by hand:

```
1. resolve config + validate
2. dependency gate, state gate
3. acquire lock
4. fire on_slice_{role}_start        <- may fail
5. create worktree (if isolated)     <- may fail
6. set in_progress_status            <- first irreversible mutation
7. spawn runner
```

If step 4 or 5 fails, the lock is released and the command exits non-zero with
the slice still at its entry status. Nothing needs manual repair.

**Consequences**

| Defect | Resolution |
|---|---|
| 1.3 empty log | Redirection is owned by the runner's `Popen`, not by shell chaining |
| 1.4 useless lock | Lock holds the runner's PID for the run's duration; released on exit |
| 1.6 dead ends | Terminal status derives from the exit code; `FAILED` is reachable |
| 1.10 shell injection | `shell=False` removes the shell from the dispatch path entirely |
| 1.10 backslash mangling | The prompt is an `argv` element, never a shell token |

### 4.2 State machine changes

Added status `FAILED`. Added transitions:

```
PLANNING           -> FAILED
EXECUTING          -> FAILED
FAILED             -> SPEC_APPROVED, PLAN_APPROVED
```

`SPEC_APPROVED` and `PLAN_APPROVED` are the entry points of the planner and
executor roles respectively, so `FAILED` returns the slice to the gate it came
from and the human decides whether to redispatch.

### 4.3 Ordering fix in `cmd_set_status`

Current order is *set `VERIFIED_CLOSED` → merge*, which makes `MERGE_CONFLICT`
unreachable (1.7). New order:

```
merge  ->  success  ->  set VERIFIED_CLOSED  ->  fire on_slice_verified_closed
       ->  conflict ->  set MERGE_CONFLICT   (legal from EXECUTION_COMPLETE)
```

### 4.4 Agent config contract

New key per agent:

| Key | Type | Meaning |
|---|---|---|
| `success_status` | string | Status set by the runner when the agent exits `0` |

Defaults: `planner.success_status = PLAN_GENERATED`,
`executor.success_status = EXECUTION_COMPLETE`. These were previously the
agent's responsibility to set from its prompt.

### 4.5 Configuration loading

`load_agent_config` performs a **recursive deep merge** of the parsed file over
`DEFAULT_CONFIG`, replacing the current mix of `dict.update` and wholesale
section replacement. Scalars and lists are replaced; mappings are merged key by
key.

Global `harness.default` / `harness.provider` become real defaults: an agent that
declares no `harness` / `provider` inherits them.

A new `validate_config(config)` runs after the merge and fails closed on:
- an agent's `allowed_statuses`, `in_progress_status` or `success_status`
  referencing a status absent from `valid_statuses`;
- a `transitions` key or target referencing an unknown status;
- an unknown key in an agent definition (typo guard).

### 4.6 Runtime artifact layout

Runtime artifacts move under `.superpowers/`:

```
.superpowers/logs/<role>_<stem>.log      (was: logs/ in CWD)
.superpowers/locks/<slice_id>.lock       (unchanged)
```

All paths are resolved from `project_root`, never from CWD — the executor runs
with `cwd=worktree` and must not split `mkdir` from its redirect target (1.10).

`check_working_tree_clean` ignores `.superpowers/logs/`, `.superpowers/locks/`
and `.worktrees/` when deciding cleanliness. The user's `.gitignore` is **not
modified** — that file belongs to the user. Instead `dispatch` prints a hint,
once per invocation, when those paths are neither ignored nor already tracked.

### 4.7 Hook event contract

Canonical, and the only names the orchestrator emits:

| Event | Fired by | When |
|---|---|---|
| `on_slice_{role}_start` | `cmd_dispatch_agent` | before spawning the runner |
| `on_{role}_complete` | `runner` | child exited `0` |
| `on_{role}_failed` | `runner` | child exited non-zero |
| `on_slice_verified_closed` | `cmd_set_status` | after a successful merge |

`load_project_hooks` validates every key in `hooks.yaml` against this set (with
`{role}` expanded from the configured agents) and **prints a warning naming the
unknown event and listing the valid ones**. This is the guard whose absence hid
1.9 for the entire life of the consuming project.

### 4.8 Entry point resolution

Three independent layers, all required:

1. **Bootstrap.** `orchestrator.py` inserts the package parent into `sys.path`
   when `__package__` is empty, so `python scripts/orchestrator.py` and
   `python -m scripts.orchestrator` both work.
2. **Absolute paths in documentation.** All commands in `SKILL.md` and
   `README.md` address the orchestrator by absolute path; the working directory
   stays the user's project.
3. **Path injection.** `hooks/session-start` already computes `PLUGIN_ROOT`. It
   prepends one resolved line to the injected skill text naming the absolute
   orchestrator path.

**Resolution of the open question (investigated 2026-07-26).** The mechanism is
not `${CLAUDE_PLUGIN_ROOT}` interpolation. The Skill tool announces the skill's
absolute location to the model on load (`Base directory for this skill: <abs>`),
and upstream skills address their bundled scripts relative to it — no official
`SKILL.md` in the installed plugin cache references `${CLAUDE_PLUGIN_ROOT}` in
its body. The placeholder-substitution machinery is therefore dropped.

This leaves one real gap, which layer 3 exists to close: a skill has **two
consumption paths**, and only one of them announces a base directory.

| Path | Base directory available? | Resolution |
|---|---|---|
| Loaded via the Skill tool | yes, announced by the harness | paths relative to the skill directory |
| Injected by `session-start` as `additionalContext` | **no** | absolute path prepended by the hook |

`SKILL.md` therefore states both: resolve `../../scripts/orchestrator.py`
relative to this skill's announced base directory, and prefer the absolute path
given at the top of the text when it was injected at session start.

### 4.9 Adapter

`OpenCodeAdapter.build_command` emits `--model {provider}/{model}`, the CLI's
native form. `extra_args` remains available for everything else and keeps its
`{provider}`/`{model}` interpolation.

### 4.10 Library boundaries

`sys.exit` is removed from `hooks.py`, `locks.py`, `git_ops.py` and `utils.py`.
These modules raise `OrchestratorError` (new, in `scripts/errors.py`); only
`orchestrator.py` and `runner.py` — the process boundaries — translate it into an
exit code. This is what makes the failure paths testable at all.

## 5. Test strategy

**Discipline: every reproduction in section 1 becomes a failing test first.**
The defects were found by running the code; the tests encode those runs.

**Cost guard — binding.** `opencode` is installed on the development machine and
a live run costs money. No test may invoke a real harness. Dispatch tests use a
stub adapter written into a temporary project via `harness_adapter`, emitting a
harmless command. This also exercises the custom-adapter loader path.

| Area | Test |
|---|---|
| Entry point | `python scripts/orchestrator.py status` and `python -m scripts.orchestrator status` both exit 0 |
| POSIX hook | `bash hooks/run-hook.cmd session-start` emits parseable JSON |
| Runner success | exit 0 → `success_status` set, lock released, log non-empty |
| Runner failure | exit non-zero → `FAILED` set, lock released, log contains child stderr |
| Runner crash | exception in the runner still releases the lock |
| Lock | acquisition is refused while the runner's PID is alive |
| Merge gate | a tree dirty only with orchestrator artifacts is considered clean |
| Merge conflict | conflict leaves the slice in `MERGE_CONFLICT`, not `VERIFIED_CLOSED` |
| State machine | `FAILED` reachable from `PLANNING`/`EXECUTING` and returns to its gate |
| Config merge | the three cases in the 1.8 table |
| Config validation | unknown status / unknown agent key fails closed with a message |
| Hook names | an unknown event name produces a warning naming it |
| Adapter | command contains `--model opencode-go/minimax-m3` |
| Integration | full `cmd_dispatch_agent` in a temporary git repo with the stub adapter |

The integration test is the important one: a single test that would have caught
1.3, 1.4 and 1.5 together.

## 6. Consumer fix

`downstream-project/.superpowers/hooks.yaml`: `on_slice_execution_start` →
`on_slice_executor_start`. This is the only change outside this repository, and
it lands as **a separate commit in that repository** — it is not part of this
branch. It is scheduled with this slice so the fix and the validation guard that
would have caught it (4.7) arrive together.

## 7. Acceptance criteria

1. Every command in `README.md` and `SKILL.md` runs successfully as written,
   verified from a clone and from a plugin-style absolute path.
2. `bash hooks/run-hook.cmd session-start` emits valid JSON.
3. A dispatched agent that exits non-zero leaves the slice in `FAILED` with a
   non-empty log, and the lock released.
4. A second dispatch is refused while the first runner is alive.
5. `VERIFIED_CLOSED` merges successfully in a repository whose only untracked
   files are orchestrator artifacts.
6. The three configuration cases from 1.8 behave as documented.
7. An unknown hook event produces a warning naming the offending key.
8. No documented claim is false: the README states only invariants that tests
   cover.
9. The full suite passes, including the new tests, with no live harness invoked.

## 8. Risks

| Risk | Mitigation |
|---|---|
| ~~`${CLAUDE_PLUGIN_ROOT}` interpolation in skill bodies is unverified~~ | RESOLVED 2026-07-26 — see 4.8; placeholder machinery dropped, two consumption paths handled explicitly |
| Detached-process semantics differ across platforms | Runner spawn is covered by tests on the development platform; POSIX flags follow the documented `start_new_session` contract |
| `FAILED` is a breaking config change | Accepted deliberately: no working installations exist; version becomes `2.0.0` |
| Deep merge changes behaviour for a config that relied on wholesale replacement | No such config exists in the wild; validation reports what it resolved |
