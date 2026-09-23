"""Bounded broker admission for an already registered worker binding."""
from contextlib import nullcontext
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from process_identity import DEAD, LIVE, UNKNOWN, ProcessIdentity
import run_state.worker_channel as worker_channel
from run_state.worker_channel import WorkerBinding, WorkerChannelRefused, WorkerChannelServer
from test_supervised_process import setup_owner


@pytest.fixture
def registered_channel(tmp_path):
    if not os.environ.get("FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR"):
        pytest.skip("controller-registered upstream runtime is required for ControlStore integration")
    supervisor, store, dispatch = setup_owner(tmp_path)
    socket_root = Path(tempfile.mkdtemp(prefix="ffs-broker-", dir="/tmp")).resolve()
    server = WorkerChannelServer(store, supervisor.token, socket_root / "worker.sock")
    handle = supervisor.launch(replace(dispatch, command=(sys.executable, "-c", "import time; time.sleep(30)")))
    server.register_worker(handle.intent_id, contract_hash=dispatch.contract_hash)
    try:
        yield server, store, supervisor, handle
    finally:
        handle.process.terminate()
        try:
            supervisor.finish(handle, timeout=10, token_usage=0)
        finally:
            server.close()
            socket_root.rmdir()


def test_broker_bootstrap_is_exact_child_single_use_and_in_memory(registered_channel, monkeypatch):
    server, store, _supervisor, handle = registered_channel
    binding = server._primary_bindings[handle.intent_id]
    broker = ProcessIdentity(binding.identity.host_id, binding.identity.boot_id,
                             binding.identity.pid + 10_000, "broker-start-token")
    reused_pid = replace(broker, start_token="reused-start-token")
    ancestry_is_live = True
    primary_status = LIVE

    def probe(identity):
        if identity == binding.identity:
            return primary_status
        return LIVE if identity in {broker, reused_pid} else DEAD

    def direct_parent(child, parent):
        return LIVE if ancestry_is_live and child == broker and parent == binding.identity else DEAD

    monkeypatch.setattr(worker_channel, "probe_identity", probe)
    monkeypatch.setattr(worker_channel, "probe_direct_parent", direct_parent)
    with store.read_transaction() as tx:
        tables_before = [row[0] for row in tx.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
        )]
        events_before = tx.execute("SELECT count(*) FROM control_events").fetchone()[0]

    token = server.issue_broker_bootstrap(handle.intent_id, broker)
    assert len(token) == 43
    assert tuple(server._broker_bootstraps) == (hashlib.sha256(token.encode()).digest(),)
    message = {"schema_version": 1, **binding.scope(), "bootstrap_token": token}

    with pytest.raises(WorkerChannelRefused, match="IPC_BROKER_BOOTSTRAP_REFUSED"):
        server._request(broker, {**message, "bootstrap_token": "A" * 43})

    with pytest.raises(WorkerChannelRefused, match="IPC_BROKER_BOOTSTRAP_REFUSED"):
        server._request(reused_pid, message)
    assert ancestry_is_live

    ancestry_is_live = False
    with pytest.raises(WorkerChannelRefused, match="IPC_BROKER_BOOTSTRAP_REFUSED"):
        server._request(broker, message)
    ancestry_is_live = True

    admitted = server._request(broker, message)
    assert admitted == {"ok": True, "scope": binding.scope()}
    assert server._bindings[broker.pid] == binding
    assert handle.intent_id not in server._broker_bootstrap_intents
    assert token.encode() not in server._broker_bootstraps

    with pytest.raises(WorkerChannelRefused, match="IPC_BROKER_BOOTSTRAP_REFUSED"):
        server._request(broker, message)
    with pytest.raises(WorkerChannelRefused, match="IPC_BROKER_NOT_AVAILABLE"):
        server.issue_broker_bootstrap(handle.intent_id, broker)

    ordinary = server._request(broker, {
        "schema_version": 1, **binding.scope(), "request_key": "broker-progress",
        "operation": "progress", "body": {"sequence": 1, "message": "brokered"},
    })
    assert ordinary["ok"] is True
    ancestry_is_live = False
    with pytest.raises(WorkerChannelRefused, match="IPC_BROKER_ANCESTRY_MISMATCH"):
        server._request(broker, {
            "schema_version": 1, **binding.scope(), "request_key": "orphan-progress",
            "operation": "progress", "body": {"sequence": 2, "message": "orphaned"},
        })
    ancestry_is_live = True
    for primary_status in (DEAD, UNKNOWN):
        with pytest.raises(WorkerChannelRefused, match="IPC_BROKER_ANCESTRY_MISMATCH"):
            server._request(broker, {
                "schema_version": 1, **binding.scope(), "request_key": "dead-parent-progress",
                "operation": "progress", "body": {"sequence": 3, "message": "parent gone"},
            })
    primary_status = LIVE
    with pytest.raises(WorkerChannelRefused, match="IPC_PEER_MISMATCH"):
        server._request(reused_pid, {
            "schema_version": 1, **binding.scope(), "request_key": "pid-reuse-progress",
            "operation": "progress", "body": {"sequence": 4, "message": "reused pid"},
        })
    with store.read_transaction() as tx:
        assert [row[0] for row in tx.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
        )] == tables_before
        assert tx.execute("SELECT count(*) FROM control_events").fetchone()[0] == events_before + 1


