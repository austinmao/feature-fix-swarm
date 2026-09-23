"""Real worker subprocess/Unix-socket requests, independently of host agents."""
from dataclasses import replace
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time

import pytest

from test_supervised_process import setup_owner
from run_state.ownership import OwnershipRefused, release_owner
from run_state.worker_channel import WorkerChannelServer, WorkerChannelRefused, request


WORKER = """
import json,sys,time
from pathlib import Path
from run_state.worker_channel import request
root=Path(sys.argv[1]); index=0; deadline=time.monotonic()+25
while not (root/'stop').exists() and time.monotonic()<deadline:
    incoming=root/f'request-{index}.json'
    if not incoming.exists():
        time.sleep(.01); continue
    payload=json.loads(incoming.read_text())
    try:
        result=request(payload['endpoint'],payload['scope'],request_key=payload['key'],
                       operation=payload['operation'],body=payload['body'])
    except Exception as error:
        result={'transport_error':type(error).__name__}
    published=root/f'result-{index}.json'
    temporary=published.with_suffix('.tmp')
    temporary.write_text(json.dumps(result))
    temporary.replace(published)
    index+=1
"""


@pytest.fixture
def live_channel(tmp_path):
    supervisor, store, dispatch = setup_owner(tmp_path)
    exchange_root = Path(dispatch.workspace) / "requests"
    exchange_root.mkdir()
    # macOS sockaddr_un has a short path limit; use a private short path.
    socket_root = Path(tempfile.mkdtemp(prefix="ffs-ipc-")).resolve()
    server = WorkerChannelServer(store, supervisor.token, socket_root / "worker.sock")
    handle = supervisor.launch(replace(dispatch, command=(sys.executable, "-c", WORKER, str(exchange_root))))
    scope = server.register_worker(handle.intent_id, contract_hash=dispatch.contract_hash)
    server.start()
    fixture = {"supervisor": supervisor, "store": store, "dispatch": dispatch,
               "handle": handle, "server": server, "root": exchange_root,
               "scope": scope, "index": 0, "socket_root": socket_root}
    try:
        yield fixture
    finally:
        (exchange_root / "stop").write_text("stop")
        try:
            handle.process.wait(timeout=5)
        except Exception:
            handle.process.terminate()
            handle.process.wait(timeout=5)
        try:
            supervisor.finish(handle, timeout=5, token_usage=0)
        except OwnershipRefused:
            pass  # revocation tests intentionally retain a stale capability
        fixture["server"].close()
        shutil.rmtree(socket_root)


def exchange(fixture, *, key="one", operation="progress", body=None, scope=None):
    index = fixture["index"]
    fixture["index"] += 1
    payload = {"endpoint": str(fixture["server"].endpoint), "scope": scope or fixture["scope"],
               "key": key, "operation": operation,
               "body": {"sequence": 1, "message": "real worker online"} if body is None else body}
    incoming = fixture["root"] / f"request-{index}.json"
    temporary = incoming.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload))
    temporary.replace(incoming)
    result = fixture["root"] / f"result-{index}.json"
    deadline = time.monotonic() + 7
    while not result.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert result.exists(), fixture["handle"].stderr_path.read_text()
    return json.loads(result.read_text())


def test_kernel_authenticated_progress_and_durable_replay(live_channel):
    first = exchange(live_channel)
    assert first["ok"] is True and first["replayed"] is False
    server = live_channel["server"]
    server.close()
    reopened = type(live_channel["store"])(live_channel["store"].db_path)
    replacement = WorkerChannelServer(reopened, live_channel["supervisor"].token, server.endpoint)
    replacement.register_worker(live_channel["handle"].intent_id, contract_hash=live_channel["dispatch"].contract_hash)
    live_channel["server"] = replacement.start()
    replay = exchange(live_channel)
    assert replay["ok"] is True and replay["replayed"] is True
    assert replay["event_id"] == first["event_id"]
    assert replay["body_sha256"] == first["body_sha256"]
    changed = exchange(live_channel, body={"sequence": 1, "message": "different bytes"})
    assert changed == {"ok": False, "code": "IDEMPOTENCY_CONFLICT"}


def test_unregistered_process_cannot_impersonate_worker(live_channel):
    with live_channel["store"].read_transaction() as tx:
        before = tx.execute("SELECT count(*) FROM control_events").fetchone()[0]
    try:
        result = request(live_channel["server"].endpoint, live_channel["scope"],
                         request_key="forged", operation="progress", body={"sequence": 1, "message": "forged"})
        assert result == {"ok": False, "code": "IPC_PEER_MISMATCH"}
    except (OSError, WorkerChannelRefused):
        pass  # rejection may close the socket before a forged sender reads
    with live_channel["store"].read_transaction() as tx:
        assert tx.execute("SELECT count(*) FROM control_events").fetchone()[0] == before


