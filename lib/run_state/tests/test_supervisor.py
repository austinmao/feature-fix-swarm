"""Real supervisor ordering observations; synthetic fixture epochs are retired.

See E/resume-20260915/supervisor-legacy-port/mapping.md. These local-command
checks do not qualify a host or prove successor recovery/reattachment.
"""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sys
import sqlite3
import tempfile
import threading

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "lib"))
from test_supervised_process import setup_owner  # noqa: E402 - repository test helpers require the paths above
from test_worker_channel_process import exchange, live_channel as live_channel  # noqa: E402 - repository test helpers require the paths above
from run_state.ownership import ControlStore, OwnershipRefused, release_owner  # noqa: E402 - repository test helpers require the paths above
from run_state.supervisor import Supervisor, SupervisorRefused  # noqa: E402 - repository test helpers require the paths above
from run_state.worker_channel import WorkerChannelServer  # noqa: E402 - repository test helpers require the paths above


def _rows(store):
    with store.read_transaction() as tx:
        return {table: [dict(row) for row in tx.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("authority_launch_intents", "authority_launch_accounting",
                              "authority_run_limits", "authority_budget_debits",
                              "authority_event_keys", "control_events")}


def _material(store, request):
    with store.read_transaction() as tx:
        event = tx.execute(
            "SELECT e.payload FROM control_events e JOIN authority_event_keys k ON e.id=k.event_id "
            "WHERE k.activity_id=? AND k.idempotency_key=?",
            (request.activity_id, "dispatch-request:" + request.request_key),
        ).fetchone()
    data = json.loads(event["payload"])["data"]
    assert data["request"] == {
        "command_sha256": hashlib.sha256(json.dumps(request.command, sort_keys=True,
                                                     separators=(",", ":")).encode()).hexdigest(),
        "workspace": request.workspace, "expected_head": request.expected_head,
        "runtime_identity": request.runtime_identity, "contract_hash": request.contract_hash,
    }
    return data


def _launch_thread(supervisor, request):
    handles, errors = [], []
    def launch():
        try:
            handles.append(supervisor.launch(request))
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=launch)
    thread.start()
    return thread, handles, errors


def _cleanup(handles):
    for handle in handles:
        if handle.process.poll() is None:
            handle.process.terminate()
            handle.process.wait(timeout=5)


def test_launch_request_persists_runtime_material_before_spawn(tmp_path):
    reached, release = threading.Event(), threading.Event()
    def fault(point):
        if point == "after_intent_commit":
            reached.set()
            assert release.wait(15)
    supervisor, store, request = setup_owner(tmp_path, fault=fault)
    sentinel = Path(request.workspace) / "ran"
    # Observe runtime refusal at the actual dispatch boundary, before any debit.
    before = _rows(store)
    with pytest.raises(SupervisorRefused):
        supervisor.launch(replace(request, runtime_identity="0" * 64))
    assert _rows(store) == before
    thread, handles, errors = _launch_thread(supervisor, request)
    try:
        assert reached.wait(15)
        assert not handles and not sentinel.exists()
        observed = _rows(ControlStore(store.db_path))
        assert len(observed["authority_launch_intents"]) == 1
        intent = observed["authority_launch_intents"][0]
        assert intent["state"] == "reserved" and intent["child_pid"] is None
        assert observed["authority_run_limits"][0]["dispatch_used"] == 1
        assert len(observed["authority_launch_accounting"]) == 1
        assert _material(store, request)["intent_id"] == intent["id"]
        assert not supervisor._handles
        release.set()
        thread.join(15)
        assert not thread.is_alive() and not errors and len(handles) == 1
        result = supervisor.finish(handles[0], timeout=10, token_usage=0)
        assert result["returncode"] == 0 and sentinel.read_text() == "yes"
        completed = _rows(store)
        with pytest.raises(OwnershipRefused, match="IDEMPOTENCY_CONFLICT"):
            supervisor.launch(replace(request, command=request.command + ("changed",)))
        assert _rows(store) == completed
        assert sentinel.read_text() == "yes"
    finally:
        release.set()
        thread.join(15)
        _cleanup(handles)


