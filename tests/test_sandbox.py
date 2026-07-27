import errno
import socket

import pytest

from scripts.errors import OrchestratorError
from scripts import sandbox


def test_project_name_follows_compose_rules():
    assert sandbox.project_name_for("feat/Alpha_1") == "feat-alpha_1"
    assert sandbox.project_name_for("feat//weird--branch--") == "feat-weird-branch"
    assert sandbox.project_name_for("") == "default"


def test_ip_for_is_deterministic_and_never_loopback_one(monkeypatch):
    monkeypatch.setattr(sandbox, "_probe", lambda ip, port=0: "free")
    first = sandbox.ip_for("feat/alpha")
    second = sandbox.ip_for("feat/alpha")
    assert first == second
    assert first != "127.0.0.1"
    assert first.startswith("127.0.0.")


def test_ip_for_skips_addresses_reported_busy(monkeypatch):
    """A branch whose hashed octet is taken must move on, not fail."""
    start = sandbox._hash_octet("feat/alpha")
    taken = f"127.0.0.{start}"
    monkeypatch.setattr(
        sandbox, "_probe", lambda ip, port=0: "busy" if ip == taken else "free"
    )
    assert sandbox.ip_for("feat/alpha") != taken


def test_ip_for_skips_addresses_the_caller_declares_busy(monkeypatch):
    start = sandbox._hash_octet("feat/alpha")
    taken = f"127.0.0.{start}"
    monkeypatch.setattr(sandbox, "_probe", lambda ip, port=0: "free")
    assert sandbox.ip_for("feat/alpha", busy=[taken]) != taken


def test_unavailable_address_aborts_with_a_remedy_not_a_busy_report(monkeypatch):
    """EADDRNOTAVAIL is a platform fact. Scanning 254 more is the wrong answer."""
    probes = []

    def fake_probe(ip, port=0):
        probes.append(ip)
        return "unavailable"

    monkeypatch.setattr(sandbox, "_probe", fake_probe)

    with pytest.raises(OrchestratorError) as excinfo:
        sandbox.ip_for("feat/alpha")

    message = str(excinfo.value)
    assert "ifconfig" in message, "the error must name the remediation command"
    assert "no free loopback" not in message.lower()
    assert len(probes) == 1, f"aborted after {len(probes)} probes, expected 1"


def test_probe_maps_errnos_to_verdicts(monkeypatch):
    class FakeSocket:
        def __init__(self, code):
            self._code = code

        def bind(self, _address):
            if self._code is None:
                return None
            raise OSError(self._code, "fake")

        def close(self):
            return None

    monkeypatch.setattr(
        socket, "socket", lambda *a, **k: FakeSocket(errno.EADDRNOTAVAIL)
    )
    assert sandbox._probe("127.0.0.9") == "unavailable"

    monkeypatch.setattr(socket, "socket", lambda *a, **k: FakeSocket(errno.EADDRINUSE))
    assert sandbox._probe("127.0.0.9") == "busy"

    monkeypatch.setattr(socket, "socket", lambda *a, **k: FakeSocket(None))
    assert sandbox._probe("127.0.0.9") == "free"


def test_state_round_trips(tmp_path):
    record = sandbox.SandboxState(
        branch="feat/alpha", ip="127.0.0.7",
        project_name="feat-alpha", started_at="2026-07-27T00:00:00+00:00",
    )
    sandbox.write_state(tmp_path, record)
    assert sandbox.read_state(tmp_path, "feat/alpha") == record


def test_read_state_is_none_when_untracked(tmp_path):
    assert sandbox.read_state(tmp_path, "feat/nothing") is None


def test_clear_state_is_idempotent(tmp_path):
    sandbox.clear_state(tmp_path, "feat/absent")  # must not raise
    record = sandbox.SandboxState("feat/a", "127.0.0.7", "feat-a", "t")
    sandbox.write_state(tmp_path, record)
    sandbox.clear_state(tmp_path, "feat/a")
    assert sandbox.read_state(tmp_path, "feat/a") is None


def test_list_states_ignores_unparsable_files(tmp_path):
    record = sandbox.SandboxState("feat/a", "127.0.0.7", "feat-a", "t")
    sandbox.write_state(tmp_path, record)
    (sandbox_state_dir := sandbox.sandbox_dir(tmp_path)).mkdir(exist_ok=True)
    (sandbox_state_dir / "garbage.json").write_text("{not json", encoding="utf-8")
    assert sandbox.list_states(tmp_path) == [record]