def test_worker_verifies_supervisor_identity_before_sending_request(live_channel):
    scope = dict(live_channel["scope"])
    scope["supervisor_identity"] = {**scope["supervisor_identity"], "start_token": "wrong-process"}
    with live_channel["store"].read_transaction() as tx:
        before = tx.execute("SELECT count(*) FROM control_events").fetchone()[0]
    assert exchange(live_channel, scope=scope) == {"transport_error": "WorkerChannelRefused"}
    with live_channel["store"].read_transaction() as tx:
        assert tx.execute("SELECT count(*) FROM control_events").fetchone()[0] == before


@pytest.mark.parametrize("field", ["repository_id", "run_id", "activity_id", "intent_id", "generation"])
def test_worker_cannot_request_sibling_scope(live_channel, field):
    scope = dict(live_channel["scope"])
    scope[field] = scope[field] + 1 if field == "generation" else "different"
    assert exchange(live_channel, scope=scope) == {"ok": False, "code": "IPC_SCOPE_MISMATCH"}


def test_evidence_is_a_request_and_cannot_claim_acceptance_or_escape(live_channel):
    accepted = exchange(live_channel, operation="evidence-request", body={"path": "result.txt", "sha256": "e" * 64})
    assert accepted["ok"] and accepted["result"] == {"status": "requested"}
    escaped = exchange(live_channel, key="escape", operation="evidence-request",
                       body={"path": "../control.sqlite3", "sha256": "e" * 64})
    assert escaped == {"ok": False, "code": "IPC_SCOPE_MISMATCH"}
    forbidden = exchange(live_channel, key="reset", operation="reset", body={})
    assert forbidden == {"ok": False, "code": "IPC_OPERATION_FORBIDDEN"}


def test_delegation_creates_one_pending_bound_child_and_never_spawns(live_channel):
    """Historical node ID: delegation now records only pending allocator work."""
    store = live_channel["store"]
    with store.read_transaction() as tx:
        binding = tx.execute("SELECT * FROM authority_child_bindings WHERE activity_id=?",
                             (live_channel["dispatch"].activity_id,)).fetchone()
    body = {"parent_activity_id": live_channel["scope"]["activity_id"], "role": "worker",
            "candidate_hash": binding["candidate_hash"], "contract_hash": binding["contract_hash"],
            "runtime_identity": binding["runtime_identity"]}
    def authority():
        with store.read_transaction() as tx:
            return {table: [dict(row) for row in tx.execute(f"SELECT * FROM {table}")]
                    for table in ("authority_activities", "authority_child_bindings", "context_workspaces",
                                  "authority_launch_intents", "authority_run_limits",
                                  "authority_launch_accounting", "authority_budget_debits")}
    before = authority()
    first = exchange(live_channel, operation="delegate-request", body=body)
    assert first["ok"] is True and first["replayed"] is False
    assert first["result"] == {"status": "pending_allocation"}
    repeated = exchange(live_channel, operation="delegate-request", body=body)
    assert repeated["ok"] is True and repeated["replayed"] is True
    assert repeated["result"] == first["result"]
    assert repeated["event_id"] == first["event_id"]
    assert repeated["body_sha256"] == first["body_sha256"]
    assert authority() == before
    assert len(before["authority_launch_intents"]) == 1
    assert before["authority_run_limits"][0]["dispatch_used"] == 1
    conflict = exchange(live_channel, operation="delegate-request", body={**body, "role": "reviewer"})
    assert conflict == {"ok": False, "code": "IDEMPOTENCY_CONFLICT"}
    old_workspace = exchange(live_channel, key="old-workspace-field", operation="delegate-request",
                             body={**body, "workspace": live_channel["dispatch"].workspace})
    assert old_workspace == {"ok": False, "code": "IPC_SCOPE_MISMATCH"}
    changed = exchange(live_channel, key="bad-runtime", operation="delegate-request",
                       body={**body, "runtime_identity": "0" * 64})
    assert changed == {"ok": False, "code": "IPC_SCOPE_MISMATCH"}
    assert authority() == before


def test_revoked_owner_cannot_accept_progress_even_from_registered_peer(live_channel):
    assert exchange(live_channel)["ok"] is True
    with live_channel["store"].transaction() as tx:
        release_owner(tx, live_channel["supervisor"].token)
    assert exchange(live_channel) == {"ok": False, "code": "FENCE_REVOKED"}


def test_unsafe_socket_parent_refuses_without_creating_endpoint(tmp_path):
    supervisor, store, _ = setup_owner(tmp_path)
    unsafe = tmp_path / "public"
    unsafe.mkdir(mode=0o755)
    with pytest.raises(WorkerChannelRefused, match="UNSAFE_IPC_ENDPOINT"):
        WorkerChannelServer(store, supervisor.token, unsafe / "worker.sock")
    assert not (unsafe / "worker.sock").exists()
