import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _find_posix_bash() -> str | None:
    """Resolve a real POSIX-capable bash, not the Windows WSL launcher stub.

    On a Windows dev machine with WSL installed, `C:\\Windows\\System32\\bash.exe`
    frequently resolves ahead of Git Bash on PATH. That stub hands the command
    line to WSL, which mangles Windows-style backslash paths instead of running
    them as a POSIX shell script (turning `C:\\a\\b.cmd` into `C:ab.cmd`) --
    this has no bearing on the actual macOS/Linux behaviour this test exists to
    simulate, since there is no such stub on a real POSIX system. Prefer the
    same Git Bash locations `hooks/run-hook.cmd` itself probes for.
    """
    for candidate in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    found = shutil.which("bash")
    if found and "system32" in found.lower():
        return None
    return found


BASH = _find_posix_bash()


@pytest.mark.skipif(BASH is None, reason="a real (non-WSL-stub) bash is not available")
def test_run_hook_is_valid_as_a_posix_script():
    """On macOS and Linux the wrapper is executed as a shell script."""
    result = subprocess.run(
        [BASH, str(REPO_ROOT / "hooks" / "run-hook.cmd"), "session-start"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert "command not found" not in result.stderr
    assert "syntax error" not in result.stderr
    payload = json.loads(result.stdout)
    assert payload


@pytest.mark.skipif(BASH is None, reason="a real (non-WSL-stub) bash is not available")
def test_session_start_emits_the_orchestrator_skill():
    result = subprocess.run(
        [BASH, str(REPO_ROOT / "hooks" / "session-start")],
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
