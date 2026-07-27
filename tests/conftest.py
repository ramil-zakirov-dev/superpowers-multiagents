"""Shared fixtures.

`tmp_project` is a real git repository with a `.superpowers/` config and a
spec file, so dispatch and supervisor tests exercise the real code paths.

The stub adapter exists to keep tests off the real harness: `opencode` is
installed on the development machine and a live run costs money. No test may
invoke it.
"""

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

STUB_ADAPTER = '''
import sys
sys.path.insert(0, r"{repo_root}")
from scripts.adapters.base import HarnessAdapter


class StubAdapter(HarnessAdapter):
    """Emits a harmless command instead of calling a real harness."""

    def build_command(self, agent_config, task_prompt):
        return [sys.executable, "-c", agent_config.get("model", "print('stub ok')")]
'''


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def tmp_project(tmp_path):
    """A git repo with .superpowers/, a stub adapter and one approved spec."""
    _git(tmp_path, "init", "-q", ".")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")

    (tmp_path / ".superpowers").mkdir()
    (tmp_path / "stub_adapter.py").write_text(
        STUB_ADAPTER.format(repo_root=str(REPO_ROOT)), encoding="utf-8"
    )

    specs = tmp_path / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    (specs / "2026-07-26-slice-01-demo-design.md").write_text(
        '---\nslice_id: "slice-01-demo"\nstatus: SPEC_APPROVED\n---\n\n# Demo\n',
        encoding="utf-8",
    )

    (tmp_path / ".superpowers" / "agents.yaml").write_text(
        "agents:\n"
        "  planner:\n"
        "    model: \"print('stub ok')\"\n"
        "    harness_adapter: 'stub_adapter.py'\n"
        "    isolated_worktree: false\n",
        encoding="utf-8",
    )

    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


@pytest.fixture
def demo_spec(tmp_project):
    return tmp_project / "docs" / "superpowers" / "specs" / "2026-07-26-slice-01-demo-design.md"
