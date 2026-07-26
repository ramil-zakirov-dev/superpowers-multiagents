import pytest

from scripts.errors import HookError
from scripts.hooks import canonical_events, load_project_hooks, run_infrastructure_hook


def _write_hooks(project_root, text):
    sp = project_root / ".superpowers"
    sp.mkdir(parents=True, exist_ok=True)
    (sp / "hooks.yaml").write_text(text, encoding="utf-8")


def test_canonical_events_for_default_roles():
    events = canonical_events(["planner", "executor"])
    assert events == {
        "on_slice_planner_start",
        "on_planner_complete",
        "on_planner_failed",
        "on_slice_executor_start",
        "on_executor_complete",
        "on_executor_failed",
        "on_slice_verified_closed",
    }


def test_unknown_event_name_is_reported(tmp_path, capsys):
    """The exact defect that hid a never-firing sandbox hook for months."""
    _write_hooks(tmp_path, 'hooks:\n  on_slice_execution_start:\n    command: "echo hi"\n')
    load_project_hooks(tmp_path, known_events=canonical_events(["planner", "executor"]))
    out = capsys.readouterr().out
    assert "on_slice_execution_start" in out
    assert "on_slice_executor_start" in out


def test_known_event_name_is_not_reported(tmp_path, capsys):
    _write_hooks(tmp_path, 'hooks:\n  on_slice_executor_start:\n    command: "echo hi"\n')
    load_project_hooks(tmp_path, known_events=canonical_events(["planner", "executor"]))
    assert "unknown event" not in capsys.readouterr().out.lower()


def test_failing_hook_raises_instead_of_exiting(tmp_path):
    _write_hooks(tmp_path, 'hooks:\n  on_slice_executor_start:\n    command: "exit 3"\n')
    with pytest.raises(HookError, match="on_slice_executor_start"):
        run_infrastructure_hook("on_slice_executor_start", project_root=tmp_path)


def test_capture_env_collects_exported_variables(tmp_path):
    _write_hooks(
        tmp_path,
        'hooks:\n  on_slice_executor_start:\n    command: "echo LOOPBACK_IP=127.0.0.9"\n'
        "    capture_env: true\n",
    )
    env = run_infrastructure_hook("on_slice_executor_start", project_root=tmp_path)
    assert env["LOOPBACK_IP"] == "127.0.0.9"


def test_missing_event_returns_env_unchanged(tmp_path):
    _write_hooks(tmp_path, 'hooks:\n  on_slice_executor_start:\n    command: "echo hi"\n')
    env = run_infrastructure_hook("on_executor_complete", project_root=tmp_path)
    assert isinstance(env, dict)
