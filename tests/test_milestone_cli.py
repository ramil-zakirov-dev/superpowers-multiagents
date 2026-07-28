import argparse

import pytest

from scripts.orchestrator import cmd_milestone


def _args(action, **kwargs):
    base = dict(action=action, dir="", id="", title="", file="")
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_new_creates_a_brief_and_prints_the_next_command(tmp_path, capsys):
    cmd_milestone(_args("new", dir=str(tmp_path), id="milestone-1", title="Intake"))

    created = list((tmp_path / "milestones").glob("*.md"))
    assert len(created) == 1
    out = capsys.readouterr().out
    assert "MILESTONE_ACTIVE" in out


def test_new_refuses_to_overwrite_and_exits_non_zero(tmp_path, capsys):
    cmd_milestone(_args("new", dir=str(tmp_path), id="milestone-1", title="Intake"))

    with pytest.raises(SystemExit) as excinfo:
        cmd_milestone(_args("new", dir=str(tmp_path), id="milestone-1", title="Other"))

    assert excinfo.value.code == 1
    assert "already exists" in capsys.readouterr().out