def test_broker_bootstrap_refuses_non_child_before_issuing(registered_channel, monkeypatch):
    server, _store, _supervisor, handle = registered_channel
    binding = server._primary_bindings[handle.intent_id]
    broker = ProcessIdentity(binding.identity.host_id, binding.identity.boot_id,
                             binding.identity.pid + 10_000, "not-a-child")
    monkeypatch.setattr(worker_channel, "probe_identity", lambda _identity: LIVE)
    monkeypatch.setattr(worker_channel, "probe_direct_parent", lambda _child, _parent: DEAD)

    with pytest.raises(WorkerChannelRefused, match="IPC_BROKER_ANCESTRY_MISMATCH"):
        server.issue_broker_bootstrap(handle.intent_id, broker)
    assert not server._broker_bootstraps


def test_broker_bootstrap_enters_owner_fence_before_channel_lock(
    registered_channel, monkeypatch,
):
    server, store, _supervisor, handle = registered_channel
    binding = server._primary_bindings[handle.intent_id]
    broker = ProcessIdentity(binding.identity.host_id, binding.identity.boot_id,
                             binding.identity.pid + 10_000, "broker-lock-order")
    monkeypatch.setattr(worker_channel, "probe_identity", lambda _identity: LIVE)
    monkeypatch.setattr(worker_channel, "probe_direct_parent", lambda _child, _parent: LIVE)
    original_fenced = store.fenced_operation
    original_transaction = store.transaction
    owner_fence_active = False

    @contextmanager
    def checked_fence(token):
        nonlocal owner_fence_active
        owner_fence_active = True
        try:
            with original_fenced(token):
                yield
        finally:
            owner_fence_active = False

    @contextmanager
    def checked_transaction():
        assert owner_fence_active, "channel path acquired the store below its own lock"
        with original_transaction() as tx:
            yield tx

    monkeypatch.setattr(store, "fenced_operation", checked_fence)
    monkeypatch.setattr(store, "transaction", checked_transaction)
    assert len(server.issue_broker_bootstrap(handle.intent_id, broker)) == 43


