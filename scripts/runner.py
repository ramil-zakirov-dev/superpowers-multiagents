#!/usr/bin/env python3
"""Supervisor for a single dispatched agent.

This process is the background job. It owns one agent from spawn to terminal
status: it captures the agent's output, derives the slice's next status from
the child's exit code, fires the completion hook, and releases the lock on
every exit path.

Deriving status from an exit code is the point. Previously the agent was
asked, in its prompt, to set its own terminal status — so an agent that
crashed or simply forgot left the slice stranded with no way back.
"""

import argparse
import datetime
import os
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):  # invoked as a script rather than `-m`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import produced, sandbox
from scripts.config import load_agent_config, resolve_agent, validate_config
from scripts.errors import ConfigError, HookError, OrchestratorError
from scripts.git_ops import commits_since
from scripts.frontmatter import parse_frontmatter, update_frontmatter_status
from scripts.hooks import canonical_events, run_infrastructure_hook
from scripts.locks import claim_slice_lock, release_slice_lock_file

FAILED_STATUS = "FAILED"

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
    try:
        exit_code = _run_child(argv, cwd, log_file)
        _record_outcome(
            role, target_file, project_root, exit_code, log_file, sandbox_branch,
            base_ref, gate_status,
        )
        return exit_code
    finally:
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
            f"[runner] ERROR: '{role}' exited 0 but git could not count commits on "
            f"{branch} since {base_ref[:12]} — the branch may be gone. Nothing "
            f"observed the work landing, so returning the slice to its gate rather "
            f"than certifying it."
        )
    if count > 0:
        return ""
    return (
        f"[runner] ERROR: '{role}' exited 0 but left no commits on {branch} "
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


def _record_outcome(
    role: str,
    target_file: Path,
    project_root: Path,
    exit_code: int,
    log_file: Path,
    sandbox_branch: str = "",
    base_ref: str = "",
    gate_status: str = "",
) -> None:
    """Advance the slice status and fire the completion hook."""
    try:
        config = load_agent_config(project_root)
        validate_config(config)
        agent = resolve_agent(config, role)
    except OrchestratorError as exc:
        _log_and_print(
            log_file, f"[runner] configuration unusable, cannot record outcome: {exc}"
        )
        return

    state_machine = config["state_machine"]
    missing = ""
    if exit_code == 0:
        missing = _unmet_postcondition(
            role, agent, target_file, log_file, project_root, base_ref
        )
        if not missing:
            missing = _promote_produced(
                role, agent, target_file, log_file, state_machine
            )
        if missing:
            _log_and_print(log_file, missing)

    if exit_code == 0 and not missing:
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

    if exit_code != 0 and sandbox_branch:
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
