"""Contradictions between what a document claims and what can be observed.

Every fact here is derived on read and none is ever written down. A stored
"abandoned" or "empty branch" flag would go stale the moment someone
re-dispatches, and a report that repairs what it finds is a worse instrument
than one that merely says what it sees.

---

Abandoned-dispatch detection: a derived fact, never a stored one.

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

from scripts.errors import OrchestratorError
from scripts.frontmatter import parse_frontmatter
from scripts.git_ops import branch_exists, commits_since, current_branch
from scripts.locks import _lock_is_held
from scripts.paths import lock_path, logs_dir
from scripts.utils import _is_process_alive


def in_progress_statuses(config: dict) -> set[str]:
    """Every status some configured role treats as 'work in flight'."""
    return {
        agent.get("in_progress_status")
        for agent in (config.get("agents") or {}).values()
    } - {None}


def success_statuses(config: dict) -> set[str]:
    """Every status that claims some configured role finished its work.

    Read from the config for the same reason `in_progress_statuses` is: the
    names are the project's, not ours. Unlike `isolated_success_statuses`
    this covers every role — a caller waiting for work to be done does not
    care whether the role that does it runs in a worktree.
    """
    return {
        agent.get("success_status")
        for agent in (config.get("agents") or {}).values()
    } - {None}


def certifiable_statuses(config: dict) -> dict:
    """`{drafting status: the status a human may certify it into}`.

    Read off the roles that write documents rather than naming
    `PLAN_DRAFTING` here: a project that renames its statuses still gets a way
    out of a document its supervisor died over. The pair has to come from one
    role, which is why this returns a mapping and not two sets — certifying is
    "this specific drafting state is finished", not "advance anything".
    """
    return {
        agent["produced_status"]: agent["success_status"]
        for agent in (config.get("agents") or {}).values()
        if isinstance(agent, dict)
        and agent.get("produced_status")
        and agent.get("success_status")
    }


def isolated_success_statuses(config: dict) -> set[str]:
    """Every status that claims an isolated role finished successfully.

    Read from the config for the same reason `in_progress_statuses` is: a
    project that renames `EXECUTION_COMPLETE` still deserves to be told when
    the branch behind that claim is empty.
    """
    return {
        agent.get("success_status")
        for agent in (config.get("agents") or {}).values()
        if agent.get("isolated_worktree")
    } - {None}


def empty_slice_branch(slice_id: str, project_root: Path) -> str:
    """A note when a finished-looking slice has nothing on its branch, or "".

    Derived on read and never stored, like abandonment: the branch can gain
    commits the moment someone re-dispatches.

    Says nothing when the branch does not exist. That is the ordinary state of
    a slice that landed and had its branch tidied away, and `close-slice` has
    its own words for the case where it matters — flagging it here would make
    the healthy case look like a defect.
    """
    branch = f"feat/{slice_id}"
    if not branch_exists(branch, project_root):
        return ""
    count = commits_since(current_branch(project_root), branch, project_root)
    if count is None or count > 0:
        return ""
    return (
        f"{branch} has no commits; close-slice would merge nothing"
    )


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
    """True when a dispatch against this document was made and then died.

    Abandonment needs evidence of a dead dispatch, and the lock is that
    evidence. Dispatch acquires it *before* it writes the in-progress status,
    so a document sitting at one with no lock at all was never dispatched into
    it — a human claimed it, which is a legal way for work to be in progress.
    Reading an absent lock as abandonment convicted every hand-driven slice and
    told the operator to `reconcile` work that was going perfectly well.

    A `starting` lock inside its grace window counts as owned: dispatch sets
    the in-progress status before the supervisor exists, and that gap is not
    abandonment either.
    """
    if status not in in_progress:
        return False
    lock_file = lock_path(project_root, slice_id)
    if not lock_file.exists():
        return False
    return not _lock_is_held(_read_lock(lock_file), is_alive=is_alive)


def is_hand_owned(
    status: str, slice_id: str, project_root: Path, in_progress: set[str]
) -> bool:
    """True when work is in progress with no dispatch behind it.

    The complement of `is_abandoned` within the in-progress statuses, and the
    other half of what an in-progress status now means. Worth naming rather
    than leaving as "not abandoned": the report says something different about
    each, and only one of them is a defect.
    """
    return status in in_progress and not lock_path(project_root, slice_id).exists()


def gate_for_in_progress(config: dict, status: str) -> str:
    """The gate a role at `status` was dispatched from, or "" when not unique.

    What a failed or abandoned dispatch returns its document to. Derived from
    the role's own `allowed_statuses` rather than hardcoded, for the same
    reason `in_progress_statuses` is: the names belong to the project. A role
    that may be dispatched from either of two gates leaves no single place to
    return to, and guessing there would rewrite history — the caller falls back
    to something it can defend.
    """
    gates = {
        gate
        for agent in (config.get("agents") or {}).values()
        if agent.get("in_progress_status") == status
        for gate in (agent.get("allowed_statuses") or [])
    }
    return gates.pop() if len(gates) == 1 else ""


def describe_lock(lock_file: Path, data: dict | None, held: bool) -> str:
    """One sentence about a lock, from what a caller has already read.

    Deliberately takes no `is_alive` and performs no lookup. A verdict and
    the grounds it names have to come from a single observation: when they
    were two lookups, the output could contradict itself, and once did —
    "is abandoned ... pid 22776, which is alive".

    `data` is None for an absent lock and `{}` for one that would not parse.
    """
    if data is None:
        return f"no lock file at {lock_file} — nothing owns this slice"
    if not data:
        return f"lock at {lock_file} is unreadable — nothing verifiably owns this slice"
    if data.get("state") == "running" and data.get("pid"):
        liveness = "alive" if held else "not alive"
        return f"lock names supervisor pid {data['pid']}, which is {liveness}"
    if data.get("state") == "starting":
        return f"lock at {lock_file} is still 'starting' — the supervisor never claimed it"
    return f"lock at {lock_file} is in state {data.get('state')!r}"


def read_lock_state(slice_id: str, project_root: Path, *, is_alive=_is_process_alive):
    """The lock's contents and whether it is held, from one read."""
    lock_file = lock_path(project_root, slice_id)
    if not lock_file.exists():
        return lock_file, None, False
    data = _read_lock(lock_file)
    held = _lock_is_held(data, is_alive=is_alive) if data else False
    return lock_file, data, held


