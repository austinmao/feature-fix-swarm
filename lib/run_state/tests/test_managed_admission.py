"""Real processes and durable SQLite prove the shared admission authority."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import selectors
import pwd
import sqlite3
import subprocess
import sys

import pytest

from process_identity import ProcessIdentity
from run_state import managed_admission
from run_state.managed_admission import (
    ManagedAdmissionQueue, ManagedAdmissionRefused, global_admission_root,
)


_CHILD = """
import json, sys
from pathlib import Path
from run_state.managed_admission import ManagedAdmissionQueue
queue = ManagedAdmissionQueue()
ticket = queue.enqueue(state_root=Path(sys.argv[1]), run_id=sys.argv[2])
print(json.dumps({'sequence': ticket.sequence}), flush=True)
for line in sys.stdin:
    operation = line.strip()
    if operation == 'admit':
        print(json.dumps(queue.try_admit(ticket)), flush=True)
    elif operation == 'release':
        queue.release(ticket)
        print('true', flush=True)
    elif operation == 'reopen':
        queue = ManagedAdmissionQueue()
        print('true', flush=True)
"""


def _read(child):
    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout, selectors.EVENT_READ)
        assert selector.select(10), "queue process did not reply"
    line = child.stdout.readline()
    assert line, child.stderr.read()
    return json.loads(line)


def _ask(child, operation):
    child.stdin.write(operation + "\n")
    child.stdin.flush()
    return _read(child)


@contextmanager
def _controller(root, state_root, run_id):
    env = {**os.environ, "FFS_MANAGED_ADMISSION_ROOT": str(root),
           "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    child = subprocess.Popen([sys.executable, "-u", "-c", _CHILD, str(state_root), run_id],
                             env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        ready = _read(child)
        yield child, ready["sequence"]
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
        for stream in (child.stdin, child.stdout, child.stderr):
            stream.close()


def test_shared_capacity_fifo_and_restart_across_state_roots(tmp_path, monkeypatch):
    root = tmp_path / "global"
    monkeypatch.setenv("FFS_MANAGED_ADMISSION_ROOT", str(root))
    roots = [tmp_path / f"repository-state-{index}" for index in range(5)]
    with _controller(root, roots[0], "a") as (first, first_seq), \
            _controller(root, roots[1], "b") as (second, second_seq), \
            _controller(root, roots[2], "c") as (third, third_seq), \
            _controller(root, roots[3], "d") as (fourth, fourth_seq):
        assert first_seq < second_seq < third_seq < fourth_seq
        assert _ask(first, "admit") is True
        assert _ask(second, "admit") is True
        assert _ask(third, "admit") is False
        assert _ask(fourth, "admit") is False
        queue = ManagedAdmissionQueue()
        # A completely new queue handle/process retains the exact FIFO order.
        assert _ask(third, "reopen") is True
        late = queue.enqueue(state_root=roots[4], run_id="late")
        assert [row["state_root"] for row in queue.snapshot()] == list(map(str, roots))
        first.kill()
        first.wait(timeout=10)
        # A late caller can reclaim the dead owner, but cannot steal its slot.
        assert queue.try_admit(late) is False
        assert _ask(fourth, "admit") is False
        assert _ask(third, "admit") is True
        assert _ask(second, "release") is True
        assert queue.try_admit(late) is False
        assert _ask(fourth, "admit") is True
        rows = ManagedAdmissionQueue().snapshot()
        assert [row["status"] for row in rows] == ["reclaimed", "released", "active", "active", "waiting"]
        assert ProcessIdentity(rows[2]["host_id"], rows[2]["boot_id"], rows[2]["pid"], rows[2]["start_token"]) == ProcessIdentity.from_pid(third.pid)
        assert _ask(third, "release") is True
        assert queue.try_admit(late) is True
        queue.release(late)
    # Queue metadata never manufactures per-run authority or workspaces.
    assert not any(path.exists() for path in roots)


def test_simultaneous_process_admission_cannot_oversubscribe(tmp_path):
    root = tmp_path / "global"
    with ExitStack() as stack:
        children = [stack.enter_context(_controller(root, tmp_path / str(index), str(index)))[0]
                    for index in range(4)]
        with ThreadPoolExecutor(max_workers=4) as executor:
            # Every contender asks concurrently. A later waiter may need a
            # second observation if it reached SQLite ahead of the FIFO head.
            for _ in range(2):
                replies = list(executor.map(lambda child: _ask(child, "admit"), children))
                assert sum(replies) <= 2
                assert sum(row["status"] == "active" for row in ManagedAdmissionQueue(root).snapshot()) <= 2
            assert replies == [True, True, False, False]


@pytest.mark.parametrize("active", [False, True])
def test_dead_waiter_or_owner_reclaimed_only_with_verified_death(tmp_path, active):
    root = tmp_path / "global"
    with _controller(root, tmp_path / "old-state", "old") as (child, sequence):
        if active:
            assert _ask(child, "admit") is True
        queue = ManagedAdmissionQueue(root, liveness_probe=lambda _: "DEAD")
        current = queue.enqueue(state_root=tmp_path / "new-state", run_id="new")
        # Injected DEAD cannot remove a natively LIVE identity.
        assert queue.try_admit(current) is active
        assert queue.snapshot()[0]["status"] == ("active" if active else "waiting")
        child.kill()
        child.wait(timeout=10)
        uncertain = ManagedAdmissionQueue(root, liveness_probe=lambda _: "UNKNOWN")
        uncertain.try_admit(current)
        assert uncertain.snapshot()[0]["status"] == ("active" if active else "waiting")
        recovered = ManagedAdmissionQueue(root)
        assert recovered.try_admit(current) is True
        assert recovered.snapshot()[0]["status"] == "reclaimed"
        recovered.release(current)


def test_unknown_native_identity_preserves_fifo_and_capacity(tmp_path, monkeypatch):
    queue = ManagedAdmissionQueue(tmp_path / "global")
    first = queue.enqueue(state_root=tmp_path / "a", run_id="a")
    # A row from another host/namespace is UNKNOWN, never guessed to be dead.
    with sqlite3.connect(queue.path) as connection:
        connection.execute("UPDATE managed_admissions SET host_id='unobservable-host' WHERE sequence=?", (first.sequence,))
    later = queue.enqueue(state_root=tmp_path / "b", run_id="b")
    assert queue.try_admit(later) is False
    assert queue.snapshot()[0]["status"] == "waiting"
    with sqlite3.connect(queue.path) as connection:
        connection.execute("UPDATE managed_admissions SET status='active' WHERE sequence=?", (first.sequence,))
    assert queue.try_admit(later) is True
    third = queue.enqueue(state_root=tmp_path / "c", run_id="c")
    assert queue.try_admit(third) is False
    queue.release(later)
    queue.release(third)


def _assert_write_available(path):
    with sqlite3.connect(path, timeout=0, isolation_level=None) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.rollback()


def test_probes_and_waiting_do_not_hold_sqlite_transactions(tmp_path, monkeypatch):
    root = tmp_path / "global"
    observations = []
    with _controller(root, tmp_path / "dead", "dead") as (child, _):
        child.kill()
        child.wait(timeout=10)

        def probe(_):
            _assert_write_available(root / "admission.sqlite3")
            observations.append("probe")
            return "DEAD"

        queue = ManagedAdmissionQueue(root, liveness_probe=probe)
        tickets = [queue.acquire(state_root=tmp_path / str(index), run_id=str(index)) for index in range(2)]
        assert observations == ["probe"]

        def interrupted_sleep(_):
            _assert_write_available(queue.path)
            observations.append("sleep")
            raise KeyboardInterrupt()

        monkeypatch.setattr(managed_admission.time, "sleep", interrupted_sleep)
        with pytest.raises(KeyboardInterrupt):
            queue.acquire(state_root=tmp_path / "waiting", run_id="waiting")
        assert observations == ["probe", "sleep"]
        assert queue.snapshot()[-1]["status"] == "released"
        for ticket in tickets:
            queue.release(ticket)


def test_timeout_releases_waiter_and_ticket_requires_full_owner(tmp_path):
    queue = ManagedAdmissionQueue(tmp_path / "global")
    tickets = [queue.acquire(state_root=tmp_path / str(index), run_id=str(index)) for index in range(2)]
    with pytest.raises(ManagedAdmissionRefused, match="MANAGED_ADMISSION_TIMEOUT"):
        queue.acquire(state_root=tmp_path / "waiting", run_id="waiting", timeout=0)
    assert queue.snapshot()[-1]["status"] == "released"
    forged = replace(tickets[0], owner=replace(tickets[0].owner, start_token="other-incarnation"))
    with pytest.raises(ManagedAdmissionRefused, match="MANAGED_ADMISSION_IDENTITY_UNKNOWN"):
        queue.release(forged)
    with pytest.raises(ManagedAdmissionRefused, match="MANAGED_ADMISSION_TICKET_INVALID"):
        queue.release(replace(tickets[0], ticket="other-ticket"))
    for ticket in tickets:
        queue.release(ticket)


def test_authority_root_ignores_per_run_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("FFS_MANAGED_ADMISSION_ROOT", str(tmp_path / "global"))
    monkeypatch.setenv("FFS_STATE_ROOT", str(tmp_path / "run-a"))
    first = global_admission_root()
    monkeypatch.setenv("FFS_STATE_ROOT", str(tmp_path / "run-b"))
    monkeypatch.setenv("RUN_STATE_DB", str(tmp_path / "other.db"))
    assert global_admission_root() == first == tmp_path / "global"
    monkeypatch.delenv("FFS_MANAGED_ADMISSION_ROOT")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "runtime-profile-home")
    assert global_admission_root() == Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/state/feature-fix-swarm/managed-admission"


def test_unsafe_and_replaced_authority_fails_closed(tmp_path):
    root = tmp_path / "global"
    queue = ManagedAdmissionQueue(root)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(ManagedAdmissionRefused, match="MANAGED_ADMISSION_ROOT_UNSAFE"):
        ManagedAdmissionQueue(alias)
    ticket = queue.acquire(state_root=tmp_path / "run", run_id="run")
    queue.path.rename(root / "retained.sqlite3")
    replacement = ManagedAdmissionQueue(root)
    with pytest.raises(ManagedAdmissionRefused, match="MANAGED_ADMISSION_STORE_UNSAFE"):
        queue.release(ticket)
    assert replacement.snapshot() == []
