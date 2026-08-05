"""What closing a slice means for every document that carries its id.

A slice has two documents and one life. `close-slice` targets the plan, because
the plan is where execution's terminal statuses land — and the spec is then left
sitting at whatever it said when the planner finished, permanently: a spec's
path ends at `PLAN_GENERATED`, and `VERIFIED_CLOSED` is reachable only from
`EXECUTION_COMPLETE` or `MERGE_CONFLICT`, which are claims about a plan.

Measured in this repository before any of this existed: six of seven specs
misreported. Six were merely misleading, because `dependencies.resolve_document`
prefers `plans/` and the plan answers for the slice. The seventh had no plan at
all — so the spec *was* the answer, it read `SPEC_APPROVED` for work that had
shipped, and every slice that might have depended on it was blocked, as was any
milestone listing it.

So closure is **recorded** here, not transitioned. Adding
`PLAN_GENERATED -> VERIFIED_CLOSED` to the shared table would have been a
different statement entirely — that a *plan* may be closed without ever being
approved or executed — because both documents are one kind and share one table.
What is true is narrower: the slice closed, and that fact belongs on every
document carrying its id.

Two grounds are admitted for writing it, and no third:

* **Observed** — the slice's plan is already at the terminal status. Nothing is
  taken on trust; the evidence is a file on disk.
* **Asserted** — the slice has no plan at all, and a human says the work landed
  outside the pipeline. `--skip-merge` already carries exactly this meaning and
  admits it: "nothing here can verify it".

A slice whose document sits at some role's in-progress status is refused under
both, because a supervisor owns that status and `reconcile` is the command for
one that never came back.
"""

from pathlib import Path

from scripts import produced
from scripts.frontmatter import parse_frontmatter, update_frontmatter_status

CLOSED = "VERIFIED_CLOSED"

#: The sibling directory names, in the two directions this module asks about.
PLANS, SPECS = "plans", "specs"


def sibling(document: Path, subdir: str, slice_id: str):
    """The slice's other document, or None.

    `produced.find_document` is symmetric — it resolves `<document>/../<subdir>`
    and skips the source itself — so the same call finds a spec's plan and a
    plan's spec. Reusing it keeps one definition of "the slice's other
    document" rather than growing a second that could disagree.
    """
    try:
        return produced.find_document(Path(document), subdir, slice_id)
    except OSError:
        return None


def record_closure(document: Path, valid_statuses: list) -> bool:
    """Write the slice's terminal status onto `document`. No transition check.

    Deliberately bypassing the table, because the document is not making a
    transition: the slice's outcome is being recorded on it. The status name is
    still validated, so a project that renamed its terminal status is not
    silently given ours.

    Everything else `close-slice` does — the merge, the worktree removal, the
    completion hook, the sandbox teardown — belongs to the slice's *execution*
    and already ran when the plan closed. Doing any of it again for a second
    document would be doing it twice.
    """
    return update_frontmatter_status(Path(document), CLOSED, valid_statuses, {})


def _status_of(path: Path) -> str:
    return parse_frontmatter(Path(path).read_text(encoding="utf-8")).get("status", "")


def verdict(
    document: Path,
    slice_id: str,
    current_status: str,
    *,
    in_progress: set,
    skip_merge: bool,
) -> tuple[bool, str]:
    """May this document record the slice's closure? `(allowed, why not)`.

    Reached only when the ordinary transition is illegal, so every path here is
    a document that today gets `Invalid state transition` — a message naming
    neither the right file nor the reason.
    """
    if current_status in in_progress:
        return False, (
            f"'{Path(document).name}' is at '{current_status}', which means a "
            f"dispatch owns it. Closing over that would overwrite a "
            f"supervisor's own outcome with a guess about it. If the dispatch "
            f"never came back, `reconcile` is the command that says so and "
            f"offers a way out."
        )

    plan = sibling(document, PLANS, slice_id)
    if plan is not None:
        plan_status = _status_of(plan)
        if plan_status == CLOSED:
            return True, ""
        return False, (
            f"'{Path(document).name}' is this slice's spec, and close-slice "
            f"targets the plan: '{plan.name}', currently '{plan_status}'. Close "
            f"that, and this spec is closed with it."
        )

    if Path(document).parent.name != SPECS:
        return False, ""            # not a spec; today's generic refusal stands

    if not skip_merge:
        return False, (
            f"'{Path(document).name}' has no plan, so nothing on disk says this "
            f"slice was ever executed through the pipeline. If it shipped "
            f"outside it, that is a fact only you can state: re-run with "
            f"--skip-merge, which records it as an assertion rather than an "
            f"observation."
        )

    return True, ""
