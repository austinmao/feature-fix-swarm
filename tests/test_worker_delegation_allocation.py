"""Real authenticated delegation stays pending until owner allocation."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import pytest

from process_identity import ProcessIdentity
from run_state.cli import _cmd_fixture_start
from run_state.ownership import StartRequest, release_owner, reserve_resources
from run_state.supervisor import DispatchRequest, Supervisor, SupervisorRefused
from run_state.worker_channel import WorkerChannelServer
from run_state.workspace import WorkspaceRefused, begin_child_workspace_preparation, parse_input_selection, prepare_workspace, snapshot_inputs
from test_managed_preparation import _args
from test_m4_upstream_context_acceptance import _repository
from test_m4_workspace_acceptance import _copy, _selection
from test_registered_child_execution import git
from test_run_context_acceptance import INHERITED_CONTEXT_KEYS


def _wait(path, handle):
    deadline = time.monotonic() + 8
    while not path.exists():
        assert handle.process.poll() is None, handle.stderr_path.read_text()
        assert time.monotonic() < deadline, "worker IPC synchronization timed out"
        time.sleep(.01)


def test_real_worker_delegation_allocates_once_without_launch_or_spending(tmp_path, monkeypatch):
    primary = _repository(tmp_path)
    monkeypatch.chdir(primary)
    for key in INHERITED_CONTEXT_KEYS:
        monkeypatch.delenv(key, raising=False)
    base = git(primary, "rev-parse", "HEAD")

    def execute(store, token, context):
        store.configure_run_limits(token, dispatch_limit=3, token_limit=100, worker_capacity=2)
        store.bind_runtime(token, context.activity_id, "b" * 64)
        selected = (primary / "src" / "input.txt").read_bytes()
        snapshot = snapshot_inputs(
            primary, parse_input_selection(_selection(
                primary, token.repository_id, entries=[_copy("src/input.txt", selected)],
            )), tmp_path / "delegate-capture",
        )
        pending = begin_child_workspace_preparation(
            store, token, parent_activity_id=context.activity_id,
            request_key="ipc-workspace", role="worker", base_commit=base,
            selected_input_manifest=snapshot.manifest, repository_path=primary,
        )
        ready = prepare_workspace(store, token, pending, input_snapshot=snapshot)
        child = store.create_child_activity(
            token, parent_activity_id=context.activity_id, role="worker", request_key="ipc-child",
            candidate_hash=ready.input_digest, contract_hash="d" * 64, runtime_identity="b" * 64,
            workspace_binding=str(ready.path), workspace_preparation_id=ready.id,
        )
        command = (sys.executable, "-c", f"""
import json,os,time
from pathlib import Path
from run_state.worker_channel import request
scope=json.loads(os.environ['FFS_WORKER_SCOPE'])
body={{'parent_activity_id':scope['activity_id'],'role':'worker',
      'candidate_hash':{ready.input_digest!r},'contract_hash':{'d' * 64!r},
      'runtime_identity':{'b' * 64!r}}}
assert 'workspace' not in body
Path('ipc-ready').touch()
end=time.monotonic()+10
while not Path('ipc-go').exists():
    if time.monotonic()>end: raise RuntimeError('parent gate deadline')
    time.sleep(.01)
# Let the parent complete its release fence; this is the non-contention case.
time.sleep(.25)
results=[request(os.environ['FFS_WORKER_ENDPOINT'],scope,request_key='delegate-1',
 operation='delegate-request',body=body) for _ in range(2)]
results.append(request(os.environ['FFS_WORKER_ENDPOINT'],scope,request_key='delegate-1',
 operation='delegate-request',body={{**body,'role':'reviewer'}}))
Path('ipc-results.json').write_text(json.dumps(results))
end=time.monotonic()+10
while not Path('ipc-release').exists():
    if time.monotonic()>end: raise RuntimeError('parent release deadline')
    time.sleep(.01)
