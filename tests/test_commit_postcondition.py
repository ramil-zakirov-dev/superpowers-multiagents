"""What an isolated dispatch guarantees, and what proves it.

Every role's dispatch has a stated postcondition except the one whose output
is code. `_missing_artifact` checks that a role declaring `produces` left a
document the machine can read; for an isolated role it printed a note and
returned "", and nothing took its place. So an executor exiting 0 with an
empty branch was recorded EXECUTION_COMPLETE, and the next human saw a slice
that said it was ready to close while close-slice had nothing to merge.

The count is against a base ref captured when the worktree came into
existence, not against the main branch. `create_git_worktree` reuses an
existing worktree and attaches to a branch that may already carry an earlier
run's commits — counting from the main branch would credit this run with
that work.
"""

import argparse
import subprocess
import sys

import pytest

from scripts.frontmatter import parse_frontmatter
from scripts.git_ops import branch_tip, commits_since
from scripts.locks import acquire_slice_lock
from scripts.orchestrator import cmd_dispatch_agent
from scripts.paths import log_path
from scripts.runner import run_supervised
from tests.test_dispatch_integration import _wait_for


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )


def _use_isolated_executor(project_root, document, agent_body):
    """An executor role over the default machine: PLAN_APPROVED -> EXECUTING."""
    (project_root / ".superpowers" / "agents.yaml").write_text(
        "agents:\n"
        "  executor:\n"
        f"    model: {agent_body!r}\n"
        "    harness_adapter: 'stub_adapter.py'\n"
        "    isolated_worktree: true\n",
        encoding="utf-8",
    )
    _set_status(document, "PLAN_APPROVED")
    _git(project_root, "add", "-A")
    _git(project_root, "commit", "-qm", "configure executor")


