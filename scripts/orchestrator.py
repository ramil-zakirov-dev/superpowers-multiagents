#!/usr/bin/env python3
"""
superpowers-multiagents Orchestrator CLI
Manages YAML frontmatter state machine transitions, dependency checks,
Git Worktrees, conflict detection, and background OpenCode CLI subagents.
"""

import sys
import os
import re
import argparse
import subprocess
import tempfile
from pathlib import Path
from ruamel.yaml import YAML
import io

FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

VALID_STATUSES = [
    "DRAFT_SPEC",
    "SPEC_APPROVED",
    "PLANNING",
    "PLAN_GENERATED",
    "PLAN_APPROVED",
    "EXECUTING",
    "EXECUTION_COMPLETE",
    "MERGE_CONFLICT",
    "VERIFIED_CLOSED"
]

STATE_TRANSITIONS = {
    "DRAFT_SPEC": ["SPEC_APPROVED"],
    "SPEC_APPROVED": ["PLANNING", "DRAFT_SPEC"],
    "PLANNING": ["PLAN_GENERATED"],
    "PLAN_GENERATED": ["PLAN_APPROVED", "PLANNING"],
    "PLAN_APPROVED": ["EXECUTING", "PLAN_GENERATED"],
    "EXECUTING": ["EXECUTION_COMPLETE"],
    "EXECUTION_COMPLETE": ["VERIFIED_CLOSED", "EXECUTING"],
    "VERIFIED_CLOSED": [],
    "MERGE_CONFLICT": ["VERIFIED_CLOSED", "EXECUTING", "PLAN_APPROVED"]
}


def parse_frontmatter(content: str) -> dict:
    """Parses YAML frontmatter from a Markdown string using ruamel.yaml."""
    match = FRONTMATTER_PATTERN.match(content)
    if not match:
        return {}
    yaml = YAML(typ='rt')
    data = yaml.load(match.group(1))
    return dict(data) if data else {}


def update_frontmatter_status(filepath: Path, new_status: str) -> bool:
    """
    Safely updates the status field in a markdown file's YAML frontmatter.
    Enforces strict state transitions.
    Uses atomic file writing (.tmp + replace) to prevent race conditions.
    """
    if new_status not in VALID_STATUSES:
        print(f"Error: Invalid status '{new_status}'. Allowed: {VALID_STATUSES}")
        return False
    
    filepath = Path(filepath).resolve()
    if not filepath.exists():
        print(f"Error: File '{filepath}' does not exist.")
        return False

    try:
        content = filepath.read_text(encoding="utf-8")
        match = FRONTMATTER_PATTERN.match(content)
        
        yaml = YAML(typ='rt')
        yaml.preserve_quotes = True

        if match:
            yaml_text = match.group(1)
            data = yaml.load(yaml_text) or {}
            current_status = data.get("status", "UNKNOWN")
            
            # Strict transition check
            if current_status in STATE_TRANSITIONS and new_status not in STATE_TRANSITIONS[current_status] and current_status != new_status:
                print(f"Error: Invalid state transition from '{current_status}' to '{new_status}'.")
                return False

            data["status"] = new_status
            
            buf = io.StringIO()
            yaml.dump(data, buf)
            new_yaml_text = buf.getvalue().strip()
            
            new_content = f"---\n{new_yaml_text}\n---\n" + content[match.end():]
        else:
            print(f"Error: Invalid transition. No frontmatter found to transition to {new_status}.")
            return False

        parent_dir = filepath.parent
        with tempfile.NamedTemporaryFile("w", dir=parent_dir, delete=False, encoding="utf-8") as tf:
            tf.write(new_content)
            temp_name = tf.name

        os.replace(temp_name, filepath)
        print(f"Updated {filepath.name} status -> {new_status}")
        return True
    except Exception as e:
        print(f"Error updating frontmatter in {filepath}: {e}")
        return False


def load_project_hooks(project_root: Path = None) -> dict:
    """Loads .superpowers/hooks.yaml if present in the target project root."""
    if project_root is None:
        project_root = Path.cwd()
    
    hooks_file = project_root / ".superpowers" / "hooks.yaml"
    if not hooks_file.exists():
        return {}

    content = hooks_file.read_text(encoding="utf-8")
    yaml = YAML(typ='rt')
    parsed = yaml.load(content) or {}
    return parsed.get("hooks", {})


