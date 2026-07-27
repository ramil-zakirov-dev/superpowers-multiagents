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
