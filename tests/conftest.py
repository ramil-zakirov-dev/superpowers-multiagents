"""Shared fixtures.

`tmp_project` is a real git repository with a `.superpowers/` config and a
spec file, so dispatch and supervisor tests exercise the real code paths.

The stub adapter exists to keep tests off the real harness: `opencode` is
installed on the development machine and a live run costs money. No test may
invoke it.
"""

import json
import subprocess
import sys
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
        # -B: the stub agent is imported by name, and a __pycache__ beside it
        # would dirty the project tree that one test asserts stays clean.
        return [sys.executable, "-B", "-c", agent_config.get("model", "print('stub ok')")]
'''

#: What a successful planner does, now that exiting 0 is not enough: it leaves a
#: plan the state machine can read. Imported by name (`python -c "import
#: stub_agent"`) so the agents.yaml value stays a plain YAML scalar.
STUB_AGENT = '''
import pathlib

plans = pathlib.Path("docs/superpowers/plans")
plans.mkdir(parents=True, exist_ok=True)
(plans / "2026-07-26-slice-01-demo-plan.md").write_text(
    \'---\\nslice_id: "slice-01-demo"\\nstatus: PLAN_GENERATED\\n---\\n\\n# Demo Plan\\n\',
    encoding="utf-8",
)
print("stub ok")
'''

STUB_DOCKER = '''
import json, os, sys
record = os.environ["SUPERPOWERS_DOCKER_LOG"]
with open(record, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({
        "argv": sys.argv[1:],
        "loopback_ip": os.environ.get("LOOPBACK_IP"),
        "compose_project": os.environ.get("COMPOSE_PROJECT_NAME"),
    }) + "\\n")
if "--format" in sys.argv:
    print(json.dumps({"Service": "postgres", "Health": "healthy"}))
sys.exit(int(os.environ.get("SUPERPOWERS_DOCKER_EXIT", "0")))
'''


class StubDocker:
    def __init__(self, script, log):
        self.script = script
        self.log = log

    @property
    def calls(self):
        if not self.log.exists():
            return []
        return [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def argv_of(self, index):
        return self.calls[index]["argv"]


@pytest.fixture
def stub_docker(tmp_path, monkeypatch):
    """A fake `docker` that records argv instead of starting containers.

    Installed through the environment rather than by monkeypatching, so it
    is still in effect inside the detached supervisor process.
    """
    script = tmp_path / "stub_docker.py"
    script.write_text(STUB_DOCKER, encoding="utf-8")
    log = tmp_path / "docker-calls.jsonl"
    monkeypatch.setenv(
        "SUPERPOWERS_DOCKER_BIN", json.dumps([sys.executable, str(script)])
    )
    monkeypatch.setenv("SUPERPOWERS_DOCKER_LOG", str(log))
    return StubDocker(script, log)


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
    (tmp_path / "stub_agent.py").write_text(STUB_AGENT, encoding="utf-8")

    specs = tmp_path / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    (specs / "2026-07-26-slice-01-demo-design.md").write_text(
        '---\nslice_id: "slice-01-demo"\nstatus: SPEC_APPROVED\n---\n\n# Demo\n',
        encoding="utf-8",
    )

    (tmp_path / ".superpowers" / "agents.yaml").write_text(
        "agents:\n"
        "  planner:\n"
        "    model: 'import stub_agent'\n"
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
