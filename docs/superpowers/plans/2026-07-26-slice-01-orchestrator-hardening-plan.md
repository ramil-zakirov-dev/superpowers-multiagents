---
slice_id: "slice-01-orchestrator-hardening"
title: "Orchestrator hardening — TDD implementation plan"
status: PLAN_GENERATED
spec: "docs/superpowers/specs/2026-07-26-slice-01-orchestrator-hardening-design.md"
depends_on: []
---

# Orchestrator Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the orchestrator run as documented, and make its advertised invariants — concurrency, audit logging, strict state transitions, merge automation — actually hold.

**Architecture:** A new `scripts/runner.py` supervisor owns each dispatched agent for its whole lifetime: it captures the agent's output, derives the slice's terminal status from the child's exit code, and releases the lock on every exit path. `cmd_dispatch_agent` becomes a thin, `shell=False` spawn of that supervisor. Around this core, configuration gains a recursive deep merge with fail-closed validation, runtime artifacts move under `.superpowers/`, and the library modules stop calling `sys.exit`.

**Tech Stack:** Python 3.10+, `ruamel.yaml` (round-trip mode), `pytest`, `subprocess`, `git` CLI.

**Working branch:** `feat/slice-01-orchestrator-hardening` in `<plugin root>`. The spec is at `docs/superpowers/specs/2026-07-26-slice-01-orchestrator-hardening-design.md`.

## Global Constraints

- **Python 3.10+.** `scripts/utils.py` already uses `X | Y` union syntax; do not lower the floor.
- **Dependencies:** `ruamel.yaml>=0.18.5` only, at runtime. `pytest>=8.0` is a development dependency. Do not add any other runtime dependency.
- **No live harness in any test — binding.** `opencode` is installed on this machine and a real run costs money. Tests that need a dispatch must use a stub adapter written into a temporary project via the `harness_adapter` key. Never let a test invoke `opencode`.
- **Cross-platform:** Windows, macOS and Linux. Never hardcode `cmd.exe`, backslash separators, or a POSIX-only spawn flag without the matching branch.
- **Target version:** `2.0.0` in both `.claude-plugin/plugin.json` and `package.json`. Breaking config changes are accepted deliberately; do not write a migration path.
- **Documentation language:** English, matching the existing repo docs.
- **Commits:** Conventional Commits, ending with the trailer `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- **Test command:** `python -m pytest <path> -v -p no:cacheprovider` — always with `-p no:cacheprovider`.
- **Do not push.** Work stays on the branch until an explicit go-ahead.
- **Canonical hook events** (the only names the orchestrator emits): `on_slice_{role}_start`, `on_{role}_complete`, `on_{role}_failed`, `on_slice_verified_closed`.

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `scripts/errors.py` | Exception hierarchy. Library modules raise; only process boundaries exit. |
| `scripts/paths.py` | Filesystem layout of runtime artifacts, resolved from `project_root`, never CWD. |
| `scripts/runner.py` | Supervisor process: one dispatched agent, from spawn to terminal status. |
| `tests/conftest.py` | Shared fixtures: temporary git project, stub adapter. |
| `tests/test_paths.py` | Artifact path layout. |
| `tests/test_config.py` | Deep merge, inheritance, validation. |
| `tests/test_locks.py` | Acquire/claim/release semantics and the start grace window. |
| `tests/test_runner.py` | Supervisor behaviour on success, failure and exception. |
| `tests/test_dispatch_integration.py` | Full `cmd_dispatch_agent` in a temporary git repo. |
| `tests/test_hook_events.py` | Canonical event names and unknown-event warning. |
| `tests/test_entrypoint.py` | Both invocation forms of the CLI. |

**Modified:** `scripts/orchestrator.py`, `scripts/config.py`, `scripts/locks.py`, `scripts/git_ops.py`, `scripts/hooks.py`, `scripts/utils.py`, `scripts/dependencies.py`, `scripts/adapters/base.py`, `scripts/adapters/opencode.py`, `scripts/adapters/loader.py`, `tests/test_orchestrator.py`, `hooks/run-hook.cmd`, `hooks/hooks.json`, `skills/multiagent-orchestrator/SKILL.md`, `README.md`, `docs/architecture.md`, `docs/configuration.md`, `.claude-plugin/plugin.json`, `package.json`, `requirements.txt`.

---

### Task 1: Exception hierarchy

Library modules currently call `sys.exit`, which makes every failure path untestable. Replace with exceptions.

**Files:**
- Create: `scripts/errors.py`
- Modify: `scripts/utils.py:17-23`, `scripts/adapters/loader.py:21-68`
- Test: `tests/test_orchestrator.py:390-399`, `tests/test_orchestrator.py:414-424`

**Interfaces:**
- Consumes: nothing.
- Produces: `OrchestratorError`, `ConfigError`, `LockError`, `GitError`, `HookError`, `ValidationError` — all importable from `scripts.errors`. `_sanitize_id(value, label) -> str` now raises `ValidationError` instead of exiting.

- [ ] **Step 1: Write the failing test**

Replace the two `SystemExit` tests in `tests/test_orchestrator.py`. Delete the existing `test_sanitize_id_rejects_shell_metacharacters` and `test_create_git_worktree_rejects_malicious_id` plus its `create_git_worktree_safe` helper, and add:

```python
from scripts.errors import ValidationError


def test_sanitize_id_rejects_shell_metacharacters():
    """IDs with shell metacharacters must raise, not exit the process."""
    for bad in ("slice; rm -rf /", "slice && curl evil.com", "slice | cat /etc/passwd"):
        with pytest.raises(ValidationError):
            _sanitize_id(bad)


def test_create_git_worktree_rejects_malicious_id():
    """Worktree creation with a shell-injection attempt must raise ValidationError."""
    from scripts.git_ops import create_git_worktree
    with tempfile.TemporaryDirectory() as tmpdir:
        with pytest.raises(ValidationError):
            create_git_worktree("foo; rm -rf /", project_root=Path(tmpdir))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_orchestrator.py -k "sanitize or malicious" -v -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.errors'`

- [ ] **Step 3: Write minimal implementation**

Create `scripts/errors.py`:

```python
"""Exception hierarchy for the orchestrator.

Library modules raise these. Only the process boundaries — orchestrator.py
and runner.py — translate them into exit codes. This is what makes the
failure paths testable.
"""


class OrchestratorError(Exception):
    """Base class for every orchestrator failure."""


class ConfigError(OrchestratorError):
    """Invalid or unusable agent configuration."""


class LockError(OrchestratorError):
    """A slice lock could not be acquired."""


class GitError(OrchestratorError):
    """A git operation failed."""


class HookError(OrchestratorError):
    """An infrastructure hook failed."""


class ValidationError(OrchestratorError):
    """A user-supplied identifier failed validation."""
```

In `scripts/utils.py`, replace the body of `_sanitize_id` and drop the now-unused `sys` usage there:

```python
from scripts.errors import ValidationError


def _sanitize_id(value: str, label: str = "ID") -> str:
    """Validates that a string is safe for use in git branch names and paths."""
    if not SAFE_ID_PATTERN.match(value):
        raise ValidationError(
            f"{label} '{value}' contains invalid characters. "
            f"Only alphanumeric characters, hyphens, underscores and dots are allowed."
        )
    return value
```

In `scripts/adapters/loader.py`, replace both `sys.exit(1)` calls with raises, and
stop the custom-adapter import from writing `__pycache__/` into the user's
repository — that directory is not ours to create, and it makes the user's tree
dirty for the merge gate:

```python
from scripts.errors import ConfigError

# in get_harness_adapter, replacing the print+sys.exit block:
    if adapter_cls is None:
        raise ConfigError(
            f"Unknown harness type '{harness_type}'. "
            f"Available: {sorted(_BUILTIN_ADAPTERS)}"
        )
```

Replace `_load_custom_adapter` entirely:

```python
def _load_custom_adapter(relative_path: str, project_root: Path) -> HarnessAdapter:
    """Import a custom adapter from a project-local Python file.

    Bytecode writing is suppressed for the duration: the adapter lives in the
    user's repository, and dropping a `__pycache__/` beside it would leave
    their working tree dirty.
    """
    adapter_file = (Path(project_root) / relative_path).resolve()
    if not adapter_file.exists():
        raise ConfigError(f"Custom harness adapter not found: {adapter_file}")

    module_name = f"custom_adapter_{adapter_file.stem}"
    spec = importlib.util.spec_from_file_location(module_name, adapter_file)
    if spec is None or spec.loader is None:
        raise ConfigError(f"Could not load custom harness adapter: {adapter_file}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module

    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous

    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if (isinstance(attr, type)
                and issubclass(attr, HarnessAdapter)
                and attr is not HarnessAdapter):
            return attr()

    raise ConfigError(f"No HarnessAdapter subclass found in {adapter_file}.")
```

Add a test for the bytecode guard to `tests/test_orchestrator.py`:

```python
def test_custom_adapter_import_leaves_no_pycache(tmp_path):
    """A custom adapter must not dirty the user's tree with __pycache__."""
    from scripts.adapters.loader import get_harness_adapter

    (tmp_path / "my_adapter.py").write_text(
        "from scripts.adapters.base import HarnessAdapter\n"
        "class Mine(HarnessAdapter):\n"
        "    def build_command(self, agent_config, task_prompt):\n"
        "        return ['echo', task_prompt]\n",
        encoding="utf-8",
    )
    get_harness_adapter({"harness_adapter": "my_adapter.py"}, tmp_path)
    assert not (tmp_path / "__pycache__").exists()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_orchestrator.py -v -p no:cacheprovider`
Expected: PASS, 36 tests (the two rewritten tests now assert on `ValidationError`)

- [ ] **Step 5: Commit**

```bash
git add scripts/errors.py scripts/utils.py scripts/adapters/loader.py tests/test_orchestrator.py
git commit -m "refactor: raise OrchestratorError instead of sys.exit in library modules

Library modules calling sys.exit made every failure path untestable, which
is why cmd_dispatch_agent has no test coverage today.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Runtime artifact layout

Artifacts are written relative to CWD today, while the executor runs with `cwd=worktree` — `mkdir` and the redirect target end up in different directories. Move everything under `project_root/.superpowers/`.

**Files:**
- Create: `scripts/paths.py`
- Test: `tests/test_paths.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `superpowers_dir(project_root) -> Path`, `logs_dir(project_root) -> Path`, `log_path(project_root, role, stem) -> Path`, `locks_dir(project_root) -> Path`, `lock_path(project_root, slice_id) -> Path`, `is_artifact_path(rel_path: str) -> bool`, and the constant `ARTIFACT_PREFIXES: tuple[str, ...]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_paths.py`:

```python
from pathlib import Path

from scripts.paths import (
    ARTIFACT_PREFIXES,
    is_artifact_path,
    lock_path,
    log_path,
    logs_dir,
    locks_dir,
)


def test_log_path_is_under_superpowers():
    root = Path("/proj")
    assert log_path(root, "executor", "slice-01-plan") == (
        root / ".superpowers" / "logs" / "executor_slice-01-plan.log"
    )


def test_lock_path_is_under_superpowers():
    root = Path("/proj")
    assert lock_path(root, "slice-01") == root / ".superpowers" / "locks" / "slice-01.lock"


def test_dirs_are_derived_from_project_root_not_cwd():
    root = Path("/some/other/place")
    assert logs_dir(root).is_absolute()
    assert locks_dir(root).is_absolute()
    assert str(logs_dir(root)).startswith(str(root))


def test_is_artifact_path_recognises_own_artifacts():
    assert is_artifact_path(".superpowers/logs/executor_x.log")
    assert is_artifact_path(".superpowers/locks/slice-01.lock")
    assert is_artifact_path(".worktrees/slice-01/")


def test_is_artifact_path_normalises_separators_and_dot_prefix():
    assert is_artifact_path(".superpowers\\logs\\executor_x.log")
    assert is_artifact_path("./.superpowers/logs/executor_x.log")


def test_is_artifact_path_rejects_user_files():
    assert not is_artifact_path("src/main.py")
    assert not is_artifact_path(".superpowersfoo/x")
    assert not is_artifact_path("docs/superpowers/specs/design.md")


def test_artifact_prefixes_are_declared():
    assert ".superpowers/logs/" in ARTIFACT_PREFIXES
    assert ".superpowers/locks/" in ARTIFACT_PREFIXES
    assert ".worktrees/" in ARTIFACT_PREFIXES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_paths.py -v -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.paths'`

- [ ] **Step 3: Write minimal implementation**

Create `scripts/paths.py`:

