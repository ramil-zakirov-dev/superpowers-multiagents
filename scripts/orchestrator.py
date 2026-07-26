#!/usr/bin/env python3
"""
superpowers-multiagents Orchestrator CLI

Thin entry point that wires together the modular components:
config, frontmatter, adapters, git_ops, hooks, locks, dependencies.
"""

import sys
import os
import json
import argparse
import subprocess
from pathlib import Path

from scripts.config import load_agent_config, DEFAULT_CONFIG
from scripts.frontmatter import parse_frontmatter, update_frontmatter_status
from scripts.utils import find_project_root
from scripts.adapters import get_harness_adapter
from scripts.git_ops import create_git_worktree, merge_and_cleanup_worktree
from scripts.hooks import load_project_hooks, run_infrastructure_hook
from scripts.locks import acquire_slice_lock, release_slice_lock
from scripts.dependencies import check_unmet_dependencies


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
    """Generic agent dispatcher — handles any role defined in agents.yaml."""
    target_file = Path(args.file)
    if not target_file.exists():
        print(f"Error: Target file '{target_file}' not found.")
        sys.exit(1)

    role = args.role
    project_root = find_project_root(target_file)
    config = load_agent_config(project_root)
    sm = config["state_machine"]

    agent_config = config.get("agents", {}).get(role)
    if not agent_config:
        print(f"Error: Agent role '{role}' is not defined in the configuration.")
        sys.exit(1)

    # Allow runtime model override
    if hasattr(args, "model") and args.model:
        agent_config["model"] = args.model

    # Dependency gate
    unmet = check_unmet_dependencies(target_file)
    if unmet:
        print(f"❌ [Dependency Gate] Cannot dispatch {role} for {target_file.name}. Unmet dependencies:")
        for dep in unmet:
            print(f"   - {dep}")
        sys.exit(1)

    # State validation
    fm = parse_frontmatter(target_file.read_text(encoding="utf-8"))
    slice_id = fm.get("slice_id", target_file.stem)
    current_status = fm.get("status", "UNKNOWN")

    allowed_statuses = agent_config.get("allowed_statuses", [])
    if allowed_statuses and current_status not in allowed_statuses:
        print(f"❌ [State Validation] Cannot dispatch {role} for {target_file.name}.")
        print(f"   Current status is '{current_status}', but {role} requires one of: {allowed_statuses}")
        sys.exit(1)

    # Concurrency lock
    lock_file = acquire_slice_lock(slice_id, project_root)

    # Worktree isolation
    if agent_config.get("isolated_worktree", False):
        cwd = create_git_worktree(slice_id, project_root)
    else:
        cwd = project_root

    # Transition to in-progress status
    in_progress_status = agent_config.get("in_progress_status")
    if in_progress_status:
        update_frontmatter_status(
            target_file, in_progress_status,
            sm["valid_statuses"], sm["transitions"],
        )

    # Infrastructure hook
    env = run_infrastructure_hook(f"on_slice_{role}_start", project_root=project_root)

    # Build command via adapter
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    log_file = logs_dir / f"{role}_{target_file.stem}.log"

    template = agent_config.get("prompt_template", "Process {file}")
    task_prompt = template.format(file=target_file.resolve())

    adapter = get_harness_adapter(agent_config, project_root)
    base_cmd = adapter.build_command(agent_config, task_prompt)

    # Chain completion hook
    orchestrator_path = Path(__file__).resolve()
    chained_cmd = (
        f'{base_cmd} && python "{orchestrator_path}" '
        f'trigger-hook --event on_{role}_complete --dir "{project_root}"'
    )

    print(f"Dispatching {agent_config.get('model')} {role.capitalize()} in background...")
    print(f"Log: {log_file}")

    if os.name == "nt":
        proc = subprocess.Popen(
            f'cmd.exe /c "{chained_cmd} > {log_file} 2>&1"',
            shell=True, cwd=cwd, env=env,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    else:
        proc = subprocess.Popen(
            f"nohup bash -c '{chained_cmd}' > {log_file} 2>&1 &",
            shell=True, cwd=cwd, env=env,
        )

    # Update lock with worker metadata
    lock_data = json.loads(lock_file.read_text(encoding="utf-8"))
    lock_data["worker_pid"] = proc.pid
    lock_data["model"] = agent_config.get("model")
    lock_data["role"] = role
    lock_file.write_text(json.dumps(lock_data, indent=2), encoding="utf-8")

    print(f"{role.capitalize()} dispatched successfully. PID: {proc.pid}")


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