""")
        with tempfile.TemporaryDirectory(prefix="ffs-delegate-", dir="/tmp") as directory:
            server = WorkerChannelServer(store, token, Path(directory).resolve() / "worker.sock").start()
            supervisor = Supervisor(store, token, evidence_root=tmp_path / "authority/evidence", worker_channel=server)
            handle = None
            try:
                handle = supervisor.launch(DispatchRequest(
                    child.id, "ipc-launch", command, str(ready.path), base, "b" * 64,
                    contract_hash="d" * 64,
                ))
                _wait(ready.path / "ipc-ready", handle)
                def authority():
                    with store.read_transaction() as tx:
                        return {
                            table: [dict(row) for row in tx.execute(f"SELECT * FROM {table}")]
                            for table in ("authority_activities", "authority_child_bindings",
                                          "authority_run_limits", "authority_launch_accounting",
                                          "authority_budget_debits", "context_workspaces")
                        }
                before = authority()
                assert before["authority_run_limits"][0]["dispatch_used"] == 1
                (ready.path / "ipc-go").touch()
                _wait(ready.path / "ipc-results.json", handle)
                first, replay, conflict = json.loads((ready.path / "ipc-results.json").read_text())
                assert first["ok"] and replay["ok"]
                assert first["replayed"] is False and replay["replayed"] is True
                allocation = replay["result"]
                assert allocation["status"] == "registered_allocation"
                assert Path(allocation["workspace"]).is_dir()
                assert conflict == {"ok": False, "code": "IDEMPOTENCY_CONFLICT"}
                after = authority()
                assert after["authority_run_limits"][0]["dispatch_used"] == 1
                assert after["authority_run_limits"][0]["token_committed"] == 0
                assert len(after["authority_activities"]) == len(before["authority_activities"]) + 1
                assert len(after["context_workspaces"]) == len(before["context_workspaces"]) + 1
                with store.read_transaction() as tx:
                    events = tx.execute(
                        "SELECT * FROM authority_event_keys WHERE activity_id=? AND idempotency_key=?",
                        (child.id, "worker-request:" + handle.intent_id + ":delegate-1"),
                    ).fetchall()
                    assert len(events) == 1
                    registration = tx.execute(
                        "SELECT e.payload FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id "
                        "WHERE k.activity_id=? AND k.idempotency_key=?",
                        (child.id, "worker-registration:" + handle.intent_id),
                    ).fetchone()
                registered = json.loads(registration["payload"])["data"]
                interrupted = store.record_event_once(
                    token, child.id, "worker-request:" + handle.intent_id + ":interrupted",
                    {"operation": "delegate-request", "intent_id": handle.intent_id,
                     "peer_identity": registered["identity"], "workspace": registered["workspace"],
                     "runtime_identity": registered["runtime_identity"],
                     "body": {"parent_activity_id": child.id, "role": "worker",
                              "candidate_hash": ready.input_digest, "contract_hash": "d" * 64,
                              "runtime_identity": "b" * 64}},
                )
                original_create = store.create_child_activity
                def interrupted_create(*args, **kwargs):
                    raise RuntimeError("simulated crash after workspace ready")
                store.create_child_activity = interrupted_create
                with pytest.raises(RuntimeError, match="simulated crash"):
                    supervisor.consume_delegate_request(interrupted["id"])
                store.create_child_activity = original_create
                after_interrupt = authority()
                assert len(after_interrupt["context_workspaces"]) == len(after["context_workspaces"]) + 1
                assert len(after_interrupt["authority_activities"]) == len(after["authority_activities"])
                with store.read_transaction() as tx:
                    preparing = tx.execute(
                        "SELECT preparation_id FROM context_workspaces WHERE repository_id=? AND run_id=? "
                        "AND child_request_key=?", (token.repository_id, token.run_id,
                                                     "delegate-allocation:" + str(interrupted["id"])),
                    ).fetchall()
                    assert len(preparing) == 1
                recovered = supervisor.consume_delegate_request(interrupted["id"])
                assert recovered["workspace_preparation_id"] == preparing[0]["preparation_id"]
                recovered_record = next(
                    row for row in git(primary, "worktree", "list", "--porcelain").split("\n\n")
                    if row.startswith(f"worktree {recovered['workspace']}\n")
                )
                assert not any(line.startswith("locked") for line in recovered_record.splitlines())
                assert supervisor.consume_delegate_request(interrupted["id"]) == recovered
                after_recovery = authority()
                assert len(after_recovery["context_workspaces"]) == len(after_interrupt["context_workspaces"])
                assert len(after_recovery["authority_activities"]) == len(after_interrupt["authority_activities"]) + 1
                # The retained event body and its authority payload hash are
                # one authentication unit; an in-place receipt tamper cannot
                # replay a real live worker request.
                with store.transaction() as tx:
                    request_key = "worker-request:" + handle.intent_id + ":delegate-1"
                    original_hash = tx.execute(
                        "SELECT payload_hash FROM authority_event_keys WHERE activity_id=? AND idempotency_key=?",
                        (child.id, request_key),
                    ).fetchone()["payload_hash"]
                    tx.execute(
                        "UPDATE authority_event_keys SET payload_hash=? WHERE activity_id=? AND idempotency_key=?",
                        ("0" * 64, child.id, request_key),
                    )
                with pytest.raises(SupervisorRefused, match="IPC_SCOPE_MISMATCH"):
                    supervisor.consume_delegate_request(allocation["event_id"])
                with store.transaction() as tx:
                    tx.execute(
                        "UPDATE authority_event_keys SET payload_hash=? WHERE activity_id=? AND idempotency_key=?",
                        (original_hash, child.id, request_key),
                    )
                # The retained capture is validated on every allocation replay,
                # including after the registered receipt has been committed.
                capture = snapshot.staging / "files" / "src" / "input.txt"
                captured_bytes = capture.read_bytes()
                capture.write_bytes(b"corrupt retained capture\n")
                with pytest.raises(WorkspaceRefused, match="SNAPSHOT_INCOMPLETE"):
                    supervisor.consume_delegate_request(allocation["event_id"])
                capture.write_bytes(captured_bytes)
                # A replacement that looks like a valid Git checkout is not
                # the native child worktree bound into the allocation receipt.
                allocated = Path(allocation["workspace"])
                retained = tmp_path / "retained-delegated-worktree"
                allocated.rename(retained)
                subprocess.run(
                    ["git", "clone", "--no-hardlinks", "-q", str(primary), str(allocated)],
                    check=True,
                )
                with pytest.raises(SupervisorRefused, match="REQUEST_BINDING_UNKNOWN"):
                    supervisor.consume_delegate_request(allocation["event_id"])
                (ready.path / "ipc-release").touch()
                result = supervisor.finish(handle, timeout=10, token_usage=0)
                assert result["returncode"] == 0, handle.stderr_path.read_text()
                # The durable receipt does not outlive its issuing worker:
                # current terminal state is revalidated before a replay result.
                with pytest.raises(SupervisorRefused, match="IPC_SCOPE_MISMATCH"):
                    supervisor.consume_delegate_request(allocation["event_id"])
                # A real owner handoff advances the fence generation.  The
                # completed worker's retained event remains non-transferable.
                with store.transaction() as tx:
                    release_owner(tx, token)
                successor = reserve_resources(store, StartRequest(
                    token.run_id, token.workspace, token.objective_digest, ProcessIdentity.current(),
                    repository_id=token.repository_id, planning_scope=token.planning_scope,
                )).token
                assert successor.generation > token.generation
                with pytest.raises(SupervisorRefused, match="IPC_SCOPE_MISMATCH"):
                    Supervisor(store, successor, evidence_root=tmp_path / "authority/evidence").consume_delegate_request(
                        allocation["event_id"],
                    )
                with store.transaction() as tx:
                    release_owner(tx, successor)
                return 0
            finally:
                (ready.path / "ipc-release").touch()
                if handle is not None and handle.process.poll() is None:
                    handle.process.terminate()
                    handle.process.wait(timeout=5)
                server.close()

    assert _cmd_fixture_start(_args(tmp_path / "authority", activity="execute"), on_ready=execute) == 0