```python
"""Filesystem layout for orchestrator runtime artifacts.

Everything the orchestrator writes at runtime lives under
`<project_root>/.superpowers/`, so one ignore rule covers it all. Paths are
always derived from an explicit project root and never from the current
working directory: the executor runs with `cwd=<worktree>`, so a relative
path would split `mkdir` from its redirect target.
"""

from pathlib import Path

SUPERPOWERS_DIRNAME = ".superpowers"

#: Paths, relative to the project root, that the orchestrator itself creates.
#: `git_ops.check_working_tree_clean` ignores these when deciding cleanliness,
#: so the orchestrator's own artifacts cannot block its own merge.
ARTIFACT_PREFIXES: tuple[str, ...] = (
    ".superpowers/logs/",
    ".superpowers/locks/",
    ".worktrees/",
)


def superpowers_dir(project_root: Path) -> Path:
    return Path(project_root) / SUPERPOWERS_DIRNAME


def logs_dir(project_root: Path) -> Path:
    return superpowers_dir(project_root) / "logs"


def log_path(project_root: Path, role: str, stem: str) -> Path:
    return logs_dir(project_root) / f"{role}_{stem}.log"


def locks_dir(project_root: Path) -> Path:
    return superpowers_dir(project_root) / "locks"


def lock_path(project_root: Path, slice_id: str) -> Path:
    return locks_dir(project_root) / f"{slice_id}.lock"


def is_artifact_path(rel_path: str) -> bool:
    """True if a repo-relative path is an orchestrator runtime artifact.

    Note the explicit `./` handling: `str.lstrip("./")` would strip the
    leading dot of `.superpowers/` and silently stop matching.
    """
    normalized = rel_path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return any(normalized.startswith(prefix) for prefix in ARTIFACT_PREFIXES)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_paths.py -v -p no:cacheprovider`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add scripts/paths.py tests/test_paths.py
git commit -m "feat: add paths module for runtime artifact layout

Artifacts move under project_root/.superpowers/ and are always resolved
from an explicit root, never CWD — the executor runs with cwd=worktree.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Recursive config merge and harness inheritance

Today `config["agents"].update(...)` wipes an agent's defaults when the user overrides one key — which silently removes `allowed_statuses` and disables the state gate — while `state_machine` is replaced wholesale, producing `KeyError: 'valid_statuses'`. The global `harness` section is never read at all.

**Files:**
- Modify: `scripts/config.py:64-88`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `ConfigError` from Task 1.
- Produces: `deep_merge(base: dict, override: dict) -> dict`, `load_agent_config(project_root: Path) -> dict`, `resolve_agent(config: dict, role: str) -> dict`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_config.py`:

```python
import pytest

from scripts.config import DEFAULT_CONFIG, deep_merge, load_agent_config, resolve_agent
from scripts.errors import ConfigError


def _write_config(project_root, text):
    sp = project_root / ".superpowers"
    sp.mkdir(parents=True, exist_ok=True)
    (sp / "agents.yaml").write_text(text, encoding="utf-8")


def test_deep_merge_merges_mappings_key_by_key():
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    assert deep_merge(base, {"a": {"y": 9}}) == {"a": {"x": 1, "y": 9}, "b": 3}


def test_deep_merge_replaces_lists_wholesale():
    assert deep_merge({"a": [1, 2, 3]}, {"a": [9]}) == {"a": [9]}


def test_deep_merge_does_not_mutate_base():
    base = {"a": {"x": 1}}
    deep_merge(base, {"a": {"x": 2}})
    assert base == {"a": {"x": 1}}


def test_partial_state_machine_override_inherits_valid_statuses(tmp_path):
    """Overriding only transitions must not drop valid_statuses."""
    _write_config(tmp_path, "state_machine:\n  transitions:\n    DRAFT_SPEC: [SPEC_APPROVED]\n")
    config = load_agent_config(tmp_path)
    assert config["state_machine"]["valid_statuses"] == DEFAULT_CONFIG["state_machine"]["valid_statuses"]
    assert config["state_machine"]["transitions"]["DRAFT_SPEC"] == ["SPEC_APPROVED"]


def test_partial_agent_override_keeps_the_state_gate(tmp_path):
    """Overriding only `model` must not silently disable allowed_statuses."""
    _write_config(tmp_path, "agents:\n  planner:\n    model: my-model\n")
    config = load_agent_config(tmp_path)
    planner = config["agents"]["planner"]
    assert planner["model"] == "my-model"
    assert planner["allowed_statuses"] == ["SPEC_APPROVED"]
    assert planner["in_progress_status"] == "PLANNING"
    assert "{file}" in planner["prompt_template"]


def test_global_harness_is_inherited_by_agents(tmp_path):
    _write_config(tmp_path, "harness:\n  default: myharness\n  provider: myprovider\n")
    config = load_agent_config(tmp_path)
    # The defaults declare an explicit harness, so clear it to observe inheritance.
    del config["agents"]["planner"]["harness"]
    del config["agents"]["planner"]["provider"]
    planner = resolve_agent(config, "planner")
    assert planner["harness"] == "myharness"
    assert planner["provider"] == "myprovider"


def test_explicit_agent_harness_wins_over_global(tmp_path):
    _write_config(
        tmp_path,
        "harness:\n  default: myharness\nagents:\n  planner:\n    harness: opencode\n",
    )
    config = load_agent_config(tmp_path)
    assert resolve_agent(config, "planner")["harness"] == "opencode"


def test_resolve_agent_rejects_unknown_role(tmp_path):
    config = load_agent_config(tmp_path)
    with pytest.raises(ConfigError, match="reviewer"):
        resolve_agent(config, "reviewer")


