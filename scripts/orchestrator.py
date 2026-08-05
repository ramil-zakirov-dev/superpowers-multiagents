#!/usr/bin/env python3
"""
superpowers-multiagents Orchestrator CLI

Thin entry point that wires together the modular components:
config, frontmatter, adapters, git_ops, hooks, locks, dependencies.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):  # invoked as a script rather than `-m`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.adapters import get_harness_adapter
from scripts.config import (
    DEFAULT_CONFIG,
    load_agent_config,
    resolve_agent,
    validate_config,
)
from scripts.dependencies import check_unmet_dependencies
from scripts.errors import OrchestratorError
from scripts.frontmatter import parse_frontmatter, update_frontmatter_status
from scripts.git_ops import (
    branch_tip,
    branch_exists,
    create_git_worktree,
    current_branch,
    is_tracked_at_head,
    merge_and_cleanup_worktree,
)
from scripts.hooks import canonical_events, run_infrastructure_hook
from scripts.locks import acquire_slice_lock, release_slice_lock, release_slice_lock_file
from scripts.paths import (
    ARTIFACT_PREFIXES, DOCS_BASE_PARTS, DOCUMENT_DIRNAMES, document_prompt_path,
    lock_path, log_path, logs_dir, resolve_docs_base,
)
from scripts import abandonment
from scripts import milestone as milestone_mod
from scripts import produced
from scripts import sandbox
from scripts import skills as skills_mod
from scripts.utils import find_project_root

#: Root of this plugin — the supervisor is spawned with this as its cwd so
#: that `python -m scripts.runner` resolves regardless of the user's cwd.
PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _warn_if_artifacts_not_ignored(project_root: Path) -> None:
    """Suggest ignoring our runtime paths, without touching the user's file."""
    result = subprocess.run(
        ["git", "check-ignore", *ARTIFACT_PREFIXES],
        cwd=project_root, capture_output=True, text=True,
    )
    ignored = set(result.stdout.split())
    missing = [p for p in ARTIFACT_PREFIXES if p not in ignored and p.rstrip("/") not in ignored]
    if missing:
        print(
            "Hint: consider adding these to .gitignore so they stay out of your diffs: "
            + " ".join(missing)
        )


def _warn_if_unpinned_lenses(lenses: list) -> None:
    """Say so when a document cites a part without a version.

    Advisory, exactly like an invisible skill. This plugin carries citations
    and never resolves them, so it cannot know whether a reference is valid —
    only whether it left off the one thing that keeps it meaning what it meant.
    """
    unpinned = skills_mod.unpinned_lenses(lenses)
    if unpinned:
        print(
            "Hint: these lenses carry no version and will drift when their "
            "upstream is rewritten: " + " ".join(unpinned)
        )


def _warn_if_near_miss_branch(slice_id: str, project_root: Path) -> None:
    """Say so when a hand-made `feature/<slice_id>` sits beside our own.

    Branch creation here is mechanical — `feat/<slice_id>`, derived from
    frontmatter — but the habit it competes with is a branch per slice, named by
    hand. The two differ by three characters, and only one of them is what
    `close-slice` merges; the other lingers looking like unfinished work.

    Advisory, because both branches existing is confusing rather than wrong,
    and only the human knows which one holds the work.
    """
    near_miss = f"feature/{slice_id}"
    if branch_exists(near_miss, project_root):
        print(
            f"Hint: '{near_miss}' also exists. This pipeline owns "
            f"'feat/{slice_id}' and merges only that one at close-slice — if "
            f"your work is on the other branch, move it over."
        )


def _warn_if_invisible_skills(agent_config: dict, adapter, cwd: Path) -> None:
    """Say so when a configured skill is not visible to the harness.

    Advisory only: skills are reinforcement, not a dependency, so a name the
    harness cannot resolve must not turn an optional improvement into a new way
    for a dispatch to be blocked.
    """
    declared = skills_mod.declared_skills(agent_config)
    if not declared:
        return
    try:
        missing = skills_mod.invisible_skills(declared, adapter.list_skills(agent_config, cwd))
        if missing:
            print(
                "Hint: these skills are not visible to the harness and will have "
                "no effect: " + " ".join(missing)
            )
    except Exception:
        # A project-supplied custom adapter's list_skills is out of our
        # control. This check is purely advisory and runs after the dispatch
        # has already committed -- it must never surface a failure to the
        # user for something that isn't the user's problem to see.
        pass


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

#: What the report calls a document that names a state the machine has no
#: transition for. Distinct from an unadopted document on purpose: this one
#: claims to be in the machine, so nothing will ever move it.
INVALID_LABEL = "INVALID"


def _classify_document(filepath: Path, frontmatter: dict, config: dict):
    """(label, note) for one document, or (None, note) when it is unadopted.

    Three outcomes, because `UNKNOWN` was three facts wearing one word:

    * no `status:` at all — the document predates the pipeline or was never
      meant to enter it. Not a defect, and there can be hundreds; the caller
      counts these instead of printing them.
    * a `status:` or `kind:` the machine does not have — a defect, and one the
      owner has to fix by hand, so it is never hidden.
    * anything else — a real state, printed as it always was.
    """
    status = frontmatter.get("status")
    if not status:
        return None, ""

    kind = milestone_mod.document_kind(frontmatter)
    if kind not in milestone_mod.KNOWN_KINDS:
        return INVALID_LABEL, (
            f"declares kind: {kind}, which is not "
            f"{' or '.join(sorted(milestone_mod.KNOWN_KINDS))}"
        )

    valid_statuses, _ = milestone_mod.machine_for(kind, config)
    if status not in valid_statuses:
        return INVALID_LABEL, f"status: {status} is not a {kind} status"
    return status, ""


