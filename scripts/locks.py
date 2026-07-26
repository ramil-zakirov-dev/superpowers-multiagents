"""File-based slice locking to prevent concurrent dispatch."""

import os
import sys
import json
from pathlib import Path

from scripts.utils import _sanitize_id, _is_process_alive


def acquire_slice_lock(slice_id: str, project_root: Path) -> Path:
    """Acquires an exclusive lock for a slice.

    Creates .superpowers/locks/<slice_id>.lock with PID info.
    Returns the lock file path. Exits if already locked by a live process.
    """
    _sanitize_id(slice_id, "slice_id")
    locks_dir = project_root / ".superpowers" / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_file = locks_dir / f"{slice_id}.lock"

    if lock_file.exists():
        try:
            lock_data = json.loads(lock_file.read_text(encoding="utf-8"))
            existing_pid = lock_data.get("pid")
            if existing_pid and _is_process_alive(existing_pid):
                print(f"Error: Slice '{slice_id}' is already locked by PID {existing_pid} "
                      f"(command: {lock_data.get('command', 'unknown')}).")
                sys.exit(1)
            else:
                lock_file.unlink()
        except (json.JSONDecodeError, KeyError):
            lock_file.unlink()

    lock_data = {
        "pid": os.getpid(),
        "slice_id": slice_id,
        "command": " ".join(sys.argv),
    }
    lock_file.write_text(json.dumps(lock_data, indent=2), encoding="utf-8")
    return lock_file


def release_slice_lock(slice_id: str, project_root: Path) -> None:
    """Releases the lock for a slice."""
    lock_file = project_root / ".superpowers" / "locks" / f"{slice_id}.lock"
    if lock_file.exists():
        lock_file.unlink()
