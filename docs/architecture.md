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
