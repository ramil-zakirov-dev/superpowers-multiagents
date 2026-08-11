"""The third verdict: a run whose fate nobody observed.

Two outcomes were not enough. When the watched process dies over an agent that
is still working — the shipped case, because `opencode run` is a client and the
session is not its child — both available answers are false. "Success" would
certify work nobody saw finish. "Failure" is what the runner said on
2026-08-11, and saying it cost the slice's containers, swept out from under the
live agent by a teardown hanging off the same exit code.

So the runner may now decline to answer. `unknown` writes no status, fires no
completion hook and tears nothing down. The asymmetry that justifies it: an
over-cautious `unknown` costs a human one reading, while a wrong `failure`
costs a destroyed stack, a false record, and an invitation to re-dispatch into
a session that is still running.

Liveness is read from the only fact that holds whatever the agent is: a working
agent changes its workspace. Nothing here knows what opencode is.
"""

import os
import signal
import sys
import time

from scripts.frontmatter import parse_frontmatter
from scripts.git_ops import branch_tip
from scripts.locks import acquire_slice_lock
from scripts.paths import log_path
from scripts.runner import run_supervised
from tests.test_commit_postcondition import (
    _git,
    _make_slice_branch,
    _set_status,
)

#: Writes to the worktree until told to stop. Stands in for the session that
#: outlives its client: the runner's own child is long gone while this keeps
#: touching files.
_BUSY_LOOP = (
    "import os, pathlib, time\n"
    "pathlib.Path('busy.pid').write_text(str(os.getpid()))\n"
    "while not pathlib.Path('stop').exists():\n"
    "    pathlib.Path('busy.txt').write_text(str(time.time()))\n"
    "    time.sleep(0.05)\n"
)

#: The client dies immediately, leaving the work going. Exactly the 2026-08-11
#: shape, minus the 4½ minutes.
DIES_LEAVING_A_WORKER = (
    "import subprocess, sys; "
    f"subprocess.Popen([sys.executable, '-B', '-c', {_BUSY_LOOP!r}]); "
    "sys.exit(1)"
)

#: The client dies and nothing is left behind — an ordinary failed run, which
#: must keep behaving exactly as it does today.
DIES_ALONE = "import sys; sys.exit(1)"