def test_malformed_yaml_fails_closed(tmp_path):
    _write_config(tmp_path, "agents: [this is: broken\n")
    with pytest.raises(ConfigError):
        load_agent_config(tmp_path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -v -p no:cacheprovider`
Expected: FAIL with `ImportError: cannot import name 'deep_merge' from 'scripts.config'`

- [ ] **Step 3: Write minimal implementation**

Replace `load_agent_config` in `scripts/config.py` and add the two new functions. Keep `DEFAULT_CONFIG` as it is for now — Task 4 amends it.

```python
import copy
import logging
from pathlib import Path

from ruamel.yaml import YAML

from scripts.errors import ConfigError
from scripts.utils import _to_plain_dict

logger = logging.getLogger("orchestrator")


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge `override` into a copy of `base`.

    Mappings merge key by key so that a partial override inherits the rest of
    the defaults. Scalars and lists are replaced wholesale — a user who lists
    `allowed_statuses` means exactly that list, not an addition to ours.
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_agent_config(project_root: Path) -> dict:
    """Load `.superpowers/agents.yaml`, deep-merged over DEFAULT_CONFIG.

    Raises ConfigError if the file exists but cannot be parsed: a config we
    cannot read is not a reason to silently run with different settings.
    """
    config_file = Path(project_root) / ".superpowers" / "agents.yaml"
    if not config_file.exists():
        return copy.deepcopy(DEFAULT_CONFIG)

    try:
        yaml = YAML(typ="rt")
        parsed = yaml.load(config_file.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise ConfigError(f"Failed to parse {config_file}: {exc}") from exc

    return deep_merge(DEFAULT_CONFIG, _to_plain_dict(parsed))


def resolve_agent(config: dict, role: str) -> dict:
    """Return a copy of an agent's config with global harness defaults applied."""
    agents = config.get("agents") or {}
    if role not in agents:
        raise ConfigError(
            f"Agent role '{role}' is not defined in the configuration. "
            f"Defined roles: {sorted(agents)}"
        )
    agent = copy.deepcopy(agents[role])
    harness = config.get("harness") or {}
    if "harness" not in agent and harness.get("default"):
        agent["harness"] = harness["default"]
    if "provider" not in agent and harness.get("provider"):
        agent["provider"] = harness["provider"]
    return agent
```

Also fix the docstring lie noted in the spec: `load_agent_config` returns a dict, not a tuple. The docstring above already does.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py tests/test_orchestrator.py -v -p no:cacheprovider`
Expected: PASS — 10 new tests plus the existing suite

- [ ] **Step 5: Commit**

```bash
git add scripts/config.py tests/test_config.py
git commit -m "fix: deep-merge agents.yaml over defaults and honour global harness

A partial agent override used to wipe allowed_statuses, silently disabling
the state gate; a partial state_machine override raised KeyError. The global
harness section was never read.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: FAILED status, success_status, and fail-closed validation

Adds the state the supervisor needs and refuses configurations that cannot work.

**Files:**
- Modify: `scripts/config.py` (`DEFAULT_CONFIG`, new `validate_config`)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `deep_merge`, `ConfigError`.
- Produces: `validate_config(config: dict) -> None`, `KNOWN_AGENT_KEYS: frozenset[str]`. `DEFAULT_CONFIG` gains status `FAILED`, transitions into and out of it, and `success_status` on both agents.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
from scripts.config import KNOWN_AGENT_KEYS, validate_config


def test_failed_status_exists_and_is_reachable():
    sm = DEFAULT_CONFIG["state_machine"]
    assert "FAILED" in sm["valid_statuses"]
    assert "FAILED" in sm["transitions"]["PLANNING"]
    assert "FAILED" in sm["transitions"]["EXECUTING"]


def test_failed_returns_to_the_gate_it_came_from():
    assert set(DEFAULT_CONFIG["state_machine"]["transitions"]["FAILED"]) == {
        "SPEC_APPROVED",
        "PLAN_APPROVED",
    }


def test_agents_declare_success_status():
    agents = DEFAULT_CONFIG["agents"]
    assert agents["planner"]["success_status"] == "PLAN_GENERATED"
    assert agents["executor"]["success_status"] == "EXECUTION_COMPLETE"


def test_default_config_validates():
    validate_config(DEFAULT_CONFIG)


def test_validate_rejects_status_outside_valid_statuses(tmp_path):
    _write_config(tmp_path, "agents:\n  planner:\n    in_progress_status: NONSENSE\n")
    with pytest.raises(ConfigError, match="NONSENSE"):
        validate_config(load_agent_config(tmp_path))


def test_validate_rejects_unknown_agent_key(tmp_path):
    _write_config(tmp_path, "agents:\n  planner:\n    modle: typo\n")
    with pytest.raises(ConfigError, match="modle"):
        validate_config(load_agent_config(tmp_path))


def test_validate_rejects_unknown_transition_target(tmp_path):
    _write_config(tmp_path, "state_machine:\n  transitions:\n    DRAFT_SPEC: [NOWHERE]\n")
    with pytest.raises(ConfigError, match="NOWHERE"):
        validate_config(load_agent_config(tmp_path))


def test_validate_rejects_empty_valid_statuses():
    with pytest.raises(ConfigError, match="valid_statuses"):
        validate_config({"state_machine": {"valid_statuses": [], "transitions": {}}, "agents": {}})


def test_known_agent_keys_cover_the_documented_schema():
    assert "success_status" in KNOWN_AGENT_KEYS
    assert "harness_adapter" in KNOWN_AGENT_KEYS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -k "failed or success_status or validate or known_agent" -v -p no:cacheprovider`
Expected: FAIL with `ImportError: cannot import name 'KNOWN_AGENT_KEYS' from 'scripts.config'`

- [ ] **Step 3: Write minimal implementation**

In `scripts/config.py`, amend `DEFAULT_CONFIG`'s `state_machine` and `agents`:

```python
    "state_machine": {
        "valid_statuses": [
            "DRAFT_SPEC", "SPEC_APPROVED", "PLANNING", "PLAN_GENERATED",
            "PLAN_APPROVED", "EXECUTING", "EXECUTION_COMPLETE",
            "MERGE_CONFLICT", "FAILED", "VERIFIED_CLOSED"
        ],
        "transitions": {
            "DRAFT_SPEC": ["SPEC_APPROVED"],
            "SPEC_APPROVED": ["PLANNING", "DRAFT_SPEC"],
            "PLANNING": ["PLAN_GENERATED", "FAILED"],
            "PLAN_GENERATED": ["PLAN_APPROVED", "PLANNING"],
            "PLAN_APPROVED": ["EXECUTING", "PLAN_GENERATED"],
            "EXECUTING": ["EXECUTION_COMPLETE", "MERGE_CONFLICT", "FAILED"],
            "EXECUTION_COMPLETE": ["VERIFIED_CLOSED", "EXECUTING", "MERGE_CONFLICT"],
            "FAILED": ["SPEC_APPROVED", "PLAN_APPROVED"],
            "VERIFIED_CLOSED": [],
            "MERGE_CONFLICT": ["VERIFIED_CLOSED", "EXECUTING", "PLAN_APPROVED"]
        }
    },
```

Add `"success_status": "PLAN_GENERATED",` to the `planner` agent and `"success_status": "EXECUTION_COMPLETE",` to the `executor` agent, next to their existing `in_progress_status`.

Then append the validator:

```python
KNOWN_AGENT_KEYS = frozenset({
    "model",
    "harness",
    "provider",
    "allowed_statuses",
    "in_progress_status",
    "success_status",
    "isolated_worktree",
    "prompt_template",
    "extra_args",
    "harness_adapter",
})


def validate_config(config: dict) -> None:
    """Fail closed on a configuration that cannot work.

    Catching a typo here is the difference between a readable error and an
    agent dispatched with a silently disabled state gate.
    """
    state_machine = config.get("state_machine") or {}
    valid_statuses = state_machine.get("valid_statuses") or []
    if not valid_statuses:
        raise ConfigError("state_machine.valid_statuses is missing or empty.")
    known = set(valid_statuses)

    for source, targets in (state_machine.get("transitions") or {}).items():
        if source not in known:
            raise ConfigError(
                f"state_machine.transitions: unknown source status '{source}'. "
                f"Known statuses: {sorted(known)}"
            )
        for target in targets or []:
            if target not in known:
                raise ConfigError(
                    f"state_machine.transitions['{source}']: unknown target status "
                    f"'{target}'. Known statuses: {sorted(known)}"
                )

    for role, agent in (config.get("agents") or {}).items():
        unknown_keys = set(agent) - KNOWN_AGENT_KEYS
        if unknown_keys:
            raise ConfigError(
                f"agent '{role}': unknown key(s) {sorted(unknown_keys)}. "
                f"Known keys: {sorted(KNOWN_AGENT_KEYS)}"
            )
        for key in ("in_progress_status", "success_status"):
            value = agent.get(key)
            if value is not None and value not in known:
                raise ConfigError(
                    f"agent '{role}'.{key} = '{value}' is not in valid_statuses."
                )
        for status in agent.get("allowed_statuses") or []:
            if status not in known:
                raise ConfigError(
                    f"agent '{role}'.allowed_statuses contains unknown status '{status}'."
                )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py tests/test_orchestrator.py -v -p no:cacheprovider`
Expected: PASS — all config tests plus the existing suite

- [ ] **Step 5: Commit**

```bash
git add scripts/config.py tests/test_config.py
git commit -m "feat: add FAILED status, success_status, and fail-closed config validation

FAILED gives a crashed agent somewhere to land: PLANNING and EXECUTING were
dead ends with no rollback. success_status moves the terminal transition from
the agent's prompt to the orchestrator.

BREAKING CHANGE: agents.yaml gains success_status; FAILED joins valid_statuses.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Lock that actually holds

The lock records the orchestrator's PID, which dies immediately after dispatch, so every later acquisition treats it as stale. Split acquisition (atomic, by the dispatcher) from ownership (claimed by the supervisor), with a bounded grace window covering the spawn gap.

**Files:**
- Modify: `scripts/locks.py` (whole file)
- Test: `tests/test_locks.py`
- Modify: `tests/test_orchestrator.py:503-558` (the three existing lock tests)

**Interfaces:**
- Consumes: `lock_path` (Task 2), `LockError`, `ValidationError` (Task 1).
- Produces: `acquire_slice_lock(slice_id, project_root) -> Path` (raises `LockError`), `claim_slice_lock(lock_file: Path, pid: int, **meta) -> None`, `release_slice_lock_file(lock_file: Path) -> None`, `release_slice_lock(slice_id, project_root) -> None`, `LOCK_START_GRACE_SECONDS: int`.

Lock file schema:

```json
{"slice_id": "slice-01", "state": "starting|running", "pid": null,
 "started_at": 1753500000.0, "command": "...", "role": "executor", "model": "..."}
```

- [ ] **Step 1: Write the failing test**

Create `tests/test_locks.py`:

```python
import json
import os
import time

import pytest

from scripts.errors import LockError
from scripts.locks import (
    LOCK_START_GRACE_SECONDS,
    acquire_slice_lock,
    claim_slice_lock,
    release_slice_lock,
    release_slice_lock_file,
)
from scripts.paths import lock_path


def test_acquire_writes_starting_state(tmp_path):
    lock_file = acquire_slice_lock("slice-01", tmp_path)
    data = json.loads(lock_file.read_text(encoding="utf-8"))
    assert data["state"] == "starting"
    assert data["pid"] is None
    assert data["slice_id"] == "slice-01"


def test_claim_records_the_live_owner(tmp_path):
    lock_file = acquire_slice_lock("slice-01", tmp_path)
    claim_slice_lock(lock_file, os.getpid(), role="executor")
    data = json.loads(lock_file.read_text(encoding="utf-8"))
    assert data["state"] == "running"
    assert data["pid"] == os.getpid()
    assert data["role"] == "executor"


def test_claimed_lock_blocks_a_second_acquisition(tmp_path):
    """The defect: the old lock named a process that died immediately."""
    lock_file = acquire_slice_lock("slice-01", tmp_path)
    claim_slice_lock(lock_file, os.getpid())
    with pytest.raises(LockError, match="slice-01"):
        acquire_slice_lock("slice-01", tmp_path)


def test_lock_held_by_a_dead_process_is_reclaimed(tmp_path):
    lock_file = acquire_slice_lock("slice-01", tmp_path)
    claim_slice_lock(lock_file, 999999999)
    reacquired = acquire_slice_lock("slice-01", tmp_path)
    data = json.loads(reacquired.read_text(encoding="utf-8"))
    assert data["state"] == "starting"


def test_starting_lock_blocks_within_the_grace_window(tmp_path):
    acquire_slice_lock("slice-01", tmp_path)
    with pytest.raises(LockError):
        acquire_slice_lock("slice-01", tmp_path)


def test_starting_lock_expires_after_the_grace_window(tmp_path):
    lock_file = acquire_slice_lock("slice-01", tmp_path)
    data = json.loads(lock_file.read_text(encoding="utf-8"))
    data["started_at"] = time.time() - (LOCK_START_GRACE_SECONDS + 1)
    lock_file.write_text(json.dumps(data), encoding="utf-8")
    assert acquire_slice_lock("slice-01", tmp_path).exists()


def test_corrupt_lock_is_reclaimed(tmp_path):
    lock_file = lock_path(tmp_path, "slice-01")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text("not json at all", encoding="utf-8")
    assert acquire_slice_lock("slice-01", tmp_path).exists()


def test_release_is_idempotent(tmp_path):
    lock_file = acquire_slice_lock("slice-01", tmp_path)
    release_slice_lock_file(lock_file)
    release_slice_lock_file(lock_file)
    assert not lock_file.exists()
    release_slice_lock("slice-01", tmp_path)
```

Then delete `test_acquire_and_release_lock`, `test_acquire_lock_blocks_on_active_process` and `test_acquire_lock_cleans_stale_lock` from `tests/test_orchestrator.py` — they assert the old schema and are superseded by the file above.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_locks.py -v -p no:cacheprovider`
Expected: FAIL with `ImportError: cannot import name 'LOCK_START_GRACE_SECONDS' from 'scripts.locks'`

- [ ] **Step 3: Write minimal implementation**

Replace the whole of `scripts/locks.py`:

```python
"""File-based slice locking.

Acquisition and ownership are separate. The dispatcher creates the lock
atomically and exits; the supervisor it spawns claims the lock with its own
PID and holds it for the run. A `starting` lock is honoured for a bounded
grace window so the gap between those two events is not a hole.
"""

import json
import os
import sys
import time
from pathlib import Path

from scripts.errors import LockError
from scripts.paths import lock_path
from scripts.utils import _is_process_alive, _sanitize_id

#: How long a lock may sit in `starting` before it is considered abandoned.
LOCK_START_GRACE_SECONDS = 60

_MAX_RECLAIM_ATTEMPTS = 3


def _lock_is_held(data: dict) -> bool:
    state = data.get("state")
    if state == "running":
        pid = data.get("pid")
        return bool(pid) and _is_process_alive(int(pid))
    if state == "starting":
        started_at = data.get("started_at") or 0
        return (time.time() - float(started_at)) < LOCK_START_GRACE_SECONDS
    return False


def acquire_slice_lock(slice_id: str, project_root: Path) -> Path:
    """Atomically create the lock for a slice.

    Raises LockError if the slice is already held by a live supervisor or by
    a dispatcher still inside its start-up grace window.
    """
    _sanitize_id(slice_id, "slice_id")
    lock_file = lock_path(project_root, slice_id)
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "slice_id": slice_id,
        "state": "starting",
        "pid": None,
        "started_at": time.time(),
        "command": " ".join(sys.argv),
    }

    for _ in range(_MAX_RECLAIM_ATTEMPTS):
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                existing = json.loads(lock_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, ValueError):
                existing = {}
            if _lock_is_held(existing):
                raise LockError(
                    f"Slice '{slice_id}' is already locked "
                    f"(state={existing.get('state')}, pid={existing.get('pid')}, "
                    f"command={existing.get('command', 'unknown')})."
                )
            lock_file.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        return lock_file

    raise LockError(
        f"Could not acquire lock for slice '{slice_id}' after "
        f"{_MAX_RECLAIM_ATTEMPTS} attempts — it is being contended."
    )


def claim_slice_lock(lock_file: Path, pid: int, **meta) -> None:
    """Take ownership of an acquired lock. Called by the supervisor."""
    lock_file = Path(lock_file)
    try:
        data = json.loads(lock_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        data = {}
    data.update(meta)
    data["pid"] = pid
    data["state"] = "running"
    lock_file.write_text(json.dumps(data, indent=2), encoding="utf-8")


def release_slice_lock_file(lock_file: Path) -> None:
    """Remove a lock by path. Safe to call more than once."""
    Path(lock_file).unlink(missing_ok=True)


def release_slice_lock(slice_id: str, project_root: Path) -> None:
    """Remove a lock by slice id."""
    release_slice_lock_file(lock_path(project_root, slice_id))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_locks.py tests/test_orchestrator.py -v -p no:cacheprovider`
Expected: PASS — 8 lock tests; the three superseded tests are gone from `test_orchestrator.py`

- [ ] **Step 5: Commit**

```bash
git add scripts/locks.py tests/test_locks.py tests/test_orchestrator.py
git commit -m "fix: make the slice lock hold a live owner

The lock recorded the dispatcher's PID, which exits right after spawning, so
it went stale within a second and never blocked anything. Acquisition is now
atomic and the supervisor claims ownership with its own PID.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Adapters return argv, and pass the provider

Two changes in one contract: `build_command` returns a list so dispatch can run with `shell=False`, and the OpenCode adapter emits the CLI's native `provider/model` form instead of dropping the provider entirely.

**Files:**
- Modify: `scripts/adapters/base.py:11-23`, `scripts/adapters/opencode.py:20-30`
- Test: `tests/test_orchestrator.py:571-593` (the two adapter tests)

**Interfaces:**
- Consumes: nothing.
- Produces: `HarnessAdapter.build_command(agent_config: dict, task_prompt: str) -> list[str]`. Every adapter, built-in or custom, must now return `list[str]`.

- [ ] **Step 1: Write the failing test**

Replace the two adapter tests at the end of `tests/test_orchestrator.py`:

```python
def test_opencode_adapter_returns_argv_list():
    """shell=False dispatch requires an argv list, not a shell string."""
    from scripts.adapters.opencode import OpenCodeAdapter

    adapter = OpenCodeAdapter()
    config = {"model": "kimi-k3", "provider": "opencode-go", "extra_args": []}
    argv = adapter.build_command(config, "Do something")
    assert isinstance(argv, list)
    assert all(isinstance(part, str) for part in argv)
    assert argv == ["opencode", "run", "--model", "opencode-go/kimi-k3", "Do something"]


def test_opencode_adapter_passes_provider_with_model():
    """The provider was silently dropped under the default empty extra_args."""
    from scripts.adapters.opencode import OpenCodeAdapter

    argv = OpenCodeAdapter().build_command(
        {"model": "minimax-m3", "provider": "opencode-go", "extra_args": []}, "Task"
    )
    assert "opencode-go/minimax-m3" in argv


def test_opencode_adapter_with_extra_args():
    from scripts.adapters.opencode import OpenCodeAdapter

    argv = OpenCodeAdapter().build_command(
        {"model": "kimi-k3", "provider": "opencode-go", "extra_args": ["--provider={provider}"]},
        "Test prompt",
    )
    assert "--provider=opencode-go" in argv


def test_prompt_is_a_single_argv_element_not_shell_quoted():
    """A prompt containing quotes must survive verbatim — no shell involved."""
    from scripts.adapters.opencode import OpenCodeAdapter

    prompt = """Read C:\\path\\file.md and say "hello" — don't quote it"""
    argv = OpenCodeAdapter().build_command({"model": "m", "provider": "p"}, prompt)
    assert argv[-1] == prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_orchestrator.py -k "adapter or argv" -v -p no:cacheprovider`
Expected: FAIL — `assert isinstance(argv, list)` fails because a `str` is returned

- [ ] **Step 3: Write minimal implementation**

Replace `scripts/adapters/base.py`:

```python
"""Base class for all harness adapters."""


class HarnessAdapter:
    """Abstract base for CLI harness adapters.

    Every adapter translates an agent configuration and a task prompt into an
    argument vector. It is a list, not a shell string: the orchestrator spawns
    agents with `shell=False`, so quoting and escaping never enter the picture
    and a prompt cannot break out of its own quotes.
    """

    def build_command(self, agent_config: dict, task_prompt: str) -> list[str]:
        """Build the argv for the given agent and prompt.

        Args:
            agent_config: Agent configuration dict from agents.yaml.
            task_prompt: The fully-rendered task prompt.

        Returns:
            A list of strings, ready for `subprocess` with `shell=False`.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement build_command()"
        )
```

Replace `scripts/adapters/opencode.py`:

```python
"""OpenCode CLI harness adapter.

Formats an argv for the OpenCode CLI. The model is passed in the CLI's native
`provider/model` form — under the default empty `extra_args` the provider was
previously dropped entirely.
"""

from scripts.adapters.base import HarnessAdapter


class OpenCodeAdapter(HarnessAdapter):
    """Adapter for the OpenCode CLI harness.

    Produces: ``opencode run --model <provider>/<model> [extra_args...] <prompt>``
    """

    def build_command(self, agent_config: dict, task_prompt: str) -> list[str]:
        model = agent_config.get("model", "kimi-k3")
        provider = agent_config.get("provider", "opencode-go")

        argv = ["opencode", "run", "--model", f"{provider}/{model}"]
        for arg in agent_config.get("extra_args") or []:
            argv.append(str(arg).format(provider=provider, model=model))
        argv.append(task_prompt)
        return argv
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_orchestrator.py -v -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/adapters/base.py scripts/adapters/opencode.py tests/test_orchestrator.py
git commit -m "feat: adapters return argv and pass provider/model

BREAKING CHANGE: HarnessAdapter.build_command returns list[str] instead of a
shell string. This is what lets dispatch run with shell=False, which removes
prompt-based shell injection and Windows path mangling as a class.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Canonical hook events and an unknown-event warning

A misspelled event is currently an unreported no-op — which is why the consuming project's `sandbox-loopback` hook has never fired. Name the canon and warn on anything else.

**Files:**
- Modify: `scripts/hooks.py` (whole file)
- Test: `tests/test_hook_events.py`
- Modify: `tests/test_orchestrator.py:235-303` (event names in the existing hook tests)

**Interfaces:**
- Consumes: `HookError` (Task 1).
- Produces: `canonical_events(roles) -> set[str]`, `load_project_hooks(project_root, known_events=None) -> dict`, `run_infrastructure_hook(event_name, project_root, current_env=None, known_events=None) -> dict` (raises `HookError`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_hook_events.py`:

```python
import pytest

from scripts.errors import HookError
from scripts.hooks import canonical_events, load_project_hooks, run_infrastructure_hook


def _write_hooks(project_root, text):
    sp = project_root / ".superpowers"
    sp.mkdir(parents=True, exist_ok=True)
    (sp / "hooks.yaml").write_text(text, encoding="utf-8")


def test_canonical_events_for_default_roles():
    events = canonical_events(["planner", "executor"])
    assert events == {
        "on_slice_planner_start",
        "on_planner_complete",
        "on_planner_failed",
        "on_slice_executor_start",
        "on_executor_complete",
        "on_executor_failed",
        "on_slice_verified_closed",
    }


def test_unknown_event_name_is_reported(tmp_path, capsys):
    """The exact defect that hid a never-firing sandbox hook for months."""
    _write_hooks(tmp_path, 'hooks:\n  on_slice_execution_start:\n    command: "echo hi"\n')
    load_project_hooks(tmp_path, known_events=canonical_events(["planner", "executor"]))
    out = capsys.readouterr().out
    assert "on_slice_execution_start" in out
    assert "on_slice_executor_start" in out


def test_known_event_name_is_not_reported(tmp_path, capsys):
    _write_hooks(tmp_path, 'hooks:\n  on_slice_executor_start:\n    command: "echo hi"\n')
    load_project_hooks(tmp_path, known_events=canonical_events(["planner", "executor"]))
    assert "unknown event" not in capsys.readouterr().out.lower()


def test_failing_hook_raises_instead_of_exiting(tmp_path):
    _write_hooks(tmp_path, 'hooks:\n  on_slice_executor_start:\n    command: "exit 3"\n')
    with pytest.raises(HookError, match="on_slice_executor_start"):
        run_infrastructure_hook("on_slice_executor_start", project_root=tmp_path)


def test_capture_env_collects_exported_variables(tmp_path):
    _write_hooks(
        tmp_path,
        'hooks:\n  on_slice_executor_start:\n    command: "echo LOOPBACK_IP=127.0.0.9"\n'
        "    capture_env: true\n",
    )
    env = run_infrastructure_hook("on_slice_executor_start", project_root=tmp_path)
    assert env["LOOPBACK_IP"] == "127.0.0.9"


def test_missing_event_returns_env_unchanged(tmp_path):
    _write_hooks(tmp_path, 'hooks:\n  on_slice_executor_start:\n    command: "echo hi"\n')
    env = run_infrastructure_hook("on_executor_complete", project_root=tmp_path)
    assert isinstance(env, dict)
```

In `tests/test_orchestrator.py`, rename every occurrence of the invented event `on_slice_execution_start` to the canonical `on_slice_executor_start` (three occurrences, in `test_load_project_hooks` and `test_run_infrastructure_hook`).

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_hook_events.py -v -p no:cacheprovider`
Expected: FAIL with `ImportError: cannot import name 'canonical_events' from 'scripts.hooks'`

- [ ] **Step 3: Write minimal implementation**

Replace `scripts/hooks.py`:

```python
"""Infrastructure hook loading and execution."""

import os
import subprocess
from pathlib import Path

from ruamel.yaml import YAML

from scripts.errors import HookError
from scripts.utils import _to_plain_dict


def canonical_events(roles) -> set[str]:
    """The complete set of event names the orchestrator ever emits."""
    events = {"on_slice_verified_closed"}
    for role in roles:
        events.add(f"on_slice_{role}_start")
        events.add(f"on_{role}_complete")
        events.add(f"on_{role}_failed")
    return events


def load_project_hooks(project_root: Path, known_events: set[str] | None = None) -> dict:
    """Load `.superpowers/hooks.yaml` if present.

    When `known_events` is supplied, every declared event is checked against
    it and anything unrecognised is reported. A hook keyed on a name the
    orchestrator never emits is silently dead otherwise.
    """
    hooks_file = Path(project_root) / ".superpowers" / "hooks.yaml"
    if not hooks_file.exists():
        return {}
    try:
        yaml = YAML(typ="rt")
        parsed = yaml.load(hooks_file.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        print(f"Warning: failed to parse {hooks_file}: {exc}. Ignoring project hooks.")
        return {}

    hooks = _to_plain_dict(parsed.get("hooks", {})) or {}

    if known_events is not None:
        for name in hooks:
            if name not in known_events:
                print(
                    f"Warning: {hooks_file} declares unknown event '{name}'. "
                    f"It will never fire. Known events: {sorted(known_events)}"
                )
    return hooks


def run_infrastructure_hook(
    event_name: str,
    project_root: Path,
    current_env: dict | None = None,
    known_events: set[str] | None = None,
) -> dict:
    """Execute a project infrastructure hook, optionally capturing env vars.

    Raises HookError on a non-zero exit. The caller decides what that means —
    dispatch releases its lock and stops before mutating any slice status.
    """
    if current_env is None:
        current_env = dict(os.environ)

    hooks = load_project_hooks(project_root, known_events=known_events)
    hook_cfg = hooks.get(event_name)
    if not hook_cfg or not isinstance(hook_cfg, dict):
        return current_env

    command = hook_cfg.get("command")
    if not command:
        return current_env

    capture_env = hook_cfg.get("capture_env", False)
    print(f"[Infrastructure Hook] Running '{event_name}': {command}")

    try:
        result = subprocess.run(
            command, shell=True, cwd=project_root,
            capture_output=True, text=True, env=current_env,
        )
    except OSError as exc:
        raise HookError(f"Hook '{event_name}' could not be started: {exc}") from exc

    if result.returncode != 0:
        raise HookError(
            f"Hook '{event_name}' failed with exit {result.returncode}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )

    print(f"[Infrastructure Hook] '{event_name}' completed successfully.")

    if capture_env and result.stdout:
        updated_env = dict(current_env)
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" in line:
                key, value = line.split("=", 1)
                updated_env[key.strip()] = value.strip().strip('"').strip("'")
        return updated_env

    return current_env
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_hook_events.py tests/test_orchestrator.py -v -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/hooks.py tests/test_hook_events.py tests/test_orchestrator.py
git commit -m "feat: name the canonical hook events and warn on unknown ones

A misspelled event name was an unreported no-op. That is how a consuming
project's sandbox-loopback hook stayed dead without anyone noticing.

Also raises HookError instead of calling sys.exit, so the caller can unwind.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Merge gate that ignores the orchestrator's own artifacts

`dispatch` creates untracked files, `git status --porcelain` reports them, and the merge gate refuses forever. Teach the gate which paths are its own.

**Files:**
- Modify: `scripts/git_ops.py` (whole file)
- Test: `tests/test_git_ops.py`

**Interfaces:**
- Consumes: `is_artifact_path` (Task 2), `GitError`, `ValidationError` (Task 1).
- Produces: `check_working_tree_clean(project_root) -> bool`, `create_git_worktree(slice_id, project_root) -> Path` (raises `GitError`), `merge_and_cleanup_worktree(slice_id, project_root) -> bool` (returns `False` on conflict, raises `GitError` if the tree is dirty).

Note the signature change: `spec_file` and `update_status_fn` are gone. Ordering now lives in the caller (Task 12).

- [ ] **Step 1: Write the failing test**

Create `tests/test_git_ops.py`:

```python
import subprocess

import pytest

from scripts.errors import GitError, ValidationError
from scripts.git_ops import (
    check_working_tree_clean,
    create_git_worktree,
    merge_and_cleanup_worktree,
)


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", ".")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_clean_repo_is_clean(repo):
    assert check_working_tree_clean(repo) is True


def test_orchestrator_artifacts_do_not_make_the_tree_dirty(repo):
    """The defect: dispatch's own logs and locks blocked its own merge."""
    (repo / ".superpowers" / "logs").mkdir(parents=True)
    (repo / ".superpowers" / "logs" / "executor_x.log").write_text("out", encoding="utf-8")
    (repo / ".superpowers" / "locks").mkdir(parents=True)
    (repo / ".superpowers" / "locks" / "slice-01.lock").write_text("{}", encoding="utf-8")
    assert check_working_tree_clean(repo) is True


def test_user_changes_still_make_the_tree_dirty(repo):
    (repo / "src.py").write_text("print(1)\n", encoding="utf-8")
    assert check_working_tree_clean(repo) is False


def test_modified_tracked_file_makes_the_tree_dirty(repo):
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    assert check_working_tree_clean(repo) is False


def test_create_worktree_rejects_unsafe_id(repo):
    with pytest.raises(ValidationError):
        create_git_worktree("foo; rm -rf /", repo)


def test_merge_brings_the_branch_home_and_removes_the_worktree(repo):
    worktree = create_git_worktree("slice-01", repo)
    (worktree / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "feat: add feature")

    assert merge_and_cleanup_worktree("slice-01", repo) is True
    assert (repo / "feature.py").exists()
    assert not worktree.exists()


def test_merge_raises_when_the_tree_is_genuinely_dirty(repo):
    create_git_worktree("slice-01", repo)
    (repo / "uncommitted.py").write_text("x\n", encoding="utf-8")
    with pytest.raises(GitError, match="dirty"):
        merge_and_cleanup_worktree("slice-01", repo)


def test_merge_returns_false_on_conflict(repo):
    worktree = create_git_worktree("slice-01", repo)
    (worktree / "README.md").write_text("branch side\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "branch change")

    (repo / "README.md").write_text("main side\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main change")

    assert merge_and_cleanup_worktree("slice-01", repo) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_git_ops.py -v -p no:cacheprovider`
Expected: FAIL — `test_orchestrator_artifacts_do_not_make_the_tree_dirty` returns `False`, and `create_git_worktree` raises `SystemExit` rather than `ValidationError`

- [ ] **Step 3: Write minimal implementation**

Replace `scripts/git_ops.py`:

```python
"""Git worktree management and merge operations."""

import subprocess
from pathlib import Path

from scripts.errors import GitError
from scripts.paths import is_artifact_path
from scripts.utils import _sanitize_id


def _porcelain_entry(line: str) -> str:
    """Extract the path from one `git status --porcelain` line."""
    entry = line[3:].strip()
    if " -> " in entry:                # renames: "R  old -> new"
        entry = entry.split(" -> ", 1)[1]
    return entry.strip().strip('"')


def check_working_tree_clean(project_root: Path) -> bool:
    """True if the tree has no changes other than orchestrator artifacts.

    The orchestrator writes logs and locks into the project it operates on.
    Counting those as dirt made the merge gate refuse unconditionally.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_root, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False

    for line in result.stdout.splitlines():
        entry = _porcelain_entry(line)
        if entry and not is_artifact_path(entry):
            return False
    return True


def create_git_worktree(slice_id: str, project_root: Path) -> Path:
    """Create an isolated worktree for a slice under `.worktrees/<slice_id>`."""
    _sanitize_id(slice_id, "slice_id")
    worktree_path = Path(project_root) / ".worktrees" / slice_id
    branch_name = f"feat/{slice_id}"

    if worktree_path.exists():
        return worktree_path

    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    created = subprocess.run(
        ["git", "worktree", "add", "-b", branch_name, str(worktree_path), "HEAD"],
        cwd=project_root, capture_output=True, text=True,
    )
    if created.returncode != 0:
        # The branch may already exist from an earlier run; attach to it.
        reused = subprocess.run(
            ["git", "worktree", "add", str(worktree_path), branch_name],
            cwd=project_root, capture_output=True, text=True,
        )
        if reused.returncode != 0:
            raise GitError(
                f"Could not create worktree for '{slice_id}': "
                f"{reused.stderr.strip() or created.stderr.strip()}"
            )
    return worktree_path


def merge_and_cleanup_worktree(slice_id: str, project_root: Path) -> bool:
    """Merge a slice branch into the current branch and drop its worktree.

    Returns True on success and False on merge conflict — a conflict is an
    expected outcome the caller records as a status. Raises GitError when the
    tree is dirty, which is a precondition failure, not an outcome.
    """
    _sanitize_id(slice_id, "slice_id")
    branch_name = f"feat/{slice_id}"
    worktree_path = Path(project_root) / ".worktrees" / slice_id

    if not check_working_tree_clean(project_root):
        raise GitError(
            "Working tree is dirty. Commit or stash your changes before merging."
        )

    merged = subprocess.run(
        ["git", "merge", branch_name],
        cwd=project_root, capture_output=True, text=True,
    )
    if merged.returncode != 0:
        return False

    if worktree_path.exists():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=project_root, capture_output=True,
        )
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_git_ops.py -v -p no:cacheprovider`
Expected: PASS, 8 tests

- [ ] **Step 5: Commit**

```bash
git add scripts/git_ops.py tests/test_git_ops.py
git commit -m "fix: stop the orchestrator's own artifacts from blocking its merge

dispatch creates untracked logs and locks in the project root; the dirty-tree
gate counted them and refused every VERIFIED_CLOSED merge.

BREAKING CHANGE: merge_and_cleanup_worktree drops its spec_file and
update_status_fn parameters — status ordering now lives in the caller.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: Unambiguous dependency resolution

`check_unmet_dependencies` reports "Spec not found" when its glob matches more than one file, and only searches the directory of the file it was handed — `plans/` for the executor, while `depends_on` lives in `specs/`.

**Files:**
- Modify: `scripts/dependencies.py` (whole file)
- Test: `tests/test_dependencies.py`

**Interfaces:**
- Consumes: `parse_frontmatter`.
- Produces: `check_unmet_dependencies(spec_file: Path, search_dirs: list[Path] | None = None) -> list[str]`.

Resolution rule, in order: a file whose frontmatter `slice_id` equals the dependency id wins outright; otherwise a filename-stem match; a tie between several candidates is reported as ambiguous rather than guessed.

- [ ] **Step 1: Write the failing test**

Create `tests/test_dependencies.py`:

```python
from pathlib import Path

from scripts.dependencies import check_unmet_dependencies


def _spec(directory: Path, name: str, slice_id: str, status: str, depends_on=None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["---", f'slice_id: "{slice_id}"', f"status: {status}"]
    if depends_on:
        lines.append("depends_on:")
        lines.extend(f'  - "{dep}"' for dep in depends_on)
    lines += ["---", "", "# Body", ""]
    path = directory / name
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_no_dependencies_is_met(tmp_path):
    spec = _spec(tmp_path / "specs", "a.md", "slice-01", "SPEC_APPROVED")
    assert check_unmet_dependencies(spec) == []


def test_open_dependency_is_reported(tmp_path):
    specs = tmp_path / "specs"
    _spec(specs, "2026-07-25-slice-01-base-design.md", "slice-01-base", "EXECUTING")
    target = _spec(specs, "2026-07-25-slice-02-design.md", "slice-02", "SPEC_APPROVED",
                   depends_on=["slice-01-base"])
    unmet = check_unmet_dependencies(target)
    assert len(unmet) == 1
    assert "slice-01-base" in unmet[0]
    assert "EXECUTING" in unmet[0]


def test_closed_dependency_is_met(tmp_path):
    specs = tmp_path / "specs"
    _spec(specs, "2026-07-25-slice-01-base-design.md", "slice-01-base", "VERIFIED_CLOSED")
    target = _spec(specs, "2026-07-25-slice-02-design.md", "slice-02", "SPEC_APPROVED",
                   depends_on=["slice-01-base"])
    assert check_unmet_dependencies(target) == []


def test_slice_id_beats_filename_similarity(tmp_path):
    """Two files whose names both contain the id; frontmatter decides."""
    specs = tmp_path / "specs"
    _spec(specs, "notes-slice-01-base-draft.md", "slice-99-unrelated", "DRAFT_SPEC")
    _spec(specs, "2026-07-25-slice-01-base-design.md", "slice-01-base", "VERIFIED_CLOSED")
    target = _spec(specs, "2026-07-25-slice-02-design.md", "slice-02", "SPEC_APPROVED",
                   depends_on=["slice-01-base"])
    assert check_unmet_dependencies(target) == []


def test_ambiguous_dependency_is_reported_as_ambiguous(tmp_path):
    specs = tmp_path / "specs"
    _spec(specs, "a-slice-01-base.md", "slice-01-base", "VERIFIED_CLOSED")
    _spec(specs, "b-slice-01-base.md", "slice-01-base", "DRAFT_SPEC")
    target = _spec(specs, "target.md", "slice-02", "SPEC_APPROVED", depends_on=["slice-01-base"])
    unmet = check_unmet_dependencies(target)
    assert len(unmet) == 1
    assert "ambiguous" in unmet[0].lower()


def test_missing_dependency_says_not_found(tmp_path):
    specs = tmp_path / "specs"
    target = _spec(specs, "target.md", "slice-02", "SPEC_APPROVED", depends_on=["ghost"])
    assert "not found" in check_unmet_dependencies(target)[0].lower()


def test_dependency_is_found_in_a_sibling_specs_dir(tmp_path):
    """A plan lives in plans/, but depends_on refers to specs/."""
    superpowers = tmp_path / "docs" / "superpowers"
    _spec(superpowers / "specs", "slice-01-design.md", "slice-01", "VERIFIED_CLOSED")
    plan = _spec(superpowers / "plans", "slice-02-plan.md", "slice-02", "PLAN_APPROVED",
                 depends_on=["slice-01"])
    assert check_unmet_dependencies(plan) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dependencies.py -v -p no:cacheprovider`
Expected: FAIL — `test_ambiguous_dependency_is_reported_as_ambiguous` reports "Spec not found", and `test_dependency_is_found_in_a_sibling_specs_dir` cannot see `specs/` from `plans/`

- [ ] **Step 3: Write minimal implementation**

Replace `scripts/dependencies.py`:

```python
"""Slice dependency checking."""

from pathlib import Path

from scripts.frontmatter import parse_frontmatter


def _candidate_dirs(spec_file: Path) -> list[Path]:
    """Directories to search: the file's own, plus its sibling specs/plans."""
    own = spec_file.parent
    dirs = [own]
    for sibling in ("specs", "plans", "milestones"):
        candidate = own.parent / sibling
        if candidate.is_dir() and candidate != own:
            dirs.append(candidate)
    return dirs


def _resolve(dep_id: str, search_dirs: list[Path], exclude: Path) -> tuple[list[Path], list[Path]]:
    """Return (matches by slice_id, matches by filename stem)."""
    by_slice_id: list[Path] = []
    by_stem: list[Path] = []
    for directory in search_dirs:
        for candidate in sorted(directory.glob("*.md")):
            if candidate.resolve() == exclude.resolve():
                continue
            frontmatter = parse_frontmatter(candidate.read_text(encoding="utf-8"))
            if frontmatter.get("slice_id") == dep_id:
                by_slice_id.append(candidate)
            elif dep_id in candidate.stem:
                by_stem.append(candidate)
    return by_slice_id, by_stem


def check_unmet_dependencies(spec_file: Path, search_dirs: list[Path] | None = None) -> list:
    """List dependencies of a slice that are not yet VERIFIED_CLOSED.

    A frontmatter `slice_id` match always wins over a filename match. Several
    equally good candidates are reported as ambiguous rather than guessed —
    silently picking one is how a dependency gate stops meaning anything.
    """
    spec_file = Path(spec_file)
    if not spec_file.exists():
        return []

    frontmatter = parse_frontmatter(spec_file.read_text(encoding="utf-8"))
    depends_on = frontmatter.get("depends_on", [])
    if isinstance(depends_on, str):
        depends_on = [depends_on]
    if not depends_on:
        return []

    dirs = search_dirs if search_dirs is not None else _candidate_dirs(spec_file)

    unmet = []
    for dep_id in depends_on:
        by_slice_id, by_stem = _resolve(dep_id, dirs, exclude=spec_file)
        matches = by_slice_id or by_stem

        if not matches:
            unmet.append(f"{dep_id} (spec not found in {[str(d) for d in dirs]})")
            continue
        if len(matches) > 1:
            names = sorted(match.name for match in matches)
            unmet.append(f"{dep_id} (ambiguous: matches {names})")
            continue

        dep_status = parse_frontmatter(matches[0].read_text(encoding="utf-8")).get(
            "status", "UNKNOWN"
        )
        if dep_status != "VERIFIED_CLOSED":
            unmet.append(f"{dep_id} (status: {dep_status})")

    return unmet
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_dependencies.py tests/test_orchestrator.py -v -p no:cacheprovider`
Expected: PASS. If the legacy `test_check_unmet_dependencies` in `test_orchestrator.py` now overlaps, delete it — `tests/test_dependencies.py` supersedes it.

- [ ] **Step 5: Commit**

```bash
git add scripts/dependencies.py tests/test_dependencies.py tests/test_orchestrator.py
git commit -m "fix: resolve slice dependencies by slice_id and report ambiguity

Two files matching a dependency id used to be reported as 'Spec not found',
and the search never left the target file's own directory, so a plan could
not see the specs it depends on.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 10: The supervisor

The core of the slice. A process that owns one dispatched agent from spawn to terminal status.

**Files:**
- Create: `scripts/runner.py`
- Create: `tests/conftest.py`
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: `load_agent_config`, `resolve_agent` (Task 3), `update_frontmatter_status`, `claim_slice_lock`, `release_slice_lock_file` (Task 5), `run_infrastructure_hook`, `canonical_events` (Task 7), `log_path` (Task 2), `HookError` (Task 1).
- Produces: `run_supervised(role, target_file, project_root, lock_file, log_file, argv, cwd) -> int` and `main(argv=None) -> int`, invoked as `python -m scripts.runner --role R --file F --project-root P --lock L --log G --cwd C -- <agent argv...>`.

- [ ] **Step 1: Write the failing test**

Create `tests/conftest.py`:

```python
"""Shared fixtures.

`tmp_project` is a real git repository with a `.superpowers/` config and a
spec file, so dispatch and supervisor tests exercise the real code paths.

The stub adapter exists to keep tests off the real harness: `opencode` is
installed on the development machine and a live run costs money. No test may
invoke it.
"""

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

STUB_ADAPTER = '''
import sys
sys.path.insert(0, r"{repo_root}")
from scripts.adapters.base import HarnessAdapter


class StubAdapter(HarnessAdapter):
    """Emits a harmless command instead of calling a real harness."""

    def build_command(self, agent_config, task_prompt):
        return [sys.executable, "-c", agent_config.get("model", "print('stub ok')")]
'''


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def tmp_project(tmp_path):
    """A git repo with .superpowers/, a stub adapter and one approved spec."""
    _git(tmp_path, "init", "-q", ".")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")

    (tmp_path / ".superpowers").mkdir()
    (tmp_path / "stub_adapter.py").write_text(
        STUB_ADAPTER.format(repo_root=str(REPO_ROOT)), encoding="utf-8"
    )

    specs = tmp_path / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    (specs / "2026-07-26-slice-01-demo-design.md").write_text(
        '---\nslice_id: "slice-01-demo"\nstatus: SPEC_APPROVED\n---\n\n# Demo\n',
        encoding="utf-8",
    )

    (tmp_path / ".superpowers" / "agents.yaml").write_text(
        "agents:\n"
        "  planner:\n"
        "    model: \"print('stub ok')\"\n"
        "    harness_adapter: 'stub_adapter.py'\n"
        "    isolated_worktree: false\n",
        encoding="utf-8",
    )

    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


@pytest.fixture
def demo_spec(tmp_project):
    return tmp_project / "docs" / "superpowers" / "specs" / "2026-07-26-slice-01-demo-design.md"
```

Create `tests/test_runner.py`:

```python
import json
import os
import sys

from scripts.frontmatter import parse_frontmatter
from scripts.locks import acquire_slice_lock
from scripts.paths import log_path
from scripts.runner import run_supervised


def _set_status(spec, status):
    text = spec.read_text(encoding="utf-8")
    spec.write_text(text.replace("status: SPEC_APPROVED", f"status: {status}"), encoding="utf-8")


def _supervise(tmp_project, demo_spec, argv):
    lock_file = acquire_slice_lock("slice-01-demo", tmp_project)
    log_file = log_path(tmp_project, "planner", demo_spec.stem)
    code = run_supervised(
        role="planner",
        target_file=demo_spec,
        project_root=tmp_project,
        lock_file=lock_file,
        log_file=log_file,
        argv=argv,
        cwd=tmp_project,
    )
    return code, lock_file, log_file


def test_success_sets_success_status(tmp_project, demo_spec):
    _set_status(demo_spec, "PLANNING")
    code, _, _ = _supervise(tmp_project, demo_spec, [sys.executable, "-c", "print('done')"])
    assert code == 0
    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "PLAN_GENERATED"


def test_failure_sets_failed(tmp_project, demo_spec):
    """A crashed agent must land in FAILED, not hang in PLANNING forever."""
    _set_status(demo_spec, "PLANNING")
    code, _, _ = _supervise(tmp_project, demo_spec, [sys.executable, "-c", "raise SystemExit(3)"])
    assert code == 3
    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "FAILED"


def test_log_captures_child_output(tmp_project, demo_spec):
    """The defect: cmd redirection bound to the last command, so logs were empty."""
    _set_status(demo_spec, "PLANNING")
    _, _, log_file = _supervise(
        tmp_project, demo_spec, [sys.executable, "-c", "print('AGENT-MARKER')"]
    )
    assert log_file.exists()
    assert "AGENT-MARKER" in log_file.read_text(encoding="utf-8")


def test_log_captures_child_stderr(tmp_project, demo_spec):
    _set_status(demo_spec, "PLANNING")
    _, _, log_file = _supervise(
        tmp_project, demo_spec,
        [sys.executable, "-c", "import sys; sys.stderr.write('BOOM-MARKER')"],
    )
    assert "BOOM-MARKER" in log_file.read_text(encoding="utf-8")


def test_lock_is_claimed_then_released(tmp_project, demo_spec):
    _set_status(demo_spec, "PLANNING")
    _, lock_file, _ = _supervise(tmp_project, demo_spec, [sys.executable, "-c", "pass"])
    assert not lock_file.exists()


def test_lock_is_released_even_when_the_child_cannot_start(tmp_project, demo_spec):
    _set_status(demo_spec, "PLANNING")
    lock_file = acquire_slice_lock("slice-01-demo", tmp_project)
    log_file = log_path(tmp_project, "planner", demo_spec.stem)
    code = run_supervised(
        role="planner", target_file=demo_spec, project_root=tmp_project,
        lock_file=lock_file, log_file=log_file,
        argv=["definitely-not-an-executable-anywhere"], cwd=tmp_project,
    )
    assert code != 0
    assert not lock_file.exists()
    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "FAILED"


def test_runner_claims_the_lock_with_its_own_pid(tmp_project, demo_spec, monkeypatch):
    _set_status(demo_spec, "PLANNING")
    lock_file = acquire_slice_lock("slice-01-demo", tmp_project)
    seen = {}

    import scripts.runner as runner_module
    original = runner_module.claim_slice_lock

    def spy(path, pid, **meta):
        seen["pid"] = pid
        return original(path, pid, **meta)

    monkeypatch.setattr(runner_module, "claim_slice_lock", spy)
    run_supervised(
        role="planner", target_file=demo_spec, project_root=tmp_project,
        lock_file=lock_file, log_file=log_path(tmp_project, "planner", demo_spec.stem),
        argv=[sys.executable, "-c", "pass"], cwd=tmp_project,
    )
    assert seen["pid"] == os.getpid()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_runner.py -v -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.runner'`

- [ ] **Step 3: Write minimal implementation**

Create `scripts/runner.py`:

```python
#!/usr/bin/env python3
"""Supervisor for a single dispatched agent.

This process is the background job. It owns one agent from spawn to terminal
status: it captures the agent's output, derives the slice's next status from
the child's exit code, fires the completion hook, and releases the lock on
every exit path.

Deriving status from an exit code is the point. Previously the agent was
asked, in its prompt, to set its own terminal status — so an agent that
crashed or simply forgot left the slice stranded with no way back.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):  # invoked as a script rather than `-m`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.config import load_agent_config, resolve_agent, validate_config
from scripts.errors import HookError, OrchestratorError
from scripts.frontmatter import update_frontmatter_status
from scripts.hooks import canonical_events, run_infrastructure_hook
from scripts.locks import claim_slice_lock, release_slice_lock_file

FAILED_STATUS = "FAILED"


def run_supervised(
    role: str,
    target_file: Path,
    project_root: Path,
    lock_file: Path,
    log_file: Path,
    argv: list,
    cwd: Path,
) -> int:
    """Run one agent to completion and record the outcome. Returns its exit code."""
    target_file = Path(target_file)
    project_root = Path(project_root)
    log_file = Path(log_file)

    claim_slice_lock(lock_file, os.getpid(), role=role)
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        exit_code = _run_child(argv, cwd, log_file)
        _record_outcome(role, target_file, project_root, exit_code, log_file)
        return exit_code
    finally:
        release_slice_lock_file(lock_file)


def _run_child(argv: list, cwd: Path, log_file: Path) -> int:
    """Spawn the agent with both streams captured. A failure to start is an outcome."""
    with open(log_file, "w", encoding="utf-8", errors="replace") as log:
        log.write(f"$ {' '.join(str(part) for part in argv)}\n\n")
        log.flush()
        try:
            completed = subprocess.run(
                [str(part) for part in argv],
                cwd=str(cwd),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            log.write(f"\n[runner] could not start the agent: {exc}\n")
            return 127
        return completed.returncode


def _record_outcome(
    role: str, target_file: Path, project_root: Path, exit_code: int, log_file: Path
) -> None:
    """Advance the slice status and fire the completion hook."""
    try:
        config = load_agent_config(project_root)
        validate_config(config)
        agent = resolve_agent(config, role)
    except OrchestratorError as exc:
        print(f"[runner] configuration unusable, cannot record outcome: {exc}")
        return

    state_machine = config["state_machine"]
    if exit_code == 0:
        new_status = agent.get("success_status")
        event = f"on_{role}_complete"
    else:
        new_status = FAILED_STATUS
        event = f"on_{role}_failed"

    if new_status:
        update_frontmatter_status(
            target_file,
            new_status,
            state_machine["valid_statuses"],
            state_machine["transitions"],
        )

    print(f"[runner] {role} exited {exit_code}; status -> {new_status}; log: {log_file}")

    try:
        run_infrastructure_hook(
            event,
            project_root=project_root,
            known_events=canonical_events(config.get("agents", {})),
        )
    except HookError as exc:
        # The agent's own outcome is already recorded; a failing completion
        # hook must not overwrite it.
        print(f"[runner] completion hook failed: {exc}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Supervise one dispatched agent.")
    parser.add_argument("--role", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--lock", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("no agent command given after '--'")

    return run_supervised(
        role=args.role,
        target_file=Path(args.file),
        project_root=Path(args.project_root),
        lock_file=Path(args.lock),
        log_file=Path(args.log),
        argv=command,
        cwd=Path(args.cwd),
    )


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_runner.py -v -p no:cacheprovider`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add scripts/runner.py tests/conftest.py tests/test_runner.py
git commit -m "feat: add runner supervisor owning the dispatched agent lifecycle

The orchestrator used to spawn a process and forget it, which is why the log,
the lock, the dead-end states and the unreachable MERGE_CONFLICT were one
defect rather than four. The supervisor captures output, derives the terminal
status from the child's exit code, and releases the lock in a finally block.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 11: Dispatch rewrite

Make `cmd_dispatch_agent` spawn the supervisor with `shell=False`, in the order that leaves nothing to repair by hand when a precondition fails.

**Files:**
- Modify: `scripts/orchestrator.py:89-189`
- Test: `tests/test_dispatch_integration.py`

**Interfaces:**
- Consumes: everything from Tasks 1–10.
- Produces: `cmd_dispatch_agent(args)` unchanged in signature; `PLUGIN_ROOT: Path` module constant.

Normative order — every fallible step precedes the first irreversible mutation:

```
1. resolve + validate config      5. create worktree (may fail)
2. dependency gate, state gate    6. set in_progress_status  <- first mutation
3. acquire lock                   7. spawn supervisor
4. fire on_slice_{role}_start (may fail)
```

- [ ] **Step 1: Write the failing test**

Create `tests/test_dispatch_integration.py`:

```python
"""End-to-end dispatch against a temporary git repo.

This is the test that would have caught the empty log, the useless lock and
the self-blocking merge gate together. It never invokes a real harness — the
project fixture wires in a stub adapter.
"""

import argparse
import json
import time

import pytest

from scripts.frontmatter import parse_frontmatter
from scripts.git_ops import check_working_tree_clean
from scripts.orchestrator import cmd_dispatch_agent
from scripts.paths import lock_path, log_path


def _args(spec, role="planner", model=None):
    return argparse.Namespace(role=role, file=str(spec), model=model)


def _wait_for(predicate, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def test_dispatch_runs_the_agent_and_reaches_a_terminal_status(tmp_project, demo_spec):
    cmd_dispatch_agent(_args(demo_spec))

    assert _wait_for(
        lambda: parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"]
        == "PLAN_GENERATED"
    ), "supervisor never advanced the slice to its success status"


def test_dispatch_writes_a_non_empty_log(tmp_project, demo_spec):
    cmd_dispatch_agent(_args(demo_spec))
    log_file = log_path(tmp_project, "planner", demo_spec.stem)
    assert _wait_for(lambda: log_file.exists() and log_file.stat().st_size > 0)
    assert "stub ok" in log_file.read_text(encoding="utf-8")


def test_dispatch_releases_the_lock_when_the_agent_finishes(tmp_project, demo_spec):
    cmd_dispatch_agent(_args(demo_spec))
    assert _wait_for(lambda: not lock_path(tmp_project, "slice-01-demo").exists())


def test_dispatch_artifacts_do_not_dirty_the_tree(tmp_project, demo_spec):
    cmd_dispatch_agent(_args(demo_spec))
    _wait_for(lambda: not lock_path(tmp_project, "slice-01-demo").exists())
    # The spec file itself is a tracked modification; discard it, then only
    # orchestrator artifacts remain.
    import subprocess
    subprocess.run(["git", "checkout", "--", "."], cwd=tmp_project, capture_output=True)
    assert check_working_tree_clean(tmp_project) is True


def test_dispatch_refused_from_the_wrong_status(tmp_project, demo_spec):
    text = demo_spec.read_text(encoding="utf-8")
    demo_spec.write_text(text.replace("SPEC_APPROVED", "DRAFT_SPEC"), encoding="utf-8")
    with pytest.raises(SystemExit):
        cmd_dispatch_agent(_args(demo_spec))
    assert not lock_path(tmp_project, "slice-01-demo").exists()


def test_failing_start_hook_leaves_the_slice_untouched(tmp_project, demo_spec):
    """A failing hook must not strand the slice in PLANNING."""
    (tmp_project / ".superpowers" / "hooks.yaml").write_text(
        'hooks:\n  on_slice_planner_start:\n    command: "exit 7"\n', encoding="utf-8"
    )
    with pytest.raises(SystemExit):
        cmd_dispatch_agent(_args(demo_spec))

    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "SPEC_APPROVED"
    assert not lock_path(tmp_project, "slice-01-demo").exists()


def test_lock_records_a_live_supervisor(tmp_project, demo_spec):
    cmd_dispatch_agent(_args(demo_spec))
    lock_file = lock_path(tmp_project, "slice-01-demo")
    if lock_file.exists():
        data = json.loads(lock_file.read_text(encoding="utf-8"))
        assert data["state"] in {"starting", "running"}
        if data["state"] == "running":
            assert data["pid"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dispatch_integration.py -v -p no:cacheprovider`
Expected: FAIL — the log is empty, the lock is never released, and the slice stays at `PLANNING`

- [ ] **Step 3: Write minimal implementation**

Replace the imports block and `cmd_dispatch_agent` in `scripts/orchestrator.py`:

```python
import argparse
import os
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):  # invoked as a script rather than `-m`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.adapters import get_harness_adapter
from scripts.config import load_agent_config, resolve_agent, validate_config
from scripts.dependencies import check_unmet_dependencies
from scripts.errors import OrchestratorError
from scripts.frontmatter import parse_frontmatter, update_frontmatter_status
from scripts.git_ops import create_git_worktree, merge_and_cleanup_worktree
from scripts.hooks import canonical_events, run_infrastructure_hook
from scripts.locks import acquire_slice_lock, release_slice_lock_file
from scripts.paths import ARTIFACT_PREFIXES, log_path, logs_dir
from scripts.utils import find_project_root

#: Root of this plugin — the supervisor is spawned with this as its cwd so
#: that `python -m scripts.runner` resolves regardless of the user's cwd.
PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _warn_if_artifacts_not_ignored(project_root: Path) -> None:
    """Suggest ignoring our runtime paths, without touching the user's file."""
    result = subprocess.run(
        ["git", "check-ignore", *ARTIFACT_PREFIXES],
        cwd=project_root, capture_output=True, text=True,
    )
    ignored = set(result.stdout.split())
    missing = [p for p in ARTIFACT_PREFIXES if p not in ignored and p.rstrip("/") not in ignored]
    if missing:
        print(
            "Hint: consider adding these to .gitignore so they stay out of your diffs: "
            + " ".join(missing)
        )


def cmd_dispatch_agent(args):
    """Dispatch an agent by role.

    Ordering is load-bearing: every step that can fail runs before the first
    irreversible mutation, so a failed precondition never leaves a slice that
    has to be repaired by hand.
    """
    target_file = Path(args.file).resolve()
    if not target_file.exists():
        print(f"Error: Target file '{target_file}' not found.")
        sys.exit(1)

    role = args.role
    project_root = find_project_root(target_file)

    # 1. Configuration
    try:
        config = load_agent_config(project_root)
        validate_config(config)
        agent_config = resolve_agent(config, role)
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    state_machine = config["state_machine"]
    known_events = canonical_events(config.get("agents", {}))

    if getattr(args, "model", None):
        agent_config["model"] = args.model

    # 2. Gates
    unmet = check_unmet_dependencies(target_file)
    if unmet:
        print(f"[Dependency Gate] Cannot dispatch {role} for {target_file.name}. Unmet:")
        for dependency in unmet:
            print(f"   - {dependency}")
        sys.exit(1)

    frontmatter = parse_frontmatter(target_file.read_text(encoding="utf-8"))
    slice_id = frontmatter.get("slice_id", target_file.stem)
    current_status = frontmatter.get("status", "UNKNOWN")

    allowed_statuses = agent_config.get("allowed_statuses") or []
    if allowed_statuses and current_status not in allowed_statuses:
        print(f"[State Validation] Cannot dispatch {role} for {target_file.name}.")
        print(f"   Current status is '{current_status}'; {role} requires one of: {allowed_statuses}")
        sys.exit(1)

    # 3. Lock
    try:
        lock_file = acquire_slice_lock(slice_id, project_root)
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    # 4-5. Fallible side effects, before any mutation we would have to undo
    try:
        env = run_infrastructure_hook(
            f"on_slice_{role}_start", project_root=project_root, known_events=known_events
        )
        if agent_config.get("isolated_worktree", False):
            cwd = create_git_worktree(slice_id, project_root)
        else:
            cwd = project_root
    except OrchestratorError as exc:
        release_slice_lock_file(lock_file)
        print(f"Error: {exc}")
        print(f"Slice '{slice_id}' left untouched at status '{current_status}'.")
        sys.exit(1)

    # 6. First irreversible mutation
    in_progress_status = agent_config.get("in_progress_status")
    if in_progress_status:
        update_frontmatter_status(
            target_file, in_progress_status,
            state_machine["valid_statuses"], state_machine["transitions"],
        )

    # 7. Spawn the supervisor
    log_file = log_path(project_root, role, target_file.stem)
    logs_dir(project_root).mkdir(parents=True, exist_ok=True)

    prompt_template = agent_config.get("prompt_template", "Process {file}")
    task_prompt = prompt_template.format(file=target_file)

    try:
        adapter = get_harness_adapter(agent_config, project_root)
        agent_argv = adapter.build_command(agent_config, task_prompt)
    except OrchestratorError as exc:
        release_slice_lock_file(lock_file)
        print(f"Error: {exc}")
        sys.exit(1)

    runner_argv = [
        sys.executable, "-m", "scripts.runner",
        "--role", role,
        "--file", str(target_file),
        "--project-root", str(project_root),
        "--lock", str(lock_file),
        "--log", str(log_file),
        "--cwd", str(cwd),
        "--", *[str(part) for part in agent_argv],
    ]

    spawn_kwargs = {"cwd": str(PLUGIN_ROOT), "env": env}
    if os.name == "nt":
        spawn_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        spawn_kwargs["start_new_session"] = True

    process = subprocess.Popen(runner_argv, **spawn_kwargs)

    print(f"Dispatched {agent_config.get('model')} as {role} (supervisor PID {process.pid}).")
    print(f"Log: {log_file}")
    _warn_if_artifacts_not_ignored(project_root)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_dispatch_integration.py -v -p no:cacheprovider`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add scripts/orchestrator.py tests/test_dispatch_integration.py
git commit -m "feat: dispatch spawns the supervisor with shell=False

Removes the shell from the dispatch path entirely: no injection through
prompt_template or extra_args, no backslash mangling of Windows paths, and
no redirection bound to the wrong command of an && chain.

Ordering is now load-bearing — the start hook and worktree creation both run
before the first status mutation, so a failing hook no longer strands a slice.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 12: Merge before status, so MERGE_CONFLICT is reachable

`cmd_set_status` sets `VERIFIED_CLOSED` first and then merges, so the conflict callback tries to leave a terminal state and is rejected. Invert.

**Files:**
- Modify: `scripts/orchestrator.py:58-86` (`cmd_set_status`)
- Test: `tests/test_set_status.py`

**Interfaces:**
- Consumes: `merge_and_cleanup_worktree` (Task 8), `validate_config` (Task 4).
- Produces: `cmd_set_status(args)` unchanged in signature.

- [ ] **Step 1: Write the failing test**

Create `tests/test_set_status.py`:

```python
import argparse
import subprocess

import pytest

from scripts.frontmatter import parse_frontmatter
from scripts.git_ops import create_git_worktree
from scripts.orchestrator import cmd_set_status


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _set_raw_status(spec, status):
    text = spec.read_text(encoding="utf-8")
    spec.write_text(text.replace("status: SPEC_APPROVED", f"status: {status}"), encoding="utf-8")


def test_plain_status_change_applies(tmp_project, demo_spec):
    cmd_set_status(argparse.Namespace(file=str(demo_spec), status="PLANNING"))
    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "PLANNING"


def test_verified_closed_merges_then_marks(tmp_project, demo_spec):
    _set_raw_status(demo_spec, "EXECUTION_COMPLETE")
    _git(tmp_project, "add", "-A")
    _git(tmp_project, "commit", "-qm", "wip")

    worktree = create_git_worktree("slice-01-demo", tmp_project)
    (worktree / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "feat: work")

    cmd_set_status(argparse.Namespace(file=str(demo_spec), status="VERIFIED_CLOSED"))

    assert (tmp_project / "feature.py").exists()
    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "VERIFIED_CLOSED"


def test_conflict_lands_in_merge_conflict_not_verified_closed(tmp_project, demo_spec):
    """The defect: MERGE_CONFLICT was set from the terminal VERIFIED_CLOSED."""
    _set_raw_status(demo_spec, "EXECUTION_COMPLETE")
    (tmp_project / "shared.py").write_text("original\n", encoding="utf-8")
    _git(tmp_project, "add", "-A")
    _git(tmp_project, "commit", "-qm", "base")

    worktree = create_git_worktree("slice-01-demo", tmp_project)
    (worktree / "shared.py").write_text("branch side\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "branch")

    (tmp_project / "shared.py").write_text("main side\n", encoding="utf-8")
    _git(tmp_project, "add", "-A")
    _git(tmp_project, "commit", "-qm", "main")

    with pytest.raises(SystemExit):
        cmd_set_status(argparse.Namespace(file=str(demo_spec), status="VERIFIED_CLOSED"))

    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "MERGE_CONFLICT"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_set_status.py -v -p no:cacheprovider`
Expected: FAIL — the conflict case ends at `VERIFIED_CLOSED`, and the merge case aborts on a dirty tree

- [ ] **Step 3: Write minimal implementation**

Replace `cmd_set_status` in `scripts/orchestrator.py`:

```python
def cmd_set_status(args):
    """Set a slice's status. VERIFIED_CLOSED merges first, then marks.

    The order matters: marking VERIFIED_CLOSED first makes the state terminal,
    after which a merge conflict cannot be recorded at all.
    """
    filepath = Path(args.file).resolve()
    project_root = find_project_root(filepath)

    try:
        config = load_agent_config(project_root)
        validate_config(config)
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    state_machine = config["state_machine"]
    valid_statuses = state_machine["valid_statuses"]
    transitions = state_machine["transitions"]

    if args.status != "VERIFIED_CLOSED":
        if not update_frontmatter_status(filepath, args.status, valid_statuses, transitions):
            sys.exit(1)
        return

    frontmatter = parse_frontmatter(filepath.read_text(encoding="utf-8"))
    slice_id = frontmatter.get("slice_id", filepath.stem)

    try:
        merged = merge_and_cleanup_worktree(slice_id, project_root)
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    if not merged:
        update_frontmatter_status(filepath, "MERGE_CONFLICT", valid_statuses, transitions)
        print(f"Merge conflict on 'feat/{slice_id}'. Slice marked MERGE_CONFLICT.")
        print("Resolve the conflict, commit, then set VERIFIED_CLOSED again.")
        sys.exit(1)

    if not update_frontmatter_status(filepath, "VERIFIED_CLOSED", valid_statuses, transitions):
        sys.exit(1)

    run_infrastructure_hook(
        "on_slice_verified_closed",
        project_root=project_root,
        known_events=canonical_events(config.get("agents", {})),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_set_status.py -v -p no:cacheprovider`
Expected: PASS, 3 tests

- [ ] **Step 5: Commit**

```bash
git add scripts/orchestrator.py tests/test_set_status.py
git commit -m "fix: merge before marking VERIFIED_CLOSED

Marking the terminal status first made MERGE_CONFLICT unreachable, so a
conflicting merge was recorded nowhere at all.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 13: A CLI that runs both ways, and a summary that finds its logs

`python scripts/orchestrator.py` raises `ModuleNotFoundError` — every documented command. `cmd_summary` still reads `logs/` relative to CWD.

**Files:**
- Modify: `scripts/orchestrator.py` (`cmd_summary`, `cmd_status`, `cmd_trigger_hook`, argument parser)
- Test: `tests/test_entrypoint.py`

**Interfaces:**
- Consumes: `logs_dir` (Task 2).
- Produces: `cmd_summary(args)` reading `args.dir` for the project root; `summary`, `status` and `trigger-hook` all accept `--dir`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_entrypoint.py`:

```python
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(argv, cwd):
    return subprocess.run(
        [sys.executable, *argv], cwd=cwd, capture_output=True, text=True
    )


def test_script_invocation_works():
    """The documented form: `python scripts/orchestrator.py status`."""
    result = _run(["scripts/orchestrator.py", "status"], cwd=REPO_ROOT)
    assert result.returncode == 0, result.stderr
    assert "ModuleNotFoundError" not in result.stderr


def test_module_invocation_works():
    result = _run(["-m", "scripts.orchestrator", "status"], cwd=REPO_ROOT)
    assert result.returncode == 0, result.stderr


def test_script_invocation_works_from_another_directory(tmp_path):
    """Installed as a plugin, cwd is the user's project, not the plugin root."""
    result = _run([str(REPO_ROOT / "scripts" / "orchestrator.py"), "status"], cwd=tmp_path)
    assert result.returncode == 0, result.stderr


def test_runner_module_is_invocable():
    result = _run(["-m", "scripts.runner", "--help"], cwd=REPO_ROOT)
    assert result.returncode == 0, result.stderr


def test_summary_reads_logs_from_the_project_root(tmp_path):
    logs = tmp_path / ".superpowers" / "logs"
    logs.mkdir(parents=True)
    (logs / "executor_slice-01-auth.log").write_text(
        "\n".join(f"Line {i}" for i in range(100)), encoding="utf-8"
    )
    result = _run(
        [str(REPO_ROOT / "scripts" / "orchestrator.py"), "summary",
         "--slice", "slice-01-auth", "--dir", str(tmp_path)],
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert "Line 99" in result.stdout
    assert "Line 49" not in result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_entrypoint.py -v -p no:cacheprovider`
Expected: FAIL — `test_script_invocation_works` sees `ModuleNotFoundError: No module named 'scripts.config'` (unless Task 11's bootstrap already landed, in which case the summary test fails on `--dir`)

- [ ] **Step 3: Write minimal implementation**

The `sys.path` bootstrap was added in Task 11. Now replace `cmd_summary` and add `--dir` to the `summary` parser in `scripts/orchestrator.py`:

```python
def cmd_summary(args):
    """Print the tail of an execution log for audit."""
    project_root = Path(args.dir).resolve() if args.dir else Path.cwd()
    directory = logs_dir(project_root)

    matching = sorted(directory.glob(f"*{args.slice}*.log")) if directory.exists() else []
    if not matching:
        print(f"No execution log for slice '{args.slice}' in {directory}")
        sys.exit(1)

    log_file = max(matching, key=lambda path: path.stat().st_mtime)
    lines = log_file.read_text(encoding="utf-8", errors="ignore").splitlines()

    print(f"\n--- LAST 50 LINES OF {log_file.name} ---")
    print("\n".join(lines[-50:]))
```

In `main()`, add the `--dir` option to the summary subparser:

```python
    p_sum = subparsers.add_parser("summary", help="Show execution summary log for audit")
    p_sum.add_argument("--slice", required=True, help="Slice ID or keyword")
    p_sum.add_argument("--dir", default="", help="Project root directory (default: cwd)")
```

Also delete the now-superseded `test_cmd_summary` from `tests/test_orchestrator.py` — it chdirs into a temp directory and asserts the old `logs/` layout.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_entrypoint.py tests/test_orchestrator.py -v -p no:cacheprovider`
Expected: PASS, 5 entry point tests

- [ ] **Step 5: Commit**

```bash
git add scripts/orchestrator.py tests/test_entrypoint.py tests/test_orchestrator.py
git commit -m "fix: make the documented CLI invocation actually work

python scripts/orchestrator.py raised ModuleNotFoundError for every command
in the README and the skill. Both invocation forms are now covered by tests,
including from a foreign working directory, which is the installed-plugin case.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 14: Restore the cross-platform hook wrapper

`hooks/run-hook.cmd` was reduced to a Windows-only batch stub, so `SessionStart` is dead on macOS and Linux. Restore the polyglot.

**Files:**
- Modify: `hooks/run-hook.cmd`, `hooks/hooks.json`
- Test: `tests/test_session_start_hook.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `hooks/run-hook.cmd` valid as both a POSIX shell script and a Windows batch file.

- [ ] **Step 1: Write the failing test**

Create `tests/test_session_start_hook.py`:

```python
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_run_hook_is_valid_as_a_posix_script():
    """On macOS and Linux the wrapper is executed as a shell script."""
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "hooks" / "run-hook.cmd"), "session-start"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert "command not found" not in result.stderr
    assert "syntax error" not in result.stderr
    payload = json.loads(result.stdout)
    assert payload


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_session_start_emits_the_orchestrator_skill():
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "hooks" / "session-start")],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    payload = json.loads(result.stdout)
    context = json.dumps(payload)
    assert "multiagent-orchestrator" in context


def test_hooks_json_has_no_shell_key():
    """Upstream does not set it and it buys nothing here."""
    config = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    entry = config["hooks"]["SessionStart"][0]["hooks"][0]
    assert "shell" not in entry
    assert entry["type"] == "command"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_session_start_hook.py -v -p no:cacheprovider`
Expected: FAIL — `bash run-hook.cmd` reports `@echo: command not found` and `syntax error: unexpected end of file`; `hooks.json` still carries `"shell": "bash"`

- [ ] **Step 3: Write minimal implementation**

Replace `hooks/run-hook.cmd` with the polyglot. The leading `: << 'CMDBLOCK'` makes bash swallow the batch section as a heredoc, while `cmd.exe` ignores the line and runs the batch:

```
: << 'CMDBLOCK'
@echo off
REM Cross-platform polyglot wrapper for hook scripts.
REM On Windows: cmd.exe runs the batch portion, which finds and calls bash.
REM On Unix: the shell interprets this as a script (: is a no-op in bash).
REM
REM Usage: run-hook.cmd <script-name> [args...]

if "%~1"=="" (
    echo run-hook.cmd: missing script name >&2
    exit /b 1
)

set "HOOK_DIR=%~dp0"

REM Try Git for Windows bash in standard locations
if exist "C:\Program Files\Git\bin\bash.exe" (
    "C:\Program Files\Git\bin\bash.exe" "%HOOK_DIR%%~1" %2 %3 %4 %5 %6 %7 %8 %9
    exit /b %ERRORLEVEL%
)
if exist "C:\Program Files (x86)\Git\bin\bash.exe" (
    "C:\Program Files (x86)\Git\bin\bash.exe" "%HOOK_DIR%%~1" %2 %3 %4 %5 %6 %7 %8 %9
    exit /b %ERRORLEVEL%
)

REM Try bash on PATH (e.g. user-installed Git Bash, MSYS2, Cygwin)
where bash >nul 2>nul
if %ERRORLEVEL% equ 0 (
    bash "%HOOK_DIR%%~1" %2 %3 %4 %5 %6 %7 %8 %9
    exit /b %ERRORLEVEL%
)

REM No bash found - exit silently rather than error
REM (plugin still works, just without SessionStart context injection)
exit /b 0
CMDBLOCK

# Unix: run the named script directly
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT_NAME="$1"
shift
exec bash "${SCRIPT_DIR}/${SCRIPT_NAME}" "$@"
```

Replace `hooks/hooks.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|clear|compact",
        "hooks": [
          {
            "type": "command",
            "command": "\"${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.cmd\" session-start",
            "async": false
          }
        ]
      }
    ]
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_session_start_hook.py -v -p no:cacheprovider`
Expected: PASS, 3 tests

- [ ] **Step 5: Commit**

```bash
git add hooks/run-hook.cmd hooks/hooks.json tests/test_session_start_hook.py
git commit -m "fix: restore the cross-platform polyglot hook wrapper

