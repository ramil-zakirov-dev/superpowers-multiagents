"""A dispatch log is evidence, and a retry must not be able to destroy it.

The log path is derived from the role and the document, so every re-dispatch
of the same slice lands on the same file. Opening it truncating meant the
second attempt erased the first — and the first is the interesting one, since
a retry is what you do *after* a failure.

Observed on 2026-08-05, and it cost a diagnosis: the run quoted in #15 refused
to do its work, was re-dispatched, and by the time anyone looked for the reason
the only surviving transcript belonged to the successful retry. The issue had
to be retracted for want of the evidence the tool had already thrown away.
"""

import re
import sys

from scripts.paths import log_path
from scripts.runner import run_supervised
from scripts.locks import acquire_slice_lock

BANNER = re.compile(r"^=== run started \S+ ===$", re.MULTILINE)


def _run(tmp_project, demo_spec, marker):
    """One supervised run whose agent prints `marker` and nothing else."""
    lock_file = acquire_slice_lock("slice-01-demo", tmp_project)
    log_file = log_path(tmp_project, "planner", demo_spec.stem)
    run_supervised(
        role="planner",
        target_file=demo_spec,
        project_root=tmp_project,
        lock_file=lock_file,
        log_file=log_file,
        argv=[sys.executable, "-c", f"print({marker!r})"],
        cwd=tmp_project,
    )
    return log_file


def test_a_retry_does_not_erase_the_failed_run_it_retries(tmp_project, demo_spec):
    """The regression, in one line: both markers, or the tool ate the evidence."""
    _run(tmp_project, demo_spec, "FIRST-RUN-MARKER")
    log_file = _run(tmp_project, demo_spec, "SECOND-RUN-MARKER")

    text = log_file.read_text(encoding="utf-8")
    assert "FIRST-RUN-MARKER" in text
    assert "SECOND-RUN-MARKER" in text


def test_each_run_opens_with_a_banner_so_the_two_can_be_told_apart(
    tmp_project, demo_spec
):
    """Accumulating without a delimiter would trade one unreadable log for
    another: a reader who cannot see where a run begins cannot tell which
    attempt an error belongs to.
    """
    _run(tmp_project, demo_spec, "FIRST-RUN-MARKER")
    log_file = _run(tmp_project, demo_spec, "SECOND-RUN-MARKER")

    assert len(BANNER.findall(log_file.read_text(encoding="utf-8"))) == 2


def test_the_runs_appear_in_the_order_they_happened(tmp_project, demo_spec):
    """`summary` prints the tail, and `wait` points at this file — both are
    only truthful if the newest run is the last thing in it.
    """
    _run(tmp_project, demo_spec, "FIRST-RUN-MARKER")
    log_file = _run(tmp_project, demo_spec, "SECOND-RUN-MARKER")

    text = log_file.read_text(encoding="utf-8")
    assert text.index("FIRST-RUN-MARKER") < text.index("SECOND-RUN-MARKER")


def test_every_run_records_the_command_it_ran(tmp_project, demo_spec):
    """One banner per run is only half the story: two dispatches of one role
    can differ by `--model`, and the argv is where that difference shows.
    """
    _run(tmp_project, demo_spec, "FIRST-RUN-MARKER")
    log_file = _run(tmp_project, demo_spec, "SECOND-RUN-MARKER")

    invocations = [
        line for line in log_file.read_text(encoding="utf-8").splitlines()
        if line.startswith("$ ")
    ]
    assert len(invocations) == 2
    assert "FIRST-RUN-MARKER" in invocations[0]
    assert "SECOND-RUN-MARKER" in invocations[1]


def test_the_outcome_of_each_run_is_kept_too(tmp_project, demo_spec):
    """`_record_outcome` already appends; the point is that the *earlier*
    run's verdict is still there to compare against.
    """
    _run(tmp_project, demo_spec, "FIRST-RUN-MARKER")
    log_file = _run(tmp_project, demo_spec, "SECOND-RUN-MARKER")

    outcomes = [
        line for line in log_file.read_text(encoding="utf-8").splitlines()
        if "[runner] planner exited" in line
    ]
    assert len(outcomes) == 2
