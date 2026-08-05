---
slice_id: "slice-06-abandoned-dispatch"
title: "Abandoned dispatch implementation plan"
status: VERIFIED_CLOSED
target_version: "2.10.0"
spec: "docs/superpowers/specs/2026-08-05-slice-06-abandoned-dispatch-design.md"
depends_on: []
---

# Abandoned Dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect a dispatch whose supervisor died before recording an outcome: `status` annotates the contradiction, `reconcile` moves the document to `FAILED` without asserting an unobserved outcome, and `wait` / `dispatch --wait` let a caller be notified when a dispatch ends — including by abandonment.

**Architecture:** Abandonment is a derived fact, never a stored one: document status ∈ some role's `in_progress_status` (from merged config, never a literal) AND no live supervisor owns the slice (lock absent or pid dead, via the same `_is_process_alive` that already governs lock reclamation). A new pure module `scripts/abandonment.py` owns the predicate, the evidence strings, and the injectable wait loop. `orchestrator.py` wires it into `status` (read-only annotation), a new `reconcile` command, a new `wait` command, and a `--wait` flag on dispatch. Nothing is ever written into a document except `reconcile`'s explicit, confirmed move to `FAILED`.

**Tech Stack:** Python 3.11, `ruamel.yaml`, `pytest`. No new dependencies.

## Global Constraints

- **Do not create a git branch or worktree.** The dispatcher owns branches and derives the branch name itself. Commit onto the branch you are handed.
- No test may invoke a real harness, a real container runtime, or the network. Process liveness and the clock are **injected** in unit tests; integration tests use the stub adapter from `tests/conftest.py`.
- The abandonment check must **never hardcode** `PLANNING` or `EXECUTING`. The in-progress set comes from `in_progress_statuses(config)`; a project that renames them must be detected by its own word.
- `status` stays **read-only**: it annotates, it never repairs. Assert in tests that the document and the lock are byte-identical after the call.
- `dispatch` without `--wait` stays **non-blocking**: it returns at supervisor spawn, exactly as today.
- `reconcile` must **refuse when a live supervisor owns the slice** — reconciling a running dispatch would race the runner's epilogue.
- Abandonment is computed on read. No "abandoned" flag is ever written into any document or lock.
- Conventional Commits (see `git log --oneline`); end each commit message with the trailer `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Run the suite as `python -m pytest -q -p no:cacheprovider`. Baseline on this branch is **425 passed in ~40s**.

## File Structure

| File | Responsibility |
|---|---|
| `scripts/abandonment.py` (create) | `in_progress_statuses`, `is_abandoned`, `lock_evidence` (Task 1); `find_slice_document`, `latest_log`, `WaitResult`, `wait_for_dispatch` (Task 4). No writes, ever. |
| `scripts/locks.py` (modify) | `_lock_is_held` gains an injectable `is_alive` keyword (Task 1). |
| `scripts/orchestrator.py` (modify) | `status` annotation (Task 2); `cmd_reconcile` (Task 3); `cmd_wait` + `_report_wait_result` (Task 4); `--wait`/`--poll` on the three dispatch parsers + wiring (Task 5); parser/main() registration (Tasks 3–5). |
| `tests/test_abandonment.py` (create) | Predicate and evidence unit tests, liveness injected (Task 1). |
| `tests/test_status_report.py` (modify) | Annotation tests + read-only assertion (Task 2). |
| `tests/test_reconcile.py` (create) | Recovery command tests (Task 3). |
| `tests/test_wait.py` (create) | Wait-loop tests with fake clock + fast `cmd_wait` exit-code tests (Task 4). |
| `tests/test_dispatch_integration.py` (modify) | `--wait` end-to-end and non-blocking default (Task 5). |
| `docs/configuration.md` (modify) | "Waiting on a dispatch, and recovering an abandoned one" section (Task 6). |
| `.claude-plugin/plugin.json`, `package.json`, `tests/test_docs_consistency.py` (modify) | Version 2.9.0 → 2.10.0 (Task 6). |

---

### Task 1: Abandonment predicate (`scripts/abandonment.py`)

**Files:**
- Create: `scripts/abandonment.py`
- Modify: `scripts/locks.py:63-79` (`_lock_is_held`)
- Test: `tests/test_abandonment.py`

**Interfaces:**
- Consumes: `scripts.locks._lock_is_held`, `scripts.paths.lock_path`, `scripts.utils._is_process_alive`, `scripts.locks.acquire_slice_lock` / `claim_slice_lock` (tests).
- Produces (used by Tasks 2, 3, 4):
  - `in_progress_statuses(config: dict) -> set[str]`
  - `is_abandoned(status: str, slice_id: str, project_root: Path, in_progress: set[str], *, is_alive=_is_process_alive) -> bool`
  - `lock_evidence(slice_id: str, project_root: Path, *, is_alive=_is_process_alive) -> str`
  - `_lock_is_held(data: dict, *, is_alive=_is_process_alive) -> bool` (new keyword, default unchanged)

- [x] **Step 1: Write the failing tests**

Create `tests/test_abandonment.py`:

```python
"""Abandonment is a derived fact: in-progress status + no live supervisor.

Every case injects liveness, so these tests never touch a real process table.
"""

import json
import time

from scripts import abandonment
from scripts.config import load_agent_config
from scripts.locks import (
    LOCK_START_GRACE_SECONDS,
    acquire_slice_lock,
    claim_slice_lock,
)
from scripts.paths import lock_path

ALWAYS_ALIVE = lambda pid: True
ALWAYS_DEAD = lambda pid: False

IN_PROGRESS = {"PLANNING", "EXECUTING"}


def _running_lock(project_root, pid, slice_id="slice-01"):
    lock_file = acquire_slice_lock(slice_id, project_root)
    claim_slice_lock(lock_file, pid)
    return lock_file


def test_alive_supervisor_plus_in_progress_status_is_not_abandoned(tmp_path):
    _running_lock(tmp_path, pid=1234)
    assert not abandonment.is_abandoned(
        "EXECUTING", "slice-01", tmp_path, IN_PROGRESS, is_alive=ALWAYS_ALIVE
    )


def test_dead_supervisor_plus_in_progress_status_is_abandoned(tmp_path):
    _running_lock(tmp_path, pid=1234)
    assert abandonment.is_abandoned(
        "EXECUTING", "slice-01", tmp_path, IN_PROGRESS, is_alive=ALWAYS_DEAD
    )


def test_dead_supervisor_plus_terminal_status_is_not_abandoned(tmp_path):
    """The dispatch ended; the runner recorded it before dying."""
    _running_lock(tmp_path, pid=1234)
    assert not abandonment.is_abandoned(
        "EXECUTION_COMPLETE", "slice-01", tmp_path, IN_PROGRESS, is_alive=ALWAYS_DEAD
    )


