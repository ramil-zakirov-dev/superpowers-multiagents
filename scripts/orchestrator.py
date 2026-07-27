#!/usr/bin/env python3
"""
superpowers-multiagents Orchestrator CLI

Thin entry point that wires together the modular components:
config, frontmatter, adapters, git_ops, hooks, locks, dependencies.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):  # invoked as a script rather than `-m`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.adapters import get_harness_adapter
from scripts.config import load_agent_config, resolve_agent, validate_config
from scripts.dependencies import check_unmet_dependencies
from scripts.errors import OrchestratorError
from scripts.frontmatter import parse_frontmatter, update_frontmatter_status
from scripts.git_ops import create_git_worktree, current_branch, merge_and_cleanup_worktree
from scripts.hooks import canonical_events, run_infrastructure_hook
from scripts.locks import acquire_slice_lock, release_slice_lock_file
from scripts.paths import ARTIFACT_PREFIXES, log_path, logs_dir
from scripts import sandbox
from scripts.utils import find_project_root

#: Root of this plugin — the supervisor is spawned with this as its cwd so
#: that `python -m scripts.runner` resolves regardless of the user's cwd.
PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _warn_if_artifacts_not_ignored(project_root: Path) -> None:
    """Suggest ignoring our runtime paths, without touching the user's file."""
    result = subprocess.run(
        ["git", "check-ignore", *ARTIFACT_PREFIXES],
        cwd=project_root, capture_output=True, text=True,
    )
    ignored = set(result.stdout.split())
    missing = [p for p in ARTIFACT_PREFIXES if p not in ignored and p.rstrip("/") not in ignored]
    if missing:
        print(
            "Hint: consider adding these to .gitignore so they stay out of your diffs: "
            + " ".join(missing)
        )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def cmd_status(args):
    """Scans and displays status of milestones, specs, and plans."""
    base_dir = Path(args.dir) if args.dir else Path("docs/superpowers")

    print("\n=======================================================")
    print("   SUPERPOWERS MULTI-AGENTS STATUS REPORT")
    print("=======================================================\n")

    for folder_name in ["milestones", "specs", "plans"]:
        folder_path = base_dir / folder_name
        print(f"--- {folder_name.upper()} ({folder_path}) ---")
        if not folder_path.exists():
            print("  (Directory not found)\n")
            continue

        md_files = sorted(list(folder_path.glob("*.md")))
        if not md_files:
            print("  (No files found)\n")
            continue

        for filepath in md_files:
            data = parse_frontmatter(filepath.read_text(encoding="utf-8"))
            status = data.get("status", "UNKNOWN")
            title = data.get("title", filepath.stem)
            print(f"  [{status:<18}] {filepath.name} - {title}")
        print()


def cmd_set_status(args):
    """Set a slice's status. VERIFIED_CLOSED merges first, then marks.

    The order matters: marking VERIFIED_CLOSED first makes the state terminal,
    after which a merge conflict cannot be recorded at all.
    """
    filepath = Path(args.file).resolve()
    project_root = find_project_root(filepath)

    try:
        config = load_agent_config(project_root)
        validate_config(config)
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    state_machine = config["state_machine"]
    valid_statuses = state_machine["valid_statuses"]
    transitions = state_machine["transitions"]

    if args.status != "VERIFIED_CLOSED":
        if not update_frontmatter_status(filepath, args.status, valid_statuses, transitions):
            sys.exit(1)
        return

    frontmatter = parse_frontmatter(filepath.read_text(encoding="utf-8"))
    slice_id = frontmatter.get("slice_id", filepath.stem)
    current_status = frontmatter.get("status", "UNKNOWN")

    # Check legality BEFORE the merge: merge_and_cleanup_worktree performs an
    # irreversible git merge and force-deletes the worktree. Discovering only
    # afterward that the transition was illegal would leave the branch merged,
    # the worktree gone, and the command reporting failure while the on-disk
    # status silently stayed put -- exactly the "mutation before the fallible
    # check" ordering this slice exists to eliminate everywhere else.
    if "VERIFIED_CLOSED" not in (transitions.get(current_status) or []):
        print(f"Error: Invalid state transition from '{current_status}' to 'VERIFIED_CLOSED'.")
        sys.exit(1)

    try:
        merged = merge_and_cleanup_worktree(slice_id, project_root)
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    if not merged:
        if not update_frontmatter_status(filepath, "MERGE_CONFLICT", valid_statuses, transitions):
            print(
                f"Error: merge conflicted on 'feat/{slice_id}', but the slice could "
                f"not be marked MERGE_CONFLICT from '{current_status}'. Resolve the "
                f"git conflict by hand; the on-disk status was not changed."
            )
            sys.exit(1)
        print(f"Merge conflict on 'feat/{slice_id}'. Slice marked MERGE_CONFLICT.")
        print("Resolve the conflict, commit, then set VERIFIED_CLOSED again.")
        sys.exit(1)

    if not update_frontmatter_status(filepath, "VERIFIED_CLOSED", valid_statuses, transitions):
        sys.exit(1)

    try:
        run_infrastructure_hook(
            "on_slice_verified_closed",
            project_root=project_root,
            known_events=canonical_events(config.get("agents", {})),
        )
    except OrchestratorError as exc:
        # The merge and the status write already succeeded; a failing
        # post-merge hook must not be reported as if the merge itself failed.
        print(f"Warning: on_slice_verified_closed hook failed: {exc}")

    mode = (
        ((config.get("sandbox") or {}).get("teardown") or {})
        .get("on_verified_closed", "volumes")
    )
    try:
        sandbox.tear_down(f"feat/{slice_id}", project_root, config, mode)
    except OrchestratorError as exc:
        print(f"Warning: sandbox teardown failed: {exc}")