def test_supervisor_fenced_child_tracer(tmp_path):
    """An actual child cannot execute while authorization is uncommitted."""
    reached, release = threading.Event(), threading.Event()
    supervisor, store, request = setup_owner(tmp_path)
    def fault(point):
        if point == "authorize_child.after_write_before_commit":
            reached.set()
            assert release.wait(15)
    store.fault_probe = fault
    sentinel = Path(request.workspace) / "ran"
    thread, handles, errors = _launch_thread(supervisor, request)
    try:
        assert reached.wait(15)
        assert not sentinel.exists()
        # ControlStore intentionally refuses concurrent operations while its write
        # transaction is paused. A read-only SQLite observer of this disposable
        # fixture sees committed bytes without acquiring mutation authority.
        observer = sqlite3.connect(store.db_path.as_uri() + "?mode=ro", uri=True)
        observer.row_factory = sqlite3.Row
        try:
            observer.execute("BEGIN")
            intent = observer.execute("SELECT * FROM authority_launch_intents").fetchone()
            assert intent["state"] == "acknowledged"
            assert intent["child_pid"] and intent["permit_id"] is None
            assert observer.execute("SELECT dispatch_used FROM authority_run_limits").fetchone()[0] == 1
        finally:
            observer.close()
        assert not sentinel.exists()
        release.set()
        thread.join(15)
        assert not thread.is_alive() and not errors and len(handles) == 1
        handle = handles[0]
        result = supervisor.finish(handle, timeout=10, token_usage=0)
        assert result["returncode"] == 0 and sentinel.read_text() == "yes"
        durable = _rows(store)["authority_launch_intents"][0]
        identity = asdict(handle.identity)
        for field in ("host_id", "boot_id", "pid", "start_token"):
            assert durable["child_" + field] == identity[field]
        assert durable["permit_id"] and durable["state"] == "completed_succeeded"
        assert _material(store, request)["intent_id"] == handle.intent_id
        assert _rows(store)["authority_run_limits"][0]["dispatch_used"] == 1
    finally:
        release.set()
        thread.join(15)
        _cleanup(handles)


def test_request_effect_serializes_before_revocation(live_channel, monkeypatch):
    reached, release, revoke_started, revoked = (threading.Event() for _ in range(4))
    store = live_channel["store"]
    original = store.record_event_once
    def record(token, activity_id, key, payload):
        if key.endswith(":ordered"):
            reached.set()
            assert release.wait(15)
        return original(token, activity_id, key, payload)
    monkeypatch.setattr(store, "record_event_once", record)
    responses, errors = [], []
    def make_request():
        try:
            responses.append(exchange(live_channel, key="ordered"))
        except BaseException as error:
            errors.append(error)
    def revoke():
        try:
            revoke_started.set()
            with store.transaction() as tx:
                release_owner(tx, live_channel["supervisor"].token)
            revoked.set()
        except BaseException as error:
            errors.append(error)
    request_thread = threading.Thread(target=make_request)
    revoke_thread = threading.Thread(target=revoke)
    request_thread.start()
    try:
        assert reached.wait(15)
        revoke_thread.start()
        assert revoke_started.wait(5)
        assert not revoked.wait(.1)  # serialization observation, never reclaim authority
        release.set()
        request_thread.join(15)
        revoke_thread.join(15)
        assert not request_thread.is_alive() and not revoke_thread.is_alive()
        assert not errors and revoked.is_set() and responses[0]["ok"]
        event_id = responses[0]["event_id"]
        committed = _rows(store)
        event_key = "worker-request:" + live_channel["handle"].intent_id + ":ordered"
        keys = [row for row in committed["authority_event_keys"] if row["idempotency_key"] == event_key]
        assert len(keys) == 1 and keys[0]["event_id"] == event_id
        data = json.loads(next(row["payload"] for row in committed["control_events"]
                               if row["id"] == event_id))["data"]
        assert data["operation"] == "progress" and data["body"]["sequence"] == 1
        assert exchange(live_channel, key="ordered") == {"ok": False, "code": "FENCE_REVOKED"}
        assert exchange(live_channel, key="later") == {"ok": False, "code": "FENCE_REVOKED"}
        assert _rows(store) == committed
    finally:
        release.set()
        request_thread.join(15)
        if revoke_thread.ident is not None:
            revoke_thread.join(15)


def test_supervisor_refuses_server_from_another_registered_store(tmp_path):
    (tmp_path / "first").mkdir()
    (tmp_path / "second").mkdir()
    supervisor, store, _ = setup_owner(tmp_path / "first")
    other, other_store, _ = setup_owner(tmp_path / "second")
    before, other_before = _rows(store), _rows(other_store)
    with tempfile.TemporaryDirectory(prefix="ffs-scope-") as directory:
        server = WorkerChannelServer(other_store, other.token, Path(directory).resolve() / "worker.sock")
        endpoint_before = server.endpoint.stat()
        try:
            with pytest.raises(SupervisorRefused, match="IPC_SCOPE_MISMATCH"):
                Supervisor(store, supervisor.token, evidence_root=supervisor.evidence_root, worker_channel=server)
            endpoint_after = server.endpoint.stat()
            assert (endpoint_after.st_dev, endpoint_after.st_ino) == (endpoint_before.st_dev, endpoint_before.st_ino)
        finally:
            server.close()
    assert _rows(store) == before and _rows(other_store) == other_before