The wrapper had been reduced to a Windows-only batch stub, so SessionStart
was a syntax error on macOS and Linux and the plugin injected nothing there.
Also restores the Git Bash probes and the deliberate silent exit when no bash
is present, and drops the non-standard shell key from hooks.json.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 15: Documentation, skill and metadata

Make the documentation describe the software that now exists, and give the skill paths that resolve.

**Files:**
- Modify: `skills/multiagent-orchestrator/SKILL.md`, `hooks/session-start`, `README.md`, `docs/architecture.md`, `docs/configuration.md`, `.claude-plugin/plugin.json`, `package.json`, `requirements.txt`
- Test: `tests/test_docs_consistency.py`

**Interfaces:**
- Consumes: `canonical_events`, `DEFAULT_CONFIG`.
- Produces: no code interface; a test that fails when the docs drift from the code.

- [ ] **Step 1: Write the failing test**

Create `tests/test_docs_consistency.py`:

```python
import json
from pathlib import Path

from scripts.config import DEFAULT_CONFIG
from scripts.hooks import canonical_events

REPO_ROOT = Path(__file__).resolve().parent.parent
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
SKILL = (REPO_ROOT / "skills" / "multiagent-orchestrator" / "SKILL.md").read_text(encoding="utf-8")
CONFIGURATION = (REPO_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")


def test_readme_does_not_reference_invented_events():
    """README used to document an on_execution_complete the code never emits."""
    known = canonical_events(DEFAULT_CONFIG["agents"])
    assert "on_execution_complete" not in README
    for event in ("on_slice_executor_start", "on_executor_complete", "on_slice_verified_closed"):
        assert event in known


def test_readme_documents_the_failed_status():
    assert "FAILED" in README


def test_configuration_documents_success_status():
    assert "success_status" in CONFIGURATION


def test_skill_resolves_the_orchestrator_by_an_absolute_or_anchored_path():
    """A bare relative path resolves against the user's project, not the plugin."""
    assert "python scripts/orchestrator.py" not in SKILL
    assert "base directory" in SKILL.lower()


def test_plugin_manifest_has_distribution_metadata():
    manifest = json.loads(
        (REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    for key in ("name", "description", "version", "author", "license", "repository"):
        assert key in manifest, f"plugin.json is missing '{key}'"
    assert manifest["version"] == "2.0.0"


def test_package_json_version_matches_plugin_manifest():
    plugin = json.loads(
        (REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    package = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    assert plugin["version"] == package["version"] == "2.0.0"


def test_requirements_declare_the_test_dependency():
    requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "ruamel.yaml" in requirements
    assert "pytest" in requirements
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_docs_consistency.py -v -p no:cacheprovider`
Expected: FAIL — README still says `on_execution_complete`, the manifest has no `author` and is at `1.0.0`, the skill still says `python scripts/orchestrator.py`