def test_missing_lock_plus_in_progress_status_is_abandoned(tmp_path):
    """A slice nothing owns is exactly what a human should be told about."""
    assert abandonment.is_abandoned(
        "PLANNING", "slice-01", tmp_path, IN_PROGRESS, is_alive=ALWAYS_ALIVE
    )


def test_missing_lock_plus_terminal_status_is_not_abandoned(tmp_path):
    assert not abandonment.is_abandoned(
        "PLAN_GENERATED", "slice-01", tmp_path, IN_PROGRESS, is_alive=ALWAYS_DEAD
    )


def test_a_starting_lock_inside_the_grace_window_is_not_abandoned(tmp_path):
    """Dispatch sets the in-progress status BEFORE spawning the supervisor;
    the gap between those two events must not read as abandonment."""
    acquire_slice_lock("slice-01", tmp_path)
    assert not abandonment.is_abandoned(
        "PLANNING", "slice-01", tmp_path, IN_PROGRESS, is_alive=ALWAYS_DEAD
    )


def test_a_starting_lock_past_the_grace_window_is_abandoned(tmp_path):
    lock_file = acquire_slice_lock("slice-01", tmp_path)
    data = json.loads(lock_file.read_text(encoding="utf-8"))
    data["started_at"] = time.time() - (LOCK_START_GRACE_SECONDS + 1)
    lock_file.write_text(json.dumps(data), encoding="utf-8")
    assert abandonment.is_abandoned(
        "PLANNING", "slice-01", tmp_path, IN_PROGRESS, is_alive=ALWAYS_DEAD
    )


def test_a_corrupt_lock_is_abandoned(tmp_path):
    lock_file = lock_path(tmp_path, "slice-01")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text("not json at all", encoding="utf-8")
    assert abandonment.is_abandoned(
        "EXECUTING", "slice-01", tmp_path, IN_PROGRESS, is_alive=ALWAYS_DEAD
    )


def test_in_progress_statuses_come_from_config_never_literals(tmp_path):
    """The regression that keeps the check honest for anyone who is not us:
    a project that renames EXECUTING must be detected by its own word."""
    (tmp_path / ".superpowers").mkdir()
    (tmp_path / ".superpowers" / "agents.yaml").write_text(
        "agents:\n"
        "  executor:\n"
        "    in_progress_status: WORKING\n",
        encoding="utf-8",
    )
    config = load_agent_config(tmp_path)
    in_progress = abandonment.in_progress_statuses(config)

    assert "WORKING" in in_progress
    _running_lock(tmp_path, pid=1234)
    assert abandonment.is_abandoned(
        "WORKING", "slice-01", tmp_path, in_progress, is_alive=ALWAYS_DEAD
    )
    assert not abandonment.is_abandoned(
        "EXECUTING", "slice-01", tmp_path, in_progress, is_alive=ALWAYS_DEAD
    )


def test_lock_evidence_names_the_dead_pid(tmp_path):
    _running_lock(tmp_path, pid=41676)
    evidence = abandonment.lock_evidence("slice-01", tmp_path, is_alive=ALWAYS_DEAD)
    assert "41676" in evidence
    assert "not alive" in evidence


def test_lock_evidence_names_the_absent_lock(tmp_path):
    evidence = abandonment.lock_evidence("slice-01", tmp_path, is_alive=ALWAYS_DEAD)
    assert "no lock file" in evidence
```

- [x] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_abandonment.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.abandonment'`

- [x] **Step 3: Make `_lock_is_held`'s liveness injectable**

In `scripts/locks.py`, change only the signature and the one call site inside `_lock_is_held` (lines 63-79). Everything else in the file is untouched:

```python
def _lock_is_held(data: dict, *, is_alive=_is_process_alive) -> bool:
    state = data.get("state")
    if state == "running":
        pid = data.get("pid")
        if not pid:
            return False
        try:
            return is_alive(int(pid))
        except (TypeError, ValueError):
            return False
    if state == "starting":
        try:
            started_at = float(data.get("started_at") or 0)
        except (TypeError, ValueError):
            return False
        return (time.time() - started_at) < LOCK_START_GRACE_SECONDS
    return False
```

- [x] **Step 4: Create `scripts/abandonment.py`**

```python
"""Abandoned-dispatch detection: a derived fact, never a stored one.

A slice is abandoned when its document sits at some role's in_progress_status
while no live supervisor owns it — the slice's lock is absent, unreadable, or
names a process that is gone. Both halves already exist in the codebase: the
status set comes from the merged config (never a hardcoded literal, so a
project that renames EXECUTING is detected by its own word), and liveness is
the same check that already governs lock reclamation. The lock self-heals;
the status does not, and the status is what the gates read.

Nothing here is ever written into a document or a lock — a stored "abandoned"
flag would go stale the moment someone re-dispatches.
"""

import json
from pathlib import Path

from scripts.locks import _lock_is_held
from scripts.paths import lock_path
from scripts.utils import _is_process_alive


def in_progress_statuses(config: dict) -> set[str]:
    """Every status some configured role treats as 'work in flight'."""
    return {
        agent.get("in_progress_status")
        for agent in (config.get("agents") or {}).values()
    } - {None}


def _read_lock(lock_file: Path) -> dict:
    try:
        return json.loads(lock_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def is_abandoned(
    status: str,
    slice_id: str,
    project_root: Path,
    in_progress: set[str],
    *,
    is_alive=_is_process_alive,
) -> bool:
    """True when the document claims in-progress work no live supervisor owns.

    A `starting` lock inside its grace window counts as owned: dispatch sets
    the in-progress status before the supervisor exists, and that gap is not
    abandonment.
    """
    if status not in in_progress:
        return False
    lock_file = lock_path(project_root, slice_id)
    if not lock_file.exists():
        return True
    return not _lock_is_held(_read_lock(lock_file), is_alive=is_alive)


def lock_evidence(slice_id: str, project_root: Path, *, is_alive=_is_process_alive) -> str:
    """What an abandonment verdict is based on, in one sentence.

    The operator is being asked to trust a verdict about a process they
    cannot see, so the verdict names its grounds: the lock's pid and its
    liveness, or the lock's absence.
    """
    lock_file = lock_path(project_root, slice_id)
    if not lock_file.exists():
        return f"no lock file at {lock_file} — nothing owns this slice"
    data = _read_lock(lock_file)
    if not data:
        return f"lock at {lock_file} is unreadable — nothing verifiably owns this slice"
    if data.get("state") == "running" and data.get("pid"):
        pid = data["pid"]
        try:
            alive = is_alive(int(pid))
        except (TypeError, ValueError):
            alive = False
        if alive:
            return f"lock names supervisor pid {pid}, which is alive"
        return f"lock names supervisor pid {pid}, which is not alive"
    if data.get("state") == "starting":
        return f"lock at {lock_file} is still 'starting' — the supervisor never claimed it"
    return f"lock at {lock_file} is in state {data.get('state')!r}"
```

