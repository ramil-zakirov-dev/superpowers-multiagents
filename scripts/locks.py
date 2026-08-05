"""File-based slice locking.

Acquisition and ownership are separate. The dispatcher creates the lock
atomically and exits; the supervisor it spawns claims the lock with its own
PID and holds it for the run. A `starting` lock is honoured for a bounded
grace window so the gap between those two events is not a hole.

Every write to the lock file is staged through a sibling temp file and made
visible with a single atomic filesystem call (`os.link` for a brand-new
lock, `os.replace` for an update to an existing one). A reader can therefore
only ever observe the lock file fully absent, fully at its previous content,
or fully at its new content — never a truncated or empty in-progress write.
Without that, a lock being claimed by its legitimate, live owner can be
misread by a racing `acquire_slice_lock` as corrupt (an empty read parses to
`{}`, which `_lock_is_held` reports as not-held) and reclaimed out from
under it — on Windows this crashes the reclaimer with a `PermissionError`
because the owner still has the destination path open; on POSIX it silently
double-acquires the slice.
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

from scripts.errors import LockError
from scripts.paths import lock_path
from scripts.utils import _is_process_alive, _sanitize_id

#: How long a lock may sit in `starting` before it is considered abandoned.
LOCK_START_GRACE_SECONDS = 60

_MAX_RECLAIM_ATTEMPTS = 3

#: Bounded retry for a transient Windows sharing violation: another thread
#: or process can briefly hold the lock file open (e.g. mid-read, or the
#: brief "pending delete" window Windows leaves between an unlink and the
#: path truly becoming free) at the exact instant a link/replace/unlink
#: targets it. POSIX never raises this; on Windows it is retried with
#: exponential backoff before being treated as a real error. Worst case for
#: one call is ~5.1s (9 sleeps of 0.01 * 2^k, k=0..8); acquire_slice_lock can
#: invoke up to _MAX_RECLAIM_ATTEMPTS such calls, so a lock that is
#: genuinely, persistently wedged at the filesystem level (not just
#: contended) can take tens of seconds to fail rather than failing fast.
#: That trade-off is deliberate: a false "corrupt, reclaim it" verdict during
#: a transient scan is worse than a slow, honest failure.
_TRANSIENT_RETRY_ATTEMPTS = 10
_TRANSIENT_RETRY_BASE_DELAY_SECONDS = 0.01


def _retry_on_sharing_violation(func, *args, **kwargs):
    for attempt in range(_TRANSIENT_RETRY_ATTEMPTS):
        try:
            return func(*args, **kwargs)
        except PermissionError:
            if attempt == _TRANSIENT_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(_TRANSIENT_RETRY_BASE_DELAY_SECONDS * (2 ** attempt))


def _lock_is_held(data: dict, *, is_alive=_is_process_alive) -> bool:
    state = data.get("state")
    if state == "running":
        pid = data.get("pid")
        if not pid:
            return False
        try:
            return is_alive(int(pid))
        except (TypeError, ValueError):
            return False
    if state == "starting":
        try:
            started_at = float(data.get("started_at") or 0)
        except (TypeError, ValueError):
            return False
        return (time.time() - started_at) < LOCK_START_GRACE_SECONDS
    return False


def _stage_payload(parent: Path, payload: dict) -> str:
    """Write `payload` to a fully-formed sibling temp file. Returns its path."""
    fd, tmp_name = tempfile.mkstemp(dir=parent, prefix=".lock-tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    return tmp_name


def _write_new(lock_file: Path, payload: dict) -> None:
    """Make `payload` visible at `lock_file`, atomically, only if absent.

    Raises FileExistsError if the destination already exists — the same
    exclusivity `os.O_CREAT | os.O_EXCL` would give, but the content is
    fully written before it ever becomes visible under that name.
    """
    tmp_name = _stage_payload(lock_file.parent, payload)
    try:
        _retry_on_sharing_violation(os.link, tmp_name, lock_file)
    finally:
        # Best-effort cleanup only: if this itself hits a sharing violation
        # that exhausts its own retries, that failure must not shadow
        # whatever `os.link` raised (typically the meaningful, expected
        # FileExistsError of a contended lock) — an orphaned temp file is
        # harmless debris, but losing the real exception is not.
        try:
            _retry_on_sharing_violation(os.unlink, tmp_name)
        except OSError:
            pass


def _write_replace(lock_file: Path, payload: dict) -> None:
    """Make `payload` visible at `lock_file`, atomically, overwriting any prior content."""
    tmp_name = _stage_payload(lock_file.parent, payload)
    _retry_on_sharing_violation(os.replace, tmp_name, lock_file)


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
            _write_new(lock_file, payload)
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
            try:
                _retry_on_sharing_violation(lock_file.unlink, missing_ok=True)
            except OSError:
                # Lost the reclaim race to another contender; the next
                # attempt's atomic create will fail again if they won, or
                # succeed if the slot is clear.
                pass
            continue
        else:
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
    _write_replace(lock_file, data)


def release_slice_lock_file(lock_file: Path) -> None:
    """Remove a lock by path. Safe to call more than once."""
    _retry_on_sharing_violation(Path(lock_file).unlink, missing_ok=True)


def release_slice_lock(slice_id: str, project_root: Path) -> None:
    """Remove a lock by slice id."""
    release_slice_lock_file(lock_path(project_root, slice_id))
