---
slice_id: "slice-02-native-sandbox"
title: "Native sandbox: per-slice infrastructure isolation owned by the orchestrator"
status: DRAFT_SPEC
target_version: "2.1.0"
depends_on: ["slice-01-orchestrator-hardening"]
---

# Slice 02 — Native Sandbox

## 1. Problem

The orchestrator's entire value proposition is running several slices in
parallel, each in its own git worktree. When those slices need infrastructure —
a database, a vector store, an object store — parallelism turns into a hazard:
every worktree's `docker compose` wants the same host ports.

A consuming project solved this outside the plugin, with a project-local skill
that binds each branch's compose stack to its own loopback IP
(`127.0.0.2..127.0.0.255`) and wires it in through `.superpowers/hooks.yaml`.
That arrangement does not work, and the reasons are structural rather than
incidental.

All findings below were reproduced empirically before this spec was written.

### 1.1 The hook cannot know which slice it is serving

`cmd_dispatch_agent` fires `on_slice_{role}_start` **before**
`create_git_worktree`, with `cwd=project_root`. Any hook that asks git "which
branch am I on?" is therefore answered with the *main* repository's branch, not
the slice's.

Running the consuming project's real allocation code from the position the hook
actually occupies:

```
hook cwd       = <downstream project root>
branch it sees = feat/sandbox-loopback-skill

  slice alpha: real branch feat/alpha -> ip 127.0.0.153  project feat-alpha
  slice beta:  real branch feat/beta  -> ip 127.0.0.105  project feat-beta

  but the hook computes: feat/sandbox-loopback-skill -> ip 127.0.0.78
                         (identical for every slice dispatched from that branch)
```

Two slices dispatched in parallel receive the same compose project name.
`docker compose -p <same>` is idempotent, so the second dispatch does not fail —
it silently attaches to the first slice's stack. Two agents then share one
database and run migrations against it concurrently. The failure is quiet,
which is worse than a port collision: a collision stops you, sharing corrupts
you.

This cannot be fixed by configuration or by editing the project's script. At
the moment the hook runs, the slice's branch **does not exist yet**, and the
only component that knows its name is the orchestrator.

### 1.2 The environment bridge is a string-parsing shell contract

`run_infrastructure_hook` passes values to the agent by parsing the hook's
stdout for `KEY=VALUE` lines. Delivering one integer — a loopback octet — to
the dispatched agent currently requires: a shell command embedded in YAML, two
processes chained with `&&`, one of them redirected to stderr so its prose does
not poison the parse, execution under `shell=True`, and line-by-line splitting
on the first `=`.

That chain has already produced a defect. The consuming project's hook invoked
its `up` subcommand, which prints only prefixed prose. Running the plugin's
real parser over both candidate outputs:

```
up  (as configured):  LOOPBACK_IP captured: *** MISSING ***
                      malformed env names created: ['[sandbox-loopback] branch']
env (the fix):        LOOPBACK_IP captured: 127.0.0.5
                      malformed env names created: []
```

The project's `docker-compose.yml` fails closed on `${LOOPBACK_IP:?}`, so the
first real dispatch would have been unable to start its stack at all. The bug
was invisible until someone ran the parser by hand.

### 1.3 The mechanism is unowned and untested

The moved code claims coverage it does not have. Its module docstring states
that the pure helpers "are unit-tested in `tests/test_sandbox_loopback.py`".
No such file exists anywhere in the consuming project:

```
git ls-files | grep -i sandbox
  -> SKILL.md, four references, the script itself. No tests.
```

470 lines of allocation logic, process management and destructive teardown,
with zero tests and a false claim of coverage in its header. This slice
therefore **reimplements under TDD**; it does not port.

### 1.4 The platform failure mode points the wrong way

Loopback allocation probes each candidate octet with `socket.bind`, and treats
every `OSError` identically:

```python
except OSError:
    return False
```

`EADDRINUSE` (the octet is taken) and `EADDRNOTAVAIL` (the platform has not
configured that address at all) are not the same condition. On a platform where
only `127.0.0.1` is configured on the loopback interface, all 254 candidates
report "busy" and the user is told `no free loopback in 127.0.0.2..255` — a
diagnosis that sends them looking for stacks to tear down when the real remedy
is a one-time interface alias. For a plugin published publicly, a misleading
error in the first five minutes of use is a support burden.

### 1.5 Root cause

The hazard is manufactured by the orchestrator and guarded by someone else.
Parallel worktrees are the plugin's own feature; port collision is that
feature's direct consequence. Delegating the guard to an opaque shell hook
means the guard runs without the one piece of information the orchestrator
holds and the project cannot derive — which slice is being dispatched.

## 2. Goals