def cmd_trigger_hook(args):
    """Manually or programmatically triggers an infrastructure hook."""
    project_root = Path(args.dir) if args.dir else Path.cwd()

    try:
        config = load_agent_config(project_root)
        validate_config(config)
        run_infrastructure_hook(
            args.event,
            project_root=project_root,
            known_events=canonical_events(config.get("agents", {})),
        )
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(1)


def cmd_dispatch_agent(args):
    """Dispatch an agent by role.

    Ordering is load-bearing: every step that can fail runs before the first
    irreversible mutation, so a failed precondition never leaves a slice that
    has to be repaired by hand.
    """
    target_file = Path(args.file).resolve()
    if not target_file.exists():
        print(f"Error: Target file '{target_file}' not found.")
        sys.exit(1)

    role = args.role
    project_root = find_project_root(target_file)

    # 1. Configuration
    try:
        config = load_agent_config(project_root)
        validate_config(config)
        agent_config = resolve_agent(config, role)
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    state_machine = config["state_machine"]
    known_events = canonical_events(config.get("agents", {}))

    if getattr(args, "model", None):
        agent_config["model"] = args.model

    # 2. Gates
    unmet = check_unmet_dependencies(target_file)
    if unmet:
        print(f"[Dependency Gate] Cannot dispatch {role} for {target_file.name}. Unmet:")
        for dependency in unmet:
            print(f"   - {dependency}")
        sys.exit(1)

    frontmatter = parse_frontmatter(target_file.read_text(encoding="utf-8"))
    slice_id = frontmatter.get("slice_id", target_file.stem)
    current_status = frontmatter.get("status", "UNKNOWN")

    allowed_statuses = agent_config.get("allowed_statuses") or []
    if allowed_statuses and current_status not in allowed_statuses:
        print(f"[State Validation] Cannot dispatch {role} for {target_file.name}.")
        print(f"   Current status is '{current_status}'; {role} requires one of: {allowed_statuses}")
        sys.exit(1)

    # 3. Lock
    try:
        lock_file = acquire_slice_lock(slice_id, project_root)
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    # 4-5. Fallible side effects, before any mutation we would have to undo.
    # Adapter resolution belongs here too, not after step 6: an unknown
    # harness or a missing custom-adapter file is exactly as fallible as a
    # failing hook, and discovering it only after the status write would
    # strand the slice at in_progress_status with no legal way back.
    log_file = log_path(project_root, role, target_file.stem)
    prompt_template = agent_config.get("prompt_template", "Process {file}")
    task_prompt = prompt_template.format(file=target_file)

    try:
        if agent_config.get("isolated_worktree", False):
            cwd = create_git_worktree(slice_id, project_root)
            sandbox_branch = f"feat/{slice_id}"
            sandbox_env = sandbox.ensure_up(sandbox_branch, project_root, config)
        else:
            cwd = project_root
            sandbox_branch = current_branch(project_root)
            sandbox_env = sandbox.resolve_env(sandbox_branch, project_root, config)

        env = dict(os.environ)
        env.update(sandbox_env)
        env.update({
            "SUPERPOWERS_SLICE_ID": slice_id,
            "SUPERPOWERS_SLICE_BRANCH": sandbox_branch,
            "SUPERPOWERS_WORKTREE": str(cwd),
        })

        # The start hook runs after the worktree and the sandbox exist, so a
        # project hook can act on both. This is a deliberate change from
        # 2.0.0, where it ran first and could observe neither.
        env = run_infrastructure_hook(
            f"on_slice_{role}_start", project_root=project_root,
            current_env=env, known_events=known_events,
        )
        adapter = get_harness_adapter(agent_config, project_root)
        agent_argv = adapter.build_command(agent_config, task_prompt)
    except OrchestratorError as exc:
        release_slice_lock_file(lock_file)
        print(f"Error: {exc}")
        print(f"Slice '{slice_id}' left untouched at status '{current_status}'.")
        sys.exit(1)

    # 6. First irreversible mutation
    in_progress_status = agent_config.get("in_progress_status")
    if in_progress_status:
        applied = update_frontmatter_status(
            target_file, in_progress_status,
            state_machine["valid_statuses"], state_machine["transitions"],
        )
        if not applied:
            release_slice_lock_file(lock_file)
            print(
                f"Error: could not transition '{slice_id}' from "
                f"'{current_status}' to '{in_progress_status}'."
            )
            print(f"Slice '{slice_id}' left untouched at status '{current_status}'.")
            sys.exit(1)

    # 7. Spawn the supervisor
    logs_dir(project_root).mkdir(parents=True, exist_ok=True)

    runner_argv = [
        sys.executable, "-m", "scripts.runner",
        "--role", role,
        "--file", str(target_file),
        "--project-root", str(project_root),
        "--lock", str(lock_file),
        "--log", str(log_file),
        "--cwd", str(cwd),
        "--sandbox-branch", sandbox_branch,
        "--", *[str(part) for part in agent_argv],
    ]

    spawn_kwargs = {"cwd": str(PLUGIN_ROOT), "env": env}
    if os.name == "nt":
        spawn_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        spawn_kwargs["start_new_session"] = True

    process = subprocess.Popen(runner_argv, **spawn_kwargs)

    print(f"Dispatched {agent_config.get('model')} as {role} (supervisor PID {process.pid}).")
    print(f"Log: {log_file}")
    _warn_if_artifacts_not_ignored(project_root)


