#!/usr/bin/env python3
"""
superpowers-multiagents Orchestrator CLI

Thin entry point that wires together the modular components:
config, frontmatter, adapters, git_ops, hooks, locks, dependencies.
"""

import argparse
import os
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
from scripts.git_ops import create_git_worktree, merge_and_cleanup_worktree
from scripts.hooks import canonical_events, run_infrastructure_hook
from scripts.locks import acquire_slice_lock, release_slice_lock_file
from scripts.paths import ARTIFACT_PREFIXES, log_path, logs_dir
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
    """Manually sets frontmatter status and triggers hooks & merge cleanups."""
    filepath = Path(args.file)
    project_root = find_project_root(filepath)
    config = load_agent_config(project_root)

    sm = config["state_machine"]
    success = update_frontmatter_status(
        filepath, args.status, sm["valid_statuses"], sm["transitions"]
    )

    if success and args.status == "VERIFIED_CLOSED":
        fm = parse_frontmatter(filepath.read_text(encoding="utf-8"))
        slice_id = fm.get("slice_id", filepath.stem)

        def _update_status(fp, st):
            update_frontmatter_status(fp, st, sm["valid_statuses"], sm["transitions"])

        merge_and_cleanup_worktree(
            slice_id, project_root,
            spec_file=filepath, update_status_fn=_update_status,
        )
        run_infrastructure_hook("on_slice_verified_closed", project_root=project_root)


def cmd_trigger_hook(args):
    """Manually or programmatically triggers an infrastructure hook."""
    project_root = Path(args.dir) if args.dir else Path.cwd()
    run_infrastructure_hook(args.event, project_root=project_root)


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

    # 4-5. Fallible side effects, before any mutation we would have to undo
    try:
        env = run_infrastructure_hook(
            f"on_slice_{role}_start", project_root=project_root, known_events=known_events
        )
        if agent_config.get("isolated_worktree", False):
            cwd = create_git_worktree(slice_id, project_root)
        else:
            cwd = project_root
    except OrchestratorError as exc:
        release_slice_lock_file(lock_file)
        print(f"Error: {exc}")
        print(f"Slice '{slice_id}' left untouched at status '{current_status}'.")
        sys.exit(1)

    # 6. First irreversible mutation
    in_progress_status = agent_config.get("in_progress_status")
    if in_progress_status:
        update_frontmatter_status(
            target_file, in_progress_status,
            state_machine["valid_statuses"], state_machine["transitions"],
        )

    # 7. Spawn the supervisor
    log_file = log_path(project_root, role, target_file.stem)
    logs_dir(project_root).mkdir(parents=True, exist_ok=True)

    prompt_template = agent_config.get("prompt_template", "Process {file}")
    task_prompt = prompt_template.format(file=target_file)

    try:
        adapter = get_harness_adapter(agent_config, project_root)
        agent_argv = adapter.build_command(agent_config, task_prompt)
    except OrchestratorError as exc:
        release_slice_lock_file(lock_file)
        print(f"Error: {exc}")
        sys.exit(1)

    runner_argv = [
        sys.executable, "-m", "scripts.runner",
        "--role", role,
        "--file", str(target_file),
        "--project-root", str(project_root),
        "--lock", str(lock_file),
        "--log", str(log_file),
        "--cwd", str(cwd),
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


def cmd_summary(args):
    """Extracts the tail of an execution log for audit."""
    slice_id = args.slice
    logs_dir = Path("logs")
    matching_logs = list(logs_dir.glob(f"*{slice_id}*.log"))
    if not matching_logs:
        print(f"No execution log found for slice '{slice_id}' in logs/")
        sys.exit(1)

    log_file = matching_logs[-1]
    content = log_file.read_text(encoding="utf-8", errors="ignore")

    print(f"\n--- LAST DIALOGUE FOR {slice_id} ---")
    lines = content.splitlines()
    print("\n".join(lines[-50:] if len(lines) > 50 else lines))


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
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
