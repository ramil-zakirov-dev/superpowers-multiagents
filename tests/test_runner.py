import os
import sys

from scripts.frontmatter import parse_frontmatter
from scripts.locks import acquire_slice_lock
from scripts.paths import log_path
from scripts.runner import main, run_supervised


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


def _write_hook(tmp_project, event_name, sentinel_name):
    """A hooks.yaml entry that touches a sentinel file, cwd=project_root."""
    script = tmp_project / f"mark_{sentinel_name}.py"
    script.write_text(
        f"open(r'{tmp_project / sentinel_name}', 'w').close()\n", encoding="utf-8"
    )
    hooks_file = tmp_project / ".superpowers" / "hooks.yaml"
    hooks_file.write_text(
        f"hooks:\n  {event_name}:\n    command: {sys.executable} {script.name}\n",
        encoding="utf-8",
    )


def test_completion_hook_fires_on_success(tmp_project, demo_spec):
    _set_status(demo_spec, "PLANNING")
    _write_hook(tmp_project, "on_planner_complete", "complete-fired.txt")
    _supervise(tmp_project, demo_spec, [sys.executable, "-c", "pass"])
    assert (tmp_project / "complete-fired.txt").exists()


def test_failure_hook_fires_on_non_zero_exit(tmp_project, demo_spec):
    _set_status(demo_spec, "PLANNING")
    _write_hook(tmp_project, "on_planner_failed", "failed-fired.txt")
    _supervise(tmp_project, demo_spec, [sys.executable, "-c", "raise SystemExit(1)"])
    assert (tmp_project / "failed-fired.txt").exists()


def test_illegal_transition_does_not_silently_claim_success(tmp_project, demo_spec):
    """update_frontmatter_status returns False (never raises) on an illegal
    transition; the runner must not print/log the new status as if it were
    applied, and must say so loudly rather than staying silent."""
    _set_status(demo_spec, "VERIFIED_CLOSED")  # terminal: no transition is legal from here
    log_file = log_path(tmp_project, "planner", demo_spec.stem)
    lock_file = acquire_slice_lock("slice-01-demo", tmp_project)
    run_supervised(
        role="planner", target_file=demo_spec, project_root=tmp_project,
        lock_file=lock_file, log_file=log_file,
        argv=[sys.executable, "-c", "print('done')"], cwd=tmp_project,
    )
    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "VERIFIED_CLOSED"
    assert "could not set status" in log_file.read_text(encoding="utf-8")


def test_main_runs_the_agent_and_returns_its_exit_code(tmp_project, demo_spec):
    _set_status(demo_spec, "PLANNING")
    lock_file = acquire_slice_lock("slice-01-demo", tmp_project)
    log_file = log_path(tmp_project, "planner", demo_spec.stem)
    code = main([
        "--role", "planner",
        "--file", str(demo_spec),
        "--project-root", str(tmp_project),
        "--lock", str(lock_file),
        "--log", str(log_file),
        "--cwd", str(tmp_project),
        "--",
        sys.executable, "-c", "raise SystemExit(5)",
    ])
    assert code == 5
    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "FAILED"


def test_log_setup_failure_still_records_a_failed_outcome(tmp_project, demo_spec):
    """If the log directory can't be created (e.g. a file already sits where
    the directory should be), the runner must still call _record_outcome
    with a synthesized failure code — not raise past it and strand the
    slice at its in-progress status with the lock already released."""
    _set_status(demo_spec, "PLANNING")
    log_file = log_path(tmp_project, "planner", demo_spec.stem)
    log_file.parent.write_text("blocks mkdir", encoding="utf-8")  # a file, not a dir
    lock_file = acquire_slice_lock("slice-01-demo", tmp_project)
    code = run_supervised(
        role="planner", target_file=demo_spec, project_root=tmp_project,
        lock_file=lock_file, log_file=log_file,
        argv=[sys.executable, "-c", "pass"], cwd=tmp_project,
    )
    assert code != 0
    assert not lock_file.exists()
    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "FAILED"


def test_main_errors_when_no_command_is_given():
    try:
        main([
            "--role", "planner", "--file", "f", "--project-root", "p",
            "--lock", "l", "--log", "g", "--cwd", "c", "--",
        ])
        assert False, "expected SystemExit from argparse.error"
    except SystemExit as exc:
        assert exc.code != 0
