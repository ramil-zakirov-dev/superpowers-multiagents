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
