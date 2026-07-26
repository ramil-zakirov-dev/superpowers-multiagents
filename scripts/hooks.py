"""Infrastructure hook loading and execution."""

import os
import subprocess
from pathlib import Path

from ruamel.yaml import YAML

from scripts.errors import HookError
from scripts.utils import _to_plain_dict


def canonical_events(roles) -> set[str]:
    """The complete set of event names the orchestrator ever emits."""
    events = {"on_slice_verified_closed"}
    for role in roles:
        events.add(f"on_slice_{role}_start")
        events.add(f"on_{role}_complete")
        events.add(f"on_{role}_failed")
    return events


def load_project_hooks(project_root: Path, known_events: set[str] | None = None) -> dict:
    """Load `.superpowers/hooks.yaml` if present.

    When `known_events` is supplied, every declared event is checked against
    it and anything unrecognised is reported. A hook keyed on a name the
    orchestrator never emits is silently dead otherwise.
    """
    hooks_file = Path(project_root) / ".superpowers" / "hooks.yaml"
    if not hooks_file.exists():
        return {}
    try:
        yaml = YAML(typ="rt")
        parsed = yaml.load(hooks_file.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        print(f"Warning: failed to parse {hooks_file}: {exc}. Ignoring project hooks.")
        return {}

    hooks = _to_plain_dict(parsed.get("hooks", {})) or {}

    if known_events is not None:
        for name in hooks:
            if name not in known_events:
                print(
                    f"Warning: {hooks_file} declares unknown event '{name}'. "
                    f"It will never fire. Known events: {sorted(known_events)}"
                )
    return hooks


def run_infrastructure_hook(
    event_name: str,
    project_root: Path,
    current_env: dict | None = None,
    known_events: set[str] | None = None,
) -> dict:
    """Execute a project infrastructure hook, optionally capturing env vars.

    Raises HookError on a non-zero exit. The caller decides what that means —
    dispatch releases its lock and stops before mutating any slice status.
    """
    if current_env is None:
        current_env = dict(os.environ)

    hooks = load_project_hooks(project_root, known_events=known_events)
    hook_cfg = hooks.get(event_name)
    if not hook_cfg or not isinstance(hook_cfg, dict):
        return current_env

    command = hook_cfg.get("command")
    if not command:
        return current_env

    capture_env = hook_cfg.get("capture_env", False)
    print(f"[Infrastructure Hook] Running '{event_name}': {command}")

    try:
        result = subprocess.run(
            command, shell=True, cwd=project_root,
            capture_output=True, text=True, env=current_env,
        )
    except OSError as exc:
        raise HookError(f"Hook '{event_name}' could not be started: {exc}") from exc

    if result.returncode != 0:
        raise HookError(
            f"Hook '{event_name}' failed with exit {result.returncode}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )

    print(f"[Infrastructure Hook] '{event_name}' completed successfully.")

    if capture_env and result.stdout:
        updated_env = dict(current_env)
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" in line:
                key, value = line.split("=", 1)
                updated_env[key.strip()] = value.strip().strip('"').strip("'")
        return updated_env

    return current_env
