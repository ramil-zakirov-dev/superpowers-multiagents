import subprocess

import pytest

from scripts.errors import GitError, ValidationError
from scripts.git_ops import (
    check_working_tree_clean,
    create_git_worktree,
    merge_and_cleanup_worktree,
)


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", ".")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_clean_repo_is_clean(repo):
    assert check_working_tree_clean(repo) is True


def test_orchestrator_artifacts_do_not_make_the_tree_dirty(repo):
    """The defect: dispatch's own logs and locks blocked its own merge."""
    (repo / ".superpowers" / "logs").mkdir(parents=True)
    (repo / ".superpowers" / "logs" / "executor_x.log").write_text("out", encoding="utf-8")
    (repo / ".superpowers" / "locks").mkdir(parents=True)
    (repo / ".superpowers" / "locks" / "slice-01.lock").write_text("{}", encoding="utf-8")
    assert check_working_tree_clean(repo) is True


def test_user_changes_still_make_the_tree_dirty(repo):
    (repo / "src.py").write_text("print(1)\n", encoding="utf-8")
    assert check_working_tree_clean(repo) is False


def test_stray_file_beside_uncommitted_artifacts_still_makes_the_tree_dirty(repo):
    """A bare `?? .superpowers/` porcelain line (git collapses a wholly-new,
    never-committed directory to one entry) must not swallow real dirt that
    happens to share that parent — only --untracked-files=all, which lists
    every file individually, keeps this distinguishable."""
    (repo / ".superpowers" / "logs").mkdir(parents=True)
    (repo / ".superpowers" / "logs" / "executor_x.log").write_text("out", encoding="utf-8")
    (repo / ".superpowers" / "debug.json").write_text("{}", encoding="utf-8")
    assert check_working_tree_clean(repo) is False


def test_modified_tracked_file_makes_the_tree_dirty(repo):
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    assert check_working_tree_clean(repo) is False


def test_create_worktree_rejects_unsafe_id(repo):
    with pytest.raises(ValidationError):
        create_git_worktree("foo; rm -rf /", repo)


def test_merge_brings_the_branch_home_and_removes_the_worktree(repo):
    worktree = create_git_worktree("slice-01", repo)
    (worktree / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "feat: add feature")

    assert merge_and_cleanup_worktree("slice-01", repo) is True
    assert (repo / "feature.py").exists()
    assert not worktree.exists()


def test_merge_raises_when_the_tree_is_genuinely_dirty(repo):
    create_git_worktree("slice-01", repo)
    (repo / "uncommitted.py").write_text("x\n", encoding="utf-8")
    with pytest.raises(GitError, match="dirty"):
        merge_and_cleanup_worktree("slice-01", repo)


def test_merge_names_the_branch_it_cannot_find(repo):
    """A ref that does not exist is not a merge conflict.

    `git merge` exits non-zero for both, and reading every non-zero exit as a
    conflict made close-slice stamp MERGE_CONFLICT on a slice that had shipped
    cleanly and simply never had a branch.
    """
    with pytest.raises(GitError, match="feat/slice-99"):
        merge_and_cleanup_worktree("slice-99", repo)


def test_skip_merge_closes_a_slice_whose_branch_is_gone(repo):
    """Deleting a branch once it is merged is ordinary hygiene, so refusing
    outright would strand every tidily-landed slice."""
    assert merge_and_cleanup_worktree("slice-99", repo, skip_merge=True) is True


def test_skip_merge_brings_nothing_home(repo):
    """The flag asserts the work is already in; it must not perform the merge
    it was given permission to skip."""
    worktree = create_git_worktree("slice-01", repo)
    (worktree / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "feat: add feature")

    assert merge_and_cleanup_worktree("slice-01", repo, skip_merge=True) is True
    assert not (repo / "feature.py").exists()
    assert not worktree.exists()


def test_skip_merge_does_not_gate_on_a_dirty_tree(repo):
    """The dirty-tree gate guards the merge. With no merge there is nothing to
    guard, and close-slice's own status write dirties the tree anyway."""
    (repo / "uncommitted.py").write_text("x\n", encoding="utf-8")
    assert merge_and_cleanup_worktree("slice-99", repo, skip_merge=True) is True


def test_merge_returns_false_on_conflict(repo):
    worktree = create_git_worktree("slice-01", repo)
    (worktree / "README.md").write_text("branch side\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "branch change")

    (repo / "README.md").write_text("main side\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main change")

    assert merge_and_cleanup_worktree("slice-01", repo) is False