def _docs_base(given, *, must_exist: bool = True, exit_code: int = 1) -> Path:
    """Resolve a `--dir` argument, or exit naming what it read.

    The refusal is the point: a report that cannot find the pipeline must say
    so rather than print `(none)` three times and exit 0.

    `exit_code` is not decoration. `wait` publishes a contract — 0 finished,
    2 abandoned, 1 timed out, 3 could not start — and an unresolvable
    directory is squarely "could not start". Exiting 1 there would tell a
    caller its dispatch is still running, which is the same conflation of
    "I could not look" with a real outcome that the rest of this module
    exists to prevent.
    """
    try:
        return resolve_docs_base(
            Path(given) if given else Path(*DOCS_BASE_PARTS), must_exist=must_exist
        )
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(exit_code)


def cmd_status(args):
    """Scans and displays status of milestones, specs, and plans."""
    base_dir = _docs_base(getattr(args, "dir", ""))
    # `all` postdates this function's other callers, and a Namespace built
    # without it must still work — the same reason cmd_dispatch_agent reads
    # `model` this way.
    show_all = getattr(args, "all", False)

    project_root = find_project_root(base_dir)
    try:
        config = load_agent_config(project_root)
    except OrchestratorError:
        # A report is a read. An unusable config is worth reporting elsewhere,
        # not worth refusing to say what is on disk.
        config = DEFAULT_CONFIG
    in_progress = abandonment.in_progress_statuses(config)
    isolated_success = abandonment.isolated_success_statuses(config)

    print("\n=======================================================")
    print("   SUPERPOWERS MULTI-AGENTS STATUS REPORT")
    # Named, and named absolutely: `(none)` is only auditable next to the
    # directory it is a statement about.
    print(f"   {base_dir}")
    print("=======================================================\n")

    for folder_name in DOCUMENT_DIRNAMES:
        folder_path = base_dir / folder_name
        print(f"--- {folder_name.upper()} ({folder_path}) ---")

        md_files = sorted(folder_path.glob("*.md")) if folder_path.is_dir() else []
        if not md_files:
            print("  (none)\n")
            continue

        unadopted = []
        for filepath in md_files:
            text = filepath.read_text(encoding="utf-8")
            data = parse_frontmatter(text)
            label, note = _classify_document(filepath, data, config)
            if label is None and not show_all:
                unadopted.append(filepath)
                continue

            title = data.get("title", filepath.stem)
            suffix = f" — {note}" if note else ""
            if milestone_mod.document_kind(data) == milestone_mod.MILESTONE_KIND:
                try:
                    resolve = milestone_mod.slice_resolver(
                        milestone_mod.search_dirs_for(filepath), exclude=filepath
                    )
                    closed, total = milestone_mod.progress(text, resolve)
                    suffix += f" ({closed}/{total} slices closed)"
                except OrchestratorError:
                    # A brief without markers is still worth listing; a broken
                    # one must not take the whole report down with it.
                    suffix += " (track state unavailable)"
            print(f"  [{(label or 'no status'):<18}] {filepath.name} - {title}{suffix}")
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
            # The mirror image, one gate later: a status claiming an isolated
            # role succeeded, over a branch carrying nothing it could have
            # succeeded at. The supervisor checks this itself when it lives to
            # do so; a status written by hand after a dead supervisor never
            # was checked, and that is the slice that reaches close-slice
            # unverified.
            elif label in isolated_success:
                slice_id = data.get("slice_id", filepath.stem)
                empty = abandonment.empty_slice_branch(slice_id, project_root)
                if empty:
                    print(f"{'':23}⚠ {empty}")

        if unadopted:
            print(
                f"  ({len(unadopted)} documents carry no lifecycle status and are "
                f"not adopted into the pipeline; --all lists them)"
            )
        print()


def _set_milestone_status(filepath, new_status, valid_statuses, transitions):
    """A milestone's transitions. No branch, no worktree, no sandbox.

    Both gates run before the status write, so a refused transition leaves the
    document exactly as it was.
    """
    text = filepath.read_text(encoding="utf-8")

    if new_status == "MILESTONE_ACTIVE":
        missing = milestone_mod.missing_sections(text)
        if missing:
            print(f"Error: {filepath.name} cannot be approved while sections are empty:")
            for section in missing:
                print(f"   - {section}")
            sys.exit(1)

    if new_status == "MILESTONE_CLOSED":
        resolve = milestone_mod.slice_resolver(
            milestone_mod.search_dirs_for(filepath), exclude=filepath
        )
        try:
            open_slices = milestone_mod.unclosed(text, resolve)
        except OrchestratorError as exc:
            print(f"Error: {exc}")
            sys.exit(1)
        if open_slices:
            print(f"Error: {filepath.name} cannot be closed; these slices are open:")
            for slice_id, status in open_slices:
                print(f"   - {slice_id} ({status})")
            sys.exit(1)

    if not update_frontmatter_status(filepath, new_status, valid_statuses, transitions):
        sys.exit(1)


