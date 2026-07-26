"""Infrastructure hook loading and execution."""

import os
import sys
import subprocess
from pathlib import Path
from ruamel.yaml import YAML

from scripts.utils import _to_plain_dict


def load_project_hooks(project_root: Path) -> dict:
    """Loads .superpowers/hooks.yaml if present."""
    hooks_file = project_root / ".superpowers" / "hooks.yaml"
    if not hooks_file.exists():
        return {}
    try:
        yaml = YAML(typ='rt')
        parsed = yaml.load(hooks_file.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return _to_plain_dict(parsed.get("hooks", {}))


def run_infrastructure_hook(
    event_name: str,
    project_root: Path,
    current_env: dict = None,
) -> dict:
    """Executes a project infrastructure hook and optionally captures env vars."""
    if current_env is None:
        current_env = dict(os.environ)

    hooks = load_project_hooks(project_root)
    hook_cfg = hooks.get(event_name)
    if not hook_cfg or not isinstance(hook_cfg, dict):
        return current_env

    command = hook_cfg.get("command")
    if not command:
        return current_env

    capture_env = hook_cfg.get("capture_env", False)
    print(f"[Infrastructure Hook] Running '{event_name}': {command}")

    try:
        res = subprocess.run(
            command, shell=True, cwd=project_root,
            capture_output=True, text=True, env=current_env
        )
        if res.returncode != 0:
            print(f"[Infrastructure Hook] Error (exit {res.returncode}):")
            print(res.stderr)
            sys.exit(res.returncode)

        print(f"[Infrastructure Hook] '{event_name}' completed successfully.")

        if capture_env and res.stdout:
            updated_env = dict(current_env)
            for line in res.stdout.splitlines():
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    updated_env[k.strip()] = v.strip().strip('"').strip("'")
            return updated_env
    except Exception as e:
        print(f"[Infrastructure Hook] Exception: {e}")
        sys.exit(1)

    return current_env
