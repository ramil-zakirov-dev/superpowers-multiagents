"""Dispatch with a sandbox configured. Never touches a real harness or docker."""

import argparse
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
agents:
  executor:
    model: "print('stub ok')"
    harness_adapter: 'stub_adapter.py'
    isolated_worktree: true
    allowed_statuses: ["SPEC_APPROVED"]
    in_progress_status: "EXECUTING"
    success_status: "EXECUTION_COMPLETE"
"""


def _args(spec, role="executor"):
    return argparse.Namespace(role=role, file=str(spec), model=None)


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
    """Inertness guard: docker must not leak into the orchestrator's contract."""
    cmd_dispatch_agent(argparse.Namespace(role="planner", file=str(demo_spec), model=None))
    assert stub_docker.calls == []


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


def _set_status(spec, status):
    text = spec.read_text(encoding="utf-8")
    import re as _re
    spec.write_text(
        _re.sub(r"status: \w+", f"status: {status}", text, count=1), encoding="utf-8"
    )