def cmd_set_status(args):
    """Set a slice's status. VERIFIED_CLOSED merges first, then marks.

    The order matters: marking VERIFIED_CLOSED first makes the state terminal,
    after which a merge conflict cannot be recorded at all.
    """
    filepath = Path(args.file).resolve()

    # A wrapper that loses its argument lands here with `--file` resolving to
    # the working directory, and read_text() on a directory raises
    # PermissionError — which names neither the command nor the missing
    # argument, and reads like a permissions problem on the whole project.
    if not filepath.is_file():
        print(f"Error: --file '{filepath}' is not a file.")
        if filepath.is_dir():
            print(
                "   That is a directory, which usually means the path argument "
                "did not reach this command. Re-run it with the path spelled out."
            )
        sys.exit(1)

    project_root = find_project_root(filepath)

    try:
        config = load_agent_config(project_root)
        validate_config(config)
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    frontmatter = parse_frontmatter(filepath.read_text(encoding="utf-8"))
    try:
        milestone_mod.check_kind_declaration(filepath, frontmatter)
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    kind = milestone_mod.document_kind(frontmatter)
    valid_statuses, transitions = milestone_mod.machine_for(kind, config)

    if kind == milestone_mod.MILESTONE_KIND:
        _set_milestone_status(filepath, args.status, valid_statuses, transitions)
        return

    if args.status != "VERIFIED_CLOSED":
        if not update_frontmatter_status(filepath, args.status, valid_statuses, transitions):
            sys.exit(1)
        return

    slice_id = frontmatter.get("slice_id", filepath.stem)
    current_status = frontmatter.get("status", "UNKNOWN")

    # Check legality BEFORE the merge: merge_and_cleanup_worktree performs an
    # irreversible git merge and force-deletes the worktree. Discovering only
    # afterward that the transition was illegal would leave the branch merged,
    # the worktree gone, and the command reporting failure while the on-disk
    # status silently stayed put -- exactly the "mutation before the fallible
    # check" ordering this slice exists to eliminate everywhere else.
    if "VERIFIED_CLOSED" not in (transitions.get(current_status) or []):
        print(f"Error: Invalid state transition from '{current_status}' to 'VERIFIED_CLOSED'.")
        sys.exit(1)

    try:
        # getattr, not args.skip_merge: cmd_set_status is called directly with
        # a hand-built Namespace in several places, and a status change that
        # merges nothing has no business demanding the flag be spelled out.
        merged = merge_and_cleanup_worktree(
            slice_id, project_root, skip_merge=getattr(args, "skip_merge", False)
        )
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    if not merged:
        if not update_frontmatter_status(filepath, "MERGE_CONFLICT", valid_statuses, transitions):
            print(
                f"Error: merge conflicted on 'feat/{slice_id}', but the slice could "
                f"not be marked MERGE_CONFLICT from '{current_status}'. Resolve the "
                f"git conflict by hand; the on-disk status was not changed."
            )
            sys.exit(1)
        print(f"Merge conflict on 'feat/{slice_id}'. Slice marked MERGE_CONFLICT.")
        print("Resolve the conflict, commit, then set VERIFIED_CLOSED again.")
        sys.exit(1)

    if not update_frontmatter_status(filepath, "VERIFIED_CLOSED", valid_statuses, transitions):
        sys.exit(1)

    # Closing a slice and refreshing the milestones that list it are one
    # command, so a checkbox cannot go stale. A sync failure is a warning: the
    # slice's outcome is already recorded, and a later step must not overturn
    # it -- the same rule the hook and the sandbox teardown below follow.
    for brief in milestone_mod.briefs_listing(slice_id, filepath):
        try:
            milestone_mod.sync_file(brief)
            print(f"Refreshed {brief.name}.")
        except (OrchestratorError, OSError) as exc:
            print(f"Warning: could not refresh {brief.name}: {exc}")

    try:
        run_infrastructure_hook(
            "on_slice_verified_closed",
            project_root=project_root,
            known_events=canonical_events(config.get("agents", {})),
        )
    except OrchestratorError as exc:
        # The merge and the status write already succeeded; a failing
        # post-merge hook must not be reported as if the merge itself failed.
        print(f"Warning: on_slice_verified_closed hook failed: {exc}")

    mode = (
        ((config.get("sandbox") or {}).get("teardown") or {})
        .get("on_verified_closed", "volumes")
    )
    try:
        sandbox.tear_down(f"feat/{slice_id}", project_root, config, mode)
    except OrchestratorError as exc:
        print(f"Warning: sandbox teardown failed: {exc}")


def cmd_trigger_hook(args):
    """Manually or programmatically triggers an infrastructure hook."""
    project_root = Path(args.dir) if args.dir else Path.cwd()

    try:
        config = load_agent_config(project_root)
        validate_config(config)
        run_infrastructure_hook(
            args.event,
            project_root=project_root,
            known_events=canonical_events(config.get("agents", {})),
        )
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(1)


