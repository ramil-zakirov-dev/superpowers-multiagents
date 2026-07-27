---
slice_id: "slice-02-native-sandbox"
title: "Native sandbox — implementation plan"
status: PLAN_GENERATED
spec: "docs/superpowers/specs/2026-07-27-slice-02-native-sandbox-design.md"
---

# Native Sandbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make per-slice infrastructure isolation a first-class, opt-in capability of the orchestrator, so a dispatched agent's compose stack is addressed by *its own* slice branch instead of whatever branch the main repository happens to be on.

**Architecture:** A new leaf module `scripts/sandbox.py` owns loopback allocation, state, and `docker compose` invocation. It never resolves a branch itself — the branch is always a parameter, which is the constructive repair of the defect this slice exists to fix. `orchestrator.py` decides *when* to call it (dispatch, and the `VERIFIED_CLOSED` transition); `runner.py` calls it once on a non-zero agent exit. The hook mechanism is deliberately left alone.

**Tech Stack:** Python 3.10+, stdlib only (`socket`, `subprocess`, `hashlib`, `json`, `errno`, `dataclasses`), `ruamel.yaml` (already a dependency), `pytest`.

## Global Constraints

- **No test may invoke a real harness or a real container runtime.** `opencode` is installed on the development machine and a live run costs money; `docker compose up` on a real project is equally out of bounds inside the suite.
- No new runtime dependencies. Standard library plus the existing `ruamel.yaml`.
- No `shell=True` anywhere in new code. Build argv lists.
- Windows and POSIX are both supported. Nothing may assume a shebang, a `.cmd` wrapper, or `/bin/sh`.
- The orchestrator never runs `sudo` and never configures host networking.
- No client-identifying names, hostnames, or absolute machine paths in any committed text. The consuming project is referred to as `downstream-project`.
- Target version is `2.1.0`, and `test_package_json_version_matches_plugin_manifest` requires both manifests to agree. It is pinned to `"2.0.0"` today, so the version bump and that test's constant change together, in Task 12.
- Run pytest as `python -m pytest ... -p no:cacheprovider --basetemp=<a writable dir>`. The default temp root on this machine denies `mkdir` and produces ~70 spurious `PermissionError: [WinError 5]` errors that are environmental, not regressions.
- Conventional Commits. Every commit message ends with the trailer `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

---

### Task 1: Artifact path for sandbox state

**Files:**
- Modify: `scripts/paths.py`
- Modify: `README.md` (runtime artifacts section)
- Modify: `docs/architecture.md` (runtime artifacts subsection)
- Test: `tests/test_paths.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `sandbox_dir(project_root: Path) -> Path`, `sandbox_state_path(project_root: Path, project_name: str) -> Path`, and `".superpowers/sandbox/"` added to `ARTIFACT_PREFIXES`.

`ARTIFACT_PREFIXES` feeds `test_runtime_artifact_paths_are_documented`, which asserts every prefix is explained in `README.md` or `docs/architecture.md`. Adding the prefix without the prose turns that test red, so the documentation belongs in this task.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_paths.py`:

```python
def test_sandbox_state_lives_under_the_superpowers_root(tmp_path):
    from scripts.paths import sandbox_dir, sandbox_state_path

    assert sandbox_dir(tmp_path) == tmp_path / ".superpowers" / "sandbox"
    assert sandbox_state_path(tmp_path, "feat-alpha") == (
        tmp_path / ".superpowers" / "sandbox" / "feat-alpha.json"
    )


def test_sandbox_state_is_a_runtime_artifact():
    from scripts.paths import ARTIFACT_PREFIXES, is_artifact_path

    assert ".superpowers/sandbox/" in ARTIFACT_PREFIXES
    assert is_artifact_path(".superpowers/sandbox/feat-alpha.json")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_paths.py -v -p no:cacheprovider --basetemp=.pytest-tmp`
Expected: FAIL with `ImportError: cannot import name 'sandbox_dir' from 'scripts.paths'`

- [ ] **Step 3: Implement the minimal code**

In `scripts/paths.py`, add `".superpowers/sandbox/"` to `ARTIFACT_PREFIXES` (keep `.worktrees/` last) and append:

```python
def sandbox_dir(project_root: Path) -> Path:
    return superpowers_dir(project_root) / "sandbox"


def sandbox_state_path(project_root: Path, project_name: str) -> Path:
    """Where one compose project's allocation record lives.

    Keyed by the compose project name rather than the raw branch, because a
    branch name may contain path separators and a compose project name is
    already constrained to `[a-z0-9_-]`.
    """
    return sandbox_dir(project_root) / f"{project_name}.json"
```

- [ ] **Step 4: Document the path so the consistency test stays green**

In `README.md`, in the "🗂 Runtime Artifacts" section, add a row describing `.superpowers/sandbox/` as holding one JSON record per compose project — the branch it belongs to, its loopback address, and when it was started — and note that the record is removed only when the stack's volumes are destroyed.

In `docs/architecture.md`, under the runtime-artifacts subsection, add the same path with one sentence: it is orchestrator state, not slice payload, which is why it does not live under `.worktrees/`.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q -p no:cacheprovider --basetemp=.pytest-tmp`
Expected: PASS, 145 tests (143 existing + 2 new).

- [ ] **Step 6: Commit**

```bash
git add scripts/paths.py tests/test_paths.py README.md docs/architecture.md
git commit -m "feat(sandbox): reserve .superpowers/sandbox for allocation state"
```

---

### Task 2: Loopback allocation, with the errno split

**Files:**
- Create: `scripts/sandbox.py`
- Modify: `scripts/errors.py`
- Test: `tests/test_sandbox.py`

**Interfaces:**
- Consumes: `scripts.errors.OrchestratorError`.
- Produces: `SandboxError`, `project_name_for(branch) -> str`, `_probe(ip, port=0) -> str` returning one of `"free" | "busy" | "unavailable"`, `preflight(ip) -> str`, `ip_for(branch, *, busy=()) -> str`.

> Refinement of spec §4.1, which typed `preflight` as `-> None`. Returning the
> verdict lets `ip_for` reuse the single probe it already paid for instead of
> binding each candidate twice. The raising behaviour is unchanged.

The bare `except OSError` in the original conflates "this address is occupied" with "this platform never configured the address". That is why an unaliased loopback interface reports 254 busy octets instead of naming a one-line remedy. `_probe` returns a three-valued verdict so the two can never be confused again.

- [ ] **Step 1: Write the failing test**

Create `tests/test_sandbox.py`:

```python
import errno
import socket

import pytest

from scripts.errors import OrchestratorError
from scripts import sandbox


def test_project_name_follows_compose_rules():
    assert sandbox.project_name_for("feat/Alpha_1") == "feat-alpha_1"
    assert sandbox.project_name_for("feat//weird--branch--") == "feat-weird-branch"
    assert sandbox.project_name_for("") == "default"


def test_ip_for_is_deterministic_and_never_loopback_one():
    first = sandbox.ip_for("feat/alpha")
    second = sandbox.ip_for("feat/alpha")
    assert first == second
    assert first != "127.0.0.1"
    assert first.startswith("127.0.0.")


def test_ip_for_skips_addresses_reported_busy(monkeypatch):
    """A branch whose hashed octet is taken must move on, not fail."""
    start = sandbox._hash_octet("feat/alpha")
    taken = f"127.0.0.{start}"
    monkeypatch.setattr(
        sandbox, "_probe", lambda ip, port=0: "busy" if ip == taken else "free"
    )
    assert sandbox.ip_for("feat/alpha") != taken


def test_ip_for_skips_addresses_the_caller_declares_busy(monkeypatch):
    start = sandbox._hash_octet("feat/alpha")
    taken = f"127.0.0.{start}"
    monkeypatch.setattr(sandbox, "_probe", lambda ip, port=0: "free")
    assert sandbox.ip_for("feat/alpha", busy=[taken]) != taken


def test_unavailable_address_aborts_with_a_remedy_not_a_busy_report(monkeypatch):
    """EADDRNOTAVAIL is a platform fact. Scanning 254 more is the wrong answer."""
    probes = []

    def fake_probe(ip, port=0):
        probes.append(ip)
        return "unavailable"

    monkeypatch.setattr(sandbox, "_probe", fake_probe)

    with pytest.raises(OrchestratorError) as excinfo:
        sandbox.ip_for("feat/alpha")

    message = str(excinfo.value)
    assert "ifconfig" in message, "the error must name the remediation command"
    assert "no free loopback" not in message.lower()
    assert len(probes) == 1, f"aborted after {len(probes)} probes, expected 1"


def test_probe_maps_errnos_to_verdicts(monkeypatch):
    class FakeSocket:
        def __init__(self, code):
            self._code = code

        def bind(self, _address):
            if self._code is None:
                return None
            raise OSError(self._code, "fake")

        def close(self):
            return None

    monkeypatch.setattr(
        socket, "socket", lambda *a, **k: FakeSocket(errno.EADDRNOTAVAIL)
    )
    assert sandbox._probe("127.0.0.9") == "unavailable"

    monkeypatch.setattr(socket, "socket", lambda *a, **k: FakeSocket(errno.EADDRINUSE))
    assert sandbox._probe("127.0.0.9") == "busy"

    monkeypatch.setattr(socket, "socket", lambda *a, **k: FakeSocket(None))
    assert sandbox._probe("127.0.0.9") == "free"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_sandbox.py -v -p no:cacheprovider --basetemp=.pytest-tmp`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.sandbox'`

- [ ] **Step 3: Add the error type**

Append to `scripts/errors.py`:

```python
class SandboxError(OrchestratorError):
    """The infrastructure sandbox could not be brought to the requested state."""
```