def _quote_posix(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_\-./:=]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _quote_powershell(value: str) -> str:
    escaped = value.replace("`", "``").replace("$", "`$").replace('"', '`"')
    return escaped


def cmd_sandbox(args):
    """Human-facing sandbox lifecycle. The orchestrator uses the module directly."""
    # `cmd` is `nargs=argparse.REMAINDER`, needed so `sandbox exec -- cmd --flag`
    # can pass arbitrary flags through to the wrapped command untouched. But
    # REMAINDER is greedy: for every other action, anything landing here means
    # a flag was placed after the action and got silently swallowed instead of
    # parsed (e.g. `sandbox status --dir X` -> action='status', cmd=['--dir',
    # 'X'], dir=''). Fail closed instead of quietly running with the wrong
    # (default) config.
    if args.action != "exec" and args.cmd:
        print(
            f"Error: unexpected extra argument(s) after '{args.action}': {args.cmd}\n"
            f"Flags like --dir/--branch/--shell/--yes must come BEFORE the action:\n"
            f"  sandbox --dir X {args.action}   (not: sandbox {args.action} --dir X)"
        )
        sys.exit(1)

    project_root = Path(args.dir).resolve() if args.dir else Path.cwd()

    try:
        config = load_agent_config(project_root)
        validate_config(config)
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    branch = args.branch or current_branch(project_root)

    try:
        if args.action == "status":
            rows = sandbox.status_rows(project_root, config)
            if not rows:
                print("No sandbox stacks are tracked.")
            for name, ip, state in rows:
                print(f"{name:<48} {ip:<12} {state}")
            return

        if args.action in ("up", "restart"):
            if args.action == "restart":
                sandbox.tear_down(branch, project_root, config, "containers")
            env = sandbox.ensure_up(branch, project_root, config)
            print(f"Stack for {branch} is up on {env['LOOPBACK_IP']}.")
            return

        if args.action == "teardown":
            mode = "volumes" if args.yes else "containers"
            if not args.yes:
                print(
                    f"Refusing to destroy volumes for {branch} without --yes. "
                    f"Stopping containers only would be `restart`; re-run with "
                    f"--yes to destroy data."
                )
                sys.exit(2)
            sandbox.tear_down(branch, project_root, config, mode)
            print(f"Stack for {branch} torn down ({mode}).")
            return

        env = sandbox.resolve_env(branch, project_root, config)
        if not env:
            print(f"No sandbox state for branch {branch}; run `sandbox up` first.")
            sys.exit(1)

        if args.action == "env":
            if args.shell == "json":
                print(json.dumps(env, indent=2))
            elif args.shell == "powershell":
                for key, value in env.items():
                    print(f'$env:{key} = "{_quote_powershell(value)}"')
            else:
                for key, value in env.items():
                    print(f"export {key}={_quote_posix(value)}")
            return

        if args.action == "exec":
            command = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
            if not command:
                print("Error: `sandbox exec` needs a command after `--`.")
                sys.exit(1)
            sys.exit(subprocess.run(
                command, cwd=str(project_root), env={**os.environ, **env}
            ).returncode)
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(1)


