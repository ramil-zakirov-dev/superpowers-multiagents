# Architecture

The superpowers-multiagents orchestrator follows a modular, plugin-driven architecture.

## Module Structure

```
scripts/
├── orchestrator.py          # CLI entry point & command handlers
├── config.py                # DEFAULT_CONFIG & agents.yaml loader
├── errors.py                # Exception hierarchy; library modules raise, process boundaries exit
├── paths.py                 # Runtime artifact layout under `.superpowers/`
├── runner.py                # Supervisor owning one dispatched agent from spawn to terminal status
├── frontmatter.py           # YAML frontmatter parsing & atomic status updates
├── git_ops.py               # Git worktree creation, merge & cleanup
├── hooks.py                 # Infrastructure hook loading & execution
├── locks.py                 # File-based slice locking (concurrency control)
├── dependencies.py          # Slice dependency checking
├── utils.py                 # ID validation, YAML conversion, project root
└── adapters/
    ├── __init__.py           # Public adapter API
    ├── base.py               # HarnessAdapter abstract base class
    ├── opencode.py           # OpenCode CLI adapter (default)
    └── loader.py             # Dynamic adapter resolution & custom loading
```

## Design Principles

- **Single Responsibility**: Each module handles exactly one concern.
- **Open/Closed**: New harnesses are added by creating a new adapter file, not by modifying the orchestrator.
- **Dependency Inversion**: The orchestrator depends on `HarnessAdapter` abstraction, not on concrete CLI tools.

## Adapter System

The orchestrator dispatches agents to background CLI processes. The **adapter** translates an agent's config into a concrete argument vector — never a shell command string, so there is no shell to inject through.

### Built-in Adapters

| Adapter | Harness Key | CLI Command Pattern |
|---------|-------------|---------------------|
| `OpenCodeAdapter` | `opencode` | `opencode run --model <provider>/<model> <prompt>` |

The `build_command()` method returns `list[str]`, which is passed to the supervisor with `shell=False` for safe argument handling.

### Custom Adapters

Users can create their own adapter by:

1. Creating a Python file (e.g. `.superpowers/scripts/my_adapter.py`)
2. Subclassing `HarnessAdapter` and implementing `build_command()`
3. Referencing it in `agents.yaml`:

```yaml
agents:
  my_agent:
    harness_adapter: './scripts/my_adapter.py'
```

## Supervision

`dispatch-agent` does not run the agent. It spawns `scripts/runner.py`, a
supervisor that owns the agent for its whole lifetime, and returns
immediately.

    dispatch-agent  ->  runner (background)  ->  agent CLI
                            |
                            +-- captures stdout+stderr to .superpowers/logs/
                            +-- claims the slice lock with its own PID
                            +-- on exit 0   -> the role's success_status
                            +-- on exit !=0 -> FAILED
                            +-- fires on_<role>_complete / on_<role>_failed
                            +-- releases the lock, on every path

The terminal status is derived from the child's exit code rather than asked
of the agent in its prompt. An agent that crashes, or simply never reports,
therefore cannot strand a slice: it lands in `FAILED`, which transitions back
to the gate it came from.

The agent command is passed as an argument vector and spawned with
`shell=False`. No shell parses a prompt, a path, or a configured argument at
any point.

### Dispatch ordering

The order of steps in `cmd_dispatch_agent` is load-bearing, not incidental.
Every step that can fail runs **before** the first irreversible mutation, so a
failed precondition never leaves a slice that has to be repaired by hand.

As of 2.1.0 the sequence is worktree → sandbox → hook → adapter → status →
spawn:

```
1. resolve + validate config          6. fire on_slice_{role}_start (may fail)
2. dependency gate, state gate        7. resolve adapter + argv     (may fail)
3. acquire lock                       8. set in_progress_status  <- first mutation
4. create worktree             (may fail)   9. spawn supervisor
5. bring up / resolve sandbox   (may fail)
```

**`on_slice_{role}_start` moved after worktree creation in 2.1.0.** In 2.0.0 it
was step 4, firing before the worktree existed; a hook could act on neither
the checked-out files nor any per-slice infrastructure. It now runs after both
step 4 (worktree) and step 5 (sandbox), so a project hook can inspect the
worktree's contents and read `LOOPBACK_IP` — and any other sandbox-rendered
variable — from its environment. This is a behavioural change to a published
contract: a `hooks.yaml` written against 2.0.0's ordering still runs, but a
hook that assumed "no worktree yet" no longer holds.

