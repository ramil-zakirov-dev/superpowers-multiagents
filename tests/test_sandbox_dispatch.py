"""Dispatch with a sandbox configured. Never touches a real harness or docker."""

import argparse
import subprocess
import time

from scripts.orchestrator import cmd_dispatch_agent
from scripts.paths import lock_path

SANDBOX_AGENTS = """\
sandbox:
  enabled: true
  compose_file: docker-compose.yml
  env:
    dsn: "postgres://{ip}:5432/db"
state_machine:
  transitions:
    SPEC_APPROVED: ["EXECUTING", "PLANNING", "DRAFT_SPEC"]
  # A failed run is not declared failed until its workspace has been quiet for
  # the settle window, so at the shipped 300s default every teardown assertion
  # below would time out waiting for a verdict the runner is still forming.
  # These tests are about what a settled failure does, not about how long
  # settling takes — `test_unknown_outcome.py` owns that question.
  settle_window_seconds: 0.2
  observation_deadline_seconds: 5
agents:
  executor:
    model: "print('stub ok')"
    harness_adapter: 'stub_adapter.py'
    isolated_worktree: true
    allowed_statuses: ["SPEC_APPROVED"]
    in_progress_status: "EXECUTING"
    success_status: "EXECUTION_COMPLETE"
"""


#: The same isolated executor with the `sandbox` block removed. Derived from
#: SANDBOX_AGENTS rather than written out again so the two cannot drift: an
#: edit to the agent definition can never leave the inertness guard below
#: exercising a different agent than the tests around it.
NO_SANDBOX_AGENTS = SANDBOX_AGENTS[SANDBOX_AGENTS.index("state_machine:"):]


def _args(spec, role="executor"):
    return argparse.Namespace(role=role, file=str(spec), model=None)


def _write_env_marker_hook(project_root, event, marker):
    """Install a hook that records the sandbox variables it was handed.

    The hook's environment is what the dispatched agent also receives, so
    this is how a test observes injection when there is no sandbox state to
    query directly.
    """
    (project_root / ".superpowers" / "hooks.yaml").write_text(
        "hooks:\n"
        f"  {event}:\n"
        f'    command: "python -c \\"import os;open(r\'{marker.as_posix()}\',\'w\')'
        '.write(os.environ.get(\'LOOPBACK_IP\',\'\')+chr(10)+'
        'os.environ.get(\'COMPOSE_PROJECT_NAME\',\'\'))\\""\n',
        encoding="utf-8",
    )


