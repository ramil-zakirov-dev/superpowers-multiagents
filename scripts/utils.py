"""Shared utilities: ID validation, YAML conversion, project root discovery."""

import os
import re
import sys
import subprocess
import logging
from pathlib import Path
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from scripts.errors import ValidationError

logger = logging.getLogger("orchestrator")

# Regex for validating slice_id / branch names
SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def _sanitize_id(value: str, label: str = "ID") -> str:
    """Validates that a string is safe for use in git branch names and paths."""
    if not SAFE_ID_PATTERN.match(value):
        raise ValidationError(
            f"{label} '{value}' contains invalid characters. "
            f"Only alphanumeric characters, hyphens, underscores and dots are allowed."
        )
    return value


def _to_plain_dict(obj) -> dict | list | str | int | float | bool | None:
    """Recursively converts ruamel.yaml CommentedMap/CommentedSeq to plain Python types."""
    if isinstance(obj, CommentedMap):
        return {str(k): _to_plain_dict(v) for k, v in obj.items()}
    elif isinstance(obj, CommentedSeq):
        return [_to_plain_dict(item) for item in obj]
    elif isinstance(obj, list):
        return [_to_plain_dict(item) for item in obj]
    elif isinstance(obj, dict):
        return {str(k): _to_plain_dict(v) for k, v in obj.items()}
    return obj


def find_project_root(start_path: Path) -> Path:
    """Walks up the directory tree looking for .superpowers/ or .git/."""
    current = start_path.resolve()
    if current.is_file():
        current = current.parent

    for directory in [current, *current.parents]:
        if (directory / ".superpowers").is_dir():
            return directory
        if (directory / ".git").is_dir():
            return directory

    logger.warning(f"Could not find project root from '{start_path}', using '{current}'.")
    return current


def _is_process_alive(pid: int) -> bool:
    """Checks if a process with the given PID is still running."""
    try:
        if os.name == "nt":
            res = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True
            )
            return str(pid) in res.stdout
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError):
        return False