Anything failing in steps 4–7 releases the lock and exits non-zero with the
slice still at its entry status. Step 8 is also checked: if the transition is
rejected, the lock is released and nothing is spawned.

The worktree created in step 4 is **intentionally left in place** if a later
step in the block (sandbox, hook, adapter resolution, or the status
transition) fails. `create_git_worktree` is idempotent — it returns the
existing path, or reattaches to the existing branch, if `.worktrees/<slice_id>`
is already there — so a retried dispatch reuses it rather than needing it
rebuilt. Tearing it down automatically on every failure would risk discarding
work a hook or human already put there, for no benefit a later retry
couldn't get for free.

### Sandbox lifecycle

A per-slice sandbox, once brought up, is torn down at exactly two sites, and
never anywhere else:

| Site | Fires after | Mode (config key) | Default `docker compose` action |
| :--- | :--- | :--- | :--- |
| `runner.py`, on agent exit (isolated agent only) | `on_{role}_failed` | `sandbox.teardown.on_failed` | `containers` → `down` |
| `orchestrator.py cmd_set_status`, on `VERIFIED_CLOSED` | `on_slice_verified_closed` | `sandbox.teardown.on_verified_closed` | `volumes` → `down -v` |

The failure-teardown site applies only to agents with `isolated_worktree:
true`. A non-isolated agent never owns a stack's lifecycle — it only ever
`resolve_env`s an existing stack, never `ensure_up`s one — so its crash never
tears down the stack it merely attached to.

**Teardown always follows the corresponding hook, never precedes or replaces
it.** Both call sites run the hook first and only then call
`sandbox.tear_down`; both wrap the teardown call in its own `try/except` that
downgrades a failure to a warning, because the slice's outcome — `FAILED` or
`VERIFIED_CLOSED` — was already recorded by the time teardown runs, and a
container that won't stop must not overturn it.

**State invariant: the allocation record under `.superpowers/sandbox/` is
deleted if and only if the volumes are destroyed.** `tear_down` calls
`clear_state` only when `mode == "volumes"`; a `containers`-mode teardown
(the failure path's default) stops the stack but keeps its recorded IP and
project name, so a subsequent dispatch for the same branch reconnects to the
same address instead of allocating a new one. Only a `volumes` teardown — the
verified-closed path's default — clears the record, because at that point
there is no more data to reconnect to.

### Runtime artifacts

All runtime state is derived from the project root, never from the current
working directory — the executor runs with `cwd=<worktree>`, so a relative path
would split a directory from the file written into it.

```
<project root>/.superpowers/logs/<role>_<stem>.log
<project root>/.superpowers/locks/<slice_id>.lock
<project root>/.superpowers/sandbox/<project_name>.json
<project root>/.worktrees/<slice_id>/
```

The `.superpowers/sandbox/` directory holds orchestrator state (allocation records for docker-compose projects) rather than slice payload, which is why it does not live under `.worktrees/`.

`git_ops.check_working_tree_clean` ignores exactly these prefixes, so the
orchestrator's own output cannot block its own merge. The user's `.gitignore`
is never written to; `dispatch-agent` prints a hint instead.

### Lock ownership

Acquisition and ownership are deliberately separate. The dispatcher creates the
lock atomically (`O_CREAT | O_EXCL`) in state `starting` and exits; the
supervisor it spawned rewrites the lock with its own PID and state `running`,
and removes it in a `finally`. A `starting` lock is honoured for a bounded grace
window so the gap between those two events is not a hole, and a `running` lock
whose PID is dead is reclaimed.

## Configuration

See [configuration.md](configuration.md) for the full `agents.yaml` schema.

## State Machine

The default lifecycle has 10 states. Both statuses and transitions can be overridden in `agents.yaml`.

```
DRAFT_SPEC → SPEC_APPROVED → PLANNING → PLAN_GENERATED → PLAN_APPROVED → EXECUTING → EXECUTION_COMPLETE → VERIFIED_CLOSED
                                                                                         ↘ MERGE_CONFLICT ↗
                                              ↘ FAILED (orchestrator exit !=0) ↗
```

`FAILED` is set by the orchestrator, from the dispatched agent's exit code, never by the agent itself. It returns the slice to the gate it came from — `PLANNING → FAILED → SPEC_APPROVED`, `EXECUTING → FAILED → PLAN_APPROVED` — so a crashed or misbehaving agent never strands a slice.