- [ ] **Step 4: Write the module**

Create `scripts/sandbox.py`:

```python
"""Per-slice infrastructure isolation via loopback addressing.

Each slice's compose stack is published on its own `127.0.0.x` address and
its own compose project, so parallel worktrees never contend for a host port.
Container-to-container traffic is unaffected: only host-side publishing is
rebound.

The branch is a parameter of every function here and is never resolved from
git inside this module. That is deliberate. This code is called from the
dispatcher, from a detached supervisor, and from a human CLI, and only the
first of those is standing anywhere near the slice's own worktree -- a
module that guessed would guess wrong in two of the three.
"""

import errno
import hashlib
import re
import socket
from typing import Iterable

from scripts.errors import SandboxError

#: Errnos meaning "the platform has not configured this address", as opposed
#: to "something else is already bound to it". Windows reports the WSA alias.
_UNAVAILABLE_ERRNOS = {errno.EADDRNOTAVAIL}
if hasattr(errno, "WSAEADDRNOTAVAIL"):  # Windows only
    _UNAVAILABLE_ERRNOS.add(errno.WSAEADDRNOTAVAIL)


def project_name_for(branch: str) -> str:
    """Map a git branch to a docker-compose project name (`[a-z0-9_-]`)."""
    if not branch:
        return "default"
    name = re.sub(r"[^a-z0-9_-]", "-", branch.lower())
    name = re.sub(r"-+", "-", name).strip("-")
    return name or "default"


def _hash_octet(branch: str) -> int:
    """A stable octet in 2..255 for this branch.

    md5 is a bucketing function here, not a security primitive: the only
    property required is that the same branch lands on the same octet across
    runs and machines.
    """
    digest = hashlib.md5(branch.encode("utf-8")).hexdigest()
    return (int(digest[:2], 16) % 254) + 2


def _probe(ip: str, port: int = 0) -> str:
    """Return 'free', 'busy' or 'unavailable' for `ip`.

    The three-valued answer is the whole point. Collapsing 'unavailable' into
    'busy' is what makes an unaliased loopback interface look like 254
    occupied stacks.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((ip, port))
        return "free"
    except OSError as exc:
        return "unavailable" if exc.errno in _UNAVAILABLE_ERRNOS else "busy"
    finally:
        sock.close()


def preflight(ip: str) -> str:
    """Probe `ip`, refusing to continue if the platform has not configured it.

    Returns 'free' or 'busy'. Raises SandboxError on 'unavailable' -- that is
    a property of the host, and no amount of scanning other octets will
    change it.
    """
    verdict = _probe(ip)
    if verdict != "unavailable":
        return verdict
    raise SandboxError(
        f"The loopback address {ip} is not configured on this host, so a "
        f"per-slice stack cannot be published on it.\n"
        f"On macOS only 127.0.0.1 exists by default; add the alias once:\n"
        f"    sudo ifconfig lo0 alias {ip} up\n"
        f"The orchestrator will not run this for you -- it does not elevate "
        f"privileges."
    )


def ip_for(branch: str, *, busy: Iterable[str] = ()) -> str:
    """Pick a free loopback address for `branch`, deterministically.

    Starts at the branch's hashed octet and walks forward, wrapping. Never
    returns 127.0.0.1: the octet range is 2..255 by construction.
    """
    busy_set = set(busy)
    start = _hash_octet(branch)
    for delta in range(254):
        octet = ((start - 2 + delta) % 254) + 2
        ip = f"127.0.0.{octet}"
        if ip in busy_set:
            continue
        if preflight(ip) == "free":
            return ip
    raise SandboxError(
        f"No free loopback address in 127.0.0.2..127.0.0.255 for branch "
        f"{branch!r}; {len(busy_set)} are held by tracked stacks. Run "
        f"`sandbox status` and tear down what you no longer need."
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_sandbox.py -v -p no:cacheprovider --basetemp=.pytest-tmp`
Expected: PASS, 6 tests.

- [ ] **Step 6: Commit**

```bash
git add scripts/sandbox.py scripts/errors.py tests/test_sandbox.py
git commit -m "feat(sandbox): deterministic loopback allocation with an errno-aware preflight"
```

---

### Task 3: Allocation state and its one invariant

**Files:**
- Modify: `scripts/sandbox.py`
- Test: `tests/test_sandbox.py`

**Interfaces:**
- Consumes: `sandbox.project_name_for`, `scripts.paths.sandbox_dir`, `scripts.paths.sandbox_state_path`.
- Produces: `SandboxState` (frozen dataclass with `branch`, `ip`, `project_name`, `started_at`), `read_state(project_root, branch) -> SandboxState | None`, `write_state(project_root, state) -> Path`, `clear_state(project_root, branch) -> None`, `list_states(project_root) -> list[SandboxState]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sandbox.py`:

```python
def test_state_round_trips(tmp_path):
    record = sandbox.SandboxState(
        branch="feat/alpha", ip="127.0.0.7",
        project_name="feat-alpha", started_at="2026-07-27T00:00:00+00:00",
    )
    sandbox.write_state(tmp_path, record)
    assert sandbox.read_state(tmp_path, "feat/alpha") == record


def test_read_state_is_none_when_untracked(tmp_path):
    assert sandbox.read_state(tmp_path, "feat/nothing") is None


def test_clear_state_is_idempotent(tmp_path):
    sandbox.clear_state(tmp_path, "feat/absent")  # must not raise
    record = sandbox.SandboxState("feat/a", "127.0.0.7", "feat-a", "t")
    sandbox.write_state(tmp_path, record)
    sandbox.clear_state(tmp_path, "feat/a")
    assert sandbox.read_state(tmp_path, "feat/a") is None


def test_list_states_ignores_unparsable_files(tmp_path):
    record = sandbox.SandboxState("feat/a", "127.0.0.7", "feat-a", "t")
    sandbox.write_state(tmp_path, record)
    (sandbox_state_dir := sandbox.sandbox_dir(tmp_path)).mkdir(exist_ok=True)
    (sandbox_state_dir / "garbage.json").write_text("{not json", encoding="utf-8")
    assert sandbox.list_states(tmp_path) == [record]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_sandbox.py -v -p no:cacheprovider --basetemp=.pytest-tmp`
Expected: FAIL with `AttributeError: module 'scripts.sandbox' has no attribute 'SandboxState'`

- [ ] **Step 3: Implement**

Add to the imports of `scripts/sandbox.py`:

```python
import dataclasses
import json
from pathlib import Path

from scripts.paths import sandbox_dir, sandbox_state_path
```

Append to `scripts/sandbox.py`:

```python
@dataclasses.dataclass(frozen=True)
class SandboxState:
    """One compose project's allocation record.

    Lifetime rule, and the only one: this record exists exactly as long as
    the stack's volumes do. A teardown that keeps volumes keeps the record,
    so a re-`up` returns the same address and the same data.
    """

    branch: str
    ip: str
    project_name: str
    started_at: str


def write_state(project_root, state: SandboxState) -> Path:
    path = sandbox_state_path(Path(project_root), state.project_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dataclasses.asdict(state), indent=2) + "\n", encoding="utf-8"
    )
    return path


def read_state(project_root, branch: str) -> "SandboxState | None":
    path = sandbox_state_path(Path(project_root), project_name_for(branch))
    if not path.is_file():
        return None
    try:
        return SandboxState(**json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


def clear_state(project_root, branch: str) -> None:
    sandbox_state_path(Path(project_root), project_name_for(branch)).unlink(
        missing_ok=True
    )


def list_states(project_root) -> list:
    directory = sandbox_dir(Path(project_root))
    if not directory.is_dir():
        return []
    found = []
    for entry in sorted(directory.glob("*.json")):
        try:
            found.append(SandboxState(**json.loads(entry.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            continue
    return found
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_sandbox.py -v -p no:cacheprovider --basetemp=.pytest-tmp`
Expected: PASS, 10 tests.

- [ ] **Step 5: Commit**

```bash
git add scripts/sandbox.py tests/test_sandbox.py
git commit -m "feat(sandbox): allocation state keyed by compose project name"
```

---

### Task 4: Configuration schema, fail-closed

**Files:**
- Modify: `scripts/config.py`
- Modify: `scripts/sandbox.py`
- Test: `tests/test_config.py`, `tests/test_sandbox.py`

**Interfaces:**
- Consumes: `scripts.config.DEFAULT_CONFIG`, `validate_config`.
- Produces: a `sandbox` block in `DEFAULT_CONFIG`; `KNOWN_SANDBOX_KEYS`, `KNOWN_TEARDOWN_KEYS`, `TEARDOWN_MODES`, `KNOWN_TOKENS` in `scripts/config.py`; `sandbox.render_env(sandbox_cfg, ip, project) -> dict[str, str]`.

Defaults are inert: `enabled: False` and an empty `env`. A project that says nothing gets nothing.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
import pytest

from scripts.config import DEFAULT_CONFIG, validate_config
from scripts.errors import ConfigError


def _with_sandbox(**overrides):
    import copy
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["sandbox"].update(overrides)
    return config


def test_sandbox_is_disabled_by_default():
    assert DEFAULT_CONFIG["sandbox"]["enabled"] is False
    assert DEFAULT_CONFIG["sandbox"]["env"] == {}


def test_unknown_sandbox_key_fails_closed():
    config = _with_sandbox(enabled=True)
    config["sandbox"]["compose_fiel"] = "typo.yml"
    with pytest.raises(ConfigError, match="compose_fiel"):
        validate_config(config)


def test_unknown_template_token_fails_closed():
    config = _with_sandbox(enabled=True, env={"dsn": "postgres://{IP}:5432/db"})
    with pytest.raises(ConfigError, match=r"\{IP\}"):
        validate_config(config)


