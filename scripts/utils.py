"""Shared utilities: ID validation, YAML conversion, project root discovery."""

import ctypes
import os
import re
import logging
import tempfile
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


#: Windows: OpenProcess access right that needs no special privilege, plus the
#: two error codes that distinguish "no such process" from "not yours to look
#: at", and the exit code a running process reports.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87
_STILL_ACTIVE = 259


def _pid_exists_windows(pid: int):
    """True / False / None when Windows itself could not answer.

    ctypes rather than `tasklist`: this runs inside a poll loop, and a check
    that spawns a child process starts answering "dead" whenever the machine
    is briefly unable to spawn one. Asking whether a pid exists needs no
    child.

    A process that has exited but still has an open handle somewhere is
    reachable by OpenProcess, so the exit code is checked too. A real process
    exiting with 259 therefore reads as alive; that is a known Windows wart
    and it errs in the direction this function is required to err in.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == _ERROR_INVALID_PARAMETER:
            return False
        if error == _ERROR_ACCESS_DENIED:
            return True
        return None
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None
        return exit_code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _pid_exists(pid: int):
    """Whether the OS knows this pid: True, False, or None for "cannot tell"."""
    if os.name == "nt":
        try:
            return _pid_exists_windows(pid)
        except (OSError, AttributeError, ValueError):
            return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # it exists; it is simply not ours
    except OSError:
        return None
    return True


def _is_process_alive(pid: int) -> bool:
    """Whether a process is running. An unanswerable lookup counts as alive.

    Every caller acts destructively on False — lock reclamation takes the
    slice, `reconcile` and `wait` declare the dispatch abandoned — so False
    has to be a claim about the process, never a shrug about the lookup.
    Failing closed here is what keeps a watchdog from convicting a living
    supervisor because the machine was briefly busy.
    """
    return _pid_exists(pid) is not False


def atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` via a staged temp file in the same directory.

    A milestone brief is a tracked file that the orchestrator rewrites in
    place. A crash halfway through a plain write would leave a truncated
    document in the working tree; `os.replace` is atomic on both POSIX and
    Windows, so the file is either the old one or the new one.
    """
    path = Path(path)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, encoding="utf-8", newline=""
    ) as handle:
        handle.write(text)
        staged = handle.name
    os.replace(staged, path)
