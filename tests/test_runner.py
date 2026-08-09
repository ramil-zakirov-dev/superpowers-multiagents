import os
import sys

from scripts.frontmatter import parse_frontmatter
from scripts.locks import acquire_slice_lock
from scripts.paths import log_path
from scripts.runner import main, run_supervised


#: A planner that actually did its job: it left a plan the machine can read.
#: Exiting 0 stopped being enough when the artifact check landed.
DID_ITS_JOB = [sys.executable, "-B", "-c", "import stub_agent"]


def _set_status(spec, status):
    text = spec.read_text(encoding="utf-8")
    spec.write_text(text.replace("status: SPEC_APPROVED", f"status: {status}"), encoding="utf-8")


def _supervise(tmp_project, demo_spec, argv, gate_status="SPEC_APPROVED"):
    """Supervise one run. `gate_status` is what a real dispatch records: the
    status the document sat at when the dispatch was accepted, and the place a
    failed run puts it back."""
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
        gate_status=gate_status,
    )
    return code, lock_file, log_file


def test_success_needs_the_artifact_not_just_a_zero_exit(tmp_project, demo_spec):
    """The observed failure: a planner that ran fine and left nothing readable.

    Recording PLAN_GENERATED here would hand the next gate a slice whose plan
    the state machine cannot see, and the natural repair at that point is to
    write frontmatter by hand — which this pipeline's own rules forbid.
    """
    _set_status(demo_spec, "PLANNING")
    code, _, log_file = _supervise(
        tmp_project, demo_spec, [sys.executable, "-c", "print('a plan, in my head')"]
    )
    assert code == 0
    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "SPEC_APPROVED"
    assert "left no document the pipeline can see" in log_file.read_text(encoding="utf-8")


def test_success_sets_success_status(tmp_project, demo_spec):
    _set_status(demo_spec, "PLANNING")
    code, _, _ = _supervise(tmp_project, demo_spec, DID_ITS_JOB)
    assert code == 0
    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "PLAN_GENERATED"


def test_failure_returns_the_document_to_the_gate_it_was_dispatched_from(
    tmp_project, demo_spec
):
    """#23: a run that died leaves the slice where the human left it.

    Not FAILED. The work a partial run did land is still on disk, and the two
    exits FAILED offered both meant re-dispatching to recover a status — which
    throws that work away. The log records what happened to the run; the status
    records where the work stands.
    """
    _set_status(demo_spec, "PLANNING")
    code, _, _ = _supervise(tmp_project, demo_spec, [sys.executable, "-c", "raise SystemExit(3)"])
    assert code == 3
    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "SPEC_APPROVED"


def test_a_run_with_no_recorded_gate_still_lands_somewhere(tmp_project, demo_spec):
    """Only reachable when run_supervised is driven directly — the dispatcher
    always records the gate. FAILED beats stranding the document in PLANNING,
    which is the one outcome nothing can walk out of."""
    _set_status(demo_spec, "PLANNING")
    code, _, _ = _supervise(
        tmp_project, demo_spec, [sys.executable, "-c", "raise SystemExit(3)"],
        gate_status="",
    )
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
    _supervise(tmp_project, demo_spec, DID_ITS_JOB)
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
    log_text = log_file.read_text(encoding="utf-8")
    assert "could not set status" in log_text
    assert "status -> PLAN_GENERATED" not in log_text
    assert "status UNCHANGED" in log_text


def test_failing_hook_does_not_overwrite_the_recorded_status(tmp_project, demo_spec):
    """A completion hook that fails must not roll back or hide the status
    the runner already wrote from the child's exit code."""
    _set_status(demo_spec, "PLANNING")
    script = tmp_project / "failing_hook.py"
    script.write_text("raise SystemExit(1)\n", encoding="utf-8")
    (tmp_project / ".superpowers" / "hooks.yaml").write_text(
        f"hooks:\n  on_planner_complete:\n    command: {sys.executable} {script.name}\n",
        encoding="utf-8",
    )
    code, _, log_file = _supervise(tmp_project, demo_spec, DID_ITS_JOB)
    assert code == 0
    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "PLAN_GENERATED"
    assert "completion hook failed" in log_file.read_text(encoding="utf-8")


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


def _plan(tmp_project):
    return tmp_project / "docs" / "superpowers" / "plans" / "2026-07-26-slice-01-demo-plan.md"


def test_the_epilogue_promotes_the_plan_the_planner_drafted(tmp_project, demo_spec):
    """#21: `PLAN_GENERATED` is a claim about completion, so the component
    that watched the run end is the one that makes it."""
    _set_status(demo_spec, "PLANNING")
    code, _, _ = _supervise(tmp_project, demo_spec, DID_ITS_JOB)

    assert code == 0
    plan = parse_frontmatter(_plan(tmp_project).read_text(encoding="utf-8"))
    assert plan["status"] == "PLAN_GENERATED"


#: A planner that produces a plan carrying whatever status it is given.
#: `%s` rather than a format literal: the body is full of braces-free but
#: quote-heavy YAML, and one substitution is all this needs.
_AGENT_TEMPLATE = '''
import pathlib

plans = pathlib.Path("docs/superpowers/plans")
plans.mkdir(parents=True, exist_ok=True)
(plans / "2026-07-26-slice-01-demo-plan.md").write_text(
    '---\\nslice_id: "slice-01-demo"\\nstatus: %s\\n---\\n\\n# P\\n',
    encoding="utf-8",
)
'''


def _agent_writing_plan_status(tmp_project, status):
    """A script that produces a plan carrying `status`, run from the project."""
    script = tmp_project / f"agent_{status.lower()}.py"
    script.write_text(_AGENT_TEMPLATE % status, encoding="utf-8")
    return [sys.executable, "-B", str(script)]


def test_a_plan_the_role_already_marked_generated_is_left_alone(tmp_project, demo_spec):
    """An agent that ignored the instruction produced exactly what the next
    gate wants. Failing the run over the disobedience would help nobody."""
    _set_status(demo_spec, "PLANNING")
    code, _, _ = _supervise(
        tmp_project, demo_spec, _agent_writing_plan_status(tmp_project, "PLAN_GENERATED")
    )

    assert code == 0
    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "PLAN_GENERATED"
    plan = parse_frontmatter(_plan(tmp_project).read_text(encoding="utf-8"))
    assert plan["status"] == "PLAN_GENERATED"


def test_a_plan_that_cannot_be_promoted_fails_the_run(tmp_project, demo_spec):
    """A document the next gate cannot advance is not a produced document.

    The natural repair at that point is to edit frontmatter by hand, which
    this pipeline's own rules forbid — so the run says so instead, and the
    spec goes back to its gate.
    """
    _set_status(demo_spec, "PLANNING")
    code, _, log_file = _supervise(
        tmp_project, demo_spec, _agent_writing_plan_status(tmp_project, "VERIFIED_CLOSED")
    )

    assert code == 0
    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "SPEC_APPROVED"
    assert "cannot move" in log_file.read_text(encoding="utf-8")