- Per-slice infrastructure isolation becomes a first-class, **opt-in**
  capability of the orchestrator, addressed by the slice's own branch.
- The value reaches the agent as process environment, not as parsed stdout.
- The orchestrator owns the lifecycle: bring up on dispatch, tear down on a
  terminal status, with destructiveness graded by outcome.
- A consuming project declares its service schema; it ships no allocation code.
- Failures are closed and their messages name the actual remedy.
- Absent configuration, the plugin's behaviour and dependencies are unchanged.

## 3. Non-goals

- Any second isolation strategy (port offsets, dedicated interfaces, remote
  hosts). One mechanism, one code path.
- Provisioning, seeding or migrating the stack's contents. The plugin brings a
  stack up and takes it down; what lives inside is the project's business.
- Auto-configuring the host's network. The preflight explains what to run; it
  never runs it, and it never invokes `sudo`.
- Container runtimes other than `docker compose`.
- Cross-machine or cross-user coordination. Isolation is per-checkout.

## 4. Architecture

### 4.1 New component: `scripts/sandbox.py`

A leaf module. It imports from `scripts.errors`, `scripts.paths` and
`scripts.locks`; it does **not** import the orchestrator. Dependencies point
inward, as elsewhere in this codebase.

```python
def preflight(ip: str) -> None
def ip_for(branch: str, *, busy: Iterable[str] = ()) -> str
def project_name_for(branch: str) -> str
def resolve_env(branch, project_root, cfg) -> dict[str, str]
def ensure_up(branch, project_root, cfg) -> dict[str, str]
def tear_down(branch, project_root, cfg, mode: str) -> None
```

`branch` is a parameter everywhere, never a return value of `git rev-parse`
inside the module. This is the constructive repair of §1.1: the code cannot
resolve the wrong branch because it is not permitted to guess one.

`resolve_env` is side-effect free — it reads existing state and returns the
environment, or an empty mapping when no stack is tracked. `ensure_up` is
idempotent: an existing state record means the same octet is reused, so a
re-dispatch lands on the same stack.

### 4.2 Configuration contract

A new top-level `sandbox` block in `.superpowers/agents.yaml`:

```yaml
sandbox:
  enabled: true
  compose_file: docker-compose.yml
  health_service: postgres
  health_timeout: 60
  env:
    pg_dsn: "postgresql://user:pass@{ip}:5432/db"
    qdrant_url: "http://{ip}:6333"
  teardown:
    on_verified_closed: volumes      # volumes | containers | none
    on_failed: containers
```

`LOOPBACK_IP` and `COMPOSE_PROJECT_NAME` are always injected and are not
declarable — they are the contract, not a setting. Template substitution
recognises exactly two tokens, `{ip}` and `{project}`. `${VAR}` expands from
the parent process environment, so a project whose DSN contains a real secret
can source it from `.env` instead of committing it to a tracked config.

Validation follows the existing `KNOWN_AGENT_KEYS` discipline and fails closed:
an unknown key under `sandbox`, an unknown template token (`{IP}` for `{ip}`),
or a `teardown` value outside the enum each raise `ConfigError`. A sandbox that
is enabled while `docker` or `compose_file` is absent is an error at dispatch,
not a silent skip — the project asked for it explicitly.

**Which agents get a sandbox** is derived from `isolated_worktree`, not from a
separate switch. A sandbox isolates a worktree; no other quantum is meaningful.

| `isolated_worktree` | on dispatch |
|---|---|
| `true` | `ensure_up`, env injected, lifecycle owned |
| `false` | env injected **if** state already exists; never brought up |

The second row is deliberate: a non-isolated agent runs in `project_root` on
the human's branch, so it must attach to the human's stack rather than start a
competing one.

### 4.3 Dispatch ordering

Inside the existing fallible block of `cmd_dispatch_agent`, before any status
mutation:

```
create_git_worktree      -> worktree, branch feat/<slice_id>   (isolated only)
sandbox.ensure_up(...)   -> sandbox_env        (enabled + isolated)
  or sandbox.resolve_env(...)                  (enabled + non-isolated)
run_infrastructure_hook  -> env                (receives sandbox_env)
get_harness_adapter      -> argv
```

The branch a non-isolated agent is addressed by is the repository's current
branch, since that is where it runs; `resolve_env` returns an empty mapping
when no stack is tracked for it, which is the ordinary case.

`on_slice_{role}_start` moves from before worktree creation to after it. Two
consequences, both intended: a project hook can now act on the worktree, and it
receives `LOOPBACK_IP` in its environment, so hook-based composition survives
without generalising the hook contract. This is a behavioural change to a
published 2.0.0 contract and is documented as such.