- [ ] **Step 3: Write minimal implementation**

**`.claude-plugin/plugin.json`:**

```json
{
  "name": "superpowers-multiagents",
  "description": "Multi-agent orchestration for Superpowers workflows: a configurable N-level agent hierarchy with supervised background execution and Markdown-frontmatter lifecycle state",
  "version": "2.0.0",
  "author": {
    "name": "Ramil Zakirov"
  },
  "license": "MIT",
  "homepage": "https://github.com/your-username/superpowers-multiagents",
  "repository": "https://github.com/your-username/superpowers-multiagents",
  "keywords": ["multi-agent", "orchestrator", "opencode", "superpowers", "tdd"]
}
```

Replace `your-username` with the real GitHub account before publishing; keep the two URLs identical to whatever it becomes.

**`package.json`:** set `"version": "2.0.0"` and change the description to
`"Multi-agent orchestration extension for Superpowers with supervised background execution"` — it must not hardcode model names that are now configuration.

**`requirements.txt`:**

```
ruamel.yaml>=0.18.5

# Development only
pytest>=8.0
```

**`skills/multiagent-orchestrator/SKILL.md`** — replace the command sections. Add this note directly under the H1:

```markdown
## Resolving the orchestrator path

The orchestrator ships with this plugin, not with the user's project. Resolve
it as `<skill base directory>/../../scripts/orchestrator.py` — the base
directory is announced when this skill is loaded. If this text was injected at
session start, the absolute path is given at the top of the injected block;
prefer that. Run every command from the user's project directory: the path
locates the orchestrator, the working directory locates the project.
```

