#!/usr/bin/env python3
"""Supervisor for a single dispatched agent.

This process is the background job. It owns one agent from spawn to terminal
status: it captures the agent's output, judges what the run left behind, fires
the completion hook, and releases the lock on every exit path.

Not asking the agent for its own status is still the point. Previously it was
asked, in its prompt, to set one — so an agent that crashed or simply forgot
left the slice stranded with no way back.

What has changed is the belief that the child's exit code answers the
question. It does not, and under the shipped harness it cannot: the argv is
`opencode run …`, a thin client to a long-lived server, so its exit reports
that the client stopped and says nothing about the session still working. Read
through the health-check distinction, the exit code is a *shallow* check
standing in for a *deep* one — and the deep one is already here, in
`_unmet_postcondition`. The exit code is now one input to the verdict rather
than the whole of it.
"""

import argparse
import datetime
import os
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):  # invoked as a script rather than `-m`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import produced, sandbox
from scripts.config import load_agent_config, resolve_agent, validate_config
from scripts.errors import ConfigError, HookError, OrchestratorError
from scripts.git_ops import commits_since
from scripts.frontmatter import parse_frontmatter, update_frontmatter_status
from scripts.hooks import canonical_events, run_infrastructure_hook
from scripts.locks import (
    claim_slice_lock,
    mark_lock_unresolved,
    release_slice_lock_file,
)

FAILED_STATUS = "FAILED"

#: The three answers a run can get. `UNKNOWN` is an outcome, not an error: it
#: is what the runner says when the process it was watching died over a
#: workspace that kept changing, and it deliberately writes nothing.
VERDICT_SUCCESS = "success"
VERDICT_GATE = "gate"
VERDICT_UNKNOWN = "unknown"

#: How long a workspace must show no change before a dead client is taken to
#: mean a dead agent. Generous on purpose: concluding "gone" too early is the
#: expensive mistake, because that is the one that sweeps a live agent's stack.
#:
#: It is paid on every failed dispatch, and that is the visible cost of this
#: design: a run that really died now takes this long to say so. Lower it and
#: the risk returned is not "a slower report" but "a stack reclaimed from an
#: agent that was only thinking".
DEFAULT_SETTLE_WINDOW_SECONDS = 300

#: How long the runner will watch a workspace that keeps changing before
#: answering `UNKNOWN` anyway. Stops the supervisor becoming the thing that
#: never ends.
DEFAULT_OBSERVATION_DEADLINE_SECONDS = 1800

_POLL_SECONDS = 5.0

#: Opens each run's section of the log. The log path is derived from the role
#: and the document, so every re-dispatch of a slice lands on the same file and
#: the runs need a visible seam between them.
RUN_BANNER = "=== run started {stamp} ==="


def run_supervised(
    role: str,
    target_file: Path,
    project_root: Path,
    lock_file: Path,
    log_file: Path,
    argv: list,
    cwd: Path,
    sandbox_branch: str = "",
    base_ref: str = "",
    gate_status: str = "",
) -> int:
    """Run one agent to completion and record the outcome. Returns its exit code."""
    target_file = Path(target_file)
    project_root = Path(project_root)
    log_file = Path(log_file)

    claim_slice_lock(lock_file, os.getpid(), role=role)
    exit_code = 127
    verdict = None
    try:
        exit_code = _run_child(argv, cwd, log_file)
        verdict = _record_outcome(
            role, target_file, project_root, exit_code, log_file, sandbox_branch,
            base_ref, gate_status,
        )
        return exit_code
    finally:
        # The lock is the only thing this process leaves behind that another
        # command reads, so it is where a verdict nobody reached has to live.
        # Releasing it on `unknown` said "nothing owns this slice" about a tree
        # that may still have an agent writing in it — indistinguishable, from
        # outside, from an abandoned dispatch, and `wait` duly advised
        # `reconcile` (issue #34).
        #
        # An exception still releases. Only a deliberate `unknown` holds: a
        # crash here means the epilogue never ran, which is the ordinary
        # abandonment the existing machinery already describes correctly.
        if verdict == VERDICT_UNKNOWN:
            mark_lock_unresolved(
                lock_file, role=role, exit_code=exit_code, log=str(log_file)
            )
        else:
            release_slice_lock_file(lock_file)