def lock_evidence(slice_id: str, project_root: Path, *, is_alive=_is_process_alive) -> str:
    """What an abandonment verdict is based on, in one sentence.

    The operator is being asked to trust a verdict about a process they
    cannot see, so the verdict names its grounds: the lock's pid and its
    liveness, or the lock's absence.
    """
    return describe_lock(*read_lock_state(slice_id, project_root, is_alive=is_alive))


DEFAULT_POLL_SECONDS = 15.0

OUTCOME_TERMINAL = "terminal"
OUTCOME_ABANDONED = "abandoned"
OUTCOME_TIMED_OUT = "timed_out"
OUTCOME_UNREADABLE_LOCK = "unreadable_lock"

#: How many consecutive unreadable polls before a wait gives up. The lock is
#: rewritten atomically, so one unparseable read is a lost race and not a
#: fact; a run of them is a corrupt file, which is the watcher's problem to
#: report rather than the dispatch's outcome to declare.
_MAX_UNREADABLE_POLLS = 4


def slice_documents(base_dir: Path, slice_id: str) -> list[Path]:
    """Every specs/ or plans/ document carrying this slice_id, specs first.

    A slice normally has two: the spec its planner was dispatched at, and the
    plan its executor was dispatched at. Both carry the same `slice_id`, which
    is why a caller asking about "the slice" has to say which dispatch it means.
    """
    found: list[Path] = []
    for subdir in ("specs", "plans"):
        directory = Path(base_dir) / subdir
        if not directory.is_dir():
            continue
        for candidate in sorted(directory.glob("*.md")):
            if candidate.stem == slice_id:
                found.append(candidate)
                continue
            data = parse_frontmatter(candidate.read_text(encoding="utf-8"))
            if data.get("slice_id") == slice_id:
                found.append(candidate)
    return found


