# Configuration Reference

The orchestrator reads `.superpowers/agents.yaml` from the target project root. If this file is absent, built-in defaults are used.

## Override Behavior

A partial override in `.superpowers/agents.yaml` **deep-merges** over the defaults. You do not need to repeat the entire config — only specify the fields you want to change. Unknown keys and unknown statuses are rejected at load time with an error.

Global `harness.default` and `harness.provider` are inherited by agents that do not set their own `harness` or `provider` fields.

## Full Schema

```yaml
# .superpowers/agents.yaml

# Global harness defaults (inherited by agents that don't override)
harness:
  default: opencode
  provider: opencode-go

# Extensible State Machine
state_machine:
  valid_statuses:
    - DRAFT_SPEC
    - SPEC_APPROVED
    - PLANNING
    - PLAN_GENERATED
    - PLAN_APPROVED
    - EXECUTING
    - EXECUTION_COMPLETE
    - FAILED
    - MERGE_CONFLICT
    - VERIFIED_CLOSED
  transitions:
    DRAFT_SPEC: ["SPEC_APPROVED"]
    SPEC_APPROVED: ["PLANNING", "DRAFT_SPEC"]
    PLANNING: ["PLAN_GENERATED", "FAILED"]
    PLAN_GENERATED: ["PLAN_APPROVED", "PLANNING"]
    PLAN_APPROVED: ["EXECUTING", "PLAN_GENERATED"]
    EXECUTING: ["EXECUTION_COMPLETE", "MERGE_CONFLICT", "FAILED"]
    EXECUTION_COMPLETE: ["VERIFIED_CLOSED", "EXECUTING", "MERGE_CONFLICT"]
    FAILED: ["SPEC_APPROVED", "PLAN_APPROVED"]
    VERIFIED_CLOSED: []
    MERGE_CONFLICT: ["VERIFIED_CLOSED", "EXECUTING", "PLAN_APPROVED"]

# Agent definitions
agents:
  planner:
    model: kimi-k3
    harness: opencode
    provider: opencode-go
    allowed_statuses:
      - SPEC_APPROVED
    in_progress_status: PLANNING
    success_status: PLAN_GENERATED
    isolated_worktree: false
    prompt_template: 'Read spec at {file} and create detailed TDD implementation plan using writing-plans skill. Save to docs/superpowers/plans/'
    extra_args: []

  executor:
    model: minimax-m3
    harness: opencode
    provider: opencode-go
    allowed_statuses:
      - PLAN_APPROVED
    in_progress_status: EXECUTING
    success_status: EXECUTION_COMPLETE
    isolated_worktree: true
    prompt_template: 'Execute implementation plan at {file} using TDD subagent execution. Check off tasks in plan as completed.'
    extra_args: []
```

## Agent Properties

| Property | Type | Description |
|----------|------|-------------|
| `model` | string | LLM model identifier (e.g. `kimi-k3`, `minimax-m3`) |
| `harness` | string | CLI harness name (`opencode`, or custom) |
| `provider` | string | LLM provider (e.g. `opencode-go`, `anthropic`) |
| `allowed_statuses` | list | Statuses from which this agent can be dispatched |
| `in_progress_status` | string | Status to set before launching the agent |
| `success_status` | string | Status set by the orchestrator when the agent exits 0 |
| `isolated_worktree` | bool | Whether to run in an isolated git worktree |
| `prompt_template` | string | Task prompt with `{file}` placeholder |
| `extra_args` | list | Additional CLI flags (supports `{provider}` interpolation) |
| `harness_adapter` | string | Path to a custom Python adapter file |

## Adding Custom Agents

Add any role to the `agents` section. Declare `success_status` alongside
`in_progress_status`: it is the status the supervisor sets when the agent exits
`0`. Without it the agent runs, but its outcome is never recorded — the
supervisor logs a warning and the slice keeps its in-progress status.