- [x] **Step 5: Run the new tests to verify they pass**

Run: `python -m pytest tests/test_abandonment.py -q -p no:cacheprovider`
Expected: 12 passed

- [x] **Step 6: Run the full suite**

Run: `python -m pytest -q -p no:cacheprovider`
Expected: **437 passed** (425 baseline + 12 new)

- [x] **Step 7: Commit**

```bash
git add scripts/abandonment.py scripts/locks.py tests/test_abandonment.py
git commit -m "feat(abandonment): derive an abandoned dispatch from status and lock liveness

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: `status` annotates the abandoned case

**Files:**
- Modify: `scripts/orchestrator.py:39-45` (imports), `scripts/orchestrator.py:166-223` (`cmd_status`)
- Test: `tests/test_status_report.py` (append)

**Interfaces:**
- Consumes: `abandonment.in_progress_statuses`, `abandonment.is_abandoned`, `abandonment.lock_evidence` (Task 1).
- Produces: the annotation line format `⚠ abandoned: <evidence>; run \`reconcile\``, indented 23 columns so it aligns under the document name. Task 3's `reconcile` is the command it names.

- [x] **Step 1: Write the failing tests**

Append to `tests/test_status_report.py`. The file currently imports only `argparse` and `pytest`; extend the import block at the top to:

```python
import argparse
import json
import os

import pytest
```

Then append:

```python
def _lock(base, slice_id, payload):
    """A supervisor lock where cmd_status's project-root resolution looks."""
    locks = base / ".superpowers" / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    (locks / f"{slice_id}.lock").write_text(json.dumps(payload), encoding="utf-8")


def test_an_abandoned_dispatch_is_annotated(base, capsys):
    _doc(base, "plans", "p.md", '---\nslice_id: "a"\nstatus: EXECUTING\n---\n')
    # pid 999999999 is gone on any real process table — no injection needed.
    _lock(base, "a", {"state": "running", "pid": 999999999})

    _status(base)
    out = capsys.readouterr().out

    assert "EXECUTING" in out          # the stored status is still shown
    assert "abandoned" in out
    assert "999999999" in out
    assert "reconcile" in out


def test_a_live_supervisor_is_not_annotated(base, capsys):
    _doc(base, "plans", "p.md", '---\nslice_id: "a"\nstatus: EXECUTING\n---\n')
    _lock(base, "a", {"state": "running", "pid": os.getpid()})

    _status(base)

    assert "abandoned" not in capsys.readouterr().out


def test_a_missing_lock_is_reported_as_abandonment(base, capsys):
    _doc(base, "plans", "p.md", '---\nslice_id: "a"\nstatus: EXECUTING\n---\n')

    _status(base)
    out = capsys.readouterr().out

    assert "abandoned" in out
    assert "no lock file" in out


def test_status_mutates_nothing_it_reports(base, capsys):
    """A report that silently repairs is a worse instrument than one that lies."""
    _doc(base, "plans", "p.md", '---\nslice_id: "a"\nstatus: EXECUTING\n---\n')
    _lock(base, "a", {"state": "running", "pid": 999999999})
    before_doc = (base / "plans" / "p.md").read_text(encoding="utf-8")
    before_lock = (base / ".superpowers" / "locks" / "a.lock").read_text(encoding="utf-8")

    _status(base)

    assert (base / "plans" / "p.md").read_text(encoding="utf-8") == before_doc
    assert (base / ".superpowers" / "locks" / "a.lock").read_text(encoding="utf-8") == before_lock
```

Note for the implementer: `cmd_status` resolves `project_root = find_project_root(base_dir.resolve())`; in these tests `base` is `tmp_path/docs/superpowers`, no `.git` exists above it, so the root resolves to `base` itself and locks live at `base/.superpowers/locks/`.

- [x] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_status_report.py -q -p no:cacheprovider`
Expected: FAIL — the three annotation tests fail on `assert "abandoned" in out`

- [x] **Step 3: Implement the annotation**

In `scripts/orchestrator.py`, add the import (keep it with the other `from scripts import ...` lines, just above `from scripts import milestone as milestone_mod`):

```python
from scripts import abandonment
```

In `cmd_status`, replace the config-loading block:

```python
    try:
        config = load_agent_config(find_project_root(base_dir.resolve()))
    except OrchestratorError:
        # A report is a read. An unusable config is worth reporting elsewhere,
        # not worth refusing to say what is on disk.
        config = DEFAULT_CONFIG
```

with:

```python
    project_root = find_project_root(base_dir.resolve())
    try:
        config = load_agent_config(project_root)
    except OrchestratorError:
        # A report is a read. An unusable config is worth reporting elsewhere,
        # not worth refusing to say what is on disk.
        config = DEFAULT_CONFIG
    in_progress = abandonment.in_progress_statuses(config)
```

Then, immediately after the row print (`print(f"  [{(label or 'no status'):<18}] {filepath.name} - {title}{suffix}")`), add:

```python
            # An in-progress status with no live supervisor behind it is a
            # contradiction, not a fact. The stored value stays visible —
            # hiding it would be its own lie — but the report no longer
            # implies the work is running. Read-only: repair is reconcile's
            # job, and a report that silently mutates state is a worse
            # instrument than one that lies quietly.
            if label in in_progress:
                slice_id = data.get("slice_id", filepath.stem)
                if abandonment.is_abandoned(label, slice_id, project_root, in_progress):
                    evidence = abandonment.lock_evidence(slice_id, project_root)
                    print(f"{'':23}⚠ abandoned: {evidence}; run `reconcile`")
```

(The 23-space indent aligns the `⚠` under the document name: 2 leading spaces + `[` + 18-char label + `]` + space.)

- [x] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_status_report.py tests/test_abandonment.py -q -p no:cacheprovider`
Expected: all pass

- [x] **Step 5: Run the full suite**

Run: `python -m pytest -q -p no:cacheprovider`
Expected: **441 passed** (437 + 4 new)

- [x] **Step 6: Commit**

```bash
git add scripts/orchestrator.py tests/test_status_report.py
git commit -m "feat(status): annotate a dispatch whose supervisor is gone

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: `reconcile` — the way out, without a false claim

**Files:**
- Modify: `scripts/orchestrator.py:39` (locks import), `scripts/orchestrator.py` (new `cmd_reconcile`, placed after `cmd_summary`), parser + dispatch in `main()`
- Test: `tests/test_reconcile.py` (create)

**Interfaces:**
- Consumes: `abandonment.in_progress_statuses`, `abandonment.is_abandoned`, `abandonment.lock_evidence` (Task 1); `milestone_mod.machine_for`, `milestone_mod.SLICE_KIND`, `milestone_mod.MILESTONE_KIND`, `milestone_mod.document_kind`; `update_frontmatter_status`; `release_slice_lock`.
- Produces: CLI `reconcile --file <document> [--dir DIR] [--yes]`. Exit codes: `0` applied; `1` refusal (live supervisor, terminal status, milestone, bad config, illegal transition); `2` refused for missing `--yes`, evidence printed, nothing mutated.

- [x] **Step 1: Write the failing tests**

Create `tests/test_reconcile.py`:

```python
"""reconcile moves an abandoned dispatch's document to FAILED — the truthful
statement about the dispatch — and releases the stale lock. It refuses to
race a live supervisor, and refuses to assert anything without --yes."""

