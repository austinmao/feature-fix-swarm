"""Real processes and durable SQLite prove the shared admission authority.

Capacity is resource-derived (``DEFAULT_MANAGED_RUN_CAPACITY`` is only a
compatibility symbol), so every queue here, including the ones in child
processes, observes the fixed envelope from ``admission_fixture``: two
default-demand runs fit.  Waiters are served round-robin by
``(repository_id, run_id)`` after the last admitted key; with distinct,
sorted run ids that is also enqueue order.
"""
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
    LeaseIdentity, ManagedAdmissionRefused, global_admission_root,
)
from run_state.tests.admission_fixture import queue as _queue


_CHILD = """
import json, sys
from pathlib import Path
from process_identity import ProcessIdentity
from run_state.managed_admission import LeaseIdentity
from run_state.tests.admission_fixture import queue as fixture_queue
queue = fixture_queue()
ticket = queue.enqueue(state_root=Path(sys.argv[1]), run_id=sys.argv[2])
print(json.dumps({'sequence': ticket.sequence}), flush=True)
for line in sys.stdin:
    operation, _, argument = line.strip().partition(' ')
    if operation == 'admit':
        print(json.dumps(queue.try_admit(ticket)), flush=True)
    elif operation == 'bind':
        consumer = ProcessIdentity.from_pid(int(argument))
        queue.bind_consumer(ticket, LeaseIdentity(
            'repo', sys.argv[2], 'request', 'intent', 1, ticket.owner, consumer))
        print('true', flush=True)
    elif operation == 'release':
        queue.release(ticket)
        print('true', flush=True)
    elif operation == 'reopen':
        queue = fixture_queue()
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


@contextmanager
def _consumer():
    """A real launched descendant, reaped on exit so it probes natively DEAD."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        yield process
    finally:
        process.kill()
        process.wait(timeout=10)


def _release_child(child):
    # An active lease is freed only after its bound descendant is proven dead.
    with _consumer() as consumer:
        assert _ask(child, f"bind {consumer.pid}") is True
    return _ask(child, "release")


def _release(queue, ticket, run_id):
    with _consumer() as consumer:
        queue.bind_consumer(ticket, LeaseIdentity(
            "repo", run_id, "request", "intent", 1, ticket.owner,
            ProcessIdentity.from_pid(consumer.pid)))
    queue.release(ticket)


def test_shared_capacity_fair_order_and_restart_across_state_roots(tmp_path, monkeypatch):
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
        queue = _queue()
        # A completely new queue handle/process retains the exact order.
        assert _ask(third, "reopen") is True
        late = queue.enqueue(state_root=roots[4], run_id="late")
        assert [row["state_root"] for row in queue.snapshot()] == list(map(str, roots))
        with _consumer() as consumer:
            assert _ask(first, f"bind {consumer.pid}") is True
            first.kill()
            first.wait(timeout=10)
            # A dead supervisor whose launched descendant lives keeps its slot.
            assert queue.try_admit(late) is False
            assert queue.snapshot()[0]["status"] == "active"
        # Supervisor and descendant both verifiably dead: a late caller's probe
        # reclaims the slot, but cannot take it ahead of the older waiters.
        assert queue.try_admit(late) is False
        assert queue.snapshot()[0]["status"] == "reclaimed"
        assert _ask(fourth, "admit") is False
        assert _ask(third, "admit") is True
        assert _release_child(second) is True
        assert queue.try_admit(late) is False
        assert _ask(fourth, "admit") is True
        rows = _queue().snapshot()
        assert [row["status"] for row in rows] == ["reclaimed", "released", "active", "active", "waiting"]
        assert ProcessIdentity(rows[2]["host_id"], rows[2]["boot_id"], rows[2]["pid"], rows[2]["start_token"]) == ProcessIdentity.from_pid(third.pid)
        assert _release_child(third) is True
        assert queue.try_admit(late) is True
        _release(queue, late, "late")
    # Queue metadata never manufactures per-run authority or workspaces.
    assert not any(path.exists() for path in roots)


def test_simultaneous_process_admission_cannot_oversubscribe(tmp_path):
    root = tmp_path / "global"
    with ExitStack() as stack:
        children = [stack.enter_context(_controller(root, tmp_path / str(index), str(index)))[0]
                    for index in range(4)]
        with ThreadPoolExecutor(max_workers=4) as executor:
            # Every contender asks concurrently. A later waiter may need a
            # second observation if it reached SQLite ahead of its turn.
            for _ in range(2):
                replies = list(executor.map(lambda child: _ask(child, "admit"), children))
                assert sum(replies) <= 2
                assert sum(row["status"] == "active" for row in _queue(root).snapshot()) <= 2
            assert replies == [True, True, False, False]


@pytest.mark.parametrize("active", [False, True])
def test_dead_waiter_or_owner_reclaimed_only_with_verified_death(tmp_path, active):
    root = tmp_path / "global"
    # One CPU: a retained reservation or turn is observable as a refusal.
    with _consumer() as consumer, \
            _controller(root, tmp_path / "old-state", "a-old") as (child, _):
        if active:
            assert _ask(child, "admit") is True
            assert _ask(child, f"bind {consumer.pid}") is True
        queue = _queue(root, cpu=1, liveness_probe=lambda _: "DEAD")
        current = queue.enqueue(state_root=tmp_path / "new-state", run_id="b-new")
        # Injected DEAD cannot remove a natively LIVE identity or its turn.
        assert queue.try_admit(current) is False
        assert queue.snapshot()[0]["status"] == ("active" if active else "waiting")
        child.kill()
        child.wait(timeout=10)
        if active:
            # Dead supervisor, live descendant: the reservation is retained.
            assert _queue(root, cpu=1).try_admit(current) is False
            consumer.kill()
            consumer.wait(timeout=10)
            uncertain = _queue(root, cpu=1, liveness_probe=lambda _: "UNKNOWN")
            assert uncertain.try_admit(current) is False
            assert uncertain.snapshot()[0]["status"] == "active"
        recovered = _queue(root, cpu=1)
        assert recovered.try_admit(current) is True
        # A dead waiter never launched anything: it is skipped, not reclaimed,
        # because a missing descendant identity is uncertainty.
        assert recovered.snapshot()[0]["status"] == ("reclaimed" if active else "waiting")
        _release(recovered, current, "b-new")