def run_infrastructure_hook(event_name: str, project_root: Path = None, current_env: dict = None) -> dict:
    """Executes project infrastructure hooks and captures environment variables."""
    if project_root is None:
        project_root = Path.cwd()
    if current_env is None:
        current_env = dict(os.environ)

    hooks = load_project_hooks(project_root)
    hook_cfg = hooks.get(event_name)

    if not hook_cfg or not isinstance(hook_cfg, dict):
        return current_env

    command = hook_cfg.get("command")
    if not command:
        return current_env

    capture_env = hook_cfg.get("capture_env", False)
    print(f"[Infrastructure Hook] Running '{event_name}': {command}")

    try:
        res = subprocess.run(
            command,
            shell=True,
            cwd=project_root,
            capture_output=True,
            text=True,
            env=current_env
        )

        if res.returncode != 0:
            print(f"[Infrastructure Hook] Error running '{event_name}' (exit code {res.returncode}):")
            print(res.stderr)
            sys.exit(res.returncode)

        print(f"[Infrastructure Hook] '{event_name}' completed successfully.")

        if capture_env and res.stdout:
            updated_env = dict(current_env)
            for line in res.stdout.splitlines():
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    updated_env[k] = v
            return updated_env

    except Exception as e:
        print(f"[Infrastructure Hook] Exception running '{event_name}': {e}")
        sys.exit(1)

    return current_env


def check_unmet_dependencies(spec_file: Path) -> list:
    """Verifies if any slice listed in depends_on is not yet VERIFIED_CLOSED."""
    if not spec_file.exists():
        return []

    content = spec_file.read_text(encoding="utf-8")
    fm = parse_frontmatter(content)
    depends_on = fm.get("depends_on", [])

    if isinstance(depends_on, str):
        depends_on = [depends_on]

    if not depends_on:
        return []

    unmet = []
    specs_dir = spec_file.parent

    for dep_id in depends_on:
        matching = list(specs_dir.glob(f"*{dep_id}*.md"))
        if not matching:
            unmet.append(f"{dep_id} (Spec not found)")
            continue
        
        dep_spec = matching[0]
        dep_fm = parse_frontmatter(dep_spec.read_text(encoding="utf-8"))
        dep_status = dep_fm.get("status", "UNKNOWN")

        if dep_status != "VERIFIED_CLOSED":
            unmet.append(f"{dep_id} (Status: {dep_status})")

    return unmet


def create_git_worktree(slice_id: str, project_root: Path = None) -> Path:
    """Creates an isolated git worktree for a slice under .worktrees/<slice_id>."""
    if project_root is None:
        project_root = Path.cwd()

    worktrees_dir = project_root / ".worktrees"
    worktree_path = worktrees_dir / slice_id
    branch_name = f"feat/{slice_id}"

    if worktree_path.exists():
        print(f"[Git Worktree] Worktree at '{worktree_path}' already exists.")
        return worktree_path

    worktrees_dir.mkdir(exist_ok=True)
    print(f"[Git Worktree] Creating worktree for branch '{branch_name}' at '{worktree_path}'...")

    res = subprocess.run(
        f"git worktree add -b {branch_name} \"{worktree_path}\" HEAD",
        shell=True,
        cwd=project_root,
        capture_output=True,
        text=True
    )

    if res.returncode != 0:
        res2 = subprocess.run(
            f"git worktree add \"{worktree_path}\" {branch_name}",
            shell=True,
            cwd=project_root,
            capture_output=True,
            text=True
        )
        if res2.returncode != 0:
            print(f"❌ [Git Worktree] Error: Could not create worktree:")
            print(res2.stderr)
            print("Aborting execution to prevent running in the main project branch.")
            sys.exit(1)

    return worktree_path


def merge_and_cleanup_worktree(slice_id: str, spec_file: Path = None, project_root: Path = None) -> bool:
    """Merges slice worktree branch into current branch. Handles merge conflicts safely."""
    if project_root is None:
        project_root = Path.cwd()

    branch_name = f"feat/{slice_id}"
    worktree_path = project_root / ".worktrees" / slice_id

    print(f"[Git Merge] Attempting to merge '{branch_name}' into current branch...")

    res = subprocess.run(
        f"git merge {branch_name}",
        shell=True,
        cwd=project_root,
        capture_output=True,
        text=True
    )

    if res.returncode != 0:
        print(f"❌ [Git Merge Conflict] Conflict detected while merging '{branch_name}':")
        print(res.stdout)
        print(res.stderr)
        
        if spec_file and spec_file.exists():
            update_frontmatter_status(spec_file, "MERGE_CONFLICT")

        print("Merge conflict halted. Opus 5 or Human must inspect and resolve conflict files before completing merge.")
        return False

    print(f"✅ [Git Merge] Successfully merged '{branch_name}'.")

    if worktree_path.exists():
        print(f"[Git Worktree] Removing worktree at '{worktree_path}'...")
        subprocess.run(
            f"git worktree remove --force \"{worktree_path}\"",
            shell=True,
            cwd=project_root,
            capture_output=True
        )

    return True


