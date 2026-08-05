"""What `--dir` names, and what a report may claim when it found nothing.

`--dir` meant two different things depending on the subcommand: the docs base
for `status`, `wait` and `milestone new`, the project root for `trigger-hook`,
`summary`, `reconcile` and `sandbox`. Four subcommands teach the habit of
passing the project root; the other three then glob `./specs`, find nothing,
and print `(none)` under exit 0.

For a report that is the worst available failure: it does not error, it
asserts. `(none)` for all three folders is indistinguishable from a genuinely
empty pipeline, so a reader concludes there is no work in flight and acts on
it. A human loses a minute; an agent driving the state machine off that output
loses the milestone.

Two separate facts, kept separate here: a base that exists and holds no
documents is an empty pipeline and reports as one; a base that does not exist
under either reading is a question about the argument, and is refused.
"""

import argparse
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.errors import OrchestratorError
from scripts.orchestrator import cmd_status
from scripts.paths import resolve_docs_base

REPO_ROOT = Path(__file__).resolve().parent.parent


def _pipeline(root: Path, *, spec: str = "") -> Path:
    base = root / "docs" / "superpowers"
    for name in ("milestones", "specs", "plans"):
        (base / name).mkdir(parents=True, exist_ok=True)
    if spec:
        (base / "specs" / "a-design.md").write_text(spec, encoding="utf-8")
    return base


# --- the resolution itself ---


def test_a_project_root_resolves_to_its_docs_base(tmp_path):
    """The reported bug, at its root: `--dir .` is what the sibling
    subcommands teach, and it has to find the pipeline rather than deny it.
    """
    base = _pipeline(tmp_path)
    assert resolve_docs_base(tmp_path) == base


def test_the_docs_base_itself_still_resolves(tmp_path):
    """Back-compat, and not incidental: `commands/status.md` ships
    `--dir docs/superpowers`, so this is the form already in the field.
    """
    base = _pipeline(tmp_path)
    assert resolve_docs_base(base) == base


def test_the_project_root_reading_wins_when_both_could_apply(tmp_path):
    """A repository with a top-level `specs/` of its own would otherwise be
    read as its own docs base. `docs/superpowers/` is the canonical layout and
    is the more specific signal, so it decides.
    """
    base = _pipeline(tmp_path)
    (tmp_path / "specs").mkdir()
    assert resolve_docs_base(tmp_path) == base


def test_a_directory_that_is_neither_is_refused(tmp_path):
    """The alternative is the defect: globbing three folders that are not
    there and reporting the emptiness as a fact about the pipeline.
    """
    with pytest.raises(OrchestratorError) as excinfo:
        resolve_docs_base(tmp_path)

    message = str(excinfo.value)
    assert str(tmp_path.resolve()) in message, "the refusal must name what it read"
    assert "docs/superpowers" in message.replace("\\", "/"), "and both forms it tried"


def test_an_empty_pipeline_is_not_a_missing_one(tmp_path):
    """A base whose folders exist and hold nothing is a real, empty pipeline.
    Refusing that would trade one lie for a false alarm on every fresh project.
    """
    base = _pipeline(tmp_path)
    assert resolve_docs_base(tmp_path) == base


def test_a_creating_command_accepts_a_base_that_does_not_exist_yet(tmp_path):
    """`milestone new` writes the first document a project ever has, so for it
    a missing base is the normal case rather than a bad argument.
    """
    target = tmp_path / "docs" / "superpowers"
    assert resolve_docs_base(target, must_exist=False) == target


def test_a_creating_command_still_reads_a_project_root_as_one(tmp_path):
    """must_exist relaxes the refusal, not the resolution."""
    base = _pipeline(tmp_path)
    assert resolve_docs_base(tmp_path, must_exist=False) == base


# --- what the report does with it ---


def _status(base, capsys, **overrides):
    cmd_status(argparse.Namespace(**{"dir": str(base), "all": False, **overrides}))
    return capsys.readouterr().out


def test_status_over_a_project_root_lists_the_pipeline(tmp_path, capsys):
    """End to end, the line from the issue: same tree, same moment, the flag
    the sibling commands teach — and now the same answer as without it.
    """
    _pipeline(tmp_path, spec='---\nslice_id: "s"\nstatus: SPEC_APPROVED\n---\n# S\n')

    out = _status(tmp_path, capsys)

    assert "SPEC_APPROVED" in out
    assert "a-design.md" in out


def test_status_names_the_directory_it_read(tmp_path, capsys):
    """A report of "nothing" is only auditable if it says where it looked."""
    _pipeline(tmp_path)

    out = _status(tmp_path, capsys)

    assert str((tmp_path / "docs" / "superpowers").resolve()) in out


def test_status_refuses_a_directory_with_no_pipeline(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        _status(tmp_path, capsys)

    assert excinfo.value.code != 0
    out = capsys.readouterr().out
    assert "(none)" not in out, "an unresolvable path must not be reported as empty"
    assert str(tmp_path.resolve()) in out


def test_status_still_reports_a_genuinely_empty_pipeline(tmp_path, capsys):
    _pipeline(tmp_path)

    out = _status(tmp_path, capsys)

    assert out.count("(none)") == 3


# --- the flags say which of the two they mean ---


def _help(subcommand):
    result = subprocess.run(
        [sys.executable, "scripts/orchestrator.py", subcommand, "--help"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize("subcommand", ["status", "wait"])
def test_a_docs_base_subcommand_offers_the_explicit_name(subcommand):
    assert "--docs-dir" in _help(subcommand)


@pytest.mark.parametrize("subcommand", ["summary", "reconcile", "sandbox", "trigger-hook"])
def test_a_project_root_subcommand_offers_the_explicit_name(subcommand):
    assert "--project-root" in _help(subcommand)


@pytest.mark.parametrize(
    "subcommand", ["status", "wait", "summary", "reconcile", "sandbox", "trigger-hook"]
)
def test_dir_survives_as_an_alias_everywhere_it_worked(subcommand):
    """Renaming without an alias would break every caller in the field for a
    naming improvement — including this plugin's own shipped commands.
    """
    assert "--dir" in _help(subcommand)


def test_wait_refuses_an_unresolvable_directory_with_its_own_code(tmp_path, capsys):
    """`wait` publishes 0/2/1/3, and an unresolvable `--dir` is "could not
    start" (3), never "timed out" (1). Exiting 1 would tell a caller its
    dispatch is still running.
    """
    from scripts.orchestrator import cmd_wait

    with pytest.raises(SystemExit) as excinfo:
        cmd_wait(argparse.Namespace(
            dir=str(tmp_path), slice="s", file=None, timeout=None, poll=15.0
        ))

    assert excinfo.value.code == 3
    assert str(tmp_path.resolve()) in capsys.readouterr().out


def test_status_from_a_directory_with_no_project_says_so(tmp_path):
    """The CLI-level counterpart, run the way a user runs it: from somewhere
    that is not a project at all.
    """
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "orchestrator.py"), "status"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "ModuleNotFoundError" not in result.stderr
    assert "docs/superpowers" in result.stdout.replace("\\", "/")