import argparse
import os

import pytest

from scripts.frontmatter import parse_frontmatter
from scripts.locks import acquire_slice_lock, claim_slice_lock
from scripts.orchestrator import cmd_reconcile
from scripts.paths import lock_path


@pytest.fixture
def project(tmp_path):
    (tmp_path / ".superpowers").mkdir()
    docs = tmp_path / "docs" / "superpowers" / "plans"
    docs.mkdir(parents=True)
    doc = docs / "2026-08-04-slice-01-plan.md"
    doc.write_text(
        '---\nslice_id: "slice-01"\nstatus: EXECUTING\n---\n\n# Plan\n',
        encoding="utf-8",
    )
    return tmp_path, doc


def _args(doc, **overrides):
    return argparse.Namespace(
        **{"file": str(doc), "dir": "", "yes": True, **overrides}
    )


def _dead_lock(project_root, slice_id="slice-01"):
    lock_file = acquire_slice_lock(slice_id, project_root)
    claim_slice_lock(lock_file, 999999999)   # gone on any real process table
    return lock_file


def _status(doc):
    return parse_frontmatter(doc.read_text(encoding="utf-8"))["status"]


def test_reconcile_moves_an_abandoned_dispatch_to_failed(project, capsys):
    root, doc = project
    _dead_lock(root)

    cmd_reconcile(_args(doc))

    assert _status(doc) == "FAILED"
    assert not lock_path(root, "slice-01").exists()
    out = capsys.readouterr().out
    assert "999999999" in out          # the verdict names its grounds
    assert "not alive" in out


def test_reconcile_refuses_a_live_supervisor(project):
    """Reconciling a running dispatch would race the runner's own epilogue."""
    root, doc = project
    lock_file = acquire_slice_lock("slice-01", root)
    claim_slice_lock(lock_file, os.getpid())

    with pytest.raises(SystemExit) as excinfo:
        cmd_reconcile(_args(doc))

    assert excinfo.value.code == 1
    assert _status(doc) == "EXECUTING"
    assert lock_file.exists()


def test_reconcile_refuses_a_terminal_status(project):
    root, doc = project
    doc.write_text(
        doc.read_text(encoding="utf-8").replace("EXECUTING", "EXECUTION_COMPLETE"),
        encoding="utf-8",
    )
    _dead_lock(root)

    with pytest.raises(SystemExit) as excinfo:
        cmd_reconcile(_args(doc))

    assert excinfo.value.code == 1
    assert _status(doc) == "EXECUTION_COMPLETE"
    assert lock_path(root, "slice-01").exists()


def test_reconcile_without_yes_prints_the_evidence_and_mutates_nothing(project, capsys):
    root, doc = project
    _dead_lock(root)

    with pytest.raises(SystemExit) as excinfo:
        cmd_reconcile(_args(doc, yes=False))

    assert excinfo.value.code == 2
    assert _status(doc) == "EXECUTING"
    assert lock_path(root, "slice-01").exists()
    out = capsys.readouterr().out
    assert "999999999" in out          # evidence is shown even when refusing
    assert "--yes" in out


def test_a_second_reconcile_refuses_cleanly(project):
    """Idempotent in the only sense that matters: no corruption, a clear no."""
    root, doc = project
    _dead_lock(root)
    cmd_reconcile(_args(doc))
    assert _status(doc) == "FAILED"

    with pytest.raises(SystemExit) as excinfo:
        cmd_reconcile(_args(doc))

    assert excinfo.value.code == 1
    assert _status(doc) == "FAILED"


def test_reconcile_a_milestone_is_refused(project, tmp_path):
    root, _doc = project
    milestone = tmp_path / "docs" / "superpowers" / "milestones" / "m.md"
    milestone.parent.mkdir(parents=True)
    milestone.write_text(
        '---\nkind: milestone\nstatus: MILESTONE_ACTIVE\n---\n', encoding="utf-8"
    )

    with pytest.raises(SystemExit):
        cmd_reconcile(_args(milestone))
```

- [x] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_reconcile.py -q -p no:cacheprovider`
Expected: FAIL — `ImportError: cannot import name 'cmd_reconcile' from 'scripts.orchestrator'`

- [x] **Step 3: Implement `cmd_reconcile`**

In `scripts/orchestrator.py`, extend the locks import:

```python
from scripts.locks import acquire_slice_lock, release_slice_lock, release_slice_lock_file
```

Add `cmd_reconcile` immediately after `cmd_summary`:

```python
def cmd_reconcile(args):
    """Move an abandoned dispatch's document out of its in-progress state.

    Legal only when the document sits at some role's in_progress_status AND
    no live supervisor owns the slice. Moves it to FAILED — the one status
    that describes the *dispatch* truthfully: nobody recorded an outcome.
    What FAILED deliberately does not say is anything about the *work*;
    judging that is the audit the pipeline already requires before
    close-slice.
    """
    filepath = Path(args.file).resolve()
    if not filepath.is_file():
        print(f"Error: --file '{filepath}' is not a file.")
        sys.exit(1)

    project_root = (
        Path(args.dir).resolve() if getattr(args, "dir", "")
        else find_project_root(filepath)
    )

    try:
        config = load_agent_config(project_root)
        validate_config(config)
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    frontmatter = parse_frontmatter(filepath.read_text(encoding="utf-8"))
    if milestone_mod.document_kind(frontmatter) == milestone_mod.MILESTONE_KIND:
        print(
            f"Error: {filepath.name} is a milestone brief; no supervisor ever "
            f"owns one, so there is nothing to reconcile."
        )
        sys.exit(1)

    slice_id = frontmatter.get("slice_id", filepath.stem)
    current_status = frontmatter.get("status", "UNKNOWN")
    in_progress = abandonment.in_progress_statuses(config)

    if current_status not in in_progress:
        print(
            f"Error: {filepath.name} is at '{current_status}', which no role "
            f"treats as in-progress ({sorted(in_progress)}). Nothing to reconcile."
        )
        sys.exit(1)

    if not abandonment.is_abandoned(current_status, slice_id, project_root, in_progress):
        print(
            f"Error: a live supervisor owns slice '{slice_id}'. Reconciling a "
            f"running dispatch would race its epilogue — let it finish."
        )
        sys.exit(1)

    evidence = abandonment.lock_evidence(slice_id, project_root)
    print(f"Slice '{slice_id}' is abandoned:")
    print(f"   status: {current_status}")
    print(f"   {evidence}")

    if not getattr(args, "yes", False):
        print(
            f"Refusing to mark {filepath.name} FAILED without --yes. Audit the "
            f"work itself first — FAILED records that the dispatch went "
            f"unrecorded, not that the work is bad — then re-run with --yes."
        )
        sys.exit(2)

    valid_statuses, transitions = milestone_mod.machine_for(
        milestone_mod.SLICE_KIND, config
    )
    if not update_frontmatter_status(filepath, "FAILED", valid_statuses, transitions):
        print(
            f"Error: could not move '{slice_id}' from '{current_status}' to "
            f"FAILED. The stale lock was kept; if this role's machine declares "
            f"no transition to FAILED, add one in .superpowers/agents.yaml."
        )
        sys.exit(1)

    release_slice_lock(slice_id, project_root)
    print(f"Released the stale lock for '{slice_id}'.")
    print("Re-enter the pipeline from FAILED via SPEC_APPROVED or PLAN_APPROVED.")
```