The worktree is not removed when a later step in the block fails. That matches
the existing behaviour for adapter-resolution failure and keeps the recovery
story uniform.

### 4.4 Teardown sites, and their order relative to hooks

| Trigger | Site | Mode |
|---|---|---|
| non-zero agent exit, **isolated agent only** | `runner._record_outcome` | `teardown.on_failed` |
| status becomes `VERIFIED_CLOSED` | `cmd_set_status` | `teardown.on_verified_closed` |

The failure-teardown trigger applies only to agents with
`isolated_worktree: true`. A non-isolated agent never owns a stack's
lifecycle (§4.2 — it only ever `resolve_env`s an existing stack, never
`ensure_up`s one), so `cmd_dispatch_agent` passes the runner an empty
`--sandbox-branch` for a non-isolated agent regardless of what branch it
actually ran on. The runner's existing gate,
`if exit_code != 0 and sandbox_branch:`, then naturally skips teardown for
it. Without this, a non-isolated agent's crash would tear down the human's
own active stack over an unrelated failure.

The trigger is the transition actually being applied, not the merge that
usually precedes it. A slice may legitimately reach `VERIFIED_CLOSED` from
`MERGE_CONFLICT` after a human resolves it by hand, and that slice's stack must
be swept too. Conversely, a failed merge leaves the status at
`MERGE_CONFLICT`, so no teardown fires — the stack stays available for the
person doing the resolving.

Both run **after** the corresponding project hook, never before. A failure hook
is precisely where a project would capture a database dump or container logs;
tearing the stack down first would hand it an empty machine.

Teardown failure is warn-only, consistent with the surrounding post-processing:
the slice's outcome is already recorded and must not be overturned by an
unswept container.

The supervisor does not derive the branch. `cmd_dispatch_agent` passes it
explicitly as `--sandbox-branch`.

### 4.5 Slice context in the environment

Additive, and useful beyond this slice: `SUPERPOWERS_SLICE_ID`,
`SUPERPOWERS_SLICE_BRANCH` and `SUPERPOWERS_WORKTREE` are injected into the
environment of both the project hook and the dispatched agent. Today anything
needing this must guess via `git rev-parse` — and guesses wrong, as §1.1 shows.
This closes the class, not just the instance.

Note that this reaches the agent unconditionally, including when no `sandbox`
block is configured. It is the one intended environment difference from 2.0.0;
§7.6 is worded accordingly.

### 4.6 Runtime artifacts and the state invariant

State moves under the directory the plugin already owns:

```
.superpowers/sandbox/<project-name>.json
```

`.superpowers/sandbox/` joins `ARTIFACT_PREFIXES`, so it is excluded from the
working-tree cleanliness check and surfaces in the gitignore hint like the
other runtime paths. It no longer lives inside `.worktrees/`, which is slice
payload rather than orchestrator state.

One rule governs the record's lifetime:

> **The state file is deleted if and only if the volumes are destroyed.**

Everything follows. `containers` mode (the default for `FAILED`) keeps the
record, so a re-`up` returns the same octet and the same data and the failure
remains diagnosable. `volumes` mode discards it, releasing the octet. A record
without a running stack is not an error: `up` recreates it idempotently and
`status` reports `stopped`.

### 4.7 Platform preflight

Before searching for an octet, probe one candidate and branch on `errno`:

- `EADDRNOTAVAIL` — a property of the platform, not of the octet. Abort
  immediately with a `SandboxError` naming the remediation command. Do not scan
  the remaining 253 candidates and do not report them as busy.
- `EADDRINUSE` — genuinely occupied; continue the search.

The remediation is printed for a human to run. The plugin never elevates
privileges.

### 4.8 Octet allocation race

`bind`-probing is time-of-check/time-of-use: seconds pass between the probe and
compose actually binding. Two concurrent dispatches can select the same octet
and produce an opaque compose failure.

The existing atomic primitive in `scripts/locks.py` (`O_CREAT | O_EXCL`, already
tested) is reused for a short-lived global `sandbox.lock` around
"choose octet + write state". The per-slice lock cannot serve here: the
conflict is between slices by definition.

### 4.9 CLI surface

Exposed as a subcommand of the existing entry point, so no new executable and
no PATH installation are required — the `SessionStart` hook already injects the
orchestrator's absolute path.

```
orchestrator.py sandbox up | restart | status | env | exec | teardown
```

`env` takes `--shell posix|powershell|json`. The shell-eval idiom documented
today is POSIX-only and does not work for a user on PowerShell; `exec` covers
the remaining cases without any eval at all. `teardown` retains the explicit
confirmation gate for volume destruction.

### 4.10 Library boundaries