def cmd_dispatch_agent(args):
    """Dispatch an agent by role.

    Ordering is load-bearing: every step that can fail runs before the first
    irreversible mutation, so a failed precondition never leaves a slice that
    has to be repaired by hand.
    """
    target_file = Path(args.file).resolve()
    if not target_file.exists():
        print(f"Error: Target file '{target_file}' not found.")
        sys.exit(1)

    role = args.role
    project_root = find_project_root(target_file)

    # 1. Configuration
    try:
        config = load_agent_config(project_root)
        validate_config(config)
        agent_config = resolve_agent(config, role)
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    state_machine = config["state_machine"]
    known_events = canonical_events(config.get("agents", {}))

    if getattr(args, "model", None):
        agent_config["model"] = args.model

    # 2. Gates
    unmet = check_unmet_dependencies(target_file)
    if unmet:
        print(f"[Dependency Gate] Cannot dispatch {role} for {target_file.name}. Unmet:")
        for dependency in unmet:
            print(f"   - {dependency}")
        sys.exit(1)

    frontmatter = parse_frontmatter(target_file.read_text(encoding="utf-8"))

    try:
        milestone_mod.check_kind_declaration(target_file, frontmatter)
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    if milestone_mod.document_kind(frontmatter) == milestone_mod.MILESTONE_KIND:
        print(
            f"[Kind Gate] Cannot dispatch {role} for {target_file.name}: it is a "
            f"milestone brief. No agent role operates on a milestone — dispatch "
            f"against the slice spec or plan the brief lists."
        )
        sys.exit(1)

    slice_id = frontmatter.get("slice_id", target_file.stem)
    current_status = frontmatter.get("status", "UNKNOWN")

    # An isolated role's worktree is `git worktree add ... HEAD`, so it contains
    # what HEAD committed and nothing else. Dispatching one at a document that
    # is not in HEAD hands the agent a path to a file it cannot open — and the
    # failure arrives much later, in the harness's own words.
    if agent_config.get("isolated_worktree") and not is_tracked_at_head(
        target_file, project_root
    ):
        print(
            f"[Worktree Gate] Cannot dispatch {role} for {target_file.name}: it is "
            f"not committed on '{current_branch(project_root)}', the branch this "
            f"dispatch forks from."
        )
        print(
            f"   {role} runs in .worktrees/{slice_id}, created from HEAD, so it "
            f"would not contain this file. Commit it on the current branch first."
        )
        sys.exit(1)

    allowed_statuses = agent_config.get("allowed_statuses") or []
    if allowed_statuses and current_status not in allowed_statuses:
        print(f"[State Validation] Cannot dispatch {role} for {target_file.name}.")
        print(f"   Current status is '{current_status}'; {role} requires one of: {allowed_statuses}")
        sys.exit(1)

    # The path the agent will be told to open. Computed here, with the other
    # gates, because it can fail and the lock below is a mutation — the same
    # ordering rule the rest of this function keeps.
    try:
        prompt_file = document_prompt_path(target_file, project_root)
    except OrchestratorError as exc:
        print(f"[Path Gate] Cannot dispatch {role} for {target_file.name}: {exc}")
        sys.exit(1)

    # 3. Lock
    try:
        lock_file = acquire_slice_lock(slice_id, project_root)
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    # 4-5. Fallible side effects, before any mutation we would have to undo.
    # Adapter resolution belongs here too, not after step 6: an unknown
    # harness or a missing custom-adapter file is exactly as fallible as a
    # failing hook, and discovering it only after the status write would
    # strand the slice at in_progress_status with no legal way back.
    log_file = log_path(project_root, role, target_file.stem)
    prompt_template = agent_config.get("prompt_template", "Process {file}")
    # The contract the role's own output has to satisfy. A role that produces
    # nothing gets an empty block; its template does not mention one, and an
    # unused key costs a template nothing.
    produced_frontmatter = ""
    if agent_config.get("produces"):
        produced_frontmatter = produced.frontmatter_block(
            frontmatter,
            slice_id=slice_id,
            status=agent_config.get("success_status") or "",
            source_path=prompt_file,
            title_template=agent_config.get("produced_title") or "",
        )
    # The document's own citations travel with the role's skills: the agent is
    # about to be dispatched *at* this file, and what it was written against is
    # as much a part of the briefing as what the role is good at.
    declared_lenses = skills_mod.declared_lenses(frontmatter)
    task_prompt = skills_mod.compose_prompt(
        prompt_template.format(
            file=prompt_file,
            slice_id=slice_id,
            frontmatter=produced_frontmatter,
        ),
        skills_mod.declared_skills(agent_config),
        declared_lenses,
        skills_mod.declared_instructions(agent_config),
    )

    isolated = agent_config.get("isolated_worktree", False)
    base_ref = ""
    try:
        if isolated:
            cwd = create_git_worktree(slice_id, project_root)
            sandbox_branch = f"feat/{slice_id}"
            # Where the branch stands *now*, so the supervisor can later ask
            # what this run added rather than what the branch happens to
            # carry. create_git_worktree reuses an existing worktree, so on a
            # re-dispatch this is the previous run's tip, not the fork point.
            base_ref = branch_tip(sandbox_branch, project_root)
            sandbox_env = sandbox.ensure_up(sandbox_branch, project_root, config)
        else:
            cwd = project_root
            sandbox_branch = current_branch(project_root)
            sandbox_env = sandbox.resolve_env(sandbox_branch, project_root, config)

        env = dict(os.environ)
        env.update(sandbox_env)
        env.update({
            "SUPERPOWERS_SLICE_ID": slice_id,
            "SUPERPOWERS_SLICE_BRANCH": sandbox_branch,
            "SUPERPOWERS_WORKTREE": str(cwd),
        })

        # The start hook runs after the worktree and the sandbox exist, so a
        # project hook can act on both. This is a deliberate change from
        # 2.0.0, where it ran first and could observe neither.
        env = run_infrastructure_hook(
            f"on_slice_{role}_start", project_root=project_root,
            current_env=env, known_events=known_events,
        )
        adapter = get_harness_adapter(agent_config, project_root)
        agent_argv = adapter.build_command(agent_config, task_prompt)
    except OrchestratorError as exc:
        release_slice_lock_file(lock_file)
        print(f"Error: {exc}")
        print(f"Slice '{slice_id}' left untouched at status '{current_status}'.")
        sys.exit(1)

    # 6. First irreversible mutation
    in_progress_status = agent_config.get("in_progress_status")
    if in_progress_status:
        applied = update_frontmatter_status(
            target_file, in_progress_status,
            state_machine["valid_statuses"], state_machine["transitions"],
        )
        if not applied:
            release_slice_lock_file(lock_file)
            print(
                f"Error: could not transition '{slice_id}' from "
                f"'{current_status}' to '{in_progress_status}'."
            )
            print(f"Slice '{slice_id}' left untouched at status '{current_status}'.")
            sys.exit(1)

    # 7. Spawn the supervisor
    logs_dir(project_root).mkdir(parents=True, exist_ok=True)

    # Teardown-on-failure (runner.py's `if exit_code != 0 and sandbox_branch`)
    # must only ever fire for an agent that owns the stack's lifecycle. A
    # non-isolated agent only ever resolve_env()s an existing stack -- it
    # never brings one up -- so its crash must not tear down infrastructure
    # that belongs to whoever (or whatever) does own it, e.g. the human's own
    # active stack on their own branch. Passing an empty branch here, rather
    # than the real one, is what makes the runner's existing gate skip it.
    teardown_branch = sandbox_branch if isolated else ""

    runner_argv = [
        sys.executable, "-m", "scripts.runner",
        "--role", role,
        "--file", str(target_file),
        "--project-root", str(project_root),
        "--lock", str(lock_file),
        "--log", str(log_file),
        "--cwd", str(cwd),
        "--sandbox-branch", teardown_branch,
        "--base-ref", base_ref,
        "--", *[str(part) for part in agent_argv],
    ]

    spawn_kwargs = {"cwd": str(PLUGIN_ROOT), "env": env}
    if os.name == "nt":
        spawn_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        spawn_kwargs["start_new_session"] = True

    process = subprocess.Popen(runner_argv, **spawn_kwargs)

    print(f"Dispatched {agent_config.get('model')} as {role} (supervisor PID {process.pid}).")
    print(f"Log: {log_file}")
    _warn_if_invisible_skills(agent_config, adapter, cwd)
    _warn_if_unpinned_lenses(declared_lenses)
    _warn_if_near_miss_branch(slice_id, project_root)
    _warn_if_artifacts_not_ignored(project_root)

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