def _enable_sandbox(project_root):
    (project_root / ".superpowers" / "agents.yaml").write_text(
        SANDBOX_AGENTS, encoding="utf-8"
    )
    (project_root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")


def _up_calls(stub_docker):
    return [call for call in stub_docker.calls if call["argv"][-2:] == ["up", "-d"]]


def _wait_for(predicate, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def test_compose_project_comes_from_the_slice_branch(
    tmp_project, demo_spec, stub_docker
):
    """The defect this slice exists to fix.

    The main repository is on its own branch; the slice's worktree is on
    feat/<slice_id>. Anything deriving the branch from the project root -- as
    a shell hook necessarily does -- addresses the wrong stack.
    """
    _enable_sandbox(tmp_project)

    cmd_dispatch_agent(_args(demo_spec))

    calls = _up_calls(stub_docker)
    assert len(calls) == 1, f"expected one `up`, saw {len(calls)}"
    argv = calls[0]["argv"]
    assert argv[argv.index("-p") + 1] == "feat-slice-01-demo"


def test_parallel_slices_get_distinct_stacks(tmp_project, stub_docker):
    _enable_sandbox(tmp_project)
    specs = tmp_project / "docs" / "superpowers" / "specs"
    for slice_id in ("slice-alpha", "slice-beta"):
        (specs / f"{slice_id}.md").write_text(
            f'---\nslice_id: "{slice_id}"\nstatus: SPEC_APPROVED\n---\n\n# x\n',
            encoding="utf-8",
        )
    # An isolated role's worktree is made from HEAD; an uncommitted spec would
    # not be in it, and the dispatch is refused for exactly that reason.
    subprocess.run(["git", "add", "-A"], cwd=tmp_project, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "specs"], cwd=tmp_project, capture_output=True
    )
    for slice_id in ("slice-alpha", "slice-beta"):
        cmd_dispatch_agent(_args(specs / f"{slice_id}.md"))

    calls = _up_calls(stub_docker)
    projects = {call["argv"][call["argv"].index("-p") + 1] for call in calls}
    addresses = {call["loopback_ip"] for call in calls}
    assert projects == {"feat-slice-alpha", "feat-slice-beta"}
    assert len(addresses) == 2, f"both slices published on {addresses}"


def test_slice_context_reaches_the_hook(tmp_project, demo_spec, stub_docker):
    _enable_sandbox(tmp_project)
    marker = tmp_project / "hook-env.txt"
    (tmp_project / ".superpowers" / "hooks.yaml").write_text(
        "hooks:\n"
        "  on_slice_executor_start:\n"
        f'    command: "python -c \\"import os;open(r\'{marker.as_posix()}\',\'w\')'
        '.write(os.environ.get(\'SUPERPOWERS_SLICE_BRANCH\',\'\')+chr(10)+'
        'os.environ.get(\'LOOPBACK_IP\',\'\'))\\""\n',
        encoding="utf-8",
    )

    cmd_dispatch_agent(_args(demo_spec))

    branch, loopback = marker.read_text(encoding="utf-8").splitlines()
    assert branch == "feat/slice-01-demo"
    assert loopback.startswith("127.0.0.")


def test_no_sandbox_block_means_no_docker(tmp_project, demo_spec, stub_docker):
    """Inertness guard: docker must not leak into the orchestrator's contract.

    AC6 requires BOTH halves: no docker invocation (checked below via
    stub_docker) AND no sandbox variable injected into the dispatched
    agent's environment. Reuses the marker-file pattern from
    `test_slice_context_reaches_the_hook` to observe the env the hook (and
    therefore the agent) actually received, since there is no sandbox state
    to query directly when no `sandbox` block is configured.
    """
    marker = tmp_project / "hook-env.txt"
    (tmp_project / ".superpowers" / "hooks.yaml").write_text(
        "hooks:\n"
        "  on_slice_planner_start:\n"
        f'    command: "python -c \\"import os;open(r\'{marker.as_posix()}\',\'w\')'
        '.write(os.environ.get(\'LOOPBACK_IP\',\'\')+chr(10)+'
        'os.environ.get(\'COMPOSE_PROJECT_NAME\',\'\'))\\""\n',
        encoding="utf-8",
    )

    cmd_dispatch_agent(argparse.Namespace(role="planner", file=str(demo_spec), model=None))
    assert stub_docker.calls == []

    lines = marker.read_text(encoding="utf-8").splitlines()
    loopback_ip = lines[0] if lines else ""
    compose_project = lines[1] if len(lines) > 1 else ""
    assert loopback_ip == "", (
        f"LOOPBACK_IP leaked into the agent's env with no sandbox block: {loopback_ip!r}"
    )
    assert compose_project == "", (
        f"COMPOSE_PROJECT_NAME leaked into the agent's env with no sandbox "
        f"block: {compose_project!r}"
    )


def test_isolated_dispatch_without_a_sandbox_block_makes_no_docker_call(
    tmp_project, demo_spec, stub_docker
):
    """The half of AC6 a planner-based guard structurally cannot reach.

    A non-isolated agent goes through `resolve_env`, which returns an empty
    mapping whenever no state exists — so on that path the `enabled` switch
    is redundant and deleting it is invisible. Only an *isolated* dispatch
    reaches `ensure_up`, where that switch is the single thing standing
    between an unconfigured project and a real docker invocation.

    A compose file is present on purpose. Without one, removing the switch
    would fail on the missing file and this test would go red for the wrong
    reason; with one, the mutant genuinely runs `up -d` and the assertion
    below is what stops it.
    """
    (tmp_project / ".superpowers" / "agents.yaml").write_text(
        NO_SANDBOX_AGENTS, encoding="utf-8"
    )
    (tmp_project / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    marker = tmp_project / "hook-env.txt"
    _write_env_marker_hook(tmp_project, "on_slice_executor_start", marker)

    cmd_dispatch_agent(_args(demo_spec))

    assert stub_docker.calls == [], (
        f"docker was invoked for a project with no sandbox block: "
        f"{stub_docker.calls}"
    )

    lines = marker.read_text(encoding="utf-8").splitlines()
    assert (lines[0] if lines else "") == "", "LOOPBACK_IP leaked into an isolated agent"
    assert (lines[1] if len(lines) > 1 else "") == "", (
        "COMPOSE_PROJECT_NAME leaked into an isolated agent"
    )


def test_non_isolated_agent_attaches_but_never_starts_a_stack(
    tmp_project, demo_spec, stub_docker
):
    """A planner runs on the human's branch. It must reach the human's stack
    and must not race one of its own into existence."""
    from scripts import sandbox
    from scripts.git_ops import current_branch

    _enable_sandbox(tmp_project)
    (tmp_project / ".superpowers" / "agents.yaml").write_text(
        SANDBOX_AGENTS.replace("executor:", "planner:").replace(
            "isolated_worktree: true", "isolated_worktree: false"
        ),
        encoding="utf-8",
    )
    marker = tmp_project / "planner-env.txt"
    (tmp_project / ".superpowers" / "hooks.yaml").write_text(
        "hooks:\n"
        "  on_slice_planner_start:\n"
        f'    command: "python -c \\"import os;open(r\'{marker.as_posix()}\',\'w\')'
        '.write(os.environ.get(\'LOOPBACK_IP\',\'NONE\'))\\""\n',
        encoding="utf-8",
    )

    # No stack yet: the planner must dispatch anyway, with no address.
    cmd_dispatch_agent(_args(demo_spec, role="planner"))
    assert marker.read_text(encoding="utf-8") == "NONE"
    assert stub_docker.calls == [], "a non-isolated agent started a stack"

    # The first dispatch's supervisor is a detached background process; give
    # it a moment to finish and release the slice lock before dispatching
    # again, or the second call races it and finds the slice still locked.
    _wait_for(lambda: not lock_path(tmp_project, "slice-01-demo").exists())

    # With the human's stack up, the same dispatch now carries its address.
    branch = current_branch(tmp_project)
    config = {"sandbox": {"enabled": True, "compose_file": "docker-compose.yml",
                          "env": {}, "teardown": {}}}
    expected = sandbox.ensure_up(branch, tmp_project, config)["LOOPBACK_IP"]

    _set_status(demo_spec, "SPEC_APPROVED")
    cmd_dispatch_agent(_args(demo_spec, role="planner"))
    assert marker.read_text(encoding="utf-8") == expected


FAILING_EXECUTOR = SANDBOX_AGENTS.replace(
    "model: \"print('stub ok')\"", "model: \"import sys; sys.exit(3)\""
)


def test_failed_slice_stops_containers_but_keeps_volumes(
    tmp_project, demo_spec, stub_docker
):
    from scripts import sandbox

    _enable_sandbox(tmp_project)
    (tmp_project / ".superpowers" / "agents.yaml").write_text(
        FAILING_EXECUTOR, encoding="utf-8"
    )

    cmd_dispatch_agent(_args(demo_spec))

    assert _wait_for(
        lambda: any(c["argv"][-1] == "down" for c in stub_docker.calls)
    ), f"no teardown was recorded; calls were {stub_docker.calls}"

    down = [c for c in stub_docker.calls if "down" in c["argv"]][-1]
    assert "-v" not in down["argv"], "a failed slice must keep its volumes"
    assert sandbox.read_state(tmp_project, "feat/slice-01-demo") is not None


def test_non_isolated_agent_failure_does_not_tear_down_the_humans_stack(
    tmp_project, demo_spec, stub_docker
):
    """Architect-approved fix: teardown-on-failure must only apply to agents
    that own a stack's lifecycle. A non-isolated agent (e.g. a `planner`)
    never brings a stack up -- it only ever attaches to one that already
    exists on the human's own branch -- so its crash must not stop the
    human's own containers over an unrelated failure.
    """
    from scripts import sandbox
    from scripts.git_ops import current_branch

    _enable_sandbox(tmp_project)
    failing_planner = SANDBOX_AGENTS.replace("executor:", "planner:").replace(
        "isolated_worktree: true", "isolated_worktree: false"
    ).replace("model: \"print('stub ok')\"", "model: \"import sys; sys.exit(3)\"")
    (tmp_project / ".superpowers" / "agents.yaml").write_text(
        failing_planner, encoding="utf-8"
    )

    # The human's own stack is already up on their own branch, independent of
    # any dispatch -- exactly the scenario the bug clobbers.
    branch = current_branch(tmp_project)
    config = {"sandbox": {"enabled": True, "compose_file": "docker-compose.yml",
                          "env": {}, "teardown": {}}}
    sandbox.ensure_up(branch, tmp_project, config)

    cmd_dispatch_agent(_args(demo_spec, role="planner"))

    _wait_for(lambda: not lock_path(tmp_project, "slice-01-demo").exists())

    down_calls = [c for c in stub_docker.calls if "down" in c["argv"]]
    assert down_calls == [], (
        f"a non-isolated agent's failure tore down the human's stack: {down_calls}"
    )
    assert sandbox.read_state(tmp_project, branch) is not None


def test_failure_hook_observes_the_stack_before_teardown(
    tmp_project, demo_spec, stub_docker
):
    journal = tmp_project / "journal.txt"
    _enable_sandbox(tmp_project)
    (tmp_project / ".superpowers" / "agents.yaml").write_text(
        FAILING_EXECUTOR, encoding="utf-8"
    )
    (tmp_project / ".superpowers" / "hooks.yaml").write_text(
        "hooks:\n"
        "  on_executor_failed:\n"
        f'    command: "python -c \\"open(r\'{journal.as_posix()}\',\'a\')'
        '.write(\'hook\\\\n\')\\""\n',
        encoding="utf-8",
    )

    cmd_dispatch_agent(_args(demo_spec))

    assert _wait_for(lambda: journal.exists() and any(
        "down" in c["argv"] for c in stub_docker.calls
    ))
    hook_time = journal.stat().st_mtime
    docker_time = stub_docker.log.stat().st_mtime
    assert hook_time <= docker_time, "teardown ran before the failure hook"


def _set_status(spec, status):
    text = spec.read_text(encoding="utf-8")
    import re as _re
    spec.write_text(
        _re.sub(r"status: \w+", f"status: {status}", text, count=1), encoding="utf-8"
    )