```yaml
state_machine:
  valid_statuses:
    - DRAFT_SPEC
    - SPEC_APPROVED
    - PLANNING
    - PLAN_GENERATED
    - PLAN_APPROVED
    - EXECUTING
    - EXECUTION_COMPLETE
    - REVIEWING          # new
    - REVIEW_PASSED      # new
    - FAILED
    - MERGE_CONFLICT
    - VERIFIED_CLOSED
  transitions:
    EXECUTION_COMPLETE: ["REVIEWING", "VERIFIED_CLOSED", "EXECUTING", "MERGE_CONFLICT"]
    REVIEWING: ["REVIEW_PASSED", "FAILED"]
    REVIEW_PASSED: ["VERIFIED_CLOSED", "EXECUTING"]
    FAILED: ["SPEC_APPROVED", "PLAN_APPROVED", "EXECUTION_COMPLETE"]

agents:
  reviewer:
    model: claude-opus-4
    harness: opencode
    provider: anthropic
    allowed_statuses:
      - EXECUTION_COMPLETE
    in_progress_status: REVIEWING
    success_status: REVIEW_PASSED
    isolated_worktree: false
    prompt_template: 'Review the code changes for {file} and provide feedback.'
```

Every status a role names must appear in `valid_statuses`, and both
`in_progress_status` and `success_status` must be reachable by a declared
transition — otherwise the config is rejected at load time. Give `FAILED` a
transition back to your new role's entry gate, or a failed run of that role has
nowhere to return to.

Adding a role also extends the hook event set below: a `reviewer` role makes
`on_slice_reviewer_start`, `on_reviewer_complete` and `on_reviewer_failed` valid
keys in `hooks.yaml`.

## Infrastructure Hooks (`.superpowers/hooks.yaml`)

Optional. Lets a project prepare and tear down its own environment around a
dispatch — container stacks, per-branch network isolation, cache warming.

| Event | Fired by | When |
|-------|----------|------|
| `on_slice_{role}_start` | `dispatch-agent` | before the supervisor is spawned |
| `on_{role}_complete` | supervisor | the agent exited `0` |
| `on_{role}_failed` | supervisor | the agent exited non-zero |
| `on_slice_verified_closed` | `set-status` | after a successful merge |

With the default roles the full set is `on_slice_planner_start`,
`on_planner_complete`, `on_planner_failed`, `on_slice_executor_start`,
`on_executor_complete`, `on_executor_failed` and `on_slice_verified_closed`.

**A key outside this set never fires.** The orchestrator reports it as an
unknown event when it loads `hooks.yaml`, listing the valid names — silence
there once hid a project's environment hook that had never run.

| Hook property | Type | Description |
|---------------|------|-------------|
| `command` | string | Shell command, run with the project root as its working directory |
| `capture_env` | bool | Parse stdout for `KEY=VALUE` / `export KEY=VALUE` and pass them into the agent's environment |

```yaml
# .superpowers/hooks.yaml
hooks:
  on_slice_executor_start:
    command: "python .claude/skills/sandbox-loopback/scripts/sandbox_loopback.py up"
    capture_env: true

  on_executor_failed:
    command: "python .claude/skills/sandbox-loopback/scripts/sandbox_loopback.py teardown --yes"

  on_slice_verified_closed:
    command: "python .claude/skills/sandbox-loopback/scripts/sandbox_loopback.py teardown --yes"
```

A failing `on_slice_{role}_start` aborts the dispatch **before** the slice's
status is touched and releases the lock, so the slice stays at its entry gate.
A failing completion hook is reported but does not overwrite the outcome the
supervisor already recorded.

## Sandbox (per-slice infrastructure)

Optional, and **opt-in**: with no `sandbox` block in `.superpowers/agents.yaml`
— or with `sandbox.enabled` left at its default of `false` — the orchestrator
makes **no docker call at all**. Nothing about dispatch, teardown, or status
touches a container runtime unless a project's config asks for one explicitly.

When enabled, each slice's compose stack is published on its own
`127.0.0.x` loopback address and its own compose project, so parallel
worktrees never contend for a host port. Container-to-container traffic is
unaffected — only host-side publishing is rebound.

### Full schema