def _configure(project_root, document, settle, deadline, isolated=True):
    """An executor over the default machine, with both windows made small.

    `deep_merge` merges `state_machine` key by key, so naming only the two new
    keys leaves `valid_statuses` and `transitions` at their defaults.

    The sandbox is enabled here even for the tests that never look at it,
    because `tear_down` returns before touching docker when it is not — and a
    teardown assertion over a disabled sandbox passes whatever the runner does.
    `_seed_a_running_stack` supplies the other early return, the state record.
    """
    (project_root / ".superpowers" / "agents.yaml").write_text(
        "state_machine:\n"
        f"  settle_window_seconds: {settle}\n"
        f"  observation_deadline_seconds: {deadline}\n"
        "sandbox:\n"
        "  enabled: true\n"
        "  compose_file: docker-compose.yml\n"
        "agents:\n"
        "  executor:\n"
        "    model: 'unused — argv is passed to run_supervised directly'\n"
        "    harness_adapter: 'stub_adapter.py'\n"
        f"    isolated_worktree: {str(isolated).lower()}\n",
        encoding="utf-8",
    )
    (project_root / "docker-compose.yml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    _set_status(document, "PLAN_APPROVED")
    _git(project_root, "add", "-A")
    _git(project_root, "commit", "-qm", "configure executor")


def _seed_a_running_stack(project_root, branch="feat/slice-01-demo"):
    """The allocation record a `sandbox up` would have left behind."""
    from scripts import sandbox

    sandbox.write_state(
        project_root,
        sandbox.SandboxState(
            branch=branch,
            ip="127.0.0.9",
            project_name=sandbox.project_name_for(branch),
            started_at="2026-08-11T00:00:00+00:00",
        ),
    )


def _stop_the_worker(worktree):
    """Stop the stand-in session, and wait for it to actually be gone.

    Left running it keeps a handle on the worktree, and Windows then refuses
    the temp-directory cleanup, turning every later test in the session into a
    teardown error.
    """
    (worktree / "stop").write_text("", encoding="utf-8")
    pid_file = worktree / "busy.pid"
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except ValueError:
        return
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            return
        time.sleep(0.1)


def _supervise(project_root, document, worktree, argv, base, sandbox_branch=""):
    lock_file = acquire_slice_lock("slice-01-demo", project_root)
    log_file = log_path(project_root, "executor", document.stem)
    code = run_supervised(
        role="executor",
        target_file=document,
        project_root=project_root,
        lock_file=lock_file,
        log_file=log_file,
        argv=argv,
        cwd=worktree,
        base_ref=base,
        gate_status="PLAN_APPROVED",
        sandbox_branch=sandbox_branch,
    )
    return code, log_file


def _status(document):
    return parse_frontmatter(document.read_text(encoding="utf-8"))["status"]


def test_a_working_workspace_after_a_dead_client_yields_no_verdict(
    tmp_project, demo_spec
):
    """The status must stay where it is. `EXECUTING` is true — work is going
    on — and it is the only description that does not have to be walked back
    by hand later.
    """
    _configure(tmp_project, demo_spec, settle=0.5, deadline=2.0)
    _set_status(demo_spec, "EXECUTING")
    worktree = _make_slice_branch(tmp_project)
    base = branch_tip("feat/slice-01-demo", tmp_project)

    try:
        code, log_file = _supervise(
            tmp_project, demo_spec, worktree,
            [sys.executable, "-B", "-c", DIES_LEAVING_A_WORKER], base,
        )

        assert code == 1
        assert _status(demo_spec) == "EXECUTING", (
            "an unobserved run must not be recorded as an outcome"
        )
        assert "not observed" in log_file.read_text(encoding="utf-8")
    finally:
        _stop_the_worker(worktree)


def test_a_quiet_workspace_after_a_dead_client_still_returns_to_the_gate(
    tmp_project, demo_spec
):
    """Today's behaviour, kept — and now reached for a stated reason rather
    than because nothing was checked.
    """
    _configure(tmp_project, demo_spec, settle=0.2, deadline=2.0)
    _set_status(demo_spec, "EXECUTING")
    worktree = _make_slice_branch(tmp_project)
    base = branch_tip("feat/slice-01-demo", tmp_project)

    code, _ = _supervise(
        tmp_project, demo_spec, worktree,
        [sys.executable, "-B", "-c", DIES_ALONE], base,
    )

    assert code == 1
    assert _status(demo_spec) == "PLAN_APPROVED"


def test_an_unobserved_run_does_not_tear_down_its_sandbox(
    tmp_project, demo_spec, stub_docker
):
    """The assertion this slice exists for.

    Teardown is a reclamation, and a reclamation must not fire while the
    resource is in use. It hung off the client's exit code, which is the one
    signal that says nothing about whether the agent still needs its stack.
    """
    _configure(tmp_project, demo_spec, settle=0.5, deadline=2.0)
    _set_status(demo_spec, "EXECUTING")
    worktree = _make_slice_branch(tmp_project)
    base = branch_tip("feat/slice-01-demo", tmp_project)
    _seed_a_running_stack(tmp_project)

    try:
        _supervise(
            tmp_project, demo_spec, worktree,
            [sys.executable, "-B", "-c", DIES_LEAVING_A_WORKER], base,
            sandbox_branch="feat/slice-01-demo",
        )

        downs = [c for c in stub_docker.calls if "down" in c["argv"]]
        assert not downs, (
            f"the stack was swept while the agent was still working: {downs}"
        )
    finally:
        _stop_the_worker(worktree)


def test_a_run_that_really_failed_still_tears_down_its_sandbox(
    tmp_project, demo_spec, stub_docker
):
    """The falsifier for the assertion above.

    An absence is only evidence when the presence was reachable. This is the
    same configuration and the same seeded stack, differing only in that the
    workspace goes quiet — and here the sweep must happen, which is what makes
    "no down call" mean something in the test above rather than meaning that
    `tear_down` returned early on a disabled sandbox.
    """
    _configure(tmp_project, demo_spec, settle=0.2, deadline=2.0)
    _set_status(demo_spec, "EXECUTING")
    worktree = _make_slice_branch(tmp_project)
    base = branch_tip("feat/slice-01-demo", tmp_project)
    _seed_a_running_stack(tmp_project)

    _supervise(
        tmp_project, demo_spec, worktree,
        [sys.executable, "-B", "-c", DIES_ALONE], base,
        sandbox_branch="feat/slice-01-demo",
    )

    downs = [c for c in stub_docker.calls if "down" in c["argv"]]
    assert downs, (
        f"a genuinely dead run must still reclaim its stack; calls were "
        f"{stub_docker.calls}"
    )
    assert "-v" not in downs[-1]["argv"], "a failed slice keeps its volumes"