def cmd_status(args):
    """Scans and displays status of milestones, specs, and plans."""
    base_dir = Path(args.dir) if args.dir else Path("docs/superpowers")
    
    print("\n=======================================================")
    print("   SUPERPOWERS MULTI-AGENTS STATUS REPORT")
    print("   Hierarchy: Milestone -> Track -> Slice -> Plan")
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
            content = filepath.read_text(encoding="utf-8")
            data = parse_frontmatter(content)
            status = data.get("status", "UNKNOWN")
            title = data.get("title", filepath.stem)
            print(f"  [{status:<18}] {filepath.name} - {title}")
        print()


def cmd_set_status(args):
    """Manually sets frontmatter status and triggers hooks & merge cleanups."""
    filepath = Path(args.file)
    success = update_frontmatter_status(filepath, args.status)
    if success and args.status == "VERIFIED_CLOSED":
        project_root = filepath.parent.parent.parent
        fm = parse_frontmatter(filepath.read_text(encoding="utf-8"))
        slice_id = fm.get("slice_id", filepath.stem)
        
        merge_and_cleanup_worktree(slice_id, spec_file=filepath, project_root=project_root)
        run_infrastructure_hook("on_slice_verified_closed", project_root=project_root)


def cmd_trigger_hook(args):
    """Manually or programmatically triggers an infrastructure hook."""
    project_root = Path(args.dir) if args.dir else Path.cwd()
    run_infrastructure_hook(args.event, project_root=project_root)


def cmd_dispatch_planner(args):
    """Dispatches background OpenCode task for Kimi K3 planner after checking dependencies."""
    spec_file = Path(args.spec)
    if not spec_file.exists():
        print(f"Error: Spec file '{spec_file}' not found.")
        sys.exit(1)

    unmet = check_unmet_dependencies(spec_file)
    if unmet:
        print(f"❌ [Dependency Gate] Cannot dispatch planner for {spec_file.name}. Unmet dependencies:")
        for dep in unmet:
            print(f"   - {dep}")
        sys.exit(1)

    project_root = spec_file.parent.parent.parent
    env = run_infrastructure_hook("on_slice_planning_start", project_root=project_root)

    update_frontmatter_status(spec_file, "PLANNING")

    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    log_file = logs_dir / f"planner_{spec_file.stem}.log"

    opencode_cmd = (
        f"opencode run --model kimi-k3 "
        f"\"Read spec at {spec_file} and create detailed TDD implementation plan "
        f"using writing-plans skill. Save to docs/superpowers/plans/\""
    )

    # Chain trigger-hook for completion
    orchestrator_path = Path(__file__).resolve()
    chained_cmd = f"{opencode_cmd} && python \"{orchestrator_path}\" trigger-hook --event on_planning_complete --dir \"{project_root}\""

    print(f"Dispatching Kimi K3 Planner in background...")
    print(f"Log: {log_file}")

    if os.name == "nt":
        proc = subprocess.Popen(
            f"cmd.exe /c \"{chained_cmd} > {log_file} 2>&1\"",
            shell=True,
            env=env,
            creationflags=subprocess.CREATE_NEW_CONSOLE
        )
    else:
        proc = subprocess.Popen(
            f"nohup bash -c '{chained_cmd}' > {log_file} 2>&1 &",
            shell=True,
            env=env
        )

    print(f"Planner dispatched successfully. PID: {proc.pid}")