`sandbox.py` owns allocation, state and compose invocation. `orchestrator.py`
owns *when* those are called. `runner.py` gains one call and one argument.
Nothing in `hooks.py` changes: the hook mechanism stays a shell-command
contract, deliberately not made polymorphic — that would be a one-way door on a
published extension point.

The docker executable is resolved through `SUPERPOWERS_DOCKER_BIN`, falling
back to `docker` on `PATH`. This is the seam the tests use (§5) and it is the
only way to intercept the call that happens inside the detached supervisor.

## 5. Test strategy

The binding constraint from slice 01 carries forward verbatim: **no test may
invoke a real harness or a real container runtime.**

Unit level: determinism and wrap-around of `ip_for`; `127.0.0.1` never
returned; `project_name_for` against compose's naming rules; template
substitution including `ConfigError` on an unknown token; state round-trip; and
the `errno` split — a probe raising `EADDRNOTAVAIL` must abort with remediation
while `EADDRINUSE` continues the search.

Configuration: unknown key, unknown token, out-of-enum teardown mode each fail
closed.

Integration, via a stub `docker` binary that records its argv:

1. **Argv contract.** A real dispatch of an isolated agent must produce a
   `-p` derived from the slice branch `feat/<slice_id>`. This test is red
   against today's arrangement; it is the regression guard for §1.1.
2. **Parallel slices.** Two dispatches yield distinct octets and distinct
   compose project names.
3. **Failure teardown across the process boundary.** A stub agent exiting
   non-zero causes `down` **without** `-v`, and the state file survives.
4. **Success teardown.** `VERIFIED_CLOSED` causes `down -v` and removes state.
5. **Hook precedes teardown.** A project hook and the stub docker both append
   to one journal; the order is asserted.
6. **Inertness.** With no `sandbox` block, the stub docker is never invoked and
   the agent's environment gains no sandbox variable (the `SUPERPOWERS_*` slice
   context of §4.5 is expected and asserted separately). If this test ever
   fails, docker has leaked into the orchestrator's contract.

Documentation consistency is partly self-enforcing: adding
`.superpowers/sandbox/` to `ARTIFACT_PREFIXES` makes the existing
`test_runtime_artifact_paths_are_documented` require an explanation in the
docs.

## 6. Consumer migration

The consuming project removes its skill directory (SKILL.md, four reference
documents, the script), both sandbox hooks from `.superpowers/hooks.yaml`, and
the `git worktree` shell wrappers — the orchestrator already creates worktrees,
so the wrappers duplicate it. It adds a `sandbox` block, adds
`.superpowers/sandbox/` to `.gitignore`, and updates the invariant recorded in
its instructions file. Its `docker-compose.yml` is unchanged; anything already
reading `LOOPBACK_IP` from the environment keeps working.

## 7. Acceptance criteria

1. Two slices dispatched in parallel from one branch receive distinct compose
   projects and distinct loopback addresses.
2. The dispatched agent's environment carries `LOOPBACK_IP` and every declared
   template variable, with no hook and no stdout parsing involved.
3. A failed slice leaves its volumes intact and its containers stopped; a
   verified-closed slice leaves neither.
4. A project hook observes the live stack before teardown.
5. On a platform without configured loopback aliases, the first failure names
   the remediation command and does not report 254 busy octets.
6. With no `sandbox` block, no docker invocation occurs and no sandbox variable
   is injected. The only environment difference from 2.0.0 is the additive
   `SUPERPOWERS_*` slice context of §4.5.
7. Full suite green; version `2.1.0` consistent across both manifests.
8. On the consuming project, the free tier of verification passes: `up`,
   `status`, `env` and `teardown` against its real compose stack, plus two
   branches holding two independent stacks at once.
9. One real dispatch proves the end-to-end path. This tier spends money on the
   harness and runs only on explicit instruction.

## 8. Risks

**Behavioural change to a published contract.** Moving `on_slice_{role}_start`
after worktree creation changes 2.0.0 semantics. Mitigation: the single known
consumer's hook is order-independent, and the change ships documented in the
release notes rather than being discovered.

**Widening the plugin's identity.** Docker becomes a concern of a plugin whose
job is agent orchestration. Mitigation: opt-in, inert by default, and an
explicit test (§5.6) that fails if inertness regresses.

**Destructive automation.** The orchestrator gains the ability to destroy
volumes on a status transition. Mitigation: destruction is confined to
`VERIFIED_CLOSED`, where the slice has already merged; failure is graded down
to stopping containers; and the human-facing `teardown` keeps its confirmation
gate.

**Reimplementation risk.** Rewriting under TDD rather than porting means
behaviour present in the untested original may be dropped. Mitigation: the
original stays readable in the consumer's history until the migration is
verified, and its documented command surface is enumerated as the target.