```yaml
sandbox:
  enabled: true                      # opt in; false (the default) means no docker call is ever made
  compose_file: docker-compose.yml   # path to the compose file, relative to the project root
  health_service: postgres           # optional; a compose service to await via `docker compose ps`
  health_timeout: 60                 # seconds to wait for health_service to report healthy
  env:
    pg_dsn: "postgresql://user:pass@{ip}:5432/db"
    qdrant_url: "http://{ip}:6333"
  teardown:
    on_verified_closed: volumes      # volumes | containers | none
    on_failed: containers
```

| Property | Type | Description |
|----------|------|-------------|
| `enabled` | bool | Opt-in switch. `false` (the default) means the orchestrator never invokes docker. |
| `compose_file` | string | Path to the `docker compose` file, relative to the project root. |
| `health_service` | string or null | A compose service name to poll with `docker compose ps` before the dispatched agent is allowed to proceed. Omit (`null`) to skip the wait. |
| `health_timeout` | int | Seconds to wait for `health_service` to report `healthy` before dispatch fails with `SandboxError`. |
| `env` | mapping | Extra environment variables passed to the dispatched agent. Values may reference the two template tokens below and `${VAR}` from the parent process environment. |
| `teardown.on_verified_closed` | string | Teardown mode run when a slice's status becomes `VERIFIED_CLOSED`. |
| `teardown.on_failed` | string | Teardown mode run when the dispatched agent exits non-zero. |

### Template tokens

`sandbox.env` values may contain exactly two substitutions:

| Token | Expands to |
|-------|------------|
| `{ip}` | The loopback address allocated to this slice's branch (e.g. `127.0.0.78`). |
| `{project}` | The compose project name derived from the branch. |

Any other `{...}` token — including a typo like `{IP}` — is rejected with
`ConfigError` at load time, before any agent is dispatched. `${VAR}` expands
first, from the parent process environment, so a real credential can be
sourced from `.env` rather than committed to a tracked config file.

**`LOOPBACK_IP` and `COMPOSE_PROJECT_NAME` are injected unconditionally and are
not declarable.** They are the contract every compose file and hook can rely
on, not a setting a project chooses — they are not keys you write under
`sandbox.env`, and the two tokens above are how you reference their values
inside your own `env` templates.

### Teardown modes

`TEARDOWN_MODES` is the enum both `teardown.on_verified_closed` and
`teardown.on_failed` must be one of:

| Mode | Destroys |
|------|----------|
| `volumes` | `docker compose down -v` — stops and removes containers, networks, **and volumes**, and releases the loopback address (the sandbox state record is deleted). |
| `containers` | `docker compose down` (no `-v`) — stops and removes containers, but keeps volumes and the state record, so a re-`up` returns the same address and the same data. |
| `none` | Nothing. The stack is left running untouched. |

The one state invariant: **the state record is deleted if and only if the
volumes are destroyed.** `containers` mode — the default for `on_failed` —
keeps a failure diagnosable; `volumes` mode — the default for
`on_verified_closed` — releases the address a closed slice no longer needs.

### `isolated_worktree` decides who gets a sandbox

There is no separate on/off switch per agent. Whether a dispatch brings a
stack up, attaches to an existing one, or touches nothing at all follows
directly from the agent's `isolated_worktree` setting:

| `isolated_worktree` | On dispatch |
|---|---|
| `true` | `sandbox.ensure_up(...)` — allocates an address if needed, brings the stack up, injects the environment. This agent owns the stack's lifecycle. |
| `false` | `sandbox.resolve_env(...)` — injects the environment **only if** a stack already exists for the current branch; never brings one up. |

A non-isolated agent runs on the human's own branch, so it attaches to
whatever stack is already there instead of starting a competing one.

### Fail closed on a missing address: `${LOOPBACK_IP:?}`

The project's own `docker-compose.yml` must publish ports through
`${LOOPBACK_IP:?}`, not a hardcoded `127.0.0.1` or a bare `${LOOPBACK_IP}`:

```yaml
services:
  postgres:
    ports:
      - "${LOOPBACK_IP:?}:5432:5432"
```

The `:?` suffix makes `docker compose` refuse to start if `LOOPBACK_IP` is
unset, rather than silently publishing on every interface or colliding with
another slice's stack on `127.0.0.1`. This is the compose file's half of the
contract — the orchestrator's half is injecting `LOOPBACK_IP` before every
`ensure_up`.