def resolve_slice_document(
    base_dir: Path, slice_id: str, in_progress: set[str]
) -> Path:
    """The one document a wait on `slice_id` is about.

    With two documents per slice, "first one found" is a coin toss, and it
    lands wrong in the common case: the spec settles at PLAN_GENERATED long
    before the executor stops running, so a wait on the slice would read the
    spec, call the dispatch finished and exit 0 while the plan was still
    EXECUTING. The document in flight is the one being waited on, so that is
    the one to pick — and when that is not unique, refusing beats guessing.
    """
    found = slice_documents(base_dir, slice_id)
    if not found:
        raise OrchestratorError(
            f"no document under {base_dir} carries slice_id '{slice_id}'."
        )
    if len(found) == 1:
        return found[0]

    live = [
        path for path in found
        if parse_frontmatter(path.read_text(encoding="utf-8")).get("status")
        in in_progress
    ]
    if len(live) == 1:
        return live[0]

    names = ", ".join(path.name for path in found)
    reason = (
        "several are in progress" if live else "none of them is in progress"
    )
    raise OrchestratorError(
        f"ambiguous: {len(found)} documents carry slice_id '{slice_id}' "
        f"({names}) and {reason}. Name the one you mean with --file."
    )


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
    #: The grounds the verdict was reached on, captured when it was reached.
    #: Re-deriving them at print time made the output contradict itself —
    #: observed as "is abandoned ... pid 22776, which is alive", the two
    #: halves being two lookups milliseconds apart.
    evidence: str = ""


def wait_for_dispatch(
    document: Path,
    project_root: Path,
    config: dict,
    slice_id: str,
    *,
    timeout: float | None = None,
    poll: float = DEFAULT_POLL_SECONDS,
    until: set[str] | None = None,
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

    `until` narrows what ends the wait to a set of statuses — everything else
    keeps waiting, including `FAILED` and an abandoned dispatch. That is for
    the caller that only acts on success and repairs failures by hand: for it,
    returning on a failure is a wakeup it will do nothing with. It does not
    change what a terminal status means anywhere else, and `timeout` remains
    the escape, so waiting for a repair that never comes is still bounded.
    """
    in_progress = in_progress_statuses(config)
    started = monotonic()
    unreadable_polls = 0
    while True:
        elapsed = monotonic() - started
        # Lock first, then the document. The supervisor writes the terminal
        # status BEFORE releasing the lock, so a "no live supervisor" reading
        # always sees a settled document; reading first could land on the
        # in-progress status the supervisor has not yet overwritten and
        # then notice the lock already released, classifying a finished
        # dispatch as abandoned.
        lock_file, data, lock_held = read_lock_state(
            project_root=project_root, slice_id=slice_id, is_alive=is_alive
        )
        if data is not None:
            if data:
                unreadable_polls = 0
            else:
                # Present but unparseable. That is a failure to observe, not
                # an observation: the lock is rewritten atomically, so this
                # is a lost race far more often than a corrupt file. Holding
                # the wait open is the fail-closed reading — the alternative
                # convicts a live supervisor on a read error.
                unreadable_polls += 1
                if unreadable_polls >= _MAX_UNREADABLE_POLLS:
                    status = parse_frontmatter(
                        Path(document).read_text(encoding="utf-8")
                    ).get("status", "UNKNOWN")
                    return WaitResult(OUTCOME_UNREADABLE_LOCK, status, elapsed)
                lock_held = True
        # Read after the lock, never before -- see the ordering note above.
        status = parse_frontmatter(
            Path(document).read_text(encoding="utf-8")
        ).get("status", "UNKNOWN")
        settled = status not in in_progress
        if settled and (until is None or status in until):
            return WaitResult(OUTCOME_TERMINAL, status, elapsed)
        if not lock_held and not settled and until is None:
            # No live supervisor and the status never moved. Under `until`
            # this is not reported either: like a failure, it needs a human,
            # and the caller asked to hear only about work that finished.
            return WaitResult(
                OUTCOME_ABANDONED, status, elapsed,
                evidence=describe_lock(lock_file, data, lock_held),
            )
        if timeout is not None and elapsed >= timeout:
            return WaitResult(OUTCOME_TIMED_OUT, status, elapsed)
        sleep(poll)