def _quote_posix(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_\-./:=]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _quote_powershell(value: str) -> str:
    escaped = value.replace("`", "``").replace("$", "`$").replace('"', '`"')
    return escaped


def cmd_sandbox(args):
    """Human-facing sandbox lifecycle. The orchestrator uses the module directly."""
    # `cmd` is `nargs=argparse.REMAINDER`, needed so `sandbox exec -- cmd --flag`
    # can pass arbitrary flags through to the wrapped command untouched. But
    # REMAINDER is greedy: for every other action, anything landing here means
    # a flag was placed after the action and got silently swallowed instead of
    # parsed (e.g. `sandbox status --dir X` -> action='status', cmd=['--dir',
    # 'X'], dir=''). Fail closed instead of quietly running with the wrong
    # (default) config.
    if args.action != "exec" and args.cmd:
        print(
            f"Error: unexpected extra argument(s) after '{args.action}': {args.cmd}\n"
            f"Flags like --dir/--branch/--shell/--yes must come BEFORE the action:\n"
            f"  sandbox --dir X {args.action}   (not: sandbox {args.action} --dir X)"
        )
        sys.exit(1)

    project_root = Path(args.dir).resolve() if args.dir else Path.cwd()

    try:
        config = load_agent_config(project_root)
        validate_config(config)
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    branch = args.branch or current_branch(project_root)
    sandbox_cfg = config.get("sandbox") or {}

    try:
        if args.action == "status":
            rows = sandbox.status_rows(project_root, config)
            if not rows:
                print("No sandbox stacks are tracked.")
            for name, ip, state in rows:
                print(f"{name:<48} {ip:<12} {state}")
            return

        if args.action in ("up", "restart"):
            if not sandbox_cfg.get("enabled"):
                print(
                    "Error: sandbox is not enabled for this project; "
                    "nothing to bring up."
                )
                sys.exit(1)
            if args.action == "restart":
                sandbox.tear_down(branch, project_root, config, "containers")
            env = sandbox.ensure_up(branch, project_root, config)
            print(f"Stack for {branch} is up on {env['LOOPBACK_IP']}.")
            return

        if args.action == "teardown":
            if not sandbox_cfg.get("enabled"):
                print(
                    "Error: sandbox is not enabled for this project; "
                    "nothing to tear down."
                )
                sys.exit(1)
            if not args.yes:
                print(
                    f"Refusing to destroy volumes for {branch} without --yes. "
                    f"Stopping containers only would be `restart`; re-run with "
                    f"--yes to destroy data."
                )
                sys.exit(2)
            mode = "volumes"
            sandbox.tear_down(branch, project_root, config, mode)
            print(f"Stack for {branch} torn down ({mode}).")
            return

        env = sandbox.resolve_env(branch, project_root, config)
        if not env:
            print(f"No sandbox state for branch {branch}; run `sandbox up` first.")
            sys.exit(1)

        if args.action == "env":
            if args.shell == "json":
                print(json.dumps(env, indent=2))
            elif args.shell == "powershell":
                for key, value in env.items():
                    print(f'$env:{key} = "{_quote_powershell(value)}"')
            else:
                for key, value in env.items():
                    print(f"export {key}={_quote_posix(value)}")
            return

        if args.action == "exec":
            command = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
            if not command:
                print("Error: `sandbox exec` needs a command after `--`.")
                sys.exit(1)
            # subprocess.run with a bare argv list cannot resolve Windows
            # .cmd/.bat shims (e.g. npm.cmd) -- CreateProcess only searches
            # PATHEXT-registered extensions under a shell, and shell=True is
            # off the table for this command. shutil.which does that PATHEXT
            # resolution itself, without a shell.
            resolved = shutil.which(command[0])
            if resolved is None:
                print(f"Error: command not found: {command[0]}")
                sys.exit(1)
            command = [resolved, *command[1:]]
            sys.exit(subprocess.run(
                command, cwd=str(project_root), env={**os.environ, **env}
            ).returncode)
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(1)