def test_broker_delegate_reaches_owner_allocator_and_replays(
    registered_channel, monkeypatch,
):
    server, _store, supervisor, handle = registered_channel
    binding = server._primary_bindings[handle.intent_id]
    broker = ProcessIdentity(binding.identity.host_id, binding.identity.boot_id,
                             binding.identity.pid + 10_000, "broker-delegate")
    monkeypatch.setattr(worker_channel, "probe_identity", lambda _identity: LIVE)
    monkeypatch.setattr(worker_channel, "probe_direct_parent", lambda child, parent:
                        LIVE if child == broker and parent == binding.identity else DEAD)
    token = server.issue_broker_bootstrap(handle.intent_id, broker)
    server._request(broker, {
        "schema_version": 1, **binding.scope(), "bootstrap_token": token,
    })
    supervisor.worker_channel = server
    server.attach_delegate_consumer(supervisor.consume_delegate_request)
    body = {
        "parent_activity_id": binding.activity_id,
        "role": "worker",
        "candidate_hash": binding.candidate_hash,
        "contract_hash": binding.contract_hash,
        "runtime_identity": binding.runtime_identity,
    }
    message = {
        "schema_version": 1, **binding.scope(), "request_key": "broker-delegate",
        "operation": "delegate-request", "body": body,
    }
    first = server._request(broker, message)
    replay = server._request(broker, message)
    assert first["ok"] is True and first["replayed"] is False
    assert first["result"]["status"] == "registered_allocation"
    assert replay["ok"] is True and replay["replayed"] is True
    assert replay["result"] == first["result"]


class _NoopStore:
    """Keeps the native-process protocol test independent of ControlStore setup."""

    def transaction(self):
        return nullcontext(None)

    def fenced_operation(self, _token):
        return nullcontext(None)


def test_real_direct_child_consumes_supervisor_bootstrap(tmp_path):
    socket_root = Path(tempfile.mkdtemp(prefix="ffs-broker-", dir="/tmp")).resolve()
    endpoint = socket_root / "worker.sock"
    identity_path, token_path, result_path = (tmp_path / name for name in ("identity", "token", "result"))
    child_code = """
import json, os, sys, time
from pathlib import Path
from run_state.worker_channel import broker_register
identity_path, token_path, result_path, endpoint, scope = sys.argv[1:]
while not Path(token_path).exists(): time.sleep(.01)
handoff = json.loads(Path(token_path).read_text())
scope = handoff['scope']
try:
    result = {'ok': broker_register(endpoint, scope, bootstrap_token=handoff['bootstrap_token']) == scope}
except Exception as error:
    result = {'error': type(error).__name__, 'code': getattr(error, 'code', None)}
Path(result_path).write_text(json.dumps(result))
"""
    parent_code = """
import os, subprocess, sys, time
from pathlib import Path
identity_path, token_path, result_path, endpoint, scope, child_code = sys.argv[1:]
child = subprocess.Popen([sys.executable, '-c', child_code, identity_path, token_path, result_path, endpoint, scope])
Path(identity_path).write_text(str(child.pid))
child.wait(timeout=10)
"""
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "lib")}
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code, str(identity_path), str(token_path), str(result_path),
         str(endpoint), "pending", child_code], env=environment,
    )
    server = WorkerChannelServer(_NoopStore(), object(), endpoint)
    try:
        deadline = time.monotonic() + 5
        while not identity_path.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert identity_path.exists()
        parent_identity = ProcessIdentity.from_pid(parent.pid)
        broker_identity = ProcessIdentity.from_pid(int(identity_path.read_text()))
        binding = WorkerBinding(
            "repository", "run", "activity", "intent", 1, parent_identity, str(tmp_path), "runtime",
            "a" * 64, "b" * 64, ("worker",), (str(tmp_path),), ProcessIdentity.current(),
        )
        server._verify_binding = lambda _tx, _binding: None
        server._bindings[parent.pid] = binding
        server._primary_bindings[binding.intent_id] = binding
        server.start()
        scope = binding.scope()
        token = server.issue_broker_bootstrap(binding.intent_id, broker_identity)
        # The test file models the Supervisor's direct secret handoff. No
        # worker process was given a launch API or the token before approval.
        token_path.write_text(json.dumps({"bootstrap_token": token, "scope": scope}))
        parent.wait(timeout=10)
        result = json.loads(result_path.read_text())
        assert result["ok"] is True, result
    finally:
        if parent.poll() is None:
            parent.terminate()
            parent.wait(timeout=5)
        server.close()
        socket_root.rmdir()