Then rewrite each command block to use `<orchestrator>` as the resolved path, for example:

```bash
python "<orchestrator>" status --dir docs/superpowers
python "<orchestrator>" dispatch-agent --role planner --file docs/superpowers/specs/YYYY-MM-DD-slice-N-design.md
python "<orchestrator>" set-status --file docs/superpowers/plans/YYYY-MM-DD-slice-N-plan.md --status PLAN_APPROVED
python "<orchestrator>" summary --slice slice-N --dir .
```

Also update the skill's lifecycle section: the state list gains `FAILED`, and the
descriptions of agents 3 and 4 must say that the **orchestrator** sets the
terminal status from the agent's exit code — the agent is no longer asked to set
its own.

**`hooks/session-start`:** prepend the resolved absolute path to the injected
block. Change the `session_context` assignment to:

```bash
orchestrator_path="${PLUGIN_ROOT}/scripts/orchestrator.py"
session_context="<EXTREMELY_IMPORTANT>\nYou have superpowers multi-agent orchestration enabled.\n\nOrchestrator absolute path: ${orchestrator_path}\nUse it wherever the skill below writes <orchestrator>.\n\n**Below is the full content of your 'superpowers-multiagents:multiagent-orchestrator' skill:**\n\n${escaped_content}\n</EXTREMELY_IMPORTANT>"
```

