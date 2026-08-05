"""Abandoned-dispatch detection: a derived fact, never a stored one.

A slice is abandoned when its document sits at some role's in_progress_status
while no live supervisor owns it — the slice's lock is absent, unreadable, or
names a process that is gone. Both halves already exist in the codebase: the
status set comes from the merged config (never a hardcoded literal, so a
project that renames EXECUTING is detected by its own word), and liveness is
the same check that already governs lock reclamation. The lock self-heals;
the status does not, and the status is what the gates read.

Nothing here is ever written into a document or a lock — a stored "abandoned"
flag would go stale the moment someone re-dispatches.
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path

from scripts.frontmatter import parse_frontmatter
from scripts.locks import _lock_is_held
from scripts.paths import lock_path, logs_dir
from scripts.utils import _is_process_alive


def in_progress_statuses(config: dict) -> set[str]:
    """Every status some configured role treats as 'work in flight'."""
    return {
        agent.get("in_progress_status")
        for agent in (config.get("agents") or {}).values()
    } - {None}


def _read_lock(lock_file: Path) -> dict:
    try:
        return json.loads(lock_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def is_abandoned(
    status: str,
    slice_id: str,
    project_root: Path,
    in_progress: set[str],
    *,
    is_alive=_is_process_alive,
) -> bool:
    """True when the document claims in-progress work no live supervisor owns.

    A `starting` lock inside its grace window counts as owned: dispatch sets
    the in-progress status before the supervisor exists, and that gap is not
    abandonment.
    """
    if status not in in_progress:
        return False
    lock_file = lock_path(project_root, slice_id)
    if not lock_file.exists():
        return True
    return not _lock_is_held(_read_lock(lock_file), is_alive=is_alive)


def lock_evidence(slice_id: str, project_root: Path, *, is_alive=_is_process_alive) -> str:
    """What an abandonment verdict is based on, in one sentence.

    The operator is being asked to trust a verdict about a process they
    cannot see, so the verdict names its grounds: the lock's pid and its
    liveness, or the lock's absence.
    """
    lock_file = lock_path(project_root, slice_id)
    if not lock_file.exists():
        return f"no lock file at {lock_file} — nothing owns this slice"
    data = _read_lock(lock_file)
    if not data:
        return f"lock at {lock_file} is unreadable — nothing verifiably owns this slice"
    if data.get("state") == "running" and data.get("pid"):
        pid = data["pid"]
        try:
            alive = is_alive(int(pid))
        except (TypeError, ValueError):
            alive = False
        if alive:
            return f"lock names supervisor pid {pid}, which is alive"
        return f"lock names supervisor pid {pid}, which is not alive"
    if data.get("state") == "starting":
        return f"lock at {lock_file} is still 'starting' — the supervisor never claimed it"
    return f"lock at {lock_file} is in state {data.get('state')!r}"


DEFAULT_POLL_SECONDS = 15.0

OUTCOME_TERMINAL = "terminal"
OUTCOME_ABANDONED = "abandoned"
OUTCOME_TIMED_OUT = "timed_out"


def find_slice_document(base_dir: Path, slice_id: str) -> Path | None:
    """The specs/ or plans/ document carrying this slice_id, if one exists."""
    for subdir in ("specs", "plans"):
        directory = Path(base_dir) / subdir
        if not directory.is_dir():
            continue
        for candidate in sorted(directory.glob("*.md")):
            if candidate.stem == slice_id:
                return candidate
            data = parse_frontmatter(candidate.read_text(encoding="utf-8"))
            if data.get("slice_id") == slice_id:
                return candidate
    return None


def latest_log(project_root: Path, slice_id: str) -> Path | None:
    """The newest log mentioning this slice, or None — same rule as `summary`."""
    directory = logs_dir(project_root)
    matching = sorted(directory.glob(f"*{slice_id}*.log")) if directory.exists() else []
    if not matching:
        return None
    return max(matching, key=lambda path: path.stat().st_mtime)


@dataclass
class WaitResult:
    outcome: str   # OUTCOME_TERMINAL | OUTCOME_ABANDONED | OUTCOME_TIMED_OUT
    status: str    # the document's status at the moment the wait ended
    elapsed: float


def wait_for_dispatch(
    document: Path,
    project_root: Path,
    config: dict,
    slice_id: str,
    *,
    timeout: float | None = None,
    poll: float = DEFAULT_POLL_SECONDS,
    sleep=time.sleep,
    monotonic=time.monotonic,
    is_alive=_is_process_alive,
) -> WaitResult:
    """Block while the slice is in progress and a live supervisor owns it.

    The watched thing takes minutes, so the default poll is 15s — a tighter
    loop only burns wakeups. The default timeout is none: the caller that
    backgrounds this process has its own, and a timeout belongs at the
    boundary that can act on it. Liveness and the clock are injectable so
    the tests are fast and not flaky.
    """
    in_progress = in_progress_statuses(config)
    started = monotonic()
    while True:
        status = parse_frontmatter(
            Path(document).read_text(encoding="utf-8")
        ).get("status", "UNKNOWN")
        elapsed = monotonic() - started
        if status not in in_progress:
            return WaitResult(OUTCOME_TERMINAL, status, elapsed)
        if is_abandoned(status, slice_id, project_root, in_progress, is_alive=is_alive):
            return WaitResult(OUTCOME_ABANDONED, status, elapsed)
        if timeout is not None and elapsed >= timeout:
            return WaitResult(OUTCOME_TIMED_OUT, status, elapsed)
        sleep(poll)
