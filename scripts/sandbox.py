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

import contextlib
import dataclasses
import errno
import hashlib
import json
import os
import re
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from scripts.errors import LockError, SandboxError
from scripts.locks import acquire_slice_lock, release_slice_lock_file
from scripts.paths import sandbox_dir, sandbox_state_path

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
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise SandboxError(
                f"`docker compose ps` for service {service!r} (branch "
                f"{state.branch!r}) exited {result.returncode} while polling "
                f"for health: {detail or '(no output)'}"
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


def status_rows(project_root) -> list:
    """(branch, ip, state) for every tracked stack. `state` is running|stopped."""
    rows = []
    for record in list_states(project_root):
        probe = _probe(record.ip, port=0)
        rows.append((record.branch, record.ip,
                     "stopped" if probe == "free" else "running"))
    return rows