**`README.md`:**
- Replace the `on_execution_complete` example with `--event on_executor_complete`.
- Update the hooks example to the canonical four events.
- Add `FAILED` to the lifecycle table with the note "set by the orchestrator when the agent exits non-zero".
- Change the state-machine table's responsible-agent column for `PLAN_GENERATED` and `EXECUTION_COMPLETE` to "Orchestrator (from exit code)".
- Replace the Quickstart command paths with the two working forms:
  `python -m scripts.orchestrator status` from a clone, and
  `python "/abs/path/to/plugin/scripts/orchestrator.py" status` when installed.
- Replace the `Status: Production--Ready` badge with `Status: Beta`.
- Update the repository-structure block: add `scripts/errors.py`, `scripts/paths.py`, `scripts/runner.py`, and the new test modules; note that runtime artifacts live in `.superpowers/logs/` and `.superpowers/locks/`.
- In the comparison matrix, change the "State Audit" row's claim to describe what is now true: status is derived from the supervisor's exit code and the full agent transcript is captured to `.superpowers/logs/`.

**`docs/configuration.md`:**
- Add `success_status` to the agent-properties table: "Status set by the orchestrator when the agent exits 0".
- Add `FAILED` to the `valid_statuses` example and the transitions example.
- Document that a partial override deep-merges over the defaults.
- Document that global `harness.default` / `harness.provider` are inherited by agents that do not set their own.
- Document that unknown keys and unknown statuses are rejected at load time.

