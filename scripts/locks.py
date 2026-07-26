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
