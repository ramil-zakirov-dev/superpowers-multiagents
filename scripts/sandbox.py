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

import dataclasses
import errno
import hashlib
import json
import re
import socket
from pathlib import Path
from typing import Iterable

from scripts.errors import SandboxError
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
