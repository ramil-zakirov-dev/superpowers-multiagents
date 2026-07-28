"""End-to-end dispatch against a temporary git repo.

This is the test that would have caught the empty log, the useless lock and
the self-blocking merge gate together. It never invokes a real harness — the
project fixture wires in a stub adapter.
"""

import argparse
import json
import os
import signal
import subprocess
import time

import pytest

from scripts.errors import LockError
from scripts.frontmatter import parse_frontmatter
from scripts.git_ops import check_working_tree_clean
from scripts.locks import acquire_slice_lock
from scripts.orchestrator import cmd_dispatch_agent
from scripts.paths import lock_path, log_path
from scripts.utils import _is_process_alive


def _args(spec, role="planner", model=None):
    return argparse.Namespace(role=role, file=str(spec), model=model)


def _wait_for(predicate, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def _use_slow_agent(project_root, seconds):
    """Rewrite the project's agent so the stub sleeps instead of exiting at once.

    The stub adapter passes `model` to `python -c`, so the model string is the
    agent's whole behaviour.
    """
    (project_root / ".superpowers" / "agents.yaml").write_text(
        "agents:\n"
        "  planner:\n"
        f'    model: "import time; time.sleep({seconds})"\n'
        "    harness_adapter: 'stub_adapter.py'\n"
        "    isolated_worktree: false\n",
        encoding="utf-8",
    )


def _wait_for_lock_state(lock_file, state, timeout=30.0):
    """Return the lock payload once it reaches `state`, or None on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if lock_file.exists():
            try:
                data = json.loads(lock_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, ValueError):
                data = None
            if data and data.get("state") == state:
                return data
        time.sleep(0.1)
    return None


def _kill_tree(pid):
    """Stop a supervisor and the agent underneath it, so no test leaves stragglers."""
    if not pid:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
    else:
        # dispatch spawns the supervisor with start_new_session=True, so its
        # PID is the process-group leader.
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


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
    """The lock must name a process that is actually alive, and refuse others.

    The original defect was a lock naming the dispatcher, which exits within a
    second of spawning: the lock went stale immediately and blocked nothing.
    Asserting only `state in {starting, running}` passes against that defect
    too, so this pins the claimed state, a live PID, and actual refusal.

    The agent is made slow on purpose — with the default instant stub the
    supervisor is usually gone before the assertions run, which is what made
    the earlier version of this test racy.
    """
    _use_slow_agent(tmp_project, seconds=20)
    cmd_dispatch_agent(_args(demo_spec))
    lock_file = lock_path(tmp_project, "slice-01-demo")

    data = _wait_for_lock_state(lock_file, "running")
    assert data is not None, "the supervisor never claimed the lock"

    supervisor_pid = data["pid"]
    try:
        assert supervisor_pid, "lock claimed as running but carries no PID"
        assert _is_process_alive(supervisor_pid), (
            f"lock names PID {supervisor_pid}, which is not alive"
        )
        assert data["role"] == "planner"

        with pytest.raises(LockError, match="slice-01-demo"):
            acquire_slice_lock("slice-01-demo", tmp_project)
    finally:
        _kill_tree(supervisor_pid)


def test_bad_adapter_leaves_the_slice_untouched(tmp_project, demo_spec):
    """Adapter resolution is fallible (unknown harness_adapter file) and must
    be checked before the in_progress_status mutation — not after it, or a
    reachable misconfiguration strands the slice with no legal way back."""
    (tmp_project / ".superpowers" / "agents.yaml").write_text(
        "agents:\n"
        "  planner:\n"
        "    model: \"print('stub ok')\"\n"
        "    harness_adapter: 'does_not_exist.py'\n"
        "    isolated_worktree: false\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit):
        cmd_dispatch_agent(_args(demo_spec))

    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "SPEC_APPROVED"
    assert not lock_path(tmp_project, "slice-01-demo").exists()


def test_isolated_worktree_dispatch_creates_a_worktree(tmp_project, demo_spec):
    (tmp_project / ".superpowers" / "agents.yaml").write_text(
        "agents:\n"
        "  planner:\n"
        "    model: \"print('stub ok')\"\n"
        "    harness_adapter: 'stub_adapter.py'\n"
        "    isolated_worktree: true\n",
        encoding="utf-8",
    )
    cmd_dispatch_agent(_args(demo_spec))

    worktree = tmp_project / ".worktrees" / "slice-01-demo"
    assert _wait_for(lambda: worktree.exists())
    assert _wait_for(
        lambda: parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"]
        == "PLAN_GENERATED"
    )


RECORDING_ADAPTER = '''
import os, sys
sys.path.insert(0, r"{repo_root}")
from scripts.adapters.base import HarnessAdapter


class RecordingAdapter(HarnessAdapter):
    def build_command(self, agent_config, task_prompt):
        with open(os.environ["SUPERPOWERS_PROMPT_LOG"], "w", encoding="utf-8") as handle:
            handle.write(task_prompt)
        return [sys.executable, "-c", "pass"]

    def list_skills(self, agent_config, cwd):
        log = os.environ.get("SUPERPOWERS_SKILLS_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as handle:
                handle.write(str(cwd) + "\\n")
        raw = os.environ.get("SUPERPOWERS_VISIBLE_SKILLS")
        if raw is None:
            return None
        return set(filter(None, raw.split(",")))
'''


def _use_recording_adapter(project_root, skills):
    from tests.conftest import REPO_ROOT
    (project_root / "recording_adapter.py").write_text(
        RECORDING_ADAPTER.format(repo_root=str(REPO_ROOT)), encoding="utf-8"
    )
    listed = "\n".join(f"      - {name}" for name in skills)
    (project_root / ".superpowers" / "agents.yaml").write_text(
        "agents:\n"
        "  planner:\n"
        "    harness_adapter: 'recording_adapter.py'\n"
        "    isolated_worktree: false\n"
        + ("    skills:\n" + listed + "\n" if skills else ""),
        encoding="utf-8",
    )


def test_declared_skills_reach_the_agent_prompt(tmp_project, demo_spec, monkeypatch, tmp_path):
    prompt_log = tmp_path / "prompt.txt"
    monkeypatch.setenv("SUPERPOWERS_PROMPT_LOG", str(prompt_log))
    monkeypatch.setenv("SUPERPOWERS_VISIBLE_SKILLS", "clean-architecture,clean-code")
    _use_recording_adapter(tmp_project, ["clean-architecture", "clean-code"])

    cmd_dispatch_agent(_args(demo_spec))

    assert prompt_log.read_text(encoding="utf-8").endswith(
        "Use these skills where they apply: clean-architecture, clean-code."
    )


def test_no_skills_leaves_the_prompt_untouched(tmp_project, demo_spec, monkeypatch, tmp_path):
    prompt_log = tmp_path / "prompt.txt"
    monkeypatch.setenv("SUPERPOWERS_PROMPT_LOG", str(prompt_log))
    _use_recording_adapter(tmp_project, [])

    cmd_dispatch_agent(_args(demo_spec))

    assert "Use these skills" not in prompt_log.read_text(encoding="utf-8")


def test_invisible_skill_is_reported_and_dispatch_proceeds(
    tmp_project, demo_spec, monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("SUPERPOWERS_PROMPT_LOG", str(tmp_path / "prompt.txt"))
    monkeypatch.setenv("SUPERPOWERS_VISIBLE_SKILLS", "clean-code")
    _use_recording_adapter(tmp_project, ["clean-architcture", "clean-code"])

    cmd_dispatch_agent(_args(demo_spec))

    out = capsys.readouterr().out
    assert "clean-architcture" in out
    assert "not visible to the harness" in out
    assert "Dispatched" in out                      # it still ran
    assert parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"] == "PLANNING"


def test_adapter_that_cannot_tell_stays_silent(
    tmp_project, demo_spec, monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("SUPERPOWERS_PROMPT_LOG", str(tmp_path / "prompt.txt"))
    monkeypatch.delenv("SUPERPOWERS_VISIBLE_SKILLS", raising=False)
    _use_recording_adapter(tmp_project, ["clean-architecture"])

    cmd_dispatch_agent(_args(demo_spec))

    assert "not visible to the harness" not in capsys.readouterr().out


def test_no_skills_asks_the_harness_nothing(tmp_project, demo_spec, monkeypatch, tmp_path):
    """An unconfigured dispatch must not pay for a subprocess it cannot use."""
    skills_log = tmp_path / "list_skills-calls.txt"
    monkeypatch.setenv("SUPERPOWERS_PROMPT_LOG", str(tmp_path / "prompt.txt"))
    monkeypatch.setenv("SUPERPOWERS_SKILLS_LOG", str(skills_log))
    _use_recording_adapter(tmp_project, [])

    cmd_dispatch_agent(_args(demo_spec))

    assert not skills_log.exists(), (
        f"list_skills was called {skills_log.read_text(encoding='utf-8').count(chr(10))} "
        f"time(s) for a role that declares no skills"
    )