- [x] **Step 4: Register the command**

In `main()`, add the parser next to the `summary` parser:

```python
    # reconcile
    p_reconcile = subparsers.add_parser(
        "reconcile",
        help="Move an abandoned dispatch's document to FAILED and release its stale lock",
    )
    p_reconcile.add_argument("--file", required=True, help="Path to the slice document")
    p_reconcile.add_argument(
        "--dir", default="", help="Project root (default: derived from --file)"
    )
    p_reconcile.add_argument(
        "--yes", action="store_true", help="Apply the move to FAILED"
    )
```

and the dispatch branch, next to `summary`:

```python
    elif args.command == "reconcile":
        cmd_reconcile(args)
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_reconcile.py -q -p no:cacheprovider`
Expected: 6 passed

- [x] **Step 6: Run the full suite**

Run: `python -m pytest -q -p no:cacheprovider`
Expected: **447 passed** (441 + 6 new)

- [x] **Step 7: Commit**

```bash
git add scripts/orchestrator.py tests/test_reconcile.py
git commit -m "feat(reconcile): move an abandoned dispatch to FAILED and release its lock

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: `wait` — a join, so abandonment is found in bounded time

**Files:**
- Modify: `scripts/abandonment.py` (append wait machinery)
- Modify: `scripts/orchestrator.py` (new `cmd_wait` + `_report_wait_result` after `cmd_reconcile`; parser + dispatch in `main()`)
- Test: `tests/test_wait.py` (create)

**Interfaces:**
- Consumes: Task 1 predicate; `parse_frontmatter`; `logs_dir`.
- Produces (Task 5 consumes all of these):
  - `abandonment.DEFAULT_POLL_SECONDS = 15.0`
  - `abandonment.OUTCOME_TERMINAL` / `OUTCOME_ABANDONED` / `OUTCOME_TIMED_OUT` (`"terminal"` / `"abandoned"` / `"timed_out"`)
  - `abandonment.find_slice_document(base_dir: Path, slice_id: str) -> Path | None`
  - `abandonment.latest_log(project_root: Path, slice_id: str) -> Path | None`
  - `abandonment.WaitResult` — dataclass with fields `outcome: str`, `status: str`, `elapsed: float`
  - `abandonment.wait_for_dispatch(document: Path, project_root: Path, config: dict, slice_id: str, *, timeout: float | None = None, poll: float = DEFAULT_POLL_SECONDS, sleep=time.sleep, monotonic=time.monotonic, is_alive=_is_process_alive) -> WaitResult`
  - `orchestrator._report_wait_result(slice_id: str, document: Path, project_root: Path, result: WaitResult) -> int` (prints the last line, returns the exit code)
  - CLI `wait --slice <slice_id> [--dir DIR] [--timeout S] [--poll S]`. Exit `0` status left in-progress; `2` abandoned; `1` timeout; **`3` could not start waiting** (unknown slice id, unreadable config).

**Architect's amendment (audit gate).** The generated plan gave `1` to both a
timeout and an unknown slice. Separate them. `wait` exists to be run by a
machine and branched on by its exit code, and those two mean opposite things:
`1` says "not finished yet, ask again later", `3` says "this will never
finish, you named something that does not exist". Conflated, a typo in
`--slice` reads as a slow run and the caller waits forever for it. The human
message is not enough — the caller reading the code is the one that has to act.

- [x] **Step 1: Write the failing tests**

Create `tests/test_wait.py`:

```python
"""wait is a join over a dispatch: it returns when the status leaves the
role's in-progress set (exit 0), when the supervisor dies with the status
unchanged (exit 2), or when the caller's patience runs out (exit 1).

Liveness and the clock are injected into the loop itself, so these are fast
and not flaky. The cmd_wait tests use only first-iteration paths, which need
no injection at all."""

import argparse

import pytest

from scripts import abandonment
from scripts.config import DEFAULT_CONFIG
from scripts.locks import acquire_slice_lock, claim_slice_lock
from scripts.orchestrator import cmd_wait


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def document(tmp_path):
    (tmp_path / ".superpowers").mkdir()
    docs = tmp_path / "docs" / "superpowers" / "plans"
    docs.mkdir(parents=True)
    doc = docs / "2026-08-04-slice-01-plan.md"
    doc.write_text(
        '---\nslice_id: "slice-01"\nstatus: PLANNING\n---\n\n# Plan\n',
        encoding="utf-8",
    )
    return tmp_path, doc


def _running_lock(project_root, pid=1234):
    lock_file = acquire_slice_lock("slice-01", project_root)
    claim_slice_lock(lock_file, pid)
    return lock_file


def _set_status(doc, status):
    text = doc.read_text(encoding="utf-8")
    doc.write_text(text.replace("PLANNING", status), encoding="utf-8")


def test_wait_returns_terminal_when_the_status_moves(document):
    root, doc = document
    _running_lock(root)
    clock = FakeClock()

    def sleep_then_advance(seconds):
        _set_status(doc, "PLAN_GENERATED")     # the runner lands its epilogue
        clock.sleep(seconds)

    result = abandonment.wait_for_dispatch(
        doc, root, DEFAULT_CONFIG, "slice-01",
        sleep=sleep_then_advance, monotonic=clock.monotonic,
        is_alive=lambda pid: True, poll=15.0,
    )

    assert result.outcome == abandonment.OUTCOME_TERMINAL
    assert result.status == "PLAN_GENERATED"
    assert result.elapsed == 15.0


