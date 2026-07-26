"""Git worktree management and merge operations."""

import subprocess
from pathlib import Path

from scripts.errors import GitError
from scripts.paths import is_artifact_path
from scripts.utils import _sanitize_id


def _porcelain_entry(line: str) -> str:
    """Extract the path from one `git status --porcelain` line."""
    entry = line[3:].strip()
    if " -> " in entry:                # renames: "R  old -> new"
        entry = entry.split(" -> ", 1)[1]
    return entry.strip().strip('"')


def check_working_tree_clean(project_root: Path) -> bool:
    """True if the tree has no changes other than orchestrator artifacts.

    The orchestrator writes logs and locks into the project it operates on.
    Counting those as dirt made the merge gate refuse unconditionally.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_root, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False

    for line in result.stdout.splitlines():
        entry = _porcelain_entry(line)
        if entry and not is_artifact_path(entry):
            return False
    return True


def create_git_worktree(slice_id: str, project_root: Path) -> Path:
    """Create an isolated worktree for a slice under `.worktrees/<slice_id>`."""
    _sanitize_id(slice_id, "slice_id")
    worktree_path = Path(project_root) / ".worktrees" / slice_id
    branch_name = f"feat/{slice_id}"

    if worktree_path.exists():
        return worktree_path

    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    created = subprocess.run(
        ["git", "worktree", "add", "-b", branch_name, str(worktree_path), "HEAD"],
        cwd=project_root, capture_output=True, text=True,
    )
    if created.returncode != 0:
        # The branch may already exist from an earlier run; attach to it.
        reused = subprocess.run(
            ["git", "worktree", "add", str(worktree_path), branch_name],
            cwd=project_root, capture_output=True, text=True,
        )
        if reused.returncode != 0:
            raise GitError(
                f"Could not create worktree for '{slice_id}': "
                f"{reused.stderr.strip() or created.stderr.strip()}"
            )
    return worktree_path


def merge_and_cleanup_worktree(slice_id: str, project_root: Path) -> bool:
    """Merge a slice branch into the current branch and drop its worktree.

    Returns True on success and False on merge conflict — a conflict is an
    expected outcome the caller records as a status. Raises GitError when the
    tree is dirty, which is a precondition failure, not an outcome.
    """
    _sanitize_id(slice_id, "slice_id")
    branch_name = f"feat/{slice_id}"
    worktree_path = Path(project_root) / ".worktrees" / slice_id

    if not check_working_tree_clean(project_root):
        raise GitError(
            "Working tree is dirty. Commit or stash your changes before merging."
        )

    merged = subprocess.run(
        ["git", "merge", branch_name],
        cwd=project_root, capture_output=True, text=True,
    )
    if merged.returncode != 0:
        return False

    if worktree_path.exists():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=project_root, capture_output=True,
        )
    return True
