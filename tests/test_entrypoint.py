import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(argv, cwd):
    return subprocess.run(
        [sys.executable, *argv], cwd=cwd, capture_output=True, text=True
    )


def test_script_invocation_works():
    """The documented form: `python scripts/orchestrator.py status`."""
    result = _run(["scripts/orchestrator.py", "status"], cwd=REPO_ROOT)
    assert result.returncode == 0, result.stderr
    assert "ModuleNotFoundError" not in result.stderr


def test_module_invocation_works():
    result = _run(["-m", "scripts.orchestrator", "status"], cwd=REPO_ROOT)
    assert result.returncode == 0, result.stderr


def test_set_status_offers_skip_merge():
    """cmd_set_status reads the flag, but only the parser can put it there."""
    result = _run(["scripts/orchestrator.py", "set-status", "--help"], cwd=REPO_ROOT)
    assert result.returncode == 0, result.stderr
    assert "--skip-merge" in result.stdout


def test_script_invocation_works_from_another_directory():
    """Installed as a plugin, cwd is the user's project, not the plugin root.

    The project is given a real (empty) docs base: this test is about the
    sys.path bootstrap resolving from a foreign cwd, and since #11 a `status`
    pointed at no pipeline at all exits non-zero on purpose — which would
    make this pass or fail for a reason it is not about.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        for name in ("milestones", "specs", "plans"):
            (Path(tmp_dir) / "docs" / "superpowers" / name).mkdir(parents=True)
        result = _run([str(REPO_ROOT / "scripts" / "orchestrator.py"), "status"], cwd=tmp_dir)
        assert result.returncode == 0, result.stderr


def test_runner_module_is_invocable():
    result = _run(["-m", "scripts.runner", "--help"], cwd=REPO_ROOT)
    assert result.returncode == 0, result.stderr


def test_summary_reads_logs_from_the_project_root():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        logs = tmp_path / ".superpowers" / "logs"
        logs.mkdir(parents=True)
        (logs / "executor_slice-01-auth.log").write_text(
            "\n".join(f"Line {i}" for i in range(100)), encoding="utf-8"
        )
        result = _run(
            [str(REPO_ROOT / "scripts" / "orchestrator.py"), "summary",
             "--slice", "slice-01-auth", "--dir", str(tmp_path)],
            cwd=REPO_ROOT,
        )
        assert result.returncode == 0, result.stderr
        assert "Line 99" in result.stdout
        assert "Line 49" not in result.stdout
