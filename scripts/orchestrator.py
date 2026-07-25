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
import logging
import json
import atexit
from pathlib import Path
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
import io

logger = logging.getLogger("orchestrator")

# BOM-tolerant frontmatter pattern: strips optional UTF-8 BOM before ---
FRONTMATTER_PATTERN = re.compile(r"^\ufeff?---\s*\n(.*?)\n---\s*\n", re.DOTALL)

DEFAULT_PLANNER_MODEL = "kimi-k3"
DEFAULT_EXECUTOR_MODEL = "minimax-m3"

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
    "EXECUTING": ["EXECUTION_COMPLETE", "MERGE_CONFLICT"],
    "EXECUTION_COMPLETE": ["VERIFIED_CLOSED", "EXECUTING", "MERGE_CONFLICT"],
    "VERIFIED_CLOSED": [],
    "MERGE_CONFLICT": ["VERIFIED_CLOSED", "EXECUTING", "PLAN_APPROVED"]
}

# Regex for validating slice_id / branch names (alphanumeric, hyphens, underscores, dots)
SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def _sanitize_id(value: str, label: str = "ID") -> str:
    """Validates that a string is safe for use in shell commands and git branch names."""
    if not SAFE_ID_PATTERN.match(value):
        print(f"Error: {label} '{value}' contains invalid characters. "
              f"Only alphanumeric, hyphens, underscores, and dots are allowed.")
        sys.exit(1)
    return value


def _to_plain_dict(obj) -> dict | list | str | int | float | bool | None:
    """Recursively converts ruamel.yaml CommentedMap/CommentedSeq to plain Python types."""
    if isinstance(obj, CommentedMap):
        return {str(k): _to_plain_dict(v) for k, v in obj.items()}
    elif isinstance(obj, CommentedSeq):
        return [_to_plain_dict(item) for item in obj]
    elif isinstance(obj, list):
        return [_to_plain_dict(item) for item in obj]
    elif isinstance(obj, dict):
        return {str(k): _to_plain_dict(v) for k, v in obj.items()}
    return obj


def find_project_root(start_path: Path) -> Path:
    """
    Walks up the directory tree from start_path looking for a project root marker.
    Markers (in priority order): .superpowers/, .git/
    Falls back to start_path if no marker is found.
    """
    current = start_path.resolve()
    if current.is_file():
        current = current.parent

    for directory in [current, *current.parents]:
        if (directory / ".superpowers").is_dir():
            return directory
        if (directory / ".git").is_dir():
            return directory

    logger.warning(f"Could not find project root from '{start_path}', using '{current}'.")
    return current


def acquire_slice_lock(slice_id: str, project_root: Path) -> Path:
    """
    Acquires an exclusive lock for a slice to prevent concurrent dispatch.
    Creates .superpowers/locks/<slice_id>.lock with PID info.
    Returns the lock file path on success, exits on conflict.
    """
    _sanitize_id(slice_id, "slice_id")
    locks_dir = project_root / ".superpowers" / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_file = locks_dir / f"{slice_id}.lock"

    if lock_file.exists():
        try:
            lock_data = json.loads(lock_file.read_text(encoding="utf-8"))
            existing_pid = lock_data.get("pid")
            # Check if the process is still alive
            if existing_pid and _is_process_alive(existing_pid):
                print(f"❌ [Lock] Slice '{slice_id}' is already locked by PID {existing_pid} "
                      f"(command: {lock_data.get('command', 'unknown')}).")
                print(f"   Lock file: {lock_file}")
                print(f"   To force unlock: delete {lock_file}")
                sys.exit(1)
            else:
                print(f"[Lock] Stale lock found for '{slice_id}' (PID {existing_pid} is dead). Cleaning up.")
                lock_file.unlink()
        except (json.JSONDecodeError, KeyError):
            print(f"[Lock] Corrupt lock file for '{slice_id}'. Cleaning up.")
            lock_file.unlink()

    lock_data = {
        "pid": os.getpid(),
        "slice_id": slice_id,
        "command": " ".join(sys.argv),
    }
    lock_file.write_text(json.dumps(lock_data, indent=2), encoding="utf-8")
    print(f"[Lock] Acquired lock for slice '{slice_id}'.")
    return lock_file


def release_slice_lock(slice_id: str, project_root: Path) -> None:
    """Releases the lock for a slice."""
    lock_file = project_root / ".superpowers" / "locks" / f"{slice_id}.lock"
    if lock_file.exists():
        lock_file.unlink()
        print(f"[Lock] Released lock for slice '{slice_id}'.")