def cmd_summary(args):
    """Print the tail of an execution log for audit."""
    project_root = Path(args.dir).resolve() if args.dir else Path.cwd()
    directory = logs_dir(project_root)

    matching = sorted(directory.glob(f"*{args.slice}*.log")) if directory.exists() else []
    if not matching:
        print(f"No execution log for slice '{args.slice}' in {directory}")
        sys.exit(1)

    log_file = max(matching, key=lambda path: path.stat().st_mtime)
    lines = log_file.read_text(encoding="utf-8", errors="ignore").splitlines()

    print(f"\n--- LAST 50 LINES OF {log_file.name} ---")
    print("\n".join(lines[-50:]))


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
        # The grounds are the ones the verdict was reached on, not a fresh
        # lookup: re-deriving them here once produced "is abandoned ... pid
        # 22776, which is alive", a line that argues against itself.
        print(
            f"Slice '{slice_id}' is abandoned after {result.elapsed:.0f}s: "
            f"{result.evidence}."
        )
        print(f"Status is still '{result.status}'. Log: {log}")
        print(f"Audit the work, then run: reconcile --file {document} --yes")
        return 2
    if result.outcome == abandonment.OUTCOME_UNREADABLE_LOCK:
        # Exit 3, with the timeout's codes: this says the watcher failed, not
        # that the dispatch did. A caller must not read it as an outcome.
        print(
            f"Could not read the lock for '{slice_id}' on "
            f"{abandonment._MAX_UNREADABLE_POLLS} consecutive polls; giving up "
            f"without a verdict. Slice is still '{result.status}'. Log: {log}"
        )
        print(f"Inspect {lock_path(project_root, slice_id)} by hand.")
        return 3
    print(
        f"Timed out after {result.elapsed:.0f}s; slice '{slice_id}' is still "
        f"'{result.status}'. Log: {log}"
    )
    return 1


def cmd_wait(args):
    """Block until a slice's dispatch ends — by finishing or by abandonment."""
    base_dir = _docs_base(getattr(args, "dir", ""), exit_code=3)
    project_root = find_project_root(base_dir)

    try:
        config = load_agent_config(project_root)
        validate_config(config)
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(3)

    # `--file` names the dispatch exactly, the way `reconcile` already does.
    # `--slice` is the convenience, and it has to choose between the spec and
    # the plan — both carry the same slice_id, and picking the wrong one
    # reports another dispatch's settled status as this one's outcome.
    explicit = getattr(args, "file", None)
    slice_id = getattr(args, "slice", None)
    try:
        if explicit:
            document = Path(explicit)
            if not document.is_file():
                raise OrchestratorError(f"no such document: {document}")
            slice_id = parse_frontmatter(
                document.read_text(encoding="utf-8")
            ).get("slice_id") or slice_id
            if not slice_id:
                raise OrchestratorError(
                    f"{document} carries no slice_id, so its lock cannot be found."
                )
        else:
            if not slice_id:
                raise OrchestratorError("give either --slice or --file.")
            document = abandonment.resolve_slice_document(
                base_dir, slice_id, abandonment.in_progress_statuses(config)
            )
    except OrchestratorError as exc:
        print(f"Error: {exc}")
        sys.exit(3)

    result = abandonment.wait_for_dispatch(
        document, project_root, config, slice_id,
        timeout=args.timeout, poll=args.poll,
    )
    sys.exit(_report_wait_result(slice_id, document, project_root, result))


