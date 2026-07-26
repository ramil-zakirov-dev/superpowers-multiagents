# Architecture

The superpowers-multiagents orchestrator follows a modular, plugin-driven architecture.

## Module Structure

```
scripts/
├── orchestrator.py          # CLI entry point & command handlers
├── config.py                # DEFAULT_CONFIG & agents.yaml loader
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

The orchestrator dispatches agents to background CLI processes. The **adapter** translates an agent's config into a concrete shell command.

### Built-in Adapters

| Adapter | Harness Key | CLI Command Pattern |
|---------|-------------|---------------------|
| `OpenCodeAdapter` | `opencode` | `opencode run --model <model> "<prompt>"` |

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

## Configuration

See [configuration.md](configuration.md) for the full `agents.yaml` schema.

## State Machine

The default lifecycle has 9 states. Both statuses and transitions can be overridden in `agents.yaml`.

```
DRAFT_SPEC → SPEC_APPROVED → PLANNING → PLAN_GENERATED → PLAN_APPROVED → EXECUTING → EXECUTION_COMPLETE → VERIFIED_CLOSED
                                                                                         ↘ MERGE_CONFLICT ↗
```