def test_wait_returns_abandoned_when_the_pid_dies_with_status_unchanged(document):
    root, doc = document
    _running_lock(root)
    clock = FakeClock()
    alive_checks = iter([True, False])         # alive at dispatch, gone a poll later

    result = abandonment.wait_for_dispatch(
        doc, root, DEFAULT_CONFIG, "slice-01",
        sleep=clock.sleep, monotonic=clock.monotonic,
        is_alive=lambda pid: next(alive_checks), poll=15.0,
    )

    assert result.outcome == abandonment.OUTCOME_ABANDONED
    assert result.status == "PLANNING"


def test_wait_returns_timed_out_when_neither_happens(document):
    root, doc = document
    _running_lock(root)
    clock = FakeClock()

    result = abandonment.wait_for_dispatch(
        doc, root, DEFAULT_CONFIG, "slice-01", timeout=30.0,
        sleep=clock.sleep, monotonic=clock.monotonic,
        is_alive=lambda pid: True, poll=15.0,
    )

    assert result.outcome == abandonment.OUTCOME_TIMED_OUT
    assert result.status == "PLANNING"
    assert result.elapsed >= 30.0


def _args(base, **overrides):
    return argparse.Namespace(**{
        "dir": str(base), "slice": "slice-01", "timeout": None, "poll": 15.0,
        **overrides,
    })


def test_cmd_wait_exits_0_for_an_already_terminal_slice(document, capsys):
    root, doc = document
    _set_status(doc, "PLAN_GENERATED")

    with pytest.raises(SystemExit) as excinfo:
        cmd_wait(_args(root / "docs" / "superpowers"))

    assert excinfo.value.code == 0
    assert "PLAN_GENERATED" in capsys.readouterr().out


def test_cmd_wait_exits_2_and_names_reconcile_for_an_abandoned_slice(document, capsys):
    root, doc = document
    _running_lock(root, pid=999999999)         # gone on any real process table

    with pytest.raises(SystemExit) as excinfo:
        cmd_wait(_args(root / "docs" / "superpowers"))

    assert excinfo.value.code == 2
    out = capsys.readouterr().out
    assert "abandoned" in out
    assert "reconcile" in out


def test_cmd_wait_exits_1_when_the_timeout_elapses(document, capsys):
    root, doc = document
    acquire_slice_lock("slice-01", root)       # starting, inside the grace window

    with pytest.raises(SystemExit) as excinfo:
        cmd_wait(_args(root / "docs" / "superpowers", timeout=0))

    assert excinfo.value.code == 1
    assert "Timed out" in capsys.readouterr().out


def test_cmd_wait_refuses_an_unknown_slice_id(tmp_path, capsys):
    """Exit 3, not 1. A timeout means "not finished yet, ask again"; an unknown
    slice means "this will never finish". A caller branching on the code must
    be able to tell those apart, or a typo in --slice reads as a slow run and
    it waits for something that does not exist."""
    (tmp_path / ".superpowers").mkdir()

    with pytest.raises(SystemExit) as excinfo:
        cmd_wait(_args(tmp_path))

    assert excinfo.value.code == 3
    assert "no document" in capsys.readouterr().out.lower()


def test_cmd_wait_separates_cannot_start_from_timed_out(document, tmp_path, capsys):
    """The two codes must not collapse: the same call that times out on a real
    slice must report a different code than a call that cannot start at all."""
    root, _doc = document
    acquire_slice_lock("slice-01", root)

    with pytest.raises(SystemExit) as timed_out:
        cmd_wait(_args(root / "docs" / "superpowers", timeout=0))
    capsys.readouterr()

    (tmp_path / ".superpowers").mkdir()
    with pytest.raises(SystemExit) as cannot_start:
        cmd_wait(_args(tmp_path))

    assert timed_out.value.code != cannot_start.value.code
```

- [x] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_wait.py -q -p no:cacheprovider`
Expected: FAIL — `AttributeError: module 'scripts.abandonment' has no attribute 'wait_for_dispatch'` (and `ImportError` for `cmd_wait`)

- [x] **Step 3: Append the wait machinery to `scripts/abandonment.py`**

Extend the import block at the top of `scripts/abandonment.py` to:

```python
import json
import time
from dataclasses import dataclass
from pathlib import Path

from scripts.frontmatter import parse_frontmatter
from scripts.locks import _lock_is_held
from scripts.paths import lock_path, logs_dir
from scripts.utils import _is_process_alive
```

Append to the same file:

```python
DEFAULT_POLL_SECONDS = 15.0

OUTCOME_TERMINAL = "terminal"
OUTCOME_ABANDONED = "abandoned"
OUTCOME_TIMED_OUT = "timed_out"


def find_slice_document(base_dir: Path, slice_id: str) -> Path | None:
    """The specs/ or plans/ document carrying this slice_id, if one exists."""
    for subdir in ("specs", "plans"):
        directory = Path(base_dir) / subdir
        if not directory.is_dir():
            continue
        for candidate in sorted(directory.glob("*.md")):
            if candidate.stem == slice_id:
                return candidate
            data = parse_frontmatter(candidate.read_text(encoding="utf-8"))
            if data.get("slice_id") == slice_id:
                return candidate
    return None


def latest_log(project_root: Path, slice_id: str) -> Path | None:
    """The newest log mentioning this slice, or None — same rule as `summary`."""
    directory = logs_dir(project_root)
    matching = sorted(directory.glob(f"*{slice_id}*.log")) if directory.exists() else []
    if not matching:
        return None
    return max(matching, key=lambda path: path.stat().st_mtime)


@dataclass
class WaitResult:
    outcome: str   # OUTCOME_TERMINAL | OUTCOME_ABANDONED | OUTCOME_TIMED_OUT
    status: str    # the document's status at the moment the wait ended
    elapsed: float


def wait_for_dispatch(
    document: Path,
    project_root: Path,
    config: dict,
    slice_id: str,
    *,
    timeout: float | None = None,
    poll: float = DEFAULT_POLL_SECONDS,
    sleep=time.sleep,
    monotonic=time.monotonic,
    is_alive=_is_process_alive,
) -> WaitResult:
    """Block while the slice is in progress and a live supervisor owns it.

    The watched thing takes minutes, so the default poll is 15s — a tighter
    loop only burns wakeups. The default timeout is none: the caller that
    backgrounds this process has its own, and a timeout belongs at the
    boundary that can act on it. Liveness and the clock are injectable so
    the tests are fast and not flaky.
    """
    in_progress = in_progress_statuses(config)
    started = monotonic()
    while True:
        status = parse_frontmatter(
            Path(document).read_text(encoding="utf-8")
        ).get("status", "UNKNOWN")
        elapsed = monotonic() - started
        if status not in in_progress:
            return WaitResult(OUTCOME_TERMINAL, status, elapsed)
        if is_abandoned(status, slice_id, project_root, in_progress, is_alive=is_alive):
            return WaitResult(OUTCOME_ABANDONED, status, elapsed)
        if timeout is not None and elapsed >= timeout:
            return WaitResult(OUTCOME_TIMED_OUT, status, elapsed)
        sleep(poll)
```