def cmd_milestone(args):
    """Milestone brief lifecycle: create, sync track state, check completeness.

    Unlike `sandbox`, flags come after the action in the ordinary way — that
    command's flags-first constraint exists only because `exec --` needs
    `argparse.REMAINDER`, and nothing here passes a command through.
    """
    if args.action == "new":
        # The one command that writes a project's first document, so a base
        # that does not exist yet is the normal case rather than a bad
        # argument — but a project root still resolves to its docs base.
        base_dir = _docs_base(getattr(args, "dir", ""), must_exist=False)
        try:
            path = milestone_mod.create(base_dir, args.id, args.title)
        except OrchestratorError as exc:
            print(f"Error: {exc}")
            sys.exit(1)
        print(f"Created {path}")
        print("Fill every section, then run:")
        print(f"  set-status --file {path} --status MILESTONE_ACTIVE")
        return

    if args.action == "sync":
        try:
            closed, total = milestone_mod.sync_file(Path(args.file))
        except OrchestratorError as exc:
            print(f"Error: {exc}")
            sys.exit(1)
        print(f"Synced {args.file} — {closed}/{total} slices closed.")
        return

    if args.action == "check":
        try:
            _frontmatter, text = milestone_mod.load(Path(args.file))
        except OrchestratorError as exc:
            print(f"Error: {exc}")
            sys.exit(1)
        missing = milestone_mod.missing_sections(text)
        if not missing:
            print(f"{args.file}: complete — all required sections are filled.")
            return
        print(f"{args.file} is incomplete. Empty or missing sections:")
        for section in missing:
            print(f"   - {section}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI argument parser
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Superpowers Multi-Agents Orchestrator"
    )
    subparsers = parser.add_subparsers(dest="command")

    # `--dir` names two different things across this CLI — the docs base for
    # the reporting commands, the project root for the acting ones — and a
    # caller who learned the habit from one group used to get silence from
    # the other (issue #11). Both groups now carry a name that says which,
    # with `--dir` kept as an alias so nothing in the field breaks; the
    # reporting commands additionally accept either directory, resolved by
    # `paths.resolve_docs_base`, and refuse a path that is neither.
    DOCS_DIR_FLAGS = ("--docs-dir", "--dir")
    PROJECT_ROOT_FLAGS = ("--project-root", "--dir")

    # status
    p_status = subparsers.add_parser("status", help="Show status of all milestones, specs, and plans")
    p_status.add_argument(
        *DOCS_DIR_FLAGS, dest="dir", default="docs/superpowers",
        help="Docs base, or the project root holding it",
    )
    p_status.add_argument(
        "--all", action="store_true",
        help="Also list documents that carry no lifecycle status",
    )

    # set-status
    p_set = subparsers.add_parser("set-status", help="Set status of a markdown file")
    p_set.add_argument("--file", required=True, help="Path to markdown file")
    p_set.add_argument("--status", required=True, help="New status")
    p_set.add_argument(
        "--skip-merge",
        action="store_true",
        help="With VERIFIED_CLOSED: assert the slice already landed, so its "
             "branch is not merged and need not exist. Nothing verifies the "
             "claim; use it when the branch was deleted after merging.",
    )

    # trigger-hook
    p_trigger = subparsers.add_parser("trigger-hook", help="Trigger an infrastructure hook manually")
    p_trigger.add_argument("--event", required=True, help="Hook event name")
    p_trigger.add_argument(
        *PROJECT_ROOT_FLAGS, dest="dir", default="", help="Project root directory"
    )

    # dispatch-agent (generic)
    p_agent = subparsers.add_parser("dispatch-agent", help="Dispatch an agent by role")
    p_agent.add_argument("--role", required=True, help="Agent role (e.g., planner, executor)")
    p_agent.add_argument("--file", required=True, help="Path to target markdown file")
    p_agent.add_argument("--model", help="Override LLM model")
    p_agent.add_argument(
        "--wait", action="store_true",
        help="Block until the dispatch ends, then exit with its outcome "
             "(0 finished, 2 abandoned, 1 timeout)",
    )
    p_agent.add_argument(
        "--poll", type=float, default=None,
        help="With --wait: seconds between checks (default: 15)",
    )

    # dispatch-planner (backward-compat alias)
    p_plan = subparsers.add_parser("dispatch-planner", help="[Alias] Dispatch planner for a spec")
    p_plan.add_argument("--spec", required=True, help="Path to design spec file")
    p_plan.add_argument("--model", help="Override LLM model")
    p_plan.add_argument(
        "--wait", action="store_true",
        help="Block until the dispatch ends, then exit with its outcome "
             "(0 finished, 2 abandoned, 1 timeout)",
    )
    p_plan.add_argument(
        "--poll", type=float, default=None,
        help="With --wait: seconds between checks (default: 15)",
    )

    # dispatch-executor (backward-compat alias)
    p_exec = subparsers.add_parser("dispatch-executor", help="[Alias] Dispatch executor for a plan")
    p_exec.add_argument("--plan", required=True, help="Path to plan file")
    p_exec.add_argument("--model", help="Override LLM model")
    p_exec.add_argument(
        "--wait", action="store_true",
        help="Block until the dispatch ends, then exit with its outcome "
             "(0 finished, 2 abandoned, 1 timeout)",
    )
    p_exec.add_argument(
        "--poll", type=float, default=None,
        help="With --wait: seconds between checks (default: 15)",
    )

    # summary
    p_sum = subparsers.add_parser("summary", help="Show execution summary log for audit")
    p_sum.add_argument("--slice", required=True, help="Slice ID or keyword")
    p_sum.add_argument(
        *PROJECT_ROOT_FLAGS, dest="dir", default="",
        help="Project root directory (default: cwd)",
    )

    # reconcile
    p_reconcile = subparsers.add_parser(
        "reconcile",
        help="Move an abandoned dispatch's document to FAILED and release its stale lock",
    )
    p_reconcile.add_argument("--file", required=True, help="Path to the slice document")
    p_reconcile.add_argument(
        *PROJECT_ROOT_FLAGS, dest="dir", default="",
        help="Project root (default: derived from --file)",
    )
    p_reconcile.add_argument(
        "--yes", action="store_true", help="Apply the move to FAILED"
    )

    # wait
    p_wait = subparsers.add_parser(
        "wait",
        help="Block until a slice's dispatch ends "
             "(exit 0 finished, 2 abandoned, 1 timeout, 3 cannot start)",
    )
    # Not required, and not mutually exclusive by accident: `--file` is the
    # unambiguous form (a slice has two documents with one slice_id), `--slice`
    # the convenience that resolves to whichever of them is in flight.
    p_wait.add_argument("--slice", help="Slice ID to wait on (resolved to the document in flight)")
    p_wait.add_argument("--file", help="The exact document to wait on — unambiguous, as for `reconcile`")
    # Identical to `status`, as the note left here at the previous slice's
    # audit gate required: both were changed together when #11 was settled.
    p_wait.add_argument(
        *DOCS_DIR_FLAGS, dest="dir", default="docs/superpowers",
        help="Docs base, or the project root holding it (as in `status`)",
    )
    p_wait.add_argument(
        "--timeout", type=float, default=None,
        help="Give up after S seconds (default: never)",
    )
    p_wait.add_argument(
        "--poll", type=float, default=abandonment.DEFAULT_POLL_SECONDS,
        help="Seconds between checks (default: 15)",
    )

    # sandbox
    p_sandbox = subparsers.add_parser("sandbox", help="Per-slice infrastructure sandbox")
    p_sandbox.add_argument(
        "action", choices=["up", "restart", "status", "env", "exec", "teardown"]
    )
    p_sandbox.add_argument(
        *PROJECT_ROOT_FLAGS, dest="dir", default="", help="Project root (default: cwd)"
    )
    p_sandbox.add_argument("--branch", default="", help="Branch (default: current)")
    p_sandbox.add_argument(
        "--shell", default="posix", choices=["posix", "powershell", "json"],
        help="Output format for `env`",
    )
    p_sandbox.add_argument("--yes", action="store_true", help="Confirm volume destruction")
    p_sandbox.add_argument("cmd", nargs=argparse.REMAINDER, help="Command for `exec`")

    # milestone
    p_milestone = subparsers.add_parser(
        "milestone", help="Milestone brief lifecycle (new / sync / check)"
    )
    milestone_actions = p_milestone.add_subparsers(dest="action", required=True)

    p_ms_new = milestone_actions.add_parser("new", help="Create a milestone brief")
    p_ms_new.add_argument("--id", required=True, help="Milestone id, e.g. milestone-1")
    p_ms_new.add_argument("--title", required=True, help="Milestone title")
    p_ms_new.add_argument(
        *DOCS_DIR_FLAGS, dest="dir", default="docs/superpowers",
        help="Docs base, or the project root holding it (created if absent)",
    )

    p_ms_sync = milestone_actions.add_parser("sync", help="Refresh track state")
    p_ms_sync.add_argument("--file", required=True, help="Path to the milestone brief")

    p_ms_check = milestone_actions.add_parser("check", help="Check section completeness")
    p_ms_check.add_argument("--file", required=True, help="Path to the milestone brief")

    args = parser.parse_args()

    if args.command == "status":
        cmd_status(args)
    elif args.command == "set-status":
        cmd_set_status(args)
    elif args.command == "trigger-hook":
        cmd_trigger_hook(args)
    elif args.command == "dispatch-planner":
        args.role = "planner"
        args.file = args.spec
        cmd_dispatch_agent(args)
    elif args.command == "dispatch-executor":
        args.role = "executor"
        args.file = args.plan
        cmd_dispatch_agent(args)
    elif args.command == "dispatch-agent":
        cmd_dispatch_agent(args)
    elif args.command == "summary":
        cmd_summary(args)
    elif args.command == "reconcile":
        cmd_reconcile(args)
    elif args.command == "wait":
        cmd_wait(args)
    elif args.command == "sandbox":
        cmd_sandbox(args)
    elif args.command == "milestone":
        cmd_milestone(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