def _existing_log_size(log_file: Path) -> int:
    """Bytes already in the log, or 0 when that cannot be established.

    Only decides whether to write a blank line before the banner, so an
    unanswerable stat costs cosmetics and nothing else — worth swallowing
    rather than turning a formatting question into a failed dispatch.
    """
    try:
        return log_file.stat().st_size
    except OSError:
        return 0


def _run_child(argv: list, cwd: Path, log_file: Path) -> int:
    """Spawn the agent with both streams captured.

    Appends. The log path is `<role>_<document>.log`, so a re-dispatch of the
    same slice reopens the same file — and truncating meant the retry erased
    the transcript of the run it was retrying. That is backwards: the failed
    run is the one worth reading, and a retry is what you do after a failure.
    It cost a real diagnosis (issue #15 had to be retracted for want of a log
    the tool had already overwritten).

    The cost accepted in exchange is an unbounded file. No rotation, on
    purpose: this is a handful of runs per slice under an ignored directory,
    and a size policy is a knob that can be set wrong in the direction we just
    spent a slice fixing.

    A failure to even create/open the log file is treated as an outcome
    (synthesized non-zero exit), not an exception — letting it propagate
    would skip _record_outcome entirely and strand the slice at its
    in-progress status with no way back.
    """
    already_has_runs = _existing_log_size(log_file) > 0
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handle = open(log_file, "a", encoding="utf-8", errors="replace")
    except OSError as exc:
        _safe_print(f"[runner] could not create/open log file {log_file}: {exc}")
        return 127

    with handle as log:
        if already_has_runs:
            log.write("\n\n")
        stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        log.write(RUN_BANNER.format(stamp=stamp) + "\n")
        log.write(f"$ {' '.join(str(part) for part in argv)}\n\n")
        log.flush()
        try:
            completed = subprocess.run(
                [str(part) for part in argv],
                cwd=str(cwd),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            log.write(f"\n[runner] could not start the agent: {exc}\n")
            return 127
        return completed.returncode


def _safe_print(message: str) -> None:
    """print() raises ValueError on a closed stream (not just no-ops like it
    does on a None stdout). A detached background job — exactly how Task 11
    spawns this process — can hand the runner either. A failure to print a
    diagnostic must never itself become an unhandled exception on a path
    that still has real work left to do (recording status, firing a hook)."""
    try:
        print(message)
    except (ValueError, OSError):
        pass


def _log_and_print(log_file: Path, message: str) -> None:
    """Print a diagnostic AND append it to the log file.

    By the time this runs, the runner is typically a detached background
    job (Task 11 spawns it with no attached console) — stdout is a closed
    handle or the null device. The log file is the one artifact the
    dispatcher tells the operator to inspect, so outcome diagnostics belong
    there too, not only on a stream nobody is reading. Best-effort: a
    log-append failure here must not mask the outcome already recorded.
    """
    _safe_print(message)
    try:
        with open(log_file, "a", encoding="utf-8", errors="replace") as log:
            log.write(message + "\n")
    except OSError:
        pass


def _unmet_postcondition(
    role: str,
    agent: dict,
    target_file: Path,
    log_file: Path,
    project_root: Path,
    base_ref: str,
) -> str:
    """Why this run left nothing the next gate can use, or "".

    Exit code 0 means the process ended, not that the work landed. Every role
    owes an artifact; which one depends on where it worked.

    * isolated — commits on `feat/<slice_id>`, the branch `close-slice` merges
    * declares `produces` — a document the state machine can read
    * neither — nothing to check

    The isolated branch used to be a skip, on the reasoning that a worktree's
    output is not in the main tree so looking there would be meaningless. True
    of files, and it left the one role whose output is code with no check at
    all: the branch is in the main repository the whole time, and it is
    exactly where the next gate looks.
    """
    if agent.get("isolated_worktree"):
        return _no_commits_on_the_branch(
            role, target_file, log_file, project_root, base_ref
        )

    subdir = agent.get("produces")
    if not subdir:
        return ""

    frontmatter = parse_frontmatter(target_file.read_text(encoding="utf-8"))
    slice_id = frontmatter.get("slice_id", target_file.stem)
    try:
        directory = produced.documents_dir(target_file, subdir)
        if produced.find_document(target_file, subdir, slice_id) is not None:
            return ""
    except ConfigError as exc:
        return f"[runner] ERROR: {exc}"

    return (
        f"[runner] ERROR: '{role}' exited 0 but left no document the pipeline can "
        f"see: nothing in {directory} carries `slice_id: {slice_id}` with a "
        f"`status:` for the next gate to move. The work may exist and still be "
        f"invisible — a document with no frontmatter is the usual cause. Returning "
        f"the slice to its gate rather than certifying the run."
    )


def _no_commits_on_the_branch(
    role: str, target_file: Path, log_file: Path, project_root: Path, base_ref: str
) -> str:
    """Whether an isolated run left anything on the branch it was given.

    Counted from the branch tip recorded when the worktree came into being,
    not from the main branch: a re-dispatch attaches to a branch that may
    already carry an earlier run's commits, and counting from the main branch
    would credit this run with that work.

    An unanswerable count records FAILED. That reads like the inverse of the
    rule in `utils._is_process_alive`, where an unanswerable lookup counts as
    alive, and the two agree on the principle: neither acts destructively on
    what it could not observe. There a missing answer would kill a running
    dispatch; here it would certify a finished one, and certification is the
    destructive move.
    """
    frontmatter = parse_frontmatter(target_file.read_text(encoding="utf-8"))
    slice_id = frontmatter.get("slice_id", target_file.stem)
    branch = f"feat/{slice_id}"

    if not base_ref:
        # Only reachable when run_supervised is driven directly; the
        # dispatcher always records one. Say so rather than passing silently.
        _log_and_print(
            log_file,
            f"[runner] note: no base ref was recorded for '{role}', so the "
            f"commits on {branch} cannot be attributed to this run — check skipped.",
        )
        return ""

    count = commits_since(base_ref, branch, project_root)
    if count is None:
        return (
            f"[runner] ERROR: git could not count commits on {branch} since "
            f"{base_ref[:12]} for '{role}' — the branch may be gone. Nothing "
            f"observed the work landing, so returning the slice to its gate rather "
            f"than certifying it."
        )
    if count > 0:
        return ""
    return (
        f"[runner] ERROR: '{role}' left no commits on {branch} "
        f"since {base_ref[:12]}. That is the branch close-slice merges, so it "
        f"would have nothing to take. If the work exists, it is in another tree. "
        f"Returning the slice to its gate rather than certifying the run."
    )


def _promote_produced(
    role: str,
    agent: dict,
    target_file: Path,
    log_file: Path,
    state_machine: dict,
) -> str:
    """Move this run's produced document from drafting to done, or say why not.

    The status a role is told to write into its own output is the one a
    half-written file carries — it writes the file the moment it starts
    drafting and then works on it for the rest of the run. So the claim that
    the document is finished cannot be the role's; it belongs to the component
    that watched the run end, which is this one.

    Returns "" when there is nothing to do: a role that produces no document,
    a document `_unmet_postcondition` has already reported missing, or one the
    role marked finished itself. That last case is disobedience against the
    prompt and still exactly what the next gate wants — failing a run over it
    would help nobody.
    """
    subdir = agent.get("produces")
    target_status = agent.get("success_status")
    if not subdir or not target_status:
        return ""

    frontmatter = parse_frontmatter(target_file.read_text(encoding="utf-8"))
    slice_id = frontmatter.get("slice_id", target_file.stem)
    try:
        document = produced.find_document(target_file, subdir, slice_id)
    except ConfigError as exc:
        return f"[runner] ERROR: {exc}"
    if document is None:
        return ""

    current = parse_frontmatter(document.read_text(encoding="utf-8")).get("status")
    if current == target_status:
        return ""

    if update_frontmatter_status(
        document, target_status,
        state_machine["valid_statuses"], state_machine["transitions"],
    ):
        _log_and_print(log_file, f"[runner] {document.name}: {current} -> {target_status}")
        return ""

    return (
        f"[runner] ERROR: '{role}' produced {document.name} at '{current}', which "
        f"this machine cannot move to '{target_status}'. That status is what the "
        f"next gate reads, so the document would sit where nothing can advance it "
        f"— and the natural repair, editing frontmatter by hand, is the one this "
        f"pipeline forbids. Returning the slice to its gate rather than certifying "
        f"the run."
    )


def _windows(config: dict) -> tuple:
    """The settle window and observation deadline, in seconds."""
    machine = config.get("state_machine") or {}
    return (
        machine.get("settle_window_seconds", DEFAULT_SETTLE_WINDOW_SECONDS),
        machine.get(
            "observation_deadline_seconds", DEFAULT_OBSERVATION_DEADLINE_SECONDS
        ),
    )


def _slice_id_of(target_file: Path) -> str:
    frontmatter = parse_frontmatter(target_file.read_text(encoding="utf-8"))
    return frontmatter.get("slice_id", target_file.stem)


def _newest_change(root: Path) -> float:
    """The newest mtime anywhere under `root`, or 0.0 for an unreadable tree.

    Walks rather than stats the root. A directory's mtime does not reliably
    change when a file inside it is rewritten — on Windows it usually does not
    — so the root alone reports a busy worktree as quiet, which is the one
    error this function must not make.
    """
    newest = 0.0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                stamp = os.stat(os.path.join(dirpath, name)).st_mtime
            except OSError:
                continue          # vanished mid-walk: the tree is alive, if anything
            if stamp > newest:
                newest = stamp
    return newest


def _workspace_keeps_changing(
    workspace: Path, settle_window: float, deadline: float, log_file: Path
) -> bool:
    """Watch a workspace until it settles or the deadline runs out.

    Returns True when it was still changing at the deadline — the agent is
    working and the client's death said nothing about it. False when it went
    quiet for the whole settle window, which is as close to "the agent is
    gone" as an observer outside the harness can get.
    """
    mark = _newest_change(workspace)
    started = time.monotonic()
    last_change = started
    while True:
        elapsed = time.monotonic() - started
        if elapsed >= deadline:
            _log_and_print(
                log_file,
                f"[runner] {workspace.name} was still changing after "
                f"{elapsed:.0f}s of watching; giving up on a verdict rather "
                f"than inventing one.",
            )
            return True
        time.sleep(min(_POLL_SECONDS, deadline - elapsed))
        newest = _newest_change(workspace)
        if newest > mark:
            mark = newest
            last_change = time.monotonic()
        elif time.monotonic() - last_change >= settle_window:
            return False


def _fate_of_a_dead_client(
    role: str,
    agent: dict,
    target_file: Path,
    project_root: Path,
    log_file: Path,
    config: dict,
) -> str:
    """`VERDICT_GATE` or `VERDICT_UNKNOWN` for a run whose watcher died.

    Two shapes, because the two kinds of artifact fail differently.

    A **producing** role leaves a document, and a document that exists proves
    only that writing began (#21). So the client's death over one is exactly
    the case nobody can settle from outside: the document is there, it may be
    whole, and the only instrument able to tell is a human reading it. That is
    `certify`. With no document at all there is nothing to read and nothing to
    wonder about, so the slice goes back to its gate.

    An **isolated** role leaves commits, and it has already been asked for
    them by the time this runs. What is left to ask is whether more are
    coming, and only watching answers that.

    A cleanliness shortcut was tried here — a worktree straight from
    `git worktree add` is clean, so a clean one would settle the question for
    free — and it is wrong. It is read milliseconds after the client died, and
    an agent that is alive but has not written yet is indistinguishable from
    one that never will. The shortcut fires exactly in the window where being
    wrong sweeps a live agent's stack, which is the harm this whole function
    exists to prevent. The price of dropping it is that an ordinary failed
    dispatch takes a settle window to report; that is latency, and latency is
    the cheap side of this trade.
    """
    if agent.get("produces"):
        slice_id = _slice_id_of(target_file)
        try:
            document = produced.find_document(
                target_file, agent["produces"], slice_id
            )
        except ConfigError:
            return VERDICT_GATE
        if document is None:
            return VERDICT_GATE
        _log_and_print(
            log_file,
            f"[runner] '{role}' left {document.name}, and a document that "
            f"exists proves only that writing began. Nothing outside the "
            f"harness can tell a finished one from an abandoned one — read it "
            f"and run `certify --file {document.name}` if it is complete.",
        )
        return VERDICT_UNKNOWN

    if not agent.get("isolated_worktree"):
        return VERDICT_GATE

    workspace = Path(project_root) / ".worktrees" / _slice_id_of(target_file)
    if not workspace.is_dir():
        return VERDICT_GATE

    settle_window, deadline = _windows(config)
    if _workspace_keeps_changing(workspace, settle_window, deadline, log_file):
        return VERDICT_UNKNOWN
    return VERDICT_GATE


def _record_outcome(
    role: str,
    target_file: Path,
    project_root: Path,
    exit_code: int,
    log_file: Path,
    sandbox_branch: str = "",
    base_ref: str = "",
    gate_status: str = "",
) -> str | None:
    """Advance the slice status and fire the completion hook.

    Returns the verdict, which `run_supervised` needs in order to decide what
    to do with the lock — `None` when the configuration could not be read and
    no verdict was formed at all.
    """
    try:
        config = load_agent_config(project_root)
        validate_config(config)
        agent = resolve_agent(config, role)
    except OrchestratorError as exc:
        _log_and_print(
            log_file, f"[runner] configuration unusable, cannot record outcome: {exc}"
        )
        return None

    state_machine = config["state_machine"]

    # The deep check is the thing able to correct the shallow one, so gating it
    # on the shallow one agreeing switched it off in every case it was built
    # for. It now runs whenever some artifact can speak for the run.
    #
    # Which artifacts can differs, and the difference is atomicity. A commit is
    # an indivisible act of completion: on the branch means written and
    # finished, whatever the watched process exited with. A document is not —
    # a producing role writes the file the moment it starts typing (#21), so
    # its existence says there is something to read, not that anyone stopped
    # writing. That is why `_promote_produced` stays on the exit-0 path.
    #
    # A role with neither has no evidence at all, and for it the exit code
    # remains the only witness. It must not be *rescued* by a check that had
    # nothing to look at: an empty postcondition means unexamined, not passed.
    missing = ""
    examined = exit_code == 0 or bool(agent.get("isolated_worktree"))
    if examined:
        missing = _unmet_postcondition(
            role, agent, target_file, log_file, project_root, base_ref
        )
        if not missing and exit_code == 0:
            missing = _promote_produced(
                role, agent, target_file, log_file, state_machine
            )
    # `missing` is deliberately NOT printed here. Every one of those messages
    # ends by announcing a decision — "Returning the slice to its gate rather
    # than certifying the run" — and the decision has not been taken yet: the
    # liveness verdict below can still answer `unknown`, in which case nothing
    # is returned anywhere. Printed early it also dates badly, because the
    # observation is made at the client's death and the watch that follows can
    # run for half an hour. Live on 2026-08-12 that produced a log claiming the
    # run left no commits, directly above the line saying no verdict was
    # reached, on a run that had by then committed 22 times (issue #34).

    if examined and not missing:
        verdict = VERDICT_SUCCESS
    elif exit_code == 0:
        # It ended on its own terms and left nothing. Nobody is still working;
        # there is nothing to observe and nothing to wait for.
        verdict = VERDICT_GATE
    else:
        verdict = _fate_of_a_dead_client(
            role, agent, target_file, project_root, log_file, config
        )

    if verdict == VERDICT_UNKNOWN:
        # Everything below writes something down. None of it may run on an
        # answer nobody has: not the status, not the completion hook, and
        # above all not the teardown, which would reclaim a stack the agent
        # may still be using. `run_supervised` marks the lock unresolved
        # instead of releasing it, which is the other half of not inviting a
        # re-dispatch.
        _log_and_print(
            log_file,
            f"[runner] {role} exited {exit_code}, but the run's fate was not "
            f"observed; status left at its current value and nothing torn "
            f"down. log: {log_file}",
        )
        if missing:
            # What the postcondition saw, reported as a dated observation and
            # never as a conclusion. It was read the moment the client died;
            # by now the watch has run and the tree has been changing
            # throughout, so the only honest form of this sentence names when
            # it was taken and says the question is still open.
            _log_and_print(
                log_file,
                f"[runner] at the moment the client died the run had left no "
                f"artifact the pipeline could see. That was a reading taken "
                f"then, not a verdict: the workspace kept changing afterwards. "
                f"Check the branch and the worktree before concluding anything.",
            )
        _log_and_print(
            log_file,
            f"[runner] the slice's lock is now 'unresolved' and refuses a "
            f"re-dispatch. Resolve it with `certify` (a produced document that "
            f"reads as complete) or `reconcile --yes` (abandon the run and "
            f"return the slice to its gate).",
        )
        return verdict

    if missing:
        _log_and_print(log_file, missing)

    if verdict == VERDICT_SUCCESS:
        candidates = [agent.get("success_status")]
        event = f"on_{role}_complete"
    else:
        # Back to the gate this dispatch was accepted from — the place the
        # human left the slice, and the only description of it that stays true
        # when a run dies. FAILED survives as the fallback for a machine that
        # declares no edge home: a document stranded at an in-progress status
        # is worse than one in a state nothing writes any more.
        candidates = [gate_status, FAILED_STATUS]
        event = f"on_{role}_failed"
    candidates = [status for status in candidates if status]

    status_applied = False
    new_status = candidates[0] if candidates else None
    for candidate in candidates:
        if update_frontmatter_status(
            target_file,
            candidate,
            state_machine["valid_statuses"],
            state_machine["transitions"],
        ):
            status_applied = True
            new_status = candidate
            break
        _log_and_print(
            log_file,
            f"[runner] ERROR: could not set status to '{candidate}' for "
            f"{target_file} (illegal transition, missing file, or "
            f"unparsable frontmatter).",
        )
    if candidates and not status_applied:
        _log_and_print(
            log_file,
            f"[runner] ERROR: none of {candidates} could be applied to "
            f"{target_file} — the slice's on-disk status was NOT updated and "
            f"does not reflect this outcome.",
        )
    if not candidates:
        _log_and_print(
            log_file,
            f"[runner] WARNING: agent '{role}' declares no status for this "
            f"outcome; the slice's on-disk status was not updated.",
        )

    # Only claim the transition happened if it actually did — the ERROR/
    # WARNING above already explains why, when it didn't.
    status_summary = f"status -> {new_status}" if status_applied else "status UNCHANGED"
    _log_and_print(
        log_file, f"[runner] {role} exited {exit_code}; {status_summary}; log: {log_file}"
    )

    try:
        run_infrastructure_hook(
            event,
            project_root=project_root,
            known_events=canonical_events(config.get("agents", {})),
        )
    except HookError as exc:
        # The agent's own outcome is already recorded; a failing completion
        # hook must not overwrite it.
        _log_and_print(log_file, f"[runner] completion hook failed: {exc}")

    # Reclamation hangs off the verdict, never off the exit code. The two
    # differ in exactly the case that cost a stack: a client that died over an
    # agent still using it. `VERDICT_UNKNOWN` never reaches here — it returned
    # above — so this is the settled-failure path only.
    if verdict == VERDICT_GATE and sandbox_branch:
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

    return verdict


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Supervise one dispatched agent.")
    parser.add_argument("--role", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--lock", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--sandbox-branch", default="")
    parser.add_argument("--base-ref", default="")
    parser.add_argument(
        "--gate-status", default="",
        help="The status the document sat at when the dispatch was accepted; "
             "where a failed run puts it back.",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("no agent command given after '--'")

    return run_supervised(
        role=args.role,
        target_file=Path(args.file),
        project_root=Path(args.project_root),
        lock_file=Path(args.lock),
        log_file=Path(args.log),
        argv=command,
        cwd=Path(args.cwd),
        sandbox_branch=args.sandbox_branch,
        base_ref=args.base_ref,
        gate_status=args.gate_status,
    )


if __name__ == "__main__":
    sys.exit(main())