- [x] **Step 4: Implement `cmd_wait` and `_report_wait_result`**

In `scripts/orchestrator.py`, immediately after `cmd_reconcile`, add:

```python
def _report_wait_result(slice_id: str, document: Path, project_root: Path, result) -> int:
    """Print the last line and return the exit code.

    The last line names the terminal status, the elapsed time and the log
    path, so the caller needs no second command. The abandoned branch names
    `reconcile` rather than leaving the operator to look it up.
    """
    log = abandonment.latest_log(project_root, slice_id) or "(no log found)"
    if result.outcome == abandonment.OUTCOME_TERMINAL:
        print(
            f"Slice '{slice_id}' reached '{result.status}' after "
            f"{result.elapsed:.0f}s. Log: {log}"
        )
        return 0
    if result.outcome == abandonment.OUTCOME_ABANDONED:
        evidence = abandonment.lock_evidence(slice_id, project_root)
        print(
            f"Slice '{slice_id}' is abandoned after {result.elapsed:.0f}s: "
            f"{evidence}."
        )
        print(f"Status is still '{result.status}'. Log: {log}")
        print(f"Audit the work, then run: reconcile --file {document} --yes")
        return 2
    print(
        f"Timed out after {result.elapsed:.0f}s; slice '{slice_id}' is still "
        f"'{result.status}'. Log: {log}"
    )
    return 1


def cmd_wait(args):
    """Block until a slice's dispatch ends — by finishing or by abandonment."""
    base_dir = Path(args.dir) if args.dir else Path("docs/superpowers")
    project_root = find_project_root(base_dir.resolve())

    try:
        config = load_agent_config(project_root)
        validate_config(config)
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(3)

    document = abandonment.find_slice_document(base_dir, args.slice)
    if document is None:
        print(f"Error: no document under {base_dir} carries slice_id '{args.slice}'.")
        sys.exit(3)

    result = abandonment.wait_for_dispatch(
        document, project_root, config, args.slice,
        timeout=args.timeout, poll=args.poll,
    )
    sys.exit(_report_wait_result(args.slice, document, project_root, result))
```

- [x] **Step 5: Register the command**

In `main()`, add the parser next to `reconcile`:

```python
    # wait
    p_wait = subparsers.add_parser(
        "wait",
        help="Block until a slice's dispatch ends "
             "(exit 0 finished, 2 abandoned, 1 timeout, 3 cannot start)",
    )
    p_wait.add_argument("--slice", required=True, help="Slice ID to wait on")
    # NOTE (architect, audit gate): `--dir` here means the *docs base*, matching
    # `status`, not the *project root*, which is what it means for `sandbox`,
    # `trigger-hook` and `summary`. That split is issue #11 and this slice does
    # not resolve it — but do not invent a third meaning: keep `wait` identical
    # to `status`, so whatever #11 settles can change both together.
    p_wait.add_argument(
        "--dir", default="docs/superpowers", help="Base superpowers directory (as in `status`)"
    )
    p_wait.add_argument(
        "--timeout", type=float, default=None,
        help="Give up after S seconds (default: never)",
    )
    p_wait.add_argument(
        "--poll", type=float, default=abandonment.DEFAULT_POLL_SECONDS,
        help="Seconds between checks (default: 15)",
    )
```

and the dispatch branch:

```python
    elif args.command == "wait":
        cmd_wait(args)
```

- [x] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_wait.py -q -p no:cacheprovider`
Expected: 7 passed

- [x] **Step 7: Run the full suite**

Run: `python -m pytest -q -p no:cacheprovider`
Expected: **454 passed** (447 + 7 new)

- [x] **Step 8: Commit**

```bash
git add scripts/abandonment.py scripts/orchestrator.py tests/test_wait.py
git commit -m "feat(wait): join a dispatch — exit 0 finished, 2 abandoned, 1 timeout

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: `dispatch --wait` — dispatch followed by wait in one process

**Files:**
- Modify: `scripts/orchestrator.py` (three dispatch parsers; end of `cmd_dispatch_agent`, after the `_warn_if_*` calls at lines 604-607)
- Test: `tests/test_dispatch_integration.py` (append)

**Interfaces:**
- Consumes: `abandonment.wait_for_dispatch`, `abandonment.DEFAULT_POLL_SECONDS`, `orchestrator._report_wait_result` (Task 4).
- Produces: `--wait` and `--poll` flags on `dispatch-agent`, `dispatch-planner`, `dispatch-executor`. Without `--wait`, behaviour is byte-identical to today. With `--wait`, the process exits with the wait outcome: `0` finished, `2` abandoned, `1` timeout.

- [x] **Step 1: Write the failing tests**

Append to `tests/test_dispatch_integration.py`:

```python
def test_dispatch_wait_blocks_until_the_supervisor_reports(tmp_project, demo_spec):
    _use_slow_agent(tmp_project, seconds=3)
    args = _args(demo_spec)
    args.wait = True
    args.poll = 0.2

    with pytest.raises(SystemExit) as excinfo:
        cmd_dispatch_agent(args)

    assert excinfo.value.code == 0
    assert (
        parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"]
        == "PLAN_GENERATED"
    )


def test_dispatch_without_wait_returns_before_the_agent_finishes(tmp_project, demo_spec):
    """The default stays non-blocking: dispatch returns at spawn, and the
    document still claims its in-progress status when it does. A caller that
    backgrounds plain dispatch is notified the instant the supervisor is
    *spawned* — precisely the useless signal --wait exists to replace."""
    _use_slow_agent(tmp_project, seconds=20)
    cmd_dispatch_agent(_args(demo_spec))     # no wait attribute at all

    assert (
        parse_frontmatter(demo_spec.read_text(encoding="utf-8"))["status"]
        == "PLANNING"
    )

    data = _wait_for_lock_state(lock_path(tmp_project, "slice-01-demo"), "running")
    if data:                                 # don't leak a 20s sleeper
        _kill_tree(data["pid"])
```

- [x] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_dispatch_integration.py::test_dispatch_wait_blocks_until_the_supervisor_reports -q -p no:cacheprovider`
Expected: FAIL — `DID NOT RAISE SystemExit` (the flag is not threaded yet)

- [x] **Step 3: Add the flags and the wiring**

In `main()`, add to `p_agent` (after its `--model` argument):

```python
    p_agent.add_argument(
        "--wait", action="store_true",
        help="Block until the dispatch ends, then exit with its outcome "
             "(0 finished, 2 abandoned, 1 timeout)",
    )
    p_agent.add_argument(
        "--poll", type=float, default=None,
        help="With --wait: seconds between checks (default: 15)",
    )
