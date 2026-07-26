import json
import os
import threading
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


def test_writes_never_leave_a_stray_temp_file(tmp_path):
    """acquire + claim + release must not leak the sibling .lock-tmp-* file
    each atomic write stages content through."""
    lock_file = acquire_slice_lock("slice-01", tmp_path)
    claim_slice_lock(lock_file, os.getpid())
    release_slice_lock_file(lock_file)
    assert list(lock_file.parent.glob(".lock-tmp-*")) == []


def test_concurrent_acquire_claim_release_never_crashes(tmp_path):
    """Regression for the race a task-5 review reproduced: hammering
    acquire/claim/release for the same slice_id from many threads used to
    let a racing acquire misread a live, mid-write lock as corrupt and
    unlink it out from under its owner (a crash on Windows, a silent
    double-acquisition on POSIX). Atomic staged writes close this — no
    reader can ever observe a partial write, only fully-old or fully-new
    content."""
    errors = []
    lock = threading.Lock()

    def hammer():
        for _ in range(30):
            try:
                lock_file = acquire_slice_lock("slice-01", tmp_path)
            except LockError:
                continue
            try:
                claim_slice_lock(lock_file, os.getpid())
            except Exception as exc:  # pragma: no cover - failure path
                with lock:
                    errors.append(exc)
            finally:
                release_slice_lock_file(lock_file)

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
