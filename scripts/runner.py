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
import os
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):  # invoked as a script rather than `-m`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.config import load_agent_config, resolve_agent, validate_config
from scripts.errors import HookError, OrchestratorError
from scripts.frontmatter import update_frontmatter_status
from scripts.hooks import canonical_events, run_infrastructure_hook
from scripts.locks import claim_slice_lock, release_slice_lock_file

FAILED_STATUS = "FAILED"


def run_supervised(
    role: str,
    target_file: Path,
    project_root: Path,
    lock_file: Path,
    log_file: Path,
    argv: list,
    cwd: Path,
) -> int:
    """Run one agent to completion and record the outcome. Returns its exit code."""
    target_file = Path(target_file)
    project_root = Path(project_root)
    log_file = Path(log_file)

    claim_slice_lock(lock_file, os.getpid(), role=role)
    try:
        exit_code = _run_child(argv, cwd, log_file)
        _record_outcome(role, target_file, project_root, exit_code, log_file)
        return exit_code
    finally:
        release_slice_lock_file(lock_file)


def _run_child(argv: list, cwd: Path, log_file: Path) -> int:
    """Spawn the agent with both streams captured.

    A failure to even create/open the log file is treated as an outcome
    (synthesized non-zero exit), not an exception — letting it propagate
    would skip _record_outcome entirely and strand the slice at its
    in-progress status with no way back.
    """
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handle = open(log_file, "w", encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"[runner] could not create/open log file {log_file}: {exc}")
        return 127

    with handle as log:
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


def _log_and_print(log_file: Path, message: str) -> None:
    """Print a diagnostic AND append it to the log file.

    By the time this runs, the runner is typically a detached background
    job (Task 11 spawns it with no attached console) — stdout is a closed
    handle or the null device. The log file is the one artifact the
    dispatcher tells the operator to inspect, so outcome diagnostics belong
    there too, not only on a stream nobody is reading. Best-effort: a
    log-append failure here must not mask the outcome already recorded.
    """
    print(message)
    try:
        with open(log_file, "a", encoding="utf-8", errors="replace") as log:
            log.write(message + "\n")
    except OSError:
        pass


def _record_outcome(
    role: str, target_file: Path, project_root: Path, exit_code: int, log_file: Path
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
    if exit_code == 0:
        new_status = agent.get("success_status")
        event = f"on_{role}_complete"
    else:
        new_status = FAILED_STATUS
        event = f"on_{role}_failed"

    if new_status:
        updated = update_frontmatter_status(
            target_file,
            new_status,
            state_machine["valid_statuses"],
            state_machine["transitions"],
        )
        if not updated:
            _log_and_print(
                log_file,
                f"[runner] ERROR: could not set status to '{new_status}' for "
                f"{target_file} (illegal transition, missing file, or "
                f"unparsable frontmatter) — the slice's on-disk status was "
                f"NOT updated and does not reflect this outcome.",
            )
    else:
        _log_and_print(
            log_file,
            f"[runner] WARNING: agent '{role}' has no success_status configured; "
            f"the slice's on-disk status was not updated.",
        )

    _log_and_print(
        log_file, f"[runner] {role} exited {exit_code}; status -> {new_status}; log: {log_file}"
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Supervise one dispatched agent.")
    parser.add_argument("--role", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--lock", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--cwd", required=True)
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
    )


if __name__ == "__main__":
    sys.exit(main())
