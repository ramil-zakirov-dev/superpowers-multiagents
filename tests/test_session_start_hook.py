import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_run_hook_is_valid_as_a_posix_script():
    """On macOS and Linux the wrapper is executed as a shell script."""
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "hooks" / "run-hook.cmd"), "session-start"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert "command not found" not in result.stderr
    assert "syntax error" not in result.stderr
    payload = json.loads(result.stdout)
    assert payload


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_session_start_emits_the_orchestrator_skill():
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "hooks" / "session-start")],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    payload = json.loads(result.stdout)
    context = json.dumps(payload)
    assert "multiagent-orchestrator" in context


def test_hooks_json_has_no_shell_key():
    """Upstream does not set it and it buys nothing here."""
    config = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    entry = config["hooks"]["SessionStart"][0]["hooks"][0]
    assert "shell" not in entry
    assert entry["type"] == "command"
