"""Public process-identity acceptance contract for M3.

Prospective imports stay inside tests so the pre-implementation suite collects.
The tests observe only fixture-owned processes and the documented public API.
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

LIB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LIB))


def test_current_identity_is_precise_frozen_and_self_consistent() -> None:
    from process_identity import ProcessIdentity, capture_identity, probe_identity

    current = ProcessIdentity.current()
    assert dataclasses.is_dataclass(current)
    assert current.__dataclass_params__.frozen is True
    assert isinstance(current.pid, int) and not isinstance(current.pid, bool) and current.pid > 0
    assert isinstance(current.host_id, str) and current.host_id
    assert isinstance(current.boot_id, str) and current.boot_id
    assert isinstance(current.start_token, str) and current.start_token
    assert ProcessIdentity.from_pid(current.pid) == current
    assert capture_identity(current.pid) == current
    assert probe_identity(current) == "LIVE"


def test_fixture_child_identity_is_live_then_dead() -> None:
    from process_identity import ProcessIdentity, probe_identity

    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read(1)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        identity = ProcessIdentity.from_pid(child.pid)
        assert identity.pid == child.pid
        assert probe_identity(identity) == "LIVE"
    finally:
        if child.stdin is not None:
            child.stdin.write("x")
            child.stdin.close()
        child.wait(timeout=10)
    assert probe_identity(identity) == "DEAD"


def test_mismatched_start_or_boot_identity_is_not_live() -> None:
    """Constructed identities are confined to the public liveness oracle."""
    from process_identity import ProcessIdentity, probe_identity

    current = ProcessIdentity.current()
    wrong_start = dataclasses.replace(current, start_token=f"{current.start_token}-stale")
    wrong_boot = dataclasses.replace(current, boot_id=f"{current.boot_id}-stale")
    assert probe_identity(wrong_start) == "DEAD"
    assert probe_identity(wrong_boot) == "DEAD"


def test_direct_parent_accepts_fixture_child_and_rejects_live_sibling() -> None:
    from process_identity import ProcessIdentity, probe_direct_parent

    children = []
    try:
        for _ in range(2):
            children.append(subprocess.Popen(
                [sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read(1)"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            ))
        for child in children:
            assert child.stdout is not None
            assert child.stdout.readline().strip() == "ready"
        parent = ProcessIdentity.current()
        child, sibling = (ProcessIdentity.from_pid(item.pid) for item in children)
        assert probe_direct_parent(child, parent) == "LIVE"
        assert probe_direct_parent(child, sibling) == "DEAD"
        stale_parent = dataclasses.replace(parent, start_token=parent.start_token + "-stale")
        assert probe_direct_parent(child, stale_parent) == "DEAD"
    finally:
        for child in children:
            if child.stdin is not None:
                child.stdin.close()
            child.wait(timeout=10)


def test_incomplete_identity_is_unknown_not_pid_only_live() -> None:
    from process_identity import ProcessIdentity, probe_identity

    current = ProcessIdentity.current()
    assert probe_identity(dataclasses.replace(current, boot_id="")) == "UNKNOWN"
    assert probe_identity(dataclasses.replace(current, start_token="")) == "UNKNOWN"


def test_process_identity_rejects_nonpositive_pid() -> None:
    from process_identity import ProcessIdentity

    for pid in (0, -1):
        try:
            ProcessIdentity.from_pid(pid)
        except (ValueError, ProcessLookupError):
            pass
        else:
            raise AssertionError(f"nonpositive PID {pid} was accepted")


def test_process_identity_rejects_boolean_or_noninteger_pid() -> None:
    from process_identity import ProcessIdentity

    for pid in (True, 1.5, "1"):
        with pytest.raises((TypeError, ValueError)):
            ProcessIdentity.from_pid(pid)


def test_host_identity_is_stable_locally_and_foreign_host_is_unknown() -> None:
    """Only the documented OS-probe boundary receives a constructed host."""
    from process_identity import ProcessIdentity, probe_identity

    current = ProcessIdentity.current()
    repeated = ProcessIdentity.current()
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; print('ready',flush=True); sys.stdin.read(1)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "ready"
        child_identity = ProcessIdentity.from_pid(child.pid)
        assert repeated.host_id == current.host_id == child_identity.host_id
        assert repeated.boot_id == current.boot_id == child_identity.boot_id
        foreign = dataclasses.replace(current, host_id=f"foreign-namespace:{current.host_id}")
        assert probe_identity(foreign) == "UNKNOWN"
    finally:
        assert child.stdin is not None
        child.stdin.write("x")
        child.stdin.close()
        child.wait(timeout=10)


def test_dhcp_hostname_change_does_not_change_machine_identity_or_liveness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from process_identity import ProcessIdentity, probe_identity

    captured = ProcessIdentity.current()
    original_hostname = socket.gethostname()
    monkeypatch.setattr(socket, "gethostname", lambda: f"dhcp-renamed-{original_hostname}")
    repeated = ProcessIdentity.current()
    assert repeated.host_id == captured.host_id
    assert repeated.boot_id == captured.boot_id
    assert probe_identity(captured) == "LIVE"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux PID namespace seam")
def test_linux_pid_namespace_change_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import process_identity

    captured = process_identity.ProcessIdentity.current()
    namespace_path = f"/proc/{captured.pid}/ns/pid"
    native_stat = process_identity.os.stat
    observed = native_stat(namespace_path)

    def changed_namespace(path, *args, **kwargs):
        if os.fspath(path) == namespace_path:
            return SimpleNamespace(st_dev=observed.st_dev, st_ino=observed.st_ino + 1)
        return native_stat(path, *args, **kwargs)

    monkeypatch.setattr(process_identity.os, "stat", changed_namespace)
    assert process_identity.probe_identity(captured) == "UNKNOWN"