def cmd_summary(args):
    """Print the tail of an execution log for audit."""
    project_root = Path(args.dir).resolve() if args.dir else Path.cwd()
    directory = logs_dir(project_root)

    matching = sorted(directory.glob(f"*{args.slice}*.log")) if directory.exists() else []
    if not matching:
        print(f"No execution log for slice '{args.slice}' in {directory}")
        sys.exit(1)

    log_file = max(matching, key=lambda path: path.stat().st_mtime)
    lines = log_file.read_text(encoding="utf-8", errors="ignore").splitlines()

    print(f"\n--- LAST 50 LINES OF {log_file.name} ---")
    print("\n".join(lines[-50:]))


# ---------------------------------------------------------------------------
# CLI argument parser
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Superpowers Multi-Agents Orchestrator"
    )
    subparsers = parser.add_subparsers(dest="command")

    # status
    p_status = subparsers.add_parser("status", help="Show status of all milestones, specs, and plans")
    p_status.add_argument("--dir", default="docs/superpowers", help="Base superpowers directory")

    # set-status
    p_set = subparsers.add_parser("set-status", help="Set status of a markdown file")
    p_set.add_argument("--file", required=True, help="Path to markdown file")
    p_set.add_argument("--status", required=True, help="New status")

    # trigger-hook
    p_trigger = subparsers.add_parser("trigger-hook", help="Trigger an infrastructure hook manually")
    p_trigger.add_argument("--event", required=True, help="Hook event name")
    p_trigger.add_argument("--dir", default="", help="Project root directory")

    # dispatch-agent (generic)
    p_agent = subparsers.add_parser("dispatch-agent", help="Dispatch an agent by role")
    p_agent.add_argument("--role", required=True, help="Agent role (e.g., planner, executor)")
    p_agent.add_argument("--file", required=True, help="Path to target markdown file")
    p_agent.add_argument("--model", help="Override LLM model")

    # dispatch-planner (backward-compat alias)
    p_plan = subparsers.add_parser("dispatch-planner", help="[Alias] Dispatch planner for a spec")
    p_plan.add_argument("--spec", required=True, help="Path to design spec file")
    p_plan.add_argument("--model", help="Override LLM model")

    # dispatch-executor (backward-compat alias)
    p_exec = subparsers.add_parser("dispatch-executor", help="[Alias] Dispatch executor for a plan")
    p_exec.add_argument("--plan", required=True, help="Path to plan file")
    p_exec.add_argument("--model", help="Override LLM model")

    # summary
    p_sum = subparsers.add_parser("summary", help="Show execution summary log for audit")
    p_sum.add_argument("--slice", required=True, help="Slice ID or keyword")
    p_sum.add_argument("--dir", default="", help="Project root directory (default: cwd)")

    # sandbox
    p_sandbox = subparsers.add_parser("sandbox", help="Per-slice infrastructure sandbox")
    p_sandbox.add_argument(
        "action", choices=["up", "restart", "status", "env", "exec", "teardown"]
    )
    p_sandbox.add_argument("--dir", default="", help="Project root (default: cwd)")
    p_sandbox.add_argument("--branch", default="", help="Branch (default: current)")
    p_sandbox.add_argument(
        "--shell", default="posix", choices=["posix", "powershell", "json"],
        help="Output format for `env`",
    )
    p_sandbox.add_argument("--yes", action="store_true", help="Confirm volume destruction")
    p_sandbox.add_argument("cmd", nargs=argparse.REMAINDER, help="Command for `exec`")

    args = parser.parse_args()

    if args.command == "status":
        cmd_status(args)
    elif args.command == "set-status":
        cmd_set_status(args)
    elif args.command == "trigger-hook":
        cmd_trigger_hook(args)
    elif args.command == "dispatch-planner":
        args.role = "planner"
        args.file = args.spec
        cmd_dispatch_agent(args)
    elif args.command == "dispatch-executor":
        args.role = "executor"
        args.file = args.plan
        cmd_dispatch_agent(args)
    elif args.command == "dispatch-agent":
        cmd_dispatch_agent(args)
    elif args.command == "summary":
        cmd_summary(args)
    elif args.command == "sandbox":
        cmd_sandbox(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