def test_unknown_native_identity_never_admitted_but_retains_capacity(tmp_path):
    queue = _queue(tmp_path / "global")
    first = queue.enqueue(state_root=tmp_path / "a", run_id="a")
    # A row from another host/namespace is UNKNOWN, never guessed to be dead.
    with sqlite3.connect(queue.path) as connection:
        connection.execute("UPDATE managed_admissions SET host_id='unobservable-host' WHERE sequence=?", (first.sequence,))
    later = queue.enqueue(state_root=tmp_path / "b", run_id="b")
    # An unknown waiter receives no grant and cannot head-of-line block.
    assert queue.try_admit(later) is True
    assert queue.snapshot()[0]["status"] == "waiting"
    with sqlite3.connect(queue.path) as connection:
        connection.execute("UPDATE managed_admissions SET status='active' WHERE sequence=?", (first.sequence,))
    third = queue.enqueue(state_root=tmp_path / "c", run_id="c")
    # An unknown active owner keeps its reservation: it is never reclaimed.
    assert queue.try_admit(third) is False
    assert queue.snapshot()[0]["status"] == "active"
    assert queue.snapshot()[2]["limiting_resource"] == "cpu"
    _release(queue, later, "b")
    assert queue.try_admit(third) is True
    _release(queue, third, "c")


def _assert_write_available(path):
    with sqlite3.connect(path, timeout=0, isolation_level=None) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.rollback()


def test_probes_and_waiting_do_not_hold_sqlite_transactions(tmp_path, monkeypatch):
    root = tmp_path / "global"
    observations = []
    with _consumer() as consumer, _controller(root, tmp_path / "dead", "dead") as (child, _):
        assert _ask(child, "admit") is True
        assert _ask(child, f"bind {consumer.pid}") is True
        child.kill()
        child.wait(timeout=10)
        consumer.kill()
        consumer.wait(timeout=10)

        def probe(_):
            _assert_write_available(root / "admission.sqlite3")
            observations.append("probe")
            return "DEAD"

        queue = _queue(root, liveness_probe=probe)
        tickets = [queue.acquire(state_root=tmp_path / str(index), run_id=str(index), timeout=10)
                   for index in range(2)]
        # Supervisor and descendant are each probed once, outside SQLite.
        assert observations == ["probe", "probe"]
        assert queue.snapshot()[0]["status"] == "reclaimed"

        def interrupted_sleep(_):
            _assert_write_available(queue.path)
            observations.append("sleep")
            raise KeyboardInterrupt()

        with monkeypatch.context() as patch:
            patch.setattr(managed_admission.time, "sleep", interrupted_sleep)
            with pytest.raises(KeyboardInterrupt):
                queue.acquire(state_root=tmp_path / "waiting", run_id="waiting", timeout=10)
        assert observations == ["probe", "probe", "sleep"]
        assert queue.snapshot()[-1]["status"] == "released"
        for index, ticket in enumerate(tickets):
            _release(queue, ticket, str(index))


def test_timeout_releases_waiter_and_ticket_requires_full_owner(tmp_path):
    queue = _queue(tmp_path / "global")
    tickets = [queue.acquire(state_root=tmp_path / str(index), run_id=str(index), timeout=10)
               for index in range(2)]
    with pytest.raises(ManagedAdmissionRefused, match="MANAGED_ADMISSION_TIMEOUT"):
        queue.acquire(state_root=tmp_path / "waiting", run_id="waiting", timeout=0)
    assert queue.snapshot()[-1]["status"] == "released"
    forged = replace(tickets[0], owner=replace(tickets[0].owner, start_token="other-incarnation"))
    with pytest.raises(ManagedAdmissionRefused, match="MANAGED_ADMISSION_IDENTITY_UNKNOWN"):
        queue.release(forged)
    with pytest.raises(ManagedAdmissionRefused, match="MANAGED_ADMISSION_TICKET_INVALID"):
        queue.release(replace(tickets[0], ticket="other-ticket"))
    # An active lease with no proven-dead descendant cannot be released.
    with pytest.raises(ManagedAdmissionRefused, match="MANAGED_ADMISSION_DESCENDANT_RETAINED"):
        queue.release(tickets[0])
    for index, ticket in enumerate(tickets):
        _release(queue, ticket, str(index))


def test_suite_never_uses_the_per_user_admission_root(tmp_path_factory):
    root = managed_admission.global_admission_root()
    assert root.is_relative_to(tmp_path_factory.getbasetemp().resolve())


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
    queue = _queue(root)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(ManagedAdmissionRefused, match="MANAGED_ADMISSION_ROOT_UNSAFE"):
        _queue(alias)
    ticket = queue.acquire(state_root=tmp_path / "run", run_id="run", timeout=10)
    queue.path.rename(root / "retained.sqlite3")
    replacement = _queue(root)
    with pytest.raises(ManagedAdmissionRefused, match="MANAGED_ADMISSION_STORE_UNSAFE"):
        queue.release(ticket)
    assert replacement.snapshot() == []
