"""Git worktree management and merge operations."""

import sys
import subprocess
from pathlib import Path

from scripts.utils import _sanitize_id


def check_working_tree_clean(project_root: Path) -> bool:
    """Returns True if the git working tree has no uncommitted changes."""
    res = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_root,
        capture_output=True,
        text=True
    )
    if res.returncode != 0:
        return False
    return res.stdout.strip() == ""


def create_git_worktree(slice_id: str, project_root: Path) -> Path:
    """Creates an isolated git worktree for a slice under .worktrees/<slice_id>."""
    _sanitize_id(slice_id, "slice_id")
    worktrees_dir = project_root / ".worktrees"
    worktree_path = worktrees_dir / slice_id
    branch_name = f"feat/{slice_id}"

    if worktree_path.exists():
        return worktree_path

    worktrees_dir.mkdir(exist_ok=True)
    res = subprocess.run(
        ["git", "worktree", "add", "-b", branch_name, str(worktree_path), "HEAD"],
        cwd=project_root, capture_output=True, text=True
    )
    if res.returncode != 0:
        res2 = subprocess.run(
            ["git", "worktree", "add", str(worktree_path), branch_name],
            cwd=project_root, capture_output=True, text=True
        )
        if res2.returncode != 0:
            print("Error: Could not create worktree:")
            print(res2.stderr)
            sys.exit(1)
    return worktree_path


def merge_and_cleanup_worktree(
    slice_id: str,
    project_root: Path,
    spec_file: Path = None,
    update_status_fn=None,
) -> bool:
    """Merges slice worktree branch into current branch.

    Args:
        slice_id: The slice identifier.
        project_root: Project root path.
        spec_file: Optional spec file to update on conflict.
        update_status_fn: Callable(filepath, status) for conflict marking.
    """
    _sanitize_id(slice_id, "slice_id")
    branch_name = f"feat/{slice_id}"
    worktree_path = project_root / ".worktrees" / slice_id

    if not check_working_tree_clean(project_root):
        print("Error: Working tree is dirty. Commit or stash changes before merging.")
        return False

    res = subprocess.run(
        ["git", "merge", branch_name],
        cwd=project_root, capture_output=True, text=True
    )
    if res.returncode != 0:
        if spec_file and spec_file.exists() and update_status_fn:
            update_status_fn(spec_file, "MERGE_CONFLICT")
        print("Merge conflict halted.")
        return False

    if worktree_path.exists():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=project_root, capture_output=True
        )
    return True