def _is_process_alive(pid: int) -> bool:
    """Checks if a process with the given PID is still running."""
    try:
        if os.name == "nt":
            # Windows: use tasklist
            res = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True
            )
            return str(pid) in res.stdout
        else:
            # Unix: send signal 0 (no-op, just checks existence)
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError):
        return False


def check_working_tree_clean(project_root: Path) -> bool:
    """
    Checks if the git working tree is clean (no uncommitted changes).
    Returns True if clean, False if dirty.
    """
    res = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_root,
        capture_output=True,
        text=True
    )
    if res.returncode != 0:
        logger.warning(f"Could not check git status: {res.stderr}")
        return False
    return res.stdout.strip() == ""


def parse_frontmatter(content: str) -> dict:
    """Parses YAML frontmatter from a Markdown string using ruamel.yaml."""
    match = FRONTMATTER_PATTERN.match(content)
    if not match:
        return {}
    try:
        yaml = YAML(typ='rt')
        data = yaml.load(match.group(1))
        return _to_plain_dict(data) if data else {}
    except Exception as e:
        logger.warning(f"Failed to parse YAML frontmatter: {e}")
        return {}


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
        content = filepath.read_text(encoding="utf-8").lstrip("\ufeff")
        match = FRONTMATTER_PATTERN.match(content)
        
        yaml = YAML(typ='rt')
        yaml.preserve_quotes = True

        if match:
            yaml_text = match.group(1)
            try:
                data = yaml.load(yaml_text) or {}
            except Exception as e:
                print(f"Error: Could not parse YAML frontmatter in {filepath.name}: {e}")
                return False

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
    except (IOError, OSError) as e:
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
    try:
        yaml = YAML(typ='rt')
        parsed = yaml.load(content) or {}
    except Exception as e:
        logger.warning(f"Failed to parse hooks.yaml: {e}")
        return {}
    return _to_plain_dict(parsed.get("hooks", {}))


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
        # Use exact boundary matching to avoid slice-1 matching slice-10
        matching = [
            f for f in specs_dir.glob("*.md")
            if dep_id in f.stem.split("-")
            or f.stem.endswith(dep_id)
            or dep_id == f.stem
        ]
        if not matching:
            # Fallback: try substring match but only if exactly one result
            fallback = list(specs_dir.glob(f"*{dep_id}*.md"))
            if len(fallback) == 1:
                matching = fallback
            else:
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

    _sanitize_id(slice_id, "slice_id")

    worktrees_dir = project_root / ".worktrees"
    worktree_path = worktrees_dir / slice_id
    branch_name = f"feat/{slice_id}"

    if worktree_path.exists():
        print(f"[Git Worktree] Worktree at '{worktree_path}' already exists.")
        return worktree_path

    worktrees_dir.mkdir(exist_ok=True)
    print(f"[Git Worktree] Creating worktree for branch '{branch_name}' at '{worktree_path}'...")

    res = subprocess.run(
        ["git", "worktree", "add", "-b", branch_name, str(worktree_path), "HEAD"],
        cwd=project_root,
        capture_output=True,
        text=True
    )

    if res.returncode != 0:
        res2 = subprocess.run(
            ["git", "worktree", "add", str(worktree_path), branch_name],
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

    _sanitize_id(slice_id, "slice_id")

    branch_name = f"feat/{slice_id}"
    worktree_path = project_root / ".worktrees" / slice_id

    # Check for dirty working tree before attempting merge
    if not check_working_tree_clean(project_root):
        print(f"❌ [Git Merge] Working tree is dirty. Please commit or stash changes before merging.")
        print(f"   Run 'git status' in '{project_root}' to see uncommitted changes.")
        return False

    print(f"[Git Merge] Attempting to merge '{branch_name}' into current branch...")

    res = subprocess.run(
        ["git", "merge", branch_name],
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
            ["git", "worktree", "remove", "--force", str(worktree_path)],
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
        project_root = find_project_root(filepath)
        fm = parse_frontmatter(filepath.read_text(encoding="utf-8"))
        slice_id = fm.get("slice_id", filepath.stem)
        
        merge_and_cleanup_worktree(slice_id, spec_file=filepath, project_root=project_root)
        run_infrastructure_hook("on_slice_verified_closed", project_root=project_root)


def cmd_trigger_hook(args):
    """Manually or programmatically triggers an infrastructure hook."""
    project_root = Path(args.dir) if args.dir else Path.cwd()
    run_infrastructure_hook(args.event, project_root=project_root)


def cmd_dispatch_planner(args):
    """Dispatches background OpenCode task for planner after checking dependencies."""
    spec_file = Path(args.spec)
    if not spec_file.exists():
        print(f"Error: Spec file '{spec_file}' not found.")
        sys.exit(1)

    model = args.model if hasattr(args, 'model') and args.model else DEFAULT_PLANNER_MODEL

    unmet = check_unmet_dependencies(spec_file)
    if unmet:
        print(f"❌ [Dependency Gate] Cannot dispatch planner for {spec_file.name}. Unmet dependencies:")
        for dep in unmet:
            print(f"   - {dep}")
        sys.exit(1)

    project_root = find_project_root(spec_file)

    # Extract slice_id for locking
    fm = parse_frontmatter(spec_file.read_text(encoding="utf-8"))
    slice_id = fm.get("slice_id", spec_file.stem)
    lock_file = acquire_slice_lock(slice_id, project_root)

    env = run_infrastructure_hook("on_slice_planning_start", project_root=project_root)

    update_frontmatter_status(spec_file, "PLANNING")

    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    log_file = logs_dir / f"planner_{spec_file.stem}.log"

    opencode_cmd = (
        f"opencode run --model {model} "
        f"\"Read spec at {spec_file} and create detailed TDD implementation plan "
        f"using writing-plans skill. Save to docs/superpowers/plans/\""
    )

    # Chain trigger-hook for completion
    orchestrator_path = Path(__file__).resolve()
    chained_cmd = f"{opencode_cmd} && python \"{orchestrator_path}\" trigger-hook --event on_planning_complete --dir \"{project_root}\""

    print(f"Dispatching {model} Planner in background...")
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

    # Update lock with spawned PID
    lock_data = json.loads(lock_file.read_text(encoding="utf-8"))
    lock_data["worker_pid"] = proc.pid
    lock_data["model"] = model
    lock_file.write_text(json.dumps(lock_data, indent=2), encoding="utf-8")

    print(f"Planner dispatched successfully. PID: {proc.pid}")


def cmd_dispatch_executor(args):
    """Dispatches background OpenCode task for executor inside an isolated worktree."""
    plan_file = Path(args.plan)
    if not plan_file.exists():
        print(f"Error: Plan file '{plan_file}' not found.")
        sys.exit(1)

    model = args.model if hasattr(args, 'model') and args.model else DEFAULT_EXECUTOR_MODEL

    project_root = find_project_root(plan_file)
    fm = parse_frontmatter(plan_file.read_text(encoding="utf-8"))
    slice_id = fm.get("slice_id", plan_file.stem)

    # Acquire lock before creating worktree
    lock_file = acquire_slice_lock(slice_id, project_root)

    worktree_path = create_git_worktree(slice_id, project_root=project_root)
    env = run_infrastructure_hook("on_slice_execution_start", project_root=project_root)

    update_frontmatter_status(plan_file, "EXECUTING")

    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    log_file = logs_dir / f"executor_{plan_file.stem}.log"

    opencode_cmd = (
        f"opencode run --model {model} "
        f"\"Execute implementation plan at {plan_file} using TDD subagent execution. "
        f"Check off tasks in plan as completed.\""
    )

    # Chain trigger-hook for completion
    orchestrator_path = Path(__file__).resolve()
    chained_cmd = f"{opencode_cmd} && python \"{orchestrator_path}\" trigger-hook --event on_execution_complete --dir \"{project_root}\""

    print(f"Dispatching {model} Executor in background at worktree '{worktree_path}'...")
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

    # Update lock with spawned PID
    lock_data = json.loads(lock_file.read_text(encoding="utf-8"))
    lock_data["worker_pid"] = proc.pid
    lock_data["model"] = model
    lock_file.write_text(json.dumps(lock_data, indent=2), encoding="utf-8")

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

    p_plan = subparsers.add_parser("dispatch-planner", help="Dispatch planner for a spec")
    p_plan.add_argument("--spec", required=True, help="Path to design spec file")
    p_plan.add_argument("--model", default=DEFAULT_PLANNER_MODEL, help=f"LLM model for planner (default: {DEFAULT_PLANNER_MODEL})")

    p_exec = subparsers.add_parser("dispatch-executor", help="Dispatch executor for a plan")
    p_exec.add_argument("--plan", required=True, help="Path to plan file")
    p_exec.add_argument("--model", default=DEFAULT_EXECUTOR_MODEL, help=f"LLM model for executor (default: {DEFAULT_EXECUTOR_MODEL})")

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
