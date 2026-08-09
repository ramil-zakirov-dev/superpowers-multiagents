"""OpenCode CLI harness adapter.

Formats an argv for the OpenCode CLI. The model is passed in the CLI's native
`provider/model` form — under the default empty `extra_args` the provider was
previously dropped entirely.
"""

import json
import subprocess

from scripts.adapters.base import HarnessAdapter


class OpenCodeAdapter(HarnessAdapter):
    """Adapter for the OpenCode CLI harness.

    Produces: ``opencode run --model <provider>/<model> [extra_args...] <prompt>``
    """

    def build_command(
        self, agent_config: dict, task_prompt: str, cwd=None
    ) -> list[str]:
        model = agent_config.get("model", "kimi-k3")
        provider = agent_config.get("provider", "opencode-go")

        argv = ["opencode", "run", "--model", f"{provider}/{model}"]
        if cwd:
            # `--dir` is the CLI's own way to say where to run. The subprocess
            # cwd said the same thing implicitly, and an implicit location is
            # what left an isolated agent unable to tell which of two trees it
            # was in. This does not change how opencode resolves the *project*
            # — a linked worktree's `.git` is a file pointing at the parent, so
            # the project stays the parent repository and the worktree is
            # registered as one of its sandboxes — but nothing rests on an
            # inherited working directory any more.
            argv += ["--dir", str(cwd)]
        for arg in agent_config.get("extra_args") or []:
            argv.append(str(arg).format(provider=provider, model=model))
        argv.append(task_prompt)
        return argv

    def list_skills(self, agent_config: dict, cwd) -> set[str] | None:
        """Ask the CLI what it can see. Any failure means "cannot tell".

        `opencode debug skill` prints a JSON array of objects carrying at least
        a `name`. It makes no model call, so this costs nothing but a few
        seconds, and it is only ever reached when a role declares skills.
        """
        try:
            result = subprocess.run(
                ["opencode", "debug", "skill"],
                cwd=str(cwd), capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return None

        if result.returncode != 0:
            return None

        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            return None

        if not isinstance(payload, list):
            return None

        return {
            entry["name"] for entry in payload
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        }