def test_known_template_tokens_are_accepted():
    config = _with_sandbox(
        enabled=True, env={"dsn": "postgres://{ip}:5432/{project}"}
    )
    validate_config(config)  # must not raise


def test_teardown_mode_outside_the_enum_fails_closed():
    config = _with_sandbox(enabled=True)
    config["sandbox"]["teardown"]["on_failed"] = "nuke"
    with pytest.raises(ConfigError, match="nuke"):
        validate_config(config)


def test_unknown_teardown_key_fails_closed():
    config = _with_sandbox(enabled=True)
    config["sandbox"]["teardown"]["on_whatever"] = "none"
    with pytest.raises(ConfigError, match="on_whatever"):
        validate_config(config)
```

Append to `tests/test_sandbox.py`:

```python
def test_render_env_injects_the_contract_variables():
    rendered = sandbox.render_env({"env": {}}, "127.0.0.7", "feat-a")
    assert rendered["LOOPBACK_IP"] == "127.0.0.7"
    assert rendered["COMPOSE_PROJECT_NAME"] == "feat-a"


def test_render_env_substitutes_both_tokens():
    rendered = sandbox.render_env(
        {"env": {"dsn": "postgres://{ip}:5432/{project}"}}, "127.0.0.7", "feat-a"
    )
    assert rendered["dsn"] == "postgres://127.0.0.7:5432/feat-a"


def test_render_env_expands_process_environment(monkeypatch):
    """A project with a real secret sources it from the environment."""
    monkeypatch.setenv("SANDBOX_TEST_PASSWORD", "s3cret")
    rendered = sandbox.render_env(
        {"env": {"dsn": "postgres://u:${SANDBOX_TEST_PASSWORD}@{ip}/db"}},
        "127.0.0.7", "feat-a",
    )
    assert rendered["dsn"] == "postgres://u:s3cret@127.0.0.7/db"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_config.py tests/test_sandbox.py -v -p no:cacheprovider --basetemp=.pytest-tmp`
Expected: FAIL with `KeyError: 'sandbox'`

- [ ] **Step 3: Add the defaults and constants**

In `scripts/config.py`, add `import re` to the imports, add this block to `DEFAULT_CONFIG` after `"harness"`:

```python
    "sandbox": {
        "enabled": False,
        "compose_file": "docker-compose.yml",
        "health_service": None,
        "health_timeout": 60,
        "env": {},
        "teardown": {
            "on_verified_closed": "volumes",
            "on_failed": "containers",
        },
    },
```

and add next to `KNOWN_AGENT_KEYS`:

```python
KNOWN_SANDBOX_KEYS = frozenset({
    "enabled", "compose_file", "health_service", "health_timeout",
    "env", "teardown",
})

KNOWN_TEARDOWN_KEYS = frozenset({"on_verified_closed", "on_failed"})

#: `volumes` destroys data and releases the address; `containers` stops the
#: stack but keeps both, so a failure stays diagnosable; `none` leaves it up.
TEARDOWN_MODES = frozenset({"volumes", "containers", "none"})

#: The only substitutions a `sandbox.env` template may contain.
KNOWN_TOKENS = frozenset({"ip", "project"})

_TOKEN_PATTERN = re.compile(r"\{([^{}]*)\}")
```

- [ ] **Step 4: Extend `validate_config`**

Append inside `validate_config`, after the agent loop:

```python
    sandbox = config.get("sandbox") or {}
    unknown_keys = set(sandbox) - KNOWN_SANDBOX_KEYS
    if unknown_keys:
        raise ConfigError(
            f"sandbox: unknown key(s) {sorted(unknown_keys)}. "
            f"Known keys: {sorted(KNOWN_SANDBOX_KEYS)}"
        )

    teardown = sandbox.get("teardown") or {}
    unknown_teardown = set(teardown) - KNOWN_TEARDOWN_KEYS
    if unknown_teardown:
        raise ConfigError(
            f"sandbox.teardown: unknown key(s) {sorted(unknown_teardown)}. "
            f"Known keys: {sorted(KNOWN_TEARDOWN_KEYS)}"
        )
    for key, mode in teardown.items():
        if mode not in TEARDOWN_MODES:
            raise ConfigError(
                f"sandbox.teardown.{key} = '{mode}' is not a teardown mode. "
                f"Valid modes: {sorted(TEARDOWN_MODES)}"
            )

    for name, template in (sandbox.get("env") or {}).items():
        for token in _TOKEN_PATTERN.findall(str(template)):
            if token not in KNOWN_TOKENS:
                raise ConfigError(
                    f"sandbox.env.{name}: unknown template token '{{{token}}}'. "
                    f"Known tokens: {sorted('{' + t + '}' for t in KNOWN_TOKENS)}"
                )
```

- [ ] **Step 5: Implement `render_env`**

Add `import os` to `scripts/sandbox.py` imports, plus `from scripts.config import KNOWN_TOKENS  # noqa: F401  (documents the contract)` — actually import nothing from config here to keep `sandbox.py` a leaf; validation already lives in `config.py`. Append to `scripts/sandbox.py`:

```python
def render_env(sandbox_cfg: dict, ip: str, project: str) -> dict:
    """Build the environment a sandboxed agent runs with.

    LOOPBACK_IP and COMPOSE_PROJECT_NAME are injected unconditionally and are
    not declarable in config -- they are the contract, not a setting. Process
    environment expansion runs first so a project can keep a real credential
    in .env rather than in a tracked config file; token substitution runs
    after, so an expanded value containing braces is never re-interpreted.
    """
    rendered = {"LOOPBACK_IP": ip, "COMPOSE_PROJECT_NAME": project}
    for name, template in (sandbox_cfg.get("env") or {}).items():
        expanded = os.path.expandvars(str(template))
        rendered[name] = expanded.replace("{ip}", ip).replace("{project}", project)
    return rendered
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_config.py tests/test_sandbox.py -v -p no:cacheprovider --basetemp=.pytest-tmp`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add scripts/config.py scripts/sandbox.py tests/test_config.py tests/test_sandbox.py
git commit -m "feat(sandbox): fail-closed configuration schema with two-token templates"
```

---

### Task 5: Compose invocation behind a testable seam

**Files:**
- Modify: `scripts/sandbox.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_sandbox.py`

**Interfaces:**
- Consumes: `render_env`, `read_state`, `write_state`, `clear_state`, `ip_for`, `project_name_for`.
- Produces: `docker_command() -> list[str]`, `ensure_up(branch, project_root, config) -> dict`, `resolve_env(branch, project_root, config) -> dict`, `tear_down(branch, project_root, config, mode) -> None`; and a `stub_docker` pytest fixture returning an object with `.calls` (a list of argv lists) and `.script` (path).

`SUPERPOWERS_DOCKER_BIN` is the seam. It must work across a process boundary, because teardown on failure happens inside the detached supervisor where a monkeypatched function in the test process has no effect. It accepts either a plain path or a JSON argv list, so a test can point it at `[sys.executable, "<stub>.py"]` without needing a shebang on POSIX or a `.cmd` shim on Windows.

- [ ] **Step 1: Write the failing test**

Add to `tests/conftest.py`:

```python
import json
import sys

STUB_DOCKER = '''
import json, os, sys
record = os.environ["SUPERPOWERS_DOCKER_LOG"]
with open(record, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({
        "argv": sys.argv[1:],
        "loopback_ip": os.environ.get("LOOPBACK_IP"),
        "compose_project": os.environ.get("COMPOSE_PROJECT_NAME"),
    }) + "\\n")
if "--format" in sys.argv:
    print(json.dumps({"Service": "postgres", "Health": "healthy"}))
sys.exit(int(os.environ.get("SUPERPOWERS_DOCKER_EXIT", "0")))
'''


