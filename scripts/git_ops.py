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
        # --untracked-files=all: without it, git collapses a wholly-new
        # directory (e.g. the first-ever .superpowers/logs/) into one
        # `?? .superpowers/` line instead of listing the files inside it,
        # which is unmatchable against is_artifact_path's per-file prefixes.
        ["git", "status", "--porcelain", "--untracked-files=all"],
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
            # Both, not whichever spoke last. The fallback's message describes
            # the *fallback's* problem — typically "invalid reference", because
            # the branch the first attempt was supposed to create does not
            # exist — and quoting only that hides why the first attempt failed,
            # which is the question. Observed: a real failure diagnosable only
            # after this line was widened.
            raise GitError(
                f"Could not create worktree for '{slice_id}'. "
                f"Creating branch {branch_name}: "
                f"{created.stderr.strip() or f'exit {created.returncode}'}. "
                f"Attaching to an existing {branch_name}: "
                f"{reused.stderr.strip() or f'exit {reused.returncode}'}."
            )
    return worktree_path


def is_tracked_at_head(path: Path, project_root: Path) -> bool:
    """Whether HEAD's tree carries this file.

    An isolated role's worktree is created from HEAD, so this is the difference
    between a document the agent can open and a path that is simply not there.
    A repository with no commits yet answers False, which is the truthful answer
    for the same reason.
    """
    try:
        relative = Path(path).resolve().relative_to(Path(project_root).resolve())
    except ValueError:
        return False
    result = subprocess.run(
        ["git", "ls-tree", "--name-only", "HEAD", "--", relative.as_posix()],
        cwd=project_root, capture_output=True, text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def branch_exists(branch_name: str, project_root: Path) -> bool:
    """Whether `refs/heads/<branch_name>` resolves in this repository."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=project_root, capture_output=True, text=True,
    )
    return result.returncode == 0


def branch_tip(branch_name: str, project_root: Path) -> str:
    """The commit `branch_name` points at, or "" when it does not resolve.

    Captured at dispatch, immediately after the worktree exists, so the run's
    output can later be measured against where the branch started. For a fresh
    branch that is the fork point; for one being re-dispatched it is whatever
    the previous run left, which is the whole reason to record it rather than
    compare against the main branch afterwards.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", branch_name],
        cwd=project_root, capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def commits_since(base_ref: str, branch_name: str, project_root: Path):
    """How many commits `branch_name` carries that `base_ref` does not.

    None when git could not answer at all — an unresolvable ref, a missing
    branch, not a repository. That is a different fact from zero and the
    caller has to be able to tell them apart: zero means the agent left
    nothing, None means we do not know what it left.
    """
    result = subprocess.run(
        ["git", "rev-list", "--count", f"{base_ref}..{branch_name}"],
        cwd=project_root, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def merge_and_cleanup_worktree(
    slice_id: str, project_root: Path, skip_merge: bool = False
) -> bool:
    """Merge a slice branch into the current branch and drop its worktree.

    Returns True on success and False on merge conflict — a conflict is an
    expected outcome the caller records as a status. Raises GitError when the
    tree is dirty or when the branch does not exist; both are precondition
    failures, not outcomes.

    A missing branch used to be indistinguishable from a conflict, because
    `git merge` exits non-zero either way. That misdiagnosis marked slices
    MERGE_CONFLICT when they had in fact landed fast-forward and their branch
    was tidied away — so `skip_merge` lets the operator state that the work is
    already home. It is an assertion, not a guess: nothing here can verify it.
    """
    _sanitize_id(slice_id, "slice_id")
    branch_name = f"feat/{slice_id}"
    worktree_path = Path(project_root) / ".worktrees" / slice_id

    if not skip_merge:
        if not check_working_tree_clean(project_root):
            raise GitError(
                "Working tree is dirty. Commit or stash your changes before merging."
            )

        if not branch_exists(branch_name, project_root):
            raise GitError(
                f"Branch '{branch_name}' does not exist, so there is nothing to "
                f"merge. If the slice already landed and its branch was deleted, "
                f"re-run with --skip-merge; otherwise check the slice_id."
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


def current_branch(project_root: Path) -> str:
    """The checked-out branch of `project_root`, or `detached-<sha>`.

    Only used for agents that run in the project root itself. A slice's own
    branch is always derived from its slice_id, never from here.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(project_root), capture_output=True, text=True,
    )
    name = (result.stdout or "").strip()
    if result.returncode == 0 and name and name != "HEAD":
        return name
    sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(project_root), capture_output=True, text=True,
    ).stdout.strip()
    return f"detached-{sha}" if sha else "unknown"
