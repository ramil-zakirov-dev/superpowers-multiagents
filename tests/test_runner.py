import json
import os
import sys

from scripts.frontmatter import parse_frontmatter
from scripts.locks import acquire_slice_lock
from scripts.paths import log_path
from scripts.runner import run_supervised


def _set_status(spec, status):
    text = spec.read_text(encoding="utf-8")
    spec.write_text(text.replace("status: SPEC_APPROVED", f"status: {status}"), encoding="utf-8")


def _supervise(tmp_project, demo_spec, argv):
    lock_file = acquire_slice_lock("slice-01-demo", tmp_project)
    log_file = log_path(tmp_project, "planner", demo_spec.stem)
    code = run_supervised(
        role="planner",
        target_file=demo_spec,
        project_root=tmp_project,
        lock_file=lock_file,
        log_file=log_file,
        argv=argv,
        cwd=tmp_project,
    )
    return code, lock_file, log_file


def test_success_sets_success_status(tmp_project, demo_spec):
    _set_status(demo_spec, "PLANNING")
    code, _, _ = _supervise(tmp_project, demo_spec, [sys.executable, "-c", "print('done')"])
    assert code == 0
    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "PLAN_GENERATED"


def test_failure_sets_failed(tmp_project, demo_spec):
    """A crashed agent must land in FAILED, not hang in PLANNING forever."""
    _set_status(demo_spec, "PLANNING")
    code, _, _ = _supervise(tmp_project, demo_spec, [sys.executable, "-c", "raise SystemExit(3)"])
    assert code == 3
    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "FAILED"


def test_log_captures_child_output(tmp_project, demo_spec):
    """The defect: cmd redirection bound to the last command, so logs were empty."""
    _set_status(demo_spec, "PLANNING")
    _, _, log_file = _supervise(
        tmp_project, demo_spec, [sys.executable, "-c", "print('AGENT-MARKER')"]
    )
    assert log_file.exists()
    assert "AGENT-MARKER" in log_file.read_text(encoding="utf-8")


def test_log_captures_child_stderr(tmp_project, demo_spec):
    _set_status(demo_spec, "PLANNING")
    _, _, log_file = _supervise(
        tmp_project, demo_spec,
        [sys.executable, "-c", "import sys; sys.stderr.write('BOOM-MARKER')"],
    )
    assert "BOOM-MARKER" in log_file.read_text(encoding="utf-8")


def test_lock_is_claimed_then_released(tmp_project, demo_spec):
    _set_status(demo_spec, "PLANNING")
    _, lock_file, _ = _supervise(tmp_project, demo_spec, [sys.executable, "-c", "pass"])
    assert not lock_file.exists()


def test_lock_is_released_even_when_the_child_cannot_start(tmp_project, demo_spec):
    _set_status(demo_spec, "PLANNING")
    lock_file = acquire_slice_lock("slice-01-demo", tmp_project)
    log_file = log_path(tmp_project, "planner", demo_spec.stem)
    code = run_supervised(
        role="planner", target_file=demo_spec, project_root=tmp_project,
        lock_file=lock_file, log_file=log_file,
        argv=["definitely-not-an-executable-anywhere"], cwd=tmp_project,
    )
    assert code != 0
    assert not lock_file.exists()
    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "FAILED"


def test_runner_claims_the_lock_with_its_own_pid(tmp_project, demo_spec, monkeypatch):
    _set_status(demo_spec, "PLANNING")
    lock_file = acquire_slice_lock("slice-01-demo", tmp_project)
    seen = {}

    import scripts.runner as runner_module
    original = runner_module.claim_slice_lock

    def spy(path, pid, **meta):
        seen["pid"] = pid
        return original(path, pid, **meta)

    monkeypatch.setattr(runner_module, "claim_slice_lock", spy)
    run_supervised(
        role="planner", target_file=demo_spec, project_root=tmp_project,
        lock_file=lock_file, log_file=log_path(tmp_project, "planner", demo_spec.stem),
        argv=[sys.executable, "-c", "pass"], cwd=tmp_project,
    )
    assert seen["pid"] == os.getpid()