class StubDocker:
    def __init__(self, script, log):
        self.script = script
        self.log = log

    @property
    def calls(self):
        if not self.log.exists():
            return []
        return [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def argv_of(self, index):
        return self.calls[index]["argv"]


@pytest.fixture
def stub_docker(tmp_path, monkeypatch):
    """A fake `docker` that records argv instead of starting containers.

    Installed through the environment rather than by monkeypatching, so it
    is still in effect inside the detached supervisor process.
    """
    script = tmp_path / "stub_docker.py"
    script.write_text(STUB_DOCKER, encoding="utf-8")
    log = tmp_path / "docker-calls.jsonl"
    monkeypatch.setenv(
        "SUPERPOWERS_DOCKER_BIN", json.dumps([sys.executable, str(script)])
    )
    monkeypatch.setenv("SUPERPOWERS_DOCKER_LOG", str(log))
    return StubDocker(script, log)
```

Append to `tests/test_sandbox.py`:

```python
def _sandbox_config(**overrides):
    cfg = {
        "enabled": True,
        "compose_file": "docker-compose.yml",
        "health_service": None,
        "health_timeout": 5,
        "env": {"dsn": "postgres://{ip}:5432/db"},
        "teardown": {"on_verified_closed": "volumes", "on_failed": "containers"},
    }
    cfg.update(overrides)
    return {"sandbox": cfg}


def test_ensure_up_addresses_the_branch_it_was_given(tmp_path, stub_docker):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

    env = sandbox.ensure_up("feat/alpha", tmp_path, _sandbox_config())

    assert env["COMPOSE_PROJECT_NAME"] == "feat-alpha"
    assert env["dsn"] == f"postgres://{env['LOOPBACK_IP']}:5432/db"
    argv = stub_docker.argv_of(0)
    assert argv[:2] == ["compose", "-p"]
    assert argv[2] == "feat-alpha"
    assert argv[-2:] == ["up", "-d"]
    assert stub_docker.calls[0]["loopback_ip"] == env["LOOPBACK_IP"]


def test_ensure_up_is_idempotent_on_the_address(tmp_path, stub_docker):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    first = sandbox.ensure_up("feat/alpha", tmp_path, _sandbox_config())
    second = sandbox.ensure_up("feat/alpha", tmp_path, _sandbox_config())
    assert first["LOOPBACK_IP"] == second["LOOPBACK_IP"]


def test_ensure_up_is_inert_when_disabled(tmp_path, stub_docker):
    assert sandbox.ensure_up("feat/a", tmp_path, _sandbox_config(enabled=False)) == {}
    assert stub_docker.calls == []


def test_ensure_up_fails_closed_without_a_compose_file(tmp_path, stub_docker):
    with pytest.raises(OrchestratorError, match="docker-compose.yml"):
        sandbox.ensure_up("feat/alpha", tmp_path, _sandbox_config())


def test_ensure_up_fails_closed_when_compose_fails(tmp_path, stub_docker, monkeypatch):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setenv("SUPERPOWERS_DOCKER_EXIT", "1")
    with pytest.raises(OrchestratorError):
        sandbox.ensure_up("feat/alpha", tmp_path, _sandbox_config())


def test_resolve_env_has_no_side_effects(tmp_path, stub_docker):
    assert sandbox.resolve_env("feat/alpha", tmp_path, _sandbox_config()) == {}
    assert stub_docker.calls == []


def test_teardown_containers_keeps_state_and_omits_dash_v(tmp_path, stub_docker):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    sandbox.ensure_up("feat/alpha", tmp_path, _sandbox_config())

    sandbox.tear_down("feat/alpha", tmp_path, _sandbox_config(), "containers")

    argv = stub_docker.argv_of(-1)
    assert argv[-1] == "down"
    assert "-v" not in argv
    assert sandbox.read_state(tmp_path, "feat/alpha") is not None


def test_teardown_volumes_destroys_state(tmp_path, stub_docker):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    sandbox.ensure_up("feat/alpha", tmp_path, _sandbox_config())

    sandbox.tear_down("feat/alpha", tmp_path, _sandbox_config(), "volumes")

    assert stub_docker.argv_of(-1)[-2:] == ["down", "-v"]
    assert sandbox.read_state(tmp_path, "feat/alpha") is None


def test_health_gate_blocks_when_the_service_never_reports_healthy(
    tmp_path, stub_docker, monkeypatch
):
    """An agent dispatched at a stack that is not ready fails on its first
    connection, and the reason surfaces only in the agent's own log. Refuse
    at dispatch instead."""
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    # The stub only prints a healthy record when --format is present; drop the
    # marker it looks for so `ps` never reports healthy.
    monkeypatch.setattr(sandbox, "_compose", _never_healthy(sandbox._compose))

    with pytest.raises(OrchestratorError, match="healthy"):
        sandbox.ensure_up(
            "feat/alpha", tmp_path,
            _sandbox_config(health_service="postgres", health_timeout=1),
        )


def _never_healthy(real_compose):
    def wrapper(project_root, cfg, state, args, env, capture=False):
        if capture:
            class Result:
                stdout = '{"Service": "postgres", "Health": "starting"}'
                returncode = 0
            return Result()
        return real_compose(project_root, cfg, state, args, env, capture)
    return wrapper


def test_health_gate_passes_when_the_service_is_healthy(tmp_path, stub_docker):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    env = sandbox.ensure_up(
        "feat/alpha", tmp_path,
        _sandbox_config(health_service="postgres", health_timeout=5),
    )
    assert env["LOOPBACK_IP"].startswith("127.0.0.")


def test_teardown_none_touches_nothing(tmp_path, stub_docker):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    sandbox.ensure_up("feat/alpha", tmp_path, _sandbox_config())
    before = len(stub_docker.calls)

    sandbox.tear_down("feat/alpha", tmp_path, _sandbox_config(), "none")

    assert len(stub_docker.calls) == before
    assert sandbox.read_state(tmp_path, "feat/alpha") is not None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_sandbox.py -v -p no:cacheprovider --basetemp=.pytest-tmp`
Expected: FAIL with `AttributeError: module 'scripts.sandbox' has no attribute 'ensure_up'`

- [ ] **Step 3: Implement**

Add `import subprocess`, `import time`, and `from datetime import datetime, timezone` to `scripts/sandbox.py`, then append:

```python
def docker_command() -> list:
    """The argv prefix that invokes docker.

    `SUPERPOWERS_DOCKER_BIN` may hold a plain path or a JSON argv list. The
    list form exists for tests: it lets a stub be `[python, stub.py]` with no
    shebang on POSIX and no .cmd shim on Windows, and -- unlike a
    monkeypatch -- it survives into the detached supervisor process.
    """
    raw = (os.environ.get("SUPERPOWERS_DOCKER_BIN") or "").strip()
    if not raw:
        return ["docker"]
    if raw.startswith("["):
        return [str(part) for part in json.loads(raw)]
    return [raw]


def _compose_file(project_root: Path, sandbox_cfg: dict) -> Path:
    return Path(project_root) / (
        sandbox_cfg.get("compose_file") or "docker-compose.yml"
    )


def _compose(project_root: Path, sandbox_cfg: dict, state: SandboxState,
             args: list, env: dict, capture: bool = False):
    argv = [
        *docker_command(), "compose",
        "-p", state.project_name,
        "-f", str(_compose_file(project_root, sandbox_cfg)),
        *args,
    ]
    result = subprocess.run(
        argv, cwd=str(project_root), env={**os.environ, **env},
        capture_output=capture, text=True,
    )
    if result.returncode != 0 and not capture:
        raise SandboxError(
            f"`{' '.join(argv)}` exited {result.returncode}. The stack for "
            f"branch {state.branch!r} is not in the requested state."
        )
    return result


def _busy_ips(project_root: Path) -> set:
    return {record.ip for record in list_states(project_root)}


def _await_health(project_root: Path, sandbox_cfg: dict, state: SandboxState,
                  env: dict) -> None:
    service = sandbox_cfg.get("health_service")
    if not service:
        return
    timeout = float(sandbox_cfg.get("health_timeout") or 60)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _compose(
            project_root, sandbox_cfg, state,
            ["ps", "--format", "json", service], env, capture=True,
        )
        if '"healthy"' in (result.stdout or ""):
            return
        time.sleep(1.0)
    raise SandboxError(
        f"Service '{service}' did not report healthy within {timeout:g}s for "
        f"branch {state.branch!r}. Refusing to dispatch an agent at a stack "
        f"that is not ready -- it would fail on its first connection and the "
        f"reason would only surface in the agent's own log."
    )


def resolve_env(branch: str, project_root, config: dict) -> dict:
    """The environment for an existing stack. No side effects, no docker."""
    sandbox_cfg = config.get("sandbox") or {}
    if not sandbox_cfg.get("enabled"):
        return {}
    state = read_state(project_root, branch)
    if state is None:
        return {}
    return render_env(sandbox_cfg, state.ip, state.project_name)


def ensure_up(branch: str, project_root, config: dict) -> dict:
    """Bring this branch's stack up, allocating an address if it has none."""
    sandbox_cfg = config.get("sandbox") or {}
    if not sandbox_cfg.get("enabled"):
        return {}

    project_root = Path(project_root)
    compose_file = _compose_file(project_root, sandbox_cfg)
    if not compose_file.is_file():
        raise SandboxError(
            f"sandbox.enabled is true but {compose_file} does not exist. "
            f"A project that asks for a sandbox must ship a compose file."
        )

    state = read_state(project_root, branch)
    if state is None:
        state = SandboxState(
            branch=branch,
            ip=ip_for(branch, busy=_busy_ips(project_root)),
            project_name=project_name_for(branch),
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        write_state(project_root, state)

    env = render_env(sandbox_cfg, state.ip, state.project_name)
    _compose(project_root, sandbox_cfg, state, ["up", "-d"], env)
    _await_health(project_root, sandbox_cfg, state, env)
    return env


def tear_down(branch: str, project_root, config: dict, mode: str) -> None:
    """Stop this branch's stack. `mode` decides how much is destroyed."""
    sandbox_cfg = config.get("sandbox") or {}
    if not sandbox_cfg.get("enabled") or mode == "none":
        return
    state = read_state(project_root, branch)
    if state is None:
        return

    env = render_env(sandbox_cfg, state.ip, state.project_name)
    args = ["down", "-v"] if mode == "volumes" else ["down"]
    _compose(Path(project_root), sandbox_cfg, state, args, env)

    # The one state rule: the record dies with the volumes, and only with them.
    if mode == "volumes":
        clear_state(project_root, branch)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_sandbox.py -v -p no:cacheprovider --basetemp=.pytest-tmp`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/sandbox.py tests/conftest.py tests/test_sandbox.py
git commit -m "feat(sandbox): compose lifecycle behind a cross-process docker seam"
```

---

### Task 6: Serialise address allocation

**Files:**
- Modify: `scripts/sandbox.py`
- Test: `tests/test_sandbox.py`

**Interfaces:**
- Consumes: `scripts.locks.acquire_slice_lock`, `scripts.locks.release_slice_lock_file`, `scripts.errors.LockError`.
- Produces: `_allocation_lock(project_root)` context manager, used inside `ensure_up`.

Probing is time-of-check/time-of-use: seconds pass between `bind` succeeding and compose actually publishing. Two dispatches in that window can pick the same octet. The per-slice lock cannot help — the conflict is *between* slices — so the same atomic primitive is reused under a fixed id.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sandbox.py`:

```python
def test_allocation_is_serialised(tmp_path, monkeypatch):
    """A second allocator must not run while the first holds the lock."""
    from scripts.locks import acquire_slice_lock

    held = acquire_slice_lock(sandbox._ALLOC_LOCK_ID, tmp_path)
    try:
        with pytest.raises(OrchestratorError, match="allocation lock"):
            with sandbox._allocation_lock(tmp_path, attempts=2, delay=0.01):
                pass
    finally:
        held.unlink(missing_ok=True)


def test_allocation_lock_is_released_on_the_error_path(tmp_path):
    from scripts.locks import acquire_slice_lock

    with pytest.raises(RuntimeError):
        with sandbox._allocation_lock(tmp_path):
            raise RuntimeError("boom")

    # If the lock leaked, this second acquisition would raise LockError.
    acquire_slice_lock(sandbox._ALLOC_LOCK_ID, tmp_path).unlink(missing_ok=True)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_sandbox.py -k allocation -v -p no:cacheprovider --basetemp=.pytest-tmp`
Expected: FAIL with `AttributeError: module 'scripts.sandbox' has no attribute '_ALLOC_LOCK_ID'`

- [ ] **Step 3: Implement**

Add `import contextlib` to `scripts/sandbox.py` and `from scripts.errors import LockError, SandboxError` (replacing the single-name import), plus `from scripts.locks import acquire_slice_lock, release_slice_lock_file`. Append:

```python
#: Fixed id for the cross-slice allocation lock. `_sanitize_id` accepts it.
_ALLOC_LOCK_ID = "sandbox-alloc"


@contextlib.contextmanager
def _allocation_lock(project_root, attempts: int = 20, delay: float = 0.1):
    """Serialise 'choose an address and record it' across concurrent dispatches.

    The critical section is milliseconds long, so contention is retried
    briefly rather than treated as fatal -- a spurious dispatch failure would
    be a worse outcome than a short wait.
    """
    lock_file = None
    for attempt in range(attempts):
        try:
            lock_file = acquire_slice_lock(_ALLOC_LOCK_ID, Path(project_root))
            break
        except LockError:
            if attempt == attempts - 1:
                raise SandboxError(
                    f"Could not take the sandbox allocation lock after "
                    f"{attempts} attempts. Another dispatch may be wedged; "
                    f"check .superpowers/locks/{_ALLOC_LOCK_ID}.lock"
                )
            time.sleep(delay)
    try:
        yield lock_file
    finally:
        release_slice_lock_file(lock_file)
```

Then wrap the allocation in `ensure_up` — replace the `state = read_state(...)` / `if state is None:` block with:

```python
    with _allocation_lock(project_root):
        state = read_state(project_root, branch)
        if state is None:
            state = SandboxState(
                branch=branch,
                ip=ip_for(branch, busy=_busy_ips(project_root)),
                project_name=project_name_for(branch),
                started_at=datetime.now(timezone.utc).isoformat(),
            )
            write_state(project_root, state)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_sandbox.py -v -p no:cacheprovider --basetemp=.pytest-tmp`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/sandbox.py tests/test_sandbox.py
git commit -m "fix(sandbox): serialise address allocation across concurrent dispatches"
```

---

### Task 7: Wire dispatch — the regression guard for the whole slice

**Files:**
- Modify: `scripts/git_ops.py`
- Modify: `scripts/orchestrator.py:230-245` (the fallible block) and `:269-278` (runner argv)
- Test: `tests/test_sandbox_dispatch.py` (create)

**Interfaces:**
- Consumes: `sandbox.ensure_up`, `sandbox.resolve_env`, `create_git_worktree`.
- Produces: `git_ops.current_branch(project_root) -> str`; `--sandbox-branch` on the runner argv; `SUPERPOWERS_SLICE_ID`, `SUPERPOWERS_SLICE_BRANCH`, `SUPERPOWERS_WORKTREE` in the dispatched environment.

This is the task the slice exists for. The first test below is red against today's code: the hook fires before `create_git_worktree` with `cwd=project_root`, so anything deriving a branch gets the *main* repository's, and two parallel slices collapse onto one compose project.

- [ ] **Step 1: Write the failing test**

Create `tests/test_sandbox_dispatch.py`:

```python
"""Dispatch with a sandbox configured. Never touches a real harness or docker."""

import argparse

from scripts.orchestrator import cmd_dispatch_agent

SANDBOX_AGENTS = """\
sandbox:
  enabled: true
  compose_file: docker-compose.yml
  env:
    dsn: "postgres://{ip}:5432/db"
agents:
  executor:
    model: "print('stub ok')"
    harness_adapter: 'stub_adapter.py'
    isolated_worktree: true
    allowed_statuses: ["SPEC_APPROVED"]
    in_progress_status: "EXECUTING"
    success_status: "EXECUTION_COMPLETE"
"""


def _args(spec, role="executor"):
    return argparse.Namespace(role=role, file=str(spec), model=None)


def _enable_sandbox(project_root):
    (project_root / ".superpowers" / "agents.yaml").write_text(
        SANDBOX_AGENTS, encoding="utf-8"
    )
    (project_root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")


def _up_calls(stub_docker):
    return [call for call in stub_docker.calls if call["argv"][-2:] == ["up", "-d"]]


def test_compose_project_comes_from_the_slice_branch(
    tmp_project, demo_spec, stub_docker
):
    """The defect this slice exists to fix.

    The main repository is on its own branch; the slice's worktree is on
    feat/<slice_id>. Anything deriving the branch from the project root -- as
    a shell hook necessarily does -- addresses the wrong stack.
    """
    _enable_sandbox(tmp_project)

    cmd_dispatch_agent(_args(demo_spec))

    calls = _up_calls(stub_docker)
    assert len(calls) == 1, f"expected one `up`, saw {len(calls)}"
    argv = calls[0]["argv"]
    assert argv[argv.index("-p") + 1] == "feat-slice-01-demo"


def test_parallel_slices_get_distinct_stacks(tmp_project, stub_docker):
    _enable_sandbox(tmp_project)
    specs = tmp_project / "docs" / "superpowers" / "specs"
    for slice_id in ("slice-alpha", "slice-beta"):
        (specs / f"{slice_id}.md").write_text(
            f'---\nslice_id: "{slice_id}"\nstatus: SPEC_APPROVED\n---\n\n# x\n',
            encoding="utf-8",
        )
        cmd_dispatch_agent(_args(specs / f"{slice_id}.md"))

    calls = _up_calls(stub_docker)
    projects = {call["argv"][call["argv"].index("-p") + 1] for call in calls}
    addresses = {call["loopback_ip"] for call in calls}
    assert projects == {"feat-slice-alpha", "feat-slice-beta"}
    assert len(addresses) == 2, f"both slices published on {addresses}"


def test_slice_context_reaches_the_hook(tmp_project, demo_spec, stub_docker):
    _enable_sandbox(tmp_project)
    marker = tmp_project / "hook-env.txt"
    (tmp_project / ".superpowers" / "hooks.yaml").write_text(
        "hooks:\n"
        "  on_slice_executor_start:\n"
        f'    command: "python -c \\"import os;open(r\'{marker}\',\'w\')'
        '.write(os.environ.get(\'SUPERPOWERS_SLICE_BRANCH\',\'\')+chr(10)+'
        'os.environ.get(\'LOOPBACK_IP\',\'\'))\\""\n',
        encoding="utf-8",
    )

    cmd_dispatch_agent(_args(demo_spec))

    branch, loopback = marker.read_text(encoding="utf-8").splitlines()
    assert branch == "feat/slice-01-demo"
    assert loopback.startswith("127.0.0.")


def test_no_sandbox_block_means_no_docker(tmp_project, demo_spec, stub_docker):
    """Inertness guard: docker must not leak into the orchestrator's contract."""
    cmd_dispatch_agent(argparse.Namespace(role="planner", file=str(demo_spec), model=None))
    assert stub_docker.calls == []


def test_non_isolated_agent_attaches_but_never_starts_a_stack(
    tmp_project, demo_spec, stub_docker
):
    """A planner runs on the human's branch. It must reach the human's stack
    and must not race one of its own into existence."""
    from scripts import sandbox
    from scripts.git_ops import current_branch

    _enable_sandbox(tmp_project)
    (tmp_project / ".superpowers" / "agents.yaml").write_text(
        SANDBOX_AGENTS.replace("executor:", "planner:").replace(
            "isolated_worktree: true", "isolated_worktree: false"
        ),
        encoding="utf-8",
    )
    marker = tmp_project / "planner-env.txt"
    (tmp_project / ".superpowers" / "hooks.yaml").write_text(
        "hooks:\n"
        "  on_slice_planner_start:\n"
        f'    command: "python -c \\"import os;open(r\'{marker}\',\'w\')'
        '.write(os.environ.get(\'LOOPBACK_IP\',\'NONE\'))\\""\n',
        encoding="utf-8",
    )

    # No stack yet: the planner must dispatch anyway, with no address.
    cmd_dispatch_agent(_args(demo_spec, role="planner"))
    assert marker.read_text(encoding="utf-8") == "NONE"
    assert stub_docker.calls == [], "a non-isolated agent started a stack"

    # With the human's stack up, the same dispatch now carries its address.
    branch = current_branch(tmp_project)
    config = {"sandbox": {"enabled": True, "compose_file": "docker-compose.yml",
                          "env": {}, "teardown": {}}}
    expected = sandbox.ensure_up(branch, tmp_project, config)["LOOPBACK_IP"]

    _set_status(demo_spec, "SPEC_APPROVED")
    cmd_dispatch_agent(_args(demo_spec, role="planner"))
    assert marker.read_text(encoding="utf-8") == expected


def _set_status(spec, status):
    text = spec.read_text(encoding="utf-8")
    import re as _re
    spec.write_text(
        _re.sub(r"status: \w+", f"status: {status}", text, count=1), encoding="utf-8"
    )
```

The second dispatch needs the spec returned to a dispatchable status because the
first one advanced it — without `_set_status` the state gate rejects it and the
test would pass on a stale marker file.

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_sandbox_dispatch.py -v -p no:cacheprovider --basetemp=.pytest-tmp`
Expected: FAIL — no `up` calls recorded at all, because nothing calls `ensure_up` yet.

- [ ] **Step 3: Add branch resolution to git_ops**

Append to `scripts/git_ops.py`:

```python
def current_branch(project_root: Path) -> str:
    """The checked-out branch of `project_root`, or `detached-<sha>`.

    Only used for agents that run in the project root itself. A slice's own
    branch is always derived from its slice_id, never from here.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(project_root), capture_output=True, text=True,
    )
    name = (result.stdout or "").strip()
    if result.returncode == 0 and name and name != "HEAD":
        return name
    sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(project_root), capture_output=True, text=True,
    ).stdout.strip()
    return f"detached-{sha}" if sha else "unknown"
```

- [ ] **Step 4: Rewrite the fallible block in `cmd_dispatch_agent`**

Add to the imports of `scripts/orchestrator.py`:

```python
from scripts import sandbox
from scripts.git_ops import create_git_worktree, current_branch, merge_and_cleanup_worktree
```

Replace the body of the `try:` block (currently worktree → adapter) with:

```python
    try:
        if agent_config.get("isolated_worktree", False):
            cwd = create_git_worktree(slice_id, project_root)
            sandbox_branch = f"feat/{slice_id}"
            sandbox_env = sandbox.ensure_up(sandbox_branch, project_root, config)
        else:
            cwd = project_root
            sandbox_branch = current_branch(project_root)
            sandbox_env = sandbox.resolve_env(sandbox_branch, project_root, config)

        env = dict(os.environ)
        env.update(sandbox_env)
        env.update({
            "SUPERPOWERS_SLICE_ID": slice_id,
            "SUPERPOWERS_SLICE_BRANCH": sandbox_branch,
            "SUPERPOWERS_WORKTREE": str(cwd),
        })

        # The start hook runs after the worktree and the sandbox exist, so a
        # project hook can act on both. This is a deliberate change from
        # 2.0.0, where it ran first and could observe neither.
        env = run_infrastructure_hook(
            f"on_slice_{role}_start", project_root=project_root,
            current_env=env, known_events=known_events,
        )
        adapter = get_harness_adapter(agent_config, project_root)
        agent_argv = adapter.build_command(agent_config, task_prompt)
    except OrchestratorError as exc:
        release_slice_lock_file(lock_file)
        print(f"Error: {exc}")
        print(f"Slice '{slice_id}' left untouched at status '{current_status}'.")
        sys.exit(1)
```

Delete the now-duplicated `env = run_infrastructure_hook(...)` and worktree lines that preceded it.

- [ ] **Step 5: Pass the branch to the supervisor**

In the `runner_argv` list, add before the `"--"` separator:

```python
        "--sandbox-branch", sandbox_branch,
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_sandbox_dispatch.py -v -p no:cacheprovider --basetemp=.pytest-tmp`
Expected: PASS, 5 tests. `test_compose_project_comes_from_the_slice_branch` was red in Step 2.

- [ ] **Step 7: Run the full suite for ordering regressions**

Run: `python -m pytest -q -p no:cacheprovider --basetemp=.pytest-tmp`
Expected: PASS. If `tests/test_hook_events.py` asserts the hook fires before worktree creation, update that assertion — the reordering is intended and documented.

- [ ] **Step 8: Commit**

```bash
git add scripts/orchestrator.py scripts/git_ops.py tests/test_sandbox_dispatch.py
git commit -m "fix(dispatch): address the sandbox by the slice branch, not the repo branch"
```

---

### Task 8: Teardown on failure, inside the supervisor

**Files:**
- Modify: `scripts/runner.py:32-52` (`run_supervised`), `:116-177` (`_record_outcome`), `:180-195` (`main`)
- Test: `tests/test_sandbox_dispatch.py`

**Interfaces:**
- Consumes: `sandbox.tear_down`, `config["sandbox"]["teardown"]["on_failed"]`.
- Produces: `run_supervised(..., sandbox_branch: str = "")`, `--sandbox-branch` argument on the runner CLI.

A failed slice keeps its volumes so the failure stays diagnosable, and stops its containers so the address and the memory come back. Teardown runs *after* the failure hook: that hook is exactly where a project would capture a dump, and it must not be handed an empty machine.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sandbox_dispatch.py`:

```python
import time

FAILING_EXECUTOR = SANDBOX_AGENTS.replace(
    "model: \"print('stub ok')\"", "model: \"import sys; sys.exit(3)\""
)


def _wait_for(predicate, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def test_failed_slice_stops_containers_but_keeps_volumes(
    tmp_project, demo_spec, stub_docker
):
    from scripts import sandbox

    _enable_sandbox(tmp_project)
    (tmp_project / ".superpowers" / "agents.yaml").write_text(
        FAILING_EXECUTOR, encoding="utf-8"
    )

    cmd_dispatch_agent(_args(demo_spec))

    assert _wait_for(
        lambda: any(c["argv"][-1] == "down" for c in stub_docker.calls)
    ), f"no teardown was recorded; calls were {stub_docker.calls}"

    down = [c for c in stub_docker.calls if "down" in c["argv"]][-1]
    assert "-v" not in down["argv"], "a failed slice must keep its volumes"
    assert sandbox.read_state(tmp_project, "feat/slice-01-demo") is not None


def test_failure_hook_observes_the_stack_before_teardown(
    tmp_project, demo_spec, stub_docker
):
    journal = tmp_project / "journal.txt"
    _enable_sandbox(tmp_project)
    (tmp_project / ".superpowers" / "agents.yaml").write_text(
        FAILING_EXECUTOR, encoding="utf-8"
    )
    (tmp_project / ".superpowers" / "hooks.yaml").write_text(
        "hooks:\n"
        "  on_executor_failed:\n"
        f'    command: "python -c \\"open(r\'{journal}\',\'a\')'
        '.write(\'hook\\\\n\')\\""\n',
        encoding="utf-8",
    )

    cmd_dispatch_agent(_args(demo_spec))

    assert _wait_for(lambda: journal.exists() and any(
        "down" in c["argv"] for c in stub_docker.calls
    ))
    hook_time = journal.stat().st_mtime
    docker_time = stub_docker.log.stat().st_mtime
    assert hook_time <= docker_time, "teardown ran before the failure hook"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_sandbox_dispatch.py -k failed -v -p no:cacheprovider --basetemp=.pytest-tmp`
Expected: FAIL — no `down` is ever recorded.

- [ ] **Step 3: Implement**

In `scripts/runner.py`, add `from scripts import sandbox` to the imports. Change `run_supervised` to accept and forward the branch:

```python
def run_supervised(
    role: str,
    target_file: Path,
    project_root: Path,
    lock_file: Path,
    log_file: Path,
    argv: list,
    cwd: Path,
    sandbox_branch: str = "",
) -> int:
    """Run one agent to completion and record the outcome. Returns its exit code."""
    target_file = Path(target_file)
    project_root = Path(project_root)
    log_file = Path(log_file)

    claim_slice_lock(lock_file, os.getpid(), role=role)
    try:
        exit_code = _run_child(argv, cwd, log_file)
        _record_outcome(
            role, target_file, project_root, exit_code, log_file, sandbox_branch
        )
        return exit_code
    finally:
        release_slice_lock_file(lock_file)
```

Change `_record_outcome`'s signature to `(role, target_file, project_root, exit_code, log_file, sandbox_branch="")` and append, after the existing hook `try/except`:

```python
    if exit_code != 0 and sandbox_branch:
        mode = (
            ((config.get("sandbox") or {}).get("teardown") or {})
            .get("on_failed", "containers")
        )
        try:
            sandbox.tear_down(sandbox_branch, project_root, config, mode)
        except OrchestratorError as exc:
            # Same rule as the hook above: the slice's outcome is recorded and
            # must not be overturned by a container that would not sweep.
            _log_and_print(log_file, f"[runner] sandbox teardown failed: {exc}")
```

In `main`, add the argument and forward it:

```python
    parser.add_argument("--sandbox-branch", default="")
```

and add `sandbox_branch=args.sandbox_branch` to the `run_supervised(...)` call.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_sandbox_dispatch.py -v -p no:cacheprovider --basetemp=.pytest-tmp`
Expected: PASS, 7 tests.

- [ ] **Step 5: Commit**

```bash
git add scripts/runner.py tests/test_sandbox_dispatch.py
git commit -m "feat(sandbox): stop a failed slice's containers, keep its volumes"
```

---

### Task 9: Teardown on VERIFIED_CLOSED

**Files:**
- Modify: `scripts/orchestrator.py:138-151` (end of `cmd_set_status`)
- Test: `tests/test_set_status.py`

**Interfaces:**
- Consumes: `sandbox.tear_down`, `config["sandbox"]["teardown"]["on_verified_closed"]`.
- Produces: nothing new.

The trigger is the transition being applied, not the merge that usually precedes it. A slice may reach `VERIFIED_CLOSED` from `MERGE_CONFLICT` after a human resolves it, and that slice's stack must be swept too.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_set_status.py`:

```python
def test_verified_closed_destroys_volumes_and_state(tmp_project, demo_spec, stub_docker):
    from scripts import sandbox

    (tmp_project / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    agents = tmp_project / ".superpowers" / "agents.yaml"
    agents.write_text(
        "sandbox:\n  enabled: true\n" + agents.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    # Same merge setup as test_verified_closed_merges_then_marks above. Without
    # a real branch carrying a commit, cmd_set_status lands in MERGE_CONFLICT
    # and exits before any teardown -- the test would go green for the wrong
    # reason if the assertion were merely "no down -v happened".
    _set_raw_status(demo_spec, "EXECUTION_COMPLETE")
    _git(tmp_project, "add", "-A")
    _git(tmp_project, "commit", "-qm", "wip")
    worktree = create_git_worktree("slice-01-demo", tmp_project)
    (worktree / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "feat: work")

    # Bring a stack up for the slice branch, exactly as dispatch would have.
    config = {"sandbox": {"enabled": True, "compose_file": "docker-compose.yml",
                          "env": {}, "teardown": {}}}
    sandbox.ensure_up("feat/slice-01-demo", tmp_project, config)

    cmd_set_status(argparse.Namespace(file=str(demo_spec), status="VERIFIED_CLOSED"))

    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "VERIFIED_CLOSED"
    assert stub_docker.argv_of(-1)[-2:] == ["down", "-v"]
    assert sandbox.read_state(tmp_project, "feat/slice-01-demo") is None
```

The status assertion is not decoration: it is what distinguishes "teardown ran"
from "the command bailed out early and nothing ran".

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_set_status.py -k verified_closed_destroys -v -p no:cacheprovider --basetemp=.pytest-tmp`
Expected: FAIL — the last recorded call is the `up`, not a `down -v`.

- [ ] **Step 3: Implement**

In `scripts/orchestrator.py`, append to `cmd_set_status`, after the `on_slice_verified_closed` hook block:

```python
    mode = (
        ((config.get("sandbox") or {}).get("teardown") or {})
        .get("on_verified_closed", "volumes")
    )
    try:
        sandbox.tear_down(f"feat/{slice_id}", project_root, config, mode)
    except OrchestratorError as exc:
        print(f"Warning: sandbox teardown failed: {exc}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_set_status.py -v -p no:cacheprovider --basetemp=.pytest-tmp`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/orchestrator.py tests/test_set_status.py
git commit -m "feat(sandbox): destroy a closed slice's volumes on VERIFIED_CLOSED"
```

---

### Task 10: The human-facing CLI

**Files:**
- Modify: `scripts/orchestrator.py` (new `cmd_sandbox`, new subparser)
- Modify: `scripts/sandbox.py` (add `status_rows`)
- Test: `tests/test_sandbox_cli.py` (create)

**Interfaces:**
- Consumes: everything in `scripts/sandbox.py`.
- Produces: `orchestrator.py sandbox {up,restart,status,env,exec,teardown}`; `sandbox.status_rows(project_root) -> list[tuple[str, str, str]]` yielding `(branch, ip, state)`.

`env` must emit something a Windows shell can consume: the POSIX `eval $(...)` idiom documented today does not work under PowerShell.

- [ ] **Step 1: Write the failing test**

Create `tests/test_sandbox_cli.py`:

```python
import argparse
import json

import pytest

from scripts.orchestrator import cmd_sandbox


def _args(action, **kwargs):
    base = dict(action=action, dir="", branch="", shell="posix", yes=False, cmd=[])
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_env_emits_posix_exports(tmp_project, stub_docker, capsys):
    from scripts import sandbox

    (tmp_project / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    config = {"sandbox": {"enabled": True, "compose_file": "docker-compose.yml",
                          "env": {"dsn": "postgres://{ip}:5432/db"}, "teardown": {}}}
    sandbox.ensure_up("feat/alpha", tmp_project, config)

    cmd_sandbox(_args("env", dir=str(tmp_project), branch="feat/alpha"))

    out = capsys.readouterr().out
    assert "export LOOPBACK_IP=127.0.0." in out
    assert "export dsn=postgres://127.0.0." in out


def test_env_emits_powershell_assignments(tmp_project, stub_docker, capsys):
    from scripts import sandbox

    (tmp_project / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    config = {"sandbox": {"enabled": True, "compose_file": "docker-compose.yml",
                          "env": {}, "teardown": {}}}
    sandbox.ensure_up("feat/alpha", tmp_project, config)

    cmd_sandbox(_args("env", dir=str(tmp_project), branch="feat/alpha", shell="powershell"))

    assert '$env:LOOPBACK_IP = "127.0.0.' in capsys.readouterr().out


def test_env_emits_json(tmp_project, stub_docker, capsys):
    from scripts import sandbox

    (tmp_project / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    config = {"sandbox": {"enabled": True, "compose_file": "docker-compose.yml",
                          "env": {}, "teardown": {}}}
    sandbox.ensure_up("feat/alpha", tmp_project, config)

    cmd_sandbox(_args("env", dir=str(tmp_project), branch="feat/alpha", shell="json"))

    assert json.loads(capsys.readouterr().out)["COMPOSE_PROJECT_NAME"] == "feat-alpha"


def test_env_without_state_exits_nonzero(tmp_project, stub_docker):
    with pytest.raises(SystemExit) as excinfo:
        cmd_sandbox(_args("env", dir=str(tmp_project), branch="feat/none"))
    assert excinfo.value.code != 0


def test_teardown_refuses_volume_destruction_without_yes(tmp_project, stub_docker):
    from scripts import sandbox

    (tmp_project / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    config = {"sandbox": {"enabled": True, "compose_file": "docker-compose.yml",
                          "env": {}, "teardown": {}}}
    sandbox.ensure_up("feat/alpha", tmp_project, config)
    before = len(stub_docker.calls)

    with pytest.raises(SystemExit):
        cmd_sandbox(_args("teardown", dir=str(tmp_project), branch="feat/alpha"))

    assert len(stub_docker.calls) == before, "destroyed volumes without --yes"
    assert sandbox.read_state(tmp_project, "feat/alpha") is not None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_sandbox_cli.py -v -p no:cacheprovider --basetemp=.pytest-tmp`
Expected: FAIL with `ImportError: cannot import name 'cmd_sandbox'`

- [ ] **Step 3: Add `status_rows` to the module**

Append to `scripts/sandbox.py`:

```python
def status_rows(project_root) -> list:
    """(branch, ip, state) for every tracked stack. `state` is running|stopped."""
    rows = []
    for record in list_states(project_root):
        probe = _probe(record.ip, port=0)
        rows.append((record.branch, record.ip,
                     "stopped" if probe == "free" else "running"))
    return rows
```

- [ ] **Step 4: Implement the command**

Add to `scripts/orchestrator.py`:

```python
def _quote_posix(value: str) -> str:
    if re.match(r"^[A-Za-z0-9_\-./:=]+$", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def cmd_sandbox(args):
    """Human-facing sandbox lifecycle. The orchestrator uses the module directly."""
    project_root = Path(args.dir).resolve() if args.dir else Path.cwd()

    try:
        config = load_agent_config(project_root)
        validate_config(config)
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    branch = args.branch or current_branch(project_root)

    try:
        if args.action == "status":
            rows = sandbox.status_rows(project_root)
            if not rows:
                print("No sandbox stacks are tracked.")
            for name, ip, state in rows:
                print(f"{name:<48} {ip:<12} {state}")
            return

        if args.action in ("up", "restart"):
            if args.action == "restart":
                sandbox.tear_down(branch, project_root, config, "containers")
            env = sandbox.ensure_up(branch, project_root, config)
            print(f"Stack for {branch} is up on {env['LOOPBACK_IP']}.")
            return

        if args.action == "teardown":
            mode = "volumes" if args.yes else "containers"
            if not args.yes:
                print(
                    f"Refusing to destroy volumes for {branch} without --yes. "
                    f"Stopping containers only would be `restart`; re-run with "
                    f"--yes to destroy data."
                )
                sys.exit(2)
            sandbox.tear_down(branch, project_root, config, mode)
            print(f"Stack for {branch} torn down ({mode}).")
            return

        env = sandbox.resolve_env(branch, project_root, config)
        if not env:
            print(f"No sandbox state for branch {branch}; run `sandbox up` first.")
            sys.exit(1)

        if args.action == "env":
            if args.shell == "json":
                print(json.dumps(env, indent=2))
            elif args.shell == "powershell":
                for key, value in env.items():
                    print(f'$env:{key} = "{value}"')
            else:
                for key, value in env.items():
                    print(f"export {key}={_quote_posix(value)}")
            return

        if args.action == "exec":
            command = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
            if not command:
                print("Error: `sandbox exec` needs a command after `--`.")
                sys.exit(1)
            sys.exit(subprocess.run(
                command, cwd=str(project_root), env={**os.environ, **env}
            ).returncode)
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(1)
```

Add `import json`, `import re` and `from scripts import sandbox` to the imports if not present. Register the subparser in `main`:

```python
    p_sandbox = subparsers.add_parser("sandbox", help="Per-slice infrastructure sandbox")
    p_sandbox.add_argument(
        "action", choices=["up", "restart", "status", "env", "exec", "teardown"]
    )
    p_sandbox.add_argument("--dir", default="", help="Project root (default: cwd)")
    p_sandbox.add_argument("--branch", default="", help="Branch (default: current)")
    p_sandbox.add_argument(
        "--shell", default="posix", choices=["posix", "powershell", "json"],
        help="Output format for `env`",
    )
    p_sandbox.add_argument("--yes", action="store_true", help="Confirm volume destruction")
    p_sandbox.add_argument("cmd", nargs=argparse.REMAINDER, help="Command for `exec`")
```

and the dispatch arm:

```python
    elif args.command == "sandbox":
        cmd_sandbox(args)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_sandbox_cli.py -v -p no:cacheprovider --basetemp=.pytest-tmp`
Expected: PASS, 5 tests.

- [ ] **Step 6: Commit**

```bash
git add scripts/orchestrator.py scripts/sandbox.py tests/test_sandbox_cli.py
git commit -m "feat(sandbox): sandbox subcommand with posix, powershell and json env output"
```

---

### Task 11: Documentation guards

**Files:**
- Modify: `tests/test_docs_consistency.py`
- Modify: `docs/configuration.md`, `README.md`

**Interfaces:**
- Consumes: `scripts.config.KNOWN_SANDBOX_KEYS`, `TEARDOWN_MODES`.
- Produces: two new consistency tests.

Prose cannot be parsed for capability claims, so these are regression guards for the two ways this feature can rot: a config key that exists but is written down nowhere, and a documented example that no longer loads.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_docs_consistency.py`:

```python
def test_every_sandbox_config_key_is_documented():
    from scripts.config import KNOWN_SANDBOX_KEYS

    for key in sorted(KNOWN_SANDBOX_KEYS):
        assert key in CONFIGURATION, (
            f"sandbox config key '{key}' is accepted by the loader but appears "
            f"nowhere in docs/configuration.md"
        )


def test_every_teardown_mode_is_documented():
    from scripts.config import TEARDOWN_MODES

    for mode in sorted(TEARDOWN_MODES):
        assert mode in CONFIGURATION, (
            f"teardown mode '{mode}' is valid but undocumented"
        )


def test_sandbox_is_documented_as_opt_in():
    """The plugin has no docker dependency unless a project asks for one."""
    assert "opt-in" in CONFIGURATION.lower() or "opt in" in CONFIGURATION.lower()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_docs_consistency.py -v -p no:cacheprovider --basetemp=.pytest-tmp`
Expected: FAIL — `sandbox config key 'compose_file' ... appears nowhere`.

- [ ] **Step 3: Write the documentation**

Add a "Sandbox (per-slice infrastructure)" section to `docs/configuration.md` containing: a statement that it is **opt-in** and that with no `sandbox` block the orchestrator makes no docker call at all; the full annotated YAML example from spec §4.2 with every key in `KNOWN_SANDBOX_KEYS`; the two template tokens; the three teardown modes with what each destroys; the rule that `LOOPBACK_IP` and `COMPOSE_PROJECT_NAME` are injected and not declarable; the `isolated_worktree` linkage table; and the note that the compose file must publish ports through `${LOOPBACK_IP:?}` so it fails closed when the variable is absent.

Add a short "Parallel slices need isolated infrastructure" subsection to `README.md` pointing at that section and showing the `sandbox status` output shape.

- [ ] **Step 4: Verify the documented example actually loads**

Append to `tests/test_docs_consistency.py`:

```python
def test_documented_sandbox_example_survives_the_real_validator():
    """A doc example that does not load is worse than no example."""
    import re as _re

    from ruamel.yaml import YAML

    from scripts.config import DEFAULT_CONFIG, deep_merge, validate_config
    from scripts.utils import _to_plain_dict

    blocks = _re.findall(r"```yaml\n(.*?)```", CONFIGURATION, _re.DOTALL)
    sandbox_blocks = [b for b in blocks if b.lstrip().startswith("sandbox:")]
    assert sandbox_blocks, "docs/configuration.md has no sandbox YAML example"

    for block in sandbox_blocks:
        parsed = _to_plain_dict(YAML(typ="rt").load(block))
        validate_config(deep_merge(DEFAULT_CONFIG, parsed))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_docs_consistency.py -v -p no:cacheprovider --basetemp=.pytest-tmp`
Expected: PASS, 18 tests.

- [ ] **Step 6: Commit**

```bash
git add docs/configuration.md README.md tests/test_docs_consistency.py
git commit -m "docs: document the sandbox contract and guard it with consistency tests"
```

---

### Task 12: Architecture notes and the 2.1.0 release

**Files:**
- Modify: `docs/architecture.md`
- Modify: `.claude-plugin/plugin.json`, `package.json`
- Modify: `tests/test_docs_consistency.py:58,90` (the pinned `"2.0.0"` constants)
- Modify: `README.md` (mermaid diagram)

**Interfaces:**
- Consumes: nothing.
- Produces: version `2.1.0` in both manifests.

The hook reordering is a behavioural change to a published contract. It ships written down, not discovered.

- [ ] **Step 1: Write the failing test**

Edit `tests/test_docs_consistency.py`: change both `"2.0.0"` assertions to `"2.1.0"`, and append:

```python
def test_hook_ordering_change_is_recorded():
    """A published contract that changed must say so somewhere a reader looks."""
    documented = ARCHITECTURE + CONFIGURATION + README
    assert "2.1.0" in documented
    assert "after" in ARCHITECTURE and "worktree" in ARCHITECTURE
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_docs_consistency.py -v -p no:cacheprovider --basetemp=.pytest-tmp`
Expected: FAIL with `assert '2.0.0' == '2.1.0'`

- [ ] **Step 3: Bump both manifests**

Set `"version": "2.1.0"` in `.claude-plugin/plugin.json` and in `package.json`.

- [ ] **Step 4: Update the architecture document**

In `docs/architecture.md`, extend the "Dispatch ordering" subsection to the new sequence (worktree → sandbox → hook → adapter → status → spawn), stating explicitly that `on_slice_{role}_start` moved *after* worktree creation in 2.1.0, why (so a hook can act on the worktree and receive `LOOPBACK_IP`), and that the worktree is intentionally left in place when a later step in the block fails.

Add a "Sandbox lifecycle" subsection: the two teardown sites, the mode each uses, the rule that teardown always follows the corresponding hook, and the state invariant — the record is deleted if and only if the volumes are destroyed.

- [ ] **Step 5: Update the README diagram**

In the mermaid diagram, add the sandbox between worktree creation and the harness spawn, and show the two teardown edges (`FAILED → down`, `VERIFIED_CLOSED → down -v`). Keep the existing supervisor nodes.

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q -p no:cacheprovider --basetemp=.pytest-tmp`
Expected: PASS, all tests.

- [ ] **Step 7: Commit**

```bash
git add .claude-plugin/plugin.json package.json docs/architecture.md README.md tests/test_docs_consistency.py
git commit -m "release: 2.1.0 — native sandbox and the dispatch reordering it required"
```

---

### Task 13: Migrate the consuming project

**Files (in `downstream-project`, a separate repository — branch first, do not land):**
- Delete: `.claude/skills/sandbox-loopback/` (SKILL.md, `references/*.md`, `scripts/sandbox_loopback.py`)
- Modify: `.superpowers/agents.yaml`, `.superpowers/hooks.yaml`, `.gitignore`, `CLAUDE.md`

**Interfaces:**
- Consumes: the `sandbox` config contract from Task 4 and the CLI from Task 10.
- Produces: nothing the plugin depends on.

This is the acceptance test. A contract validated only against its own test suite has not met a real consumer.

- [ ] **Step 1: Create a branch**

```bash
git checkout -b feat/native-sandbox-migration
```

- [ ] **Step 2: Translate the hook into configuration**

Add to `.superpowers/agents.yaml` a `sandbox` block whose `env` reproduces exactly what the deleted script's `_compose_env` returned — every service URL and DSN — with the address written as `{ip}`. Set `health_service` to the service the compose file health-checks. Take `teardown.on_verified_closed: volumes` and `teardown.on_failed: containers`.

Then delete both sandbox hooks from `.superpowers/hooks.yaml`. If no hooks remain, delete the file.

- [ ] **Step 3: Remove the superseded skill**

```bash
git rm -r .claude/skills/sandbox-loopback
```

Add `.superpowers/sandbox/` to `.gitignore` beside the existing runtime-artifact entries.

- [ ] **Step 4: Rewrite the project invariant**

In `CLAUDE.md`, replace the sandbox-loopback invariant: the mechanism is now provided by the plugin, the compose file still fails closed on `${LOOPBACK_IP:?}`, dispatched agents receive the address automatically, and a human uses `orchestrator.py sandbox up` / `sandbox exec -- ...` before running host-side applications.

- [ ] **Step 5: Verify against the real compose stack (free tier)**

Run each and confirm the stated outcome:

```bash
python <plugin>/scripts/orchestrator.py sandbox up --dir .
```
Expected: the stack starts; the reported address is not `127.0.0.1`; `docker ps` shows the containers under the branch's compose project.

```bash
python <plugin>/scripts/orchestrator.py sandbox status --dir .
```
Expected: one row, the current branch, its address, `running`.

```bash
python <plugin>/scripts/orchestrator.py sandbox env --dir . --shell powershell
```
Expected: `$env:LOOPBACK_IP = "127.0.0.x"` plus every declared variable.

Then check out a second branch in a second worktree, run `sandbox up` there, and confirm `sandbox status` lists two rows with **two different addresses** and that both databases are independently reachable. This is the scenario that silently collapsed into one stack before this slice.

Finally `sandbox teardown --yes` in both.

- [ ] **Step 6: Commit the migration**

```bash
git add -A
git commit -m "refactor(sandbox): adopt the plugin's native sandbox, drop the local skill"
```

- [ ] **Step 7: Report, and stop**

Report the free-tier results. Do **not** run the paid dispatch and do **not** land either repository: both require an explicit go-ahead from the owner.

---

## Verification before hand-off

Run the whole suite one final time from a clean tree:

```bash
python -m pytest -q -p no:cacheprovider --basetemp=.pytest-tmp
```

Expected: all tests pass. Then confirm the inertness guard specifically, since it is the promise that this slice did not widen the plugin's identity:

```bash
python -m pytest tests/test_sandbox_dispatch.py::test_no_sandbox_block_means_no_docker -v -p no:cacheprovider --basetemp=.pytest-tmp
```

Expected: PASS.