```

Add the same two arguments to `p_plan` (after its `--model`) and to `p_exec` (after its `--model`) — identical text.

At the very end of `cmd_dispatch_agent`, after the four `_warn_if_*` calls, add:

```python
    if getattr(args, "wait", False):
        # dispatch --wait is dispatch followed by wait in one process: the
        # form a harness actually backgrounds. Backgrounding plain dispatch
        # notifies the instant the supervisor is *spawned* — precisely the
        # useless signal a caller has without this flag. The default (no
        # flag) is unchanged: non-blocking, returning here at spawn.
        result = abandonment.wait_for_dispatch(
            target_file, project_root, config, slice_id,
            poll=getattr(args, "poll", None) or abandonment.DEFAULT_POLL_SECONDS,
        )
        sys.exit(_report_wait_result(slice_id, target_file, project_root, result))
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_dispatch_integration.py -q -p no:cacheprovider`
Expected: all pass, including the two new ones

- [x] **Step 5: Run the full suite**

Run: `python -m pytest -q -p no:cacheprovider`
Expected: **456 passed** (454 + 2 new)

- [x] **Step 6: Commit**

```bash
git add scripts/orchestrator.py tests/test_dispatch_integration.py
git commit -m "feat(dispatch): --wait blocks until the supervisor reports

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Documentation and version 2.10.0

**Files:**
- Modify: `docs/configuration.md` (new section after `## Adding Custom Agents`, before `## Infrastructure Hooks`)
- Modify: `.claude-plugin/plugin.json:4`, `package.json:3`
- Modify: `tests/test_docs_consistency.py:61` and `tests/test_docs_consistency.py:380` (pinned version literals)
- Test: `tests/test_docs_consistency.py` (one new test)

**Interfaces:**
- Consumes: everything above; no new code interfaces.
- Produces: user-facing documentation of `wait`, `reconcile`, `dispatch --wait`, and the Windows `kill -0` trap; plugin version `2.10.0`.

- [x] **Step 1: Write the failing test**

Append to `tests/test_docs_consistency.py`:

```python
def test_wait_and_reconcile_are_documented():
    """The escape hatch must be written down where an operator reads."""
    assert "reconcile" in CONFIGURATION
    assert "--wait" in CONFIGURATION
    assert "kill -0" in CONFIGURATION
```

- [x] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_docs_consistency.py::test_wait_and_reconcile_are_documented -q -p no:cacheprovider`
Expected: FAIL — `assert 'reconcile' in CONFIGURATION`

- [x] **Step 3: Add the documentation section**

In `docs/configuration.md`, insert this section immediately before `## Infrastructure Hooks (`.superpowers/hooks.yaml`)`:

````markdown
## Waiting on a dispatch, and recovering an abandoned one

`dispatch` returns immediately by design: the supervisor it spawns is
deliberately detached, and a blocking dispatch would hold your turn for the
whole run while leaving the agent running with nobody to record its outcome.
To be notified when a dispatch actually ends, background the blocking form
instead:

```bash
python scripts/orchestrator.py dispatch-agent --role executor --file <plan> --wait
```

`wait` (also standalone: `wait --slice <slice_id> [--timeout S] [--poll S]`)
blocks while the slice is in progress and a live supervisor owns it, then
exits `0` when the status moves, `2` when the supervisor died with the status
unchanged, `1` when `--timeout` elapses with neither (the default is no
timeout — the caller backgrounding the wait has its own), and `3` when it
could not start waiting at all: an unknown `--slice`, or a config it cannot
read. `1` and `3` are kept apart on purpose — `1` means "not finished yet",
`3` means "this will never finish", and a caller that cannot tell them apart
waits forever on a typo. Its last line names the terminal status, the elapsed
time and the log path, so no second command is needed.

Do **not** hand-roll this loop. On Windows, `kill -0 <pid>` in Git Bash
reports a live native pid as "No such process" (measured on Windows 11, pid
1336 alive per `Get-Process`), so a hand-rolled waiter reports completion on
its first iteration, every time — failing open exactly when it matters.

If a supervisor dies before recording an outcome, the document keeps claiming
work is in progress, and every reader believes it. `status` says so instead
of repeating the stored value as fact (the stored status stays visible —
hiding it would be its own lie):

```
  [EXECUTING          ] 2026-08-04-foo-plan.md - ...
                        ⚠ abandoned: lock names supervisor pid 41676, which is not alive; run `reconcile`
```

`reconcile --file <document> --yes` is the way out. Legal only when the
document sits at a role's `in_progress_status` and no live supervisor owns
the slice, it moves the document to `FAILED` — the truthful statement about
the *dispatch*, which went unrecorded — releases the stale lock, and prints
what it based the verdict on: the lock's pid, its liveness, the status it
moved from. `FAILED` says nothing about the *work*; judging that is the audit
the pipeline already requires before `close-slice`. From `FAILED` the machine
allows `SPEC_APPROVED` and `PLAN_APPROVED`, so you re-enter at the gate you
choose. Without `--yes`, reconcile prints the same evidence and changes
nothing. It refuses outright when a live supervisor owns the slice —
reconciling a running dispatch would race the runner's own epilogue.
````

- [x] **Step 4: Bump the version to 2.10.0**

In `.claude-plugin/plugin.json` line 4 and `package.json` line 3, change `"version": "2.9.0"` to `"version": "2.10.0"`. (Additive commands and one new annotation; no breaking change to any existing invocation.)

In `tests/test_docs_consistency.py`, update the two pinned literals:
- line 61: `assert manifest["version"] == "2.10.0"`
- line 380: `assert plugin["version"] == package["version"] == "2.10.0"`

- [x] **Step 5: Run the docs tests to verify they pass**

Run: `python -m pytest tests/test_docs_consistency.py -q -p no:cacheprovider`
Expected: all pass, including the new one

- [x] **Step 6: Run the full suite**

Run: `python -m pytest -q -p no:cacheprovider`
Expected: **458 passed** (456 + 1 new + 1 from the architect's exit-code amendment)

- [x] **Step 7: Commit**

```bash
git add docs/configuration.md .claude-plugin/plugin.json package.json tests/test_docs_consistency.py
git commit -m "docs(configuration): wait/reconcile lifecycle; version 2.10.0

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Verification (whole slice, after Task 6)

- [x] Full suite green: `python -m pytest -q -p no:cacheprovider --basetemp=<writable dir>` → **460 passed** (458 as planned, plus 2 from the architect's `--slice` resolution fix)
- [x] `python scripts/orchestrator.py reconcile --help` and `python scripts/orchestrator.py wait --help` both render
- [x] `git log --oneline main..feat/slice-06-abandoned-dispatch` shows **eight** Conventional Commits, each with the trailer (six planned, plus the cross-process race fix and the `--slice` resolution fix)