def _main_branch(project_root):
    """Whatever `git init` called it here — not assumed to be `main`."""
    return _git(project_root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


#: An agent that commits inside whatever tree it was given as cwd.
COMMITS_SOMETHING = (
    "import pathlib, subprocess; "
    "pathlib.Path('work.txt').write_text('done'); "
    "subprocess.run(['git', 'add', '-A']); "
    "subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', "
    "'commit', '-qm', 'feat: the work'])"
)

DOES_NOTHING = "print('I thought about it')"


def _supervise_executor(project_root, document, argv, base_ref, cwd=None):
    lock_file = acquire_slice_lock("slice-01-demo", project_root)
    log_file = log_path(project_root, "executor", document.stem)
    code = run_supervised(
        role="executor",
        target_file=document,
        project_root=project_root,
        lock_file=lock_file,
        log_file=log_file,
        argv=argv,
        cwd=cwd or project_root,
        base_ref=base_ref,
    )
    return code, log_file


def _set_status(document, status):
    lines = document.read_text(encoding="utf-8").splitlines(keepends=True)
    document.write_text(
        "".join(
            f"status: {status}\n" if line.startswith("status:") else line
            for line in lines
        ),
        encoding="utf-8",
    )


def _make_slice_branch(project_root):
    """The branch a dispatch owns, as `create_git_worktree` would leave it."""
    worktree = project_root / ".worktrees" / "slice-01-demo"
    _git(project_root, "worktree", "add", "-b", "feat/slice-01-demo",
         str(worktree), "HEAD")
    return worktree


# --- the postcondition itself ---


def test_an_isolated_run_that_left_no_commits_is_recorded_failed(
    tmp_project, demo_spec
):
    _use_isolated_executor(tmp_project, demo_spec, DOES_NOTHING)
    _set_status(demo_spec, "EXECUTING")
    worktree = _make_slice_branch(tmp_project)
    base = branch_tip("feat/slice-01-demo", tmp_project)

    code, log_file = _supervise_executor(
        tmp_project, demo_spec,
        [sys.executable, "-B", "-c", DOES_NOTHING], base, cwd=worktree,
    )

    assert code == 0
    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "FAILED"
    log = log_file.read_text(encoding="utf-8")
    assert "feat/slice-01-demo" in log, "the verdict must name the branch it looked at"
    assert "no commits" in log


def test_an_isolated_run_that_committed_reaches_its_success_status(
    tmp_project, demo_spec
):
    _use_isolated_executor(tmp_project, demo_spec, COMMITS_SOMETHING)
    _set_status(demo_spec, "EXECUTING")
    worktree = _make_slice_branch(tmp_project)
    base = branch_tip("feat/slice-01-demo", tmp_project)

    code, _ = _supervise_executor(
        tmp_project, demo_spec,
        [sys.executable, "-B", "-c", COMMITS_SOMETHING], base, cwd=worktree,
    )

    assert code == 0
    assert (
        parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"]
        == "EXECUTION_COMPLETE"
    )


def test_a_redispatch_that_adds_nothing_fails_even_on_a_branch_with_history(
    tmp_project, demo_spec
):
    """The test that pins base-ref semantics. Counting `main..feat/...` passes
    here — the branch does carry commits — and credits this run with an
    earlier one's work.
    """
    _use_isolated_executor(tmp_project, demo_spec, DOES_NOTHING)
    _set_status(demo_spec, "EXECUTING")
    worktree = _make_slice_branch(tmp_project)
    subprocess.run([sys.executable, "-B", "-c", COMMITS_SOMETHING], cwd=worktree)
    assert commits_since(_main_branch(tmp_project), "feat/slice-01-demo", tmp_project) == 1

    base = branch_tip("feat/slice-01-demo", tmp_project)   # after the earlier run
    code, log_file = _supervise_executor(
        tmp_project, demo_spec,
        [sys.executable, "-B", "-c", DOES_NOTHING], base, cwd=worktree,
    )

    assert code == 0
    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "FAILED"
    assert "no commits" in log_file.read_text(encoding="utf-8")


def test_an_unanswerable_count_does_not_certify_the_run(tmp_project, demo_spec):
    """Absence of evidence is not certification. This reads like the opposite
    of the liveness rule — where an unanswerable lookup counts as alive — and
    it is not: there, a missing answer would be used to *kill* a running
    dispatch; here it would be used to *bless* a finished one. Both refuse to
    act destructively on what they could not observe.
    """
    _use_isolated_executor(tmp_project, demo_spec, DOES_NOTHING)
    _set_status(demo_spec, "EXECUTING")

    code, log_file = _supervise_executor(
        tmp_project, demo_spec,
        [sys.executable, "-B", "-c", DOES_NOTHING],
        "0000000000000000000000000000000000000000",      # no such ref
    )

    assert code == 0
    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "FAILED"
    assert "could not count" in log_file.read_text(encoding="utf-8")


def test_a_non_isolated_role_still_answers_with_its_document(tmp_project, demo_spec):
    """The check that already worked is untouched: the artifact for a role that
    produces a document is still that document.
    """
    _set_status(demo_spec, "PLANNING")
    lock_file = acquire_slice_lock("slice-01-demo", tmp_project)
    log_file = log_path(tmp_project, "planner", demo_spec.stem)

    run_supervised(
        role="planner", target_file=demo_spec, project_root=tmp_project,
        lock_file=lock_file, log_file=log_file,
        argv=[sys.executable, "-c", "print('a plan, in my head')"],
        cwd=tmp_project,
    )

    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "FAILED"
    assert "left no document the pipeline can see" in log_file.read_text(encoding="utf-8")


# --- and the dispatcher has to supply the base ref, or none of it fires ---


def test_dispatch_wires_the_base_ref_end_to_end(tmp_project, demo_spec):
    """Through the real command: an isolated dispatch whose agent commits
    nothing must land in FAILED. If the dispatcher forgets to capture and pass
    the base ref, the check silently does nothing and this is the only test
    that notices.
    """
    _use_isolated_executor(tmp_project, demo_spec, DOES_NOTHING)

    cmd_dispatch_agent(argparse.Namespace(
        role="executor", file=str(demo_spec), model=None
    ))

    assert _wait_for(
        lambda: parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"]
        == "FAILED"
    ), "an isolated dispatch that produced no commits was not recorded FAILED"


def test_dispatch_records_success_when_the_agent_commits(tmp_project, demo_spec):
    _use_isolated_executor(tmp_project, demo_spec, COMMITS_SOMETHING)

    cmd_dispatch_agent(argparse.Namespace(
        role="executor", file=str(demo_spec), model=None
    ))

    assert _wait_for(
        lambda: parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"]
        == "EXECUTION_COMPLETE"
    )
    assert commits_since(_main_branch(tmp_project), "feat/slice-01-demo", tmp_project) >= 1
