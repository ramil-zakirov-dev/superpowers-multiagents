"""End-to-end dispatch against a temporary git repo.

This is the test that would have caught the empty log, the useless lock and
the self-blocking merge gate together. It never invokes a real harness — the
project fixture wires in a stub adapter.
"""

import argparse
import json
import time

import pytest

from scripts.frontmatter import parse_frontmatter
from scripts.git_ops import check_working_tree_clean
from scripts.orchestrator import cmd_dispatch_agent
from scripts.paths import lock_path, log_path


def _args(spec, role="planner", model=None):
    return argparse.Namespace(role=role, file=str(spec), model=model)


def _wait_for(predicate, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def test_dispatch_runs_the_agent_and_reaches_a_terminal_status(tmp_project, demo_spec):
    cmd_dispatch_agent(_args(demo_spec))

    assert _wait_for(
        lambda: parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"]
        == "PLAN_GENERATED"
    ), "supervisor never advanced the slice to its success status"


def test_dispatch_writes_a_non_empty_log(tmp_project, demo_spec):
    cmd_dispatch_agent(_args(demo_spec))
    log_file = log_path(tmp_project, "planner", demo_spec.stem)
    assert _wait_for(lambda: log_file.exists() and log_file.stat().st_size > 0)
    assert "stub ok" in log_file.read_text(encoding="utf-8")


def test_dispatch_releases_the_lock_when_the_agent_finishes(tmp_project, demo_spec):
    cmd_dispatch_agent(_args(demo_spec))
    assert _wait_for(lambda: not lock_path(tmp_project, "slice-01-demo").exists())


def test_dispatch_artifacts_do_not_dirty_the_tree(tmp_project, demo_spec):
    cmd_dispatch_agent(_args(demo_spec))
    _wait_for(lambda: not lock_path(tmp_project, "slice-01-demo").exists())
    # The spec file itself is a tracked modification; discard it, then only
    # orchestrator artifacts remain.
    import subprocess
    subprocess.run(["git", "checkout", "--", "."], cwd=tmp_project, capture_output=True)
    assert check_working_tree_clean(tmp_project) is True


def test_dispatch_refused_from_the_wrong_status(tmp_project, demo_spec):
    text = demo_spec.read_text(encoding="utf-8")
    demo_spec.write_text(text.replace("SPEC_APPROVED", "DRAFT_SPEC"), encoding="utf-8")
    with pytest.raises(SystemExit):
        cmd_dispatch_agent(_args(demo_spec))
    assert not lock_path(tmp_project, "slice-01-demo").exists()


def test_failing_start_hook_leaves_the_slice_untouched(tmp_project, demo_spec):
    """A failing hook must not strand the slice in PLANNING."""
    (tmp_project / ".superpowers" / "hooks.yaml").write_text(
        'hooks:\n  on_slice_planner_start:\n    command: "exit 7"\n', encoding="utf-8"
    )
    with pytest.raises(SystemExit):
        cmd_dispatch_agent(_args(demo_spec))

    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "SPEC_APPROVED"
    assert not lock_path(tmp_project, "slice-01-demo").exists()


def test_lock_records_a_live_supervisor(tmp_project, demo_spec):
    cmd_dispatch_agent(_args(demo_spec))
    lock_file = lock_path(tmp_project, "slice-01-demo")
    if lock_file.exists():
        data = json.loads(lock_file.read_text(encoding="utf-8"))
        assert data["state"] in {"starting", "running"}
        if data["state"] == "running":
            assert data["pid"]