def cmd_dispatch_executor(args):
    """Dispatches background OpenCode task for Minimax M3 executor inside an isolated worktree."""
    plan_file = Path(args.plan)
    if not plan_file.exists():
        print(f"Error: Plan file '{plan_file}' not found.")
        sys.exit(1)

    project_root = plan_file.parent.parent.parent
    fm = parse_frontmatter(plan_file.read_text(encoding="utf-8"))
    slice_id = fm.get("slice_id", plan_file.stem)

    worktree_path = create_git_worktree(slice_id, project_root=project_root)
    env = run_infrastructure_hook("on_slice_execution_start", project_root=project_root)

    update_frontmatter_status(plan_file, "EXECUTING")

    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    log_file = logs_dir / f"executor_{plan_file.stem}.log"

    opencode_cmd = (
        f"opencode run --model minimax-m3 "
        f"\"Execute implementation plan at {plan_file} using TDD subagent execution. "
        f"Check off tasks in plan as completed.\""
    )

    # Chain trigger-hook for completion
    orchestrator_path = Path(__file__).resolve()
    chained_cmd = f"{opencode_cmd} && python \"{orchestrator_path}\" trigger-hook --event on_execution_complete --dir \"{project_root}\""

    print(f"Dispatching Minimax M3 Executor in background at worktree '{worktree_path}'...")
    print(f"Log: {log_file}")

    if os.name == "nt":
        proc = subprocess.Popen(
            f"cmd.exe /c \"{chained_cmd} > {log_file} 2>&1\"",
            shell=True,
            cwd=worktree_path,
            env=env,
            creationflags=subprocess.CREATE_NEW_CONSOLE
        )
    else:
        proc = subprocess.Popen(
            f"nohup bash -c '{chained_cmd}' > {log_file} 2>&1 &",
            shell=True,
            cwd=worktree_path,
            env=env
        )

    print(f"Executor dispatched successfully. PID: {proc.pid}")


def cmd_summary(args):
    """Extracts the final response from OpenCode execution log for Opus 5 audit."""
    slice_id = args.slice
    logs_dir = Path("logs")
    
    matching_logs = list(logs_dir.glob(f"*{slice_id}*.log"))
    if not matching_logs:
        print(f"No execution log found for slice '{slice_id}' in logs/")
        sys.exit(1)

    log_file = matching_logs[-1]
    content = log_file.read_text(encoding="utf-8", errors="ignore")

    print("\n=======================================================")
    print(f"   AUDIT SUMMARY FOR SLICE: {slice_id}")
    print(f"   Source Log: {log_file.name}")
    print("=======================================================\n")

    lines = content.splitlines()
    tail_lines = lines[-50:] if len(lines) > 50 else lines

    print("--- LAST EXECUTOR DIALOGUE / RESPONSE OUTPUT ---")
    print("\n".join(tail_lines))
    print("\n=======================================================\n")


def main():
    parser = argparse.ArgumentParser(description="Superpowers Multi-Agents Orchestrator")
    subparsers = parser.add_subparsers(dest="command")

    p_status = subparsers.add_parser("status", help="Show status of all milestones, specs, and plans")
    p_status.add_argument("--dir", default="docs/superpowers", help="Base superpowers directory")

    p_set = subparsers.add_parser("set-status", help="Set status of a markdown file")
    p_set.add_argument("--file", required=True, help="Path to markdown file")
    p_set.add_argument("--status", required=True, help="New status")

    p_trigger = subparsers.add_parser("trigger-hook", help="Trigger an infrastructure hook manually")
    p_trigger.add_argument("--event", required=True, help="Hook event name (e.g. on_execution_complete)")
    p_trigger.add_argument("--dir", default="", help="Project root directory")

    p_plan = subparsers.add_parser("dispatch-planner", help="Dispatch Kimi K3 planner for a spec")
    p_plan.add_argument("--spec", required=True, help="Path to design spec file")

    p_exec = subparsers.add_parser("dispatch-executor", help="Dispatch Minimax M3 executor for a plan")
    p_exec.add_argument("--plan", required=True, help="Path to plan file")

    p_sum = subparsers.add_parser("summary", help="Show execution summary log for Opus 5 audit")
    p_sum.add_argument("--slice", required=True, help="Slice ID or keyword")

    args = parser.parse_args()

    if args.command == "status":
        cmd_status(args)
    elif args.command == "set-status":
        cmd_set_status(args)
    elif args.command == "trigger-hook":
        cmd_trigger_hook(args)
    elif args.command == "dispatch-planner":
        cmd_dispatch_planner(args)
    elif args.command == "dispatch-executor":
        cmd_dispatch_executor(args)
    elif args.command == "summary":
        cmd_summary(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