**`docs/architecture.md`:**
- Add three rows to the module table: `errors.py` — "Exception hierarchy; library modules raise, process boundaries exit"; `paths.py` — "Runtime artifact layout under `.superpowers/`"; `runner.py` — "Supervisor owning one dispatched agent from spawn to terminal status".
- Update the adapter table: `build_command` returns `list[str]`, and the OpenCode pattern is `opencode run --model <provider>/<model> <prompt>`.
- Add this section verbatim after "Adapter System":

```markdown
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_docs_consistency.py -v -p no:cacheprovider`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add README.md docs/ skills/ hooks/session-start .claude-plugin/plugin.json package.json requirements.txt tests/test_docs_consistency.py
git commit -m "docs: describe the software that now exists

Removes the invented on_execution_complete event, the unearned
Production-Ready badge, and command paths that cannot resolve when the plugin
is installed. Documents FAILED, success_status, deep merge and harness
inheritance. Adds distribution metadata and bumps to 2.0.0.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 16: Full-suite verification and the consumer fix

Confirm the whole suite green, then repair the one consumer whose hook has never fired.

**Files:**
- Modify: `<downstream project root>\.superpowers\hooks.yaml` (a **different repository** — commit there separately)
- Test: the full suite

**Interfaces:**
- Consumes: everything.
- Produces: nothing.

- [ ] **Step 1: Run the full suite**

Run: `python -m pytest tests/ -v -p no:cacheprovider`
Expected: PASS, all tests. Record the count. If anything fails, fix it before continuing — do not proceed on a red suite.

- [ ] **Step 2: Verify the acceptance criteria by hand**

Run each and confirm the stated output:

```bash
python -m scripts.orchestrator status
python scripts/orchestrator.py status
bash hooks/run-hook.cmd session-start
```

Expected: the first two print the status report; the third prints JSON containing `multiagent-orchestrator`.

Map each of the nine acceptance criteria in §7 of the spec to its evidence and
write the mapping into the commit message of Step 5. Criteria 3, 4, 5 and 6 are
covered by `tests/test_runner.py`, `tests/test_locks.py::test_claimed_lock_blocks_a_second_acquisition`,
`tests/test_git_ops.py::test_orchestrator_artifacts_do_not_make_the_tree_dirty`
and `tests/test_config.py` respectively — cite the test, not an impression.

- [ ] **Step 3: Confirm the unknown-event warning fires on the real consumer config**

Run:

```bash
python -c "import sys; sys.path.insert(0,'.'); from scripts.hooks import load_project_hooks, canonical_events; from scripts.config import DEFAULT_CONFIG; from pathlib import Path; load_project_hooks(Path(r'<downstream project root>'), known_events=canonical_events(DEFAULT_CONFIG['agents']))"
```

Expected: a warning naming `on_slice_execution_start` and listing `on_slice_executor_start` among the known events. This is the guard proving itself against the bug it was written for.

- [ ] **Step 4: Fix the consumer**

In `<downstream project root>\.superpowers\hooks.yaml`, rename the event:

```yaml
hooks:
  on_slice_executor_start:
    command: "python .claude/skills/sandbox-loopback/scripts/sandbox_loopback.py up"
    capture_env: true

  on_slice_verified_closed:
    command: "python .claude/skills/sandbox-loopback/scripts/sandbox_loopback.py teardown --yes"
```

Re-run the command from Step 3 and confirm no warning is printed.

- [ ] **Step 5: Commit both repositories**

In `superpowers-multiagents`:

```bash
git add -A
git commit -m "chore: verify full suite for slice-01 orchestrator hardening

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

In `downstream-project` (separate repository, separate commit):

```bash
git add .superpowers/hooks.yaml
git commit -m "fix(superpowers): correct hook event name so sandbox-loopback fires

The orchestrator emits on_slice_executor_start; this file declared
on_slice_execution_start, so the hook has never run and LOOPBACK_IP was
never captured — while docker-compose.yml is fail-closed on it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Done criteria

All boxes above ticked, the full suite green, and the nine acceptance criteria in
§7 of the spec confirmed by hand. The branch is **not** pushed and **not** merged
without an explicit go-ahead.
