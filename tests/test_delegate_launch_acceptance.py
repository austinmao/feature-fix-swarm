"""Independent generic delegate-launch acceptance using real worker IPC.

All repositories/commits are disposable fixtures. No native admission is claimed.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import pytest

from process_identity import UNKNOWN
from run_state.cli import _cmd_fixture_start
from run_state.ownership import OwnershipRefused, release_owner
from run_state.state import ControlStoreRefused
from run_state.supervisor import DispatchRequest, Supervisor, SupervisorRefused
from run_state.worker_channel import WorkerChannelServer
from run_state.workspace import (
    WorkspaceRefused, begin_child_workspace_preparation, parse_input_selection, prepare_workspace, snapshot_inputs,
)
from test_managed_preparation import _args
from test_m4_upstream_context_acceptance import _repository
from test_m4_workspace_acceptance import _copy, _selection
from test_registered_child_execution import git
from test_run_context_acceptance import INHERITED_CONTEXT_KEYS

REFUSALS = (SupervisorRefused, OwnershipRefused, ControlStoreRefused, WorkspaceRefused)
RUNTIME = "b" * 64
CONTRACT = "d" * 64


def _exercise(tmp_path, monkeypatch, case, *, dispatch_limit=4):
    primary = _repository(tmp_path)
    monkeypatch.chdir(primary)
    for key in INHERITED_CONTEXT_KEYS:
        monkeypatch.delenv(key, raising=False)
    base = git(primary, "rev-parse", "HEAD")

    def execute(store, token, context):
        store.configure_run_limits(token, dispatch_limit=dispatch_limit, token_limit=100, worker_capacity=4)
        store.bind_runtime(token, context.activity_id, RUNTIME)
        selected = (primary / "src/input.txt").read_bytes()
        snapshot = snapshot_inputs(primary, parse_input_selection(_selection(
            primary, token.repository_id, entries=[_copy("src/input.txt", selected)],
        )), tmp_path / "capture")
        pending = begin_child_workspace_preparation(
            store, token, parent_activity_id=context.activity_id, request_key="origin-workspace",
            role="worker", base_commit=base, selected_input_manifest=snapshot.manifest,
            repository_path=primary,
        )
        ready = prepare_workspace(store, token, pending, input_snapshot=snapshot)
        origin = store.create_child_activity(
            token, parent_activity_id=context.activity_id, role="worker", request_key="origin",
            candidate_hash=ready.input_digest, contract_hash=CONTRACT, runtime_identity=RUNTIME,
            workspace_binding=str(ready.path), workspace_preparation_id=ready.id,
        )
        body = dict(parent_activity_id=origin.id, role="worker", candidate_hash=ready.input_digest,
                    contract_hash=CONTRACT, runtime_identity=RUNTIME)
        origin_command = (sys.executable, "-c", f"""
import json,os,time
from pathlib import Path
from run_state.worker_channel import request
scope=json.loads(os.environ['FFS_WORKER_SCOPE'])
end=time.monotonic()+90
Path('origin-ready').touch()
while not Path('origin-go').exists():
    if time.monotonic()>end: raise RuntimeError('origin gate expired')
    time.sleep(.01)
response=request(os.environ['FFS_WORKER_ENDPOINT'],scope,request_key='delegate',
 operation='delegate-request',body={body!r},timeout=30)
allocation_pending=Path('allocation.json.pending')
allocation_pending.write_text(json.dumps(response))
allocation_pending.replace('allocation.json')
while not Path('origin-release').exists():
    if time.monotonic()>end: raise RuntimeError('origin release expired')
    time.sleep(.01)
""")
        with tempfile.TemporaryDirectory(prefix="dl9-", dir="/tmp") as endpoint:
            server = WorkerChannelServer(store, token, Path(endpoint).resolve() / "ipc").start()
            supervisor = Supervisor(store, token, evidence_root=tmp_path / "authority/evidence", worker_channel=server)
            origin_handle = None
            handles = []
            try:
                origin_handle = supervisor.launch(DispatchRequest(
                    origin.id, "origin-launch", origin_command, str(ready.path), base, RUNTIME,
                    contract_hash=CONTRACT,
                ))

                def wait(name):
                    deadline = time.monotonic() + 30
                    while not (ready.path / name).exists():
                        assert origin_handle.process.poll() is None, origin_handle.stderr_path.read_text()
                        assert time.monotonic() < deadline, name
                        time.sleep(.01)
                    return ready.path / name

                wait("origin-ready")
                (ready.path / "origin-go").touch()
                response = json.loads(wait("allocation.json").read_text())
                assert response["ok"], response
                allocation = response["result"]
                assert allocation["status"] == "registered_allocation"
                workspace = Path(allocation["workspace"])
                marker = workspace / "delegate-executed"
                assert not marker.exists()

                def rows(table):
                    with store.read_transaction() as tx:
                        return [dict(row) for row in tx.execute(f"SELECT * FROM {table}")]

                def accounting():
                    return {table: rows(table) for table in (
                        "authority_launch_intents", "authority_budget_debits",
                        "authority_launch_accounting", "authority_run_limits", "authority_activities",
                    )}

                command = (sys.executable, "-c",
                           "from pathlib import Path; Path('delegate-executed').write_text('real child'); print('delegate-output')")
                c = SimpleNamespace(store=store, token=token, context=context, origin=origin,
                                    origin_handle=origin_handle, supervisor=supervisor, allocation=allocation,
                                    event_id=allocation["event_id"], workspace=workspace, marker=marker,
                                    snapshot=snapshot, command=command, accounting=accounting, rows=rows,
                                    handles=handles, primary=primary)
                c.before = accounting()
                assert len(c.before["authority_launch_intents"]) == 1
                assert c.before["authority_run_limits"][0]["dispatch_used"] == 1
                case(c)
                return 0
            finally:
                (ready.path / "origin-release").touch()
                for handle in handles + ([origin_handle] if origin_handle else []):
                    if handle.process.poll() is None:
                        try:
                            handle.process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            handle.process.terminate()
                            handle.process.wait(timeout=5)
                server.close()

    assert _cmd_fixture_start(_args(tmp_path / "authority", activity="execute"), on_ready=execute) == 0


def _launch(c, *, supervisor=None, **changes):
    options = dict(command=c.command, runtime_identity=RUNTIME, token_reservation=13)
    options.update(changes)
    handle = (supervisor or c.supervisor).launch_delegate_request(c.event_id, **options)
    c.handles.append(handle)
    return handle


def test_authenticated_allocation_launches_real_child_once_and_replays_without_new_debit(tmp_path, monkeypatch):
    def case(c):
        handle = _launch(c)
        assert handle.process.wait(timeout=10) == 0, handle.stderr_path.read_text()
        assert c.marker.read_text() == "real child"
        assert "delegate-output" in handle.stdout_path.read_text()
        assert handle.activity_id == c.allocation["activity_id"]
        after = c.accounting()
        assert len(after["authority_launch_intents"]) == 2
        assert after["authority_run_limits"][0]["dispatch_used"] == 2
        assert after["authority_run_limits"][0]["token_committed"] == 13
        with c.store.read_transaction() as tx:
            events = tx.execute("SELECT * FROM authority_event_keys WHERE activity_id=? AND idempotency_key=?",
                                (handle.activity_id, "dispatch-request:delegate-launch:" + str(c.event_id))).fetchall()
            assert len(events) == 1
        restart = Supervisor(c.store, c.token, evidence_root=c.supervisor.evidence_root, worker_channel=None)
        for supervisor in [c.supervisor, restart]:
            with pytest.raises(REFUSALS) as replay:
                _launch(c, supervisor=supervisor)
            assert replay.value.code == "INTENT_RECONCILIATION_REQUIRED"
            assert c.accounting() == after
        result = c.supervisor.finish(handle, timeout=10, token_usage=13)
        assert result["returncode"] == 0
        completed = c.accounting()
        with pytest.raises(REFUSALS) as replay:
            _launch(c)
        assert replay.value.code == "REQUEST_ALREADY_COMPLETED"
        assert c.accounting() == completed
    _exercise(tmp_path, monkeypatch, case)


@pytest.mark.parametrize("selection", ["command", "runtime", "tokens"])
def test_changed_trusted_selection_cannot_replay_or_spend_again(tmp_path, monkeypatch, selection):
    def case(c):
        handle = _launch(c)
        assert handle.process.wait(timeout=10) == 0
        before = c.accounting()
        changes = {"command": c.command + ("different",)} if selection == "command" else (
            {"runtime_identity": "c" * 64} if selection == "runtime" else {"token_reservation": 14})
        with pytest.raises(REFUSALS) as conflict:
            _launch(c, **changes)
        assert conflict.value.code == ("RUNTIME_DRIFT" if selection == "runtime" else "IDEMPOTENCY_CONFLICT")
        assert c.accounting() == before
    _exercise(tmp_path, monkeypatch, case)


def test_exhausted_dispatch_budget_refuses_allocation_launch_without_new_debit(tmp_path, monkeypatch):
    def case(c):
        with pytest.raises(REFUSALS):
            _launch(c)
        assert c.accounting() == c.before
        assert not c.marker.exists()
    _exercise(tmp_path, monkeypatch, case, dispatch_limit=1)


@pytest.mark.parametrize("corruption", ["event", "capture", "parent_dead", "parent_unknown", "direct_wrong_key"])
def test_current_origin_and_retained_authority_are_required_before_reservation(tmp_path, monkeypatch, corruption):
    def case(c):
        if corruption == "event":
            with c.store.transaction() as tx:
                tx.execute("UPDATE authority_event_keys SET payload_hash=? WHERE event_id=?", ("0" * 64, c.event_id))
        elif corruption == "capture":
            (c.snapshot.staging / "files/src/input.txt").write_bytes(b"tampered retained capture")
        elif corruption == "parent_dead":
            c.origin_handle.process.kill()
            c.origin_handle.process.wait(timeout=5)
        elif corruption == "parent_unknown":
            from run_state import supervisor as implementation
            original = implementation.probe_identity
            monkeypatch.setattr(implementation, "probe_identity",
                                lambda identity: UNKNOWN if identity.pid == c.origin_handle.identity.pid else original(identity))
        with pytest.raises(REFUSALS):
            if corruption == "direct_wrong_key":
                c.supervisor.launch(DispatchRequest(
                    c.allocation["activity_id"], "bypass-delegate-event", c.command, str(c.workspace),
                    git(c.primary, "rev-parse", "HEAD"), RUNTIME, contract_hash=CONTRACT,
                ))
            else:
                _launch(c)
        assert c.accounting() == c.before
        assert not c.marker.exists()
    _exercise(tmp_path, monkeypatch, case)


@pytest.mark.parametrize("point", ["after_intent_commit", "after_spawn_before_ack", "after_ack_before_authorization", "after_authorization_before_release"])
@pytest.mark.parametrize("fault", ["parent_dead", "parent_unknown", "fence", "capture"])
def test_every_effect_boundary_refuses_lost_origin_without_execution_or_refund(tmp_path, monkeypatch, point, fault):
    def case(c):
        reached = False
        def barrier(actual):
            nonlocal reached
            if actual != point:
                return
            reached = True
            assert not c.marker.exists(), "command executed before permit release"
            if fault == "parent_dead":
                c.origin_handle.process.kill()
                c.origin_handle.process.wait(timeout=5)
            elif fault == "parent_unknown":
                from run_state import supervisor as implementation
                original = implementation.probe_identity
                monkeypatch.setattr(implementation, "probe_identity",
                                    lambda identity: UNKNOWN if identity.pid == c.origin_handle.identity.pid else original(identity))
            elif fault == "fence":
                with c.store.transaction() as tx:
                    release_owner(tx, c.token)
            else:
                (c.snapshot.staging / "files/src/input.txt").write_bytes(b"tampered after admission")
        c.supervisor.fault_probe = barrier
        with pytest.raises(REFUSALS):
            _launch(c)
        assert reached
        assert not c.marker.exists()
        after = c.accounting()
        assert len(after["authority_launch_intents"]) == 2
        assert after["authority_run_limits"][0]["dispatch_used"] == 2
        assert after["authority_run_limits"][0]["token_committed"] == 13
        child_before = next(r for r in c.before["authority_activities"] if r["id"] == c.allocation["activity_id"])
        child_after = next(r for r in after["authority_activities"] if r["id"] == c.allocation["activity_id"])
        assert child_after["remaining_retry_budget"] == child_before["remaining_retry_budget"] - 1
        reservations = [r for r in after["authority_launch_accounting"]
                        if r["intent_id"] != c.origin_handle.intent_id]
        assert len(reservations) == 1
        assert reservations[0]["token_reservation"] == 13
        assert reservations[0]["token_final"] is None
        assert after["authority_budget_debits"] == c.before["authority_budget_debits"]
    _exercise(tmp_path, monkeypatch, case)


def test_completed_delegate_with_selected_workspace_edits_replays_completion_without_spending(tmp_path, monkeypatch):
    def case(c):
        command = (sys.executable, "-c",
                   "from pathlib import Path; Path('src/input.txt').write_text('actual selected edit'); print('edited')")
        handle = _launch(c, command=command)
        assert handle.process.wait(timeout=10) == 0
        assert (c.workspace / "src/input.txt").read_text() == "actual selected edit"
        result = c.supervisor.finish(handle, timeout=10, token_usage=13)
        assert result["returncode"] == 0
        before = c.accounting()
        with pytest.raises(REFUSALS) as replay:
            _launch(c, command=command)
        assert replay.value.code == "REQUEST_ALREADY_COMPLETED"
        assert c.accounting() == before
    _exercise(tmp_path, monkeypatch, case)


def test_controller_restart_after_reserved_intent_cannot_spawn_or_debit_again(tmp_path, monkeypatch):
    class ControllerCrash(RuntimeError):
        pass

    def case(c):
        reached = False
        def crash(point):
            nonlocal reached
            if point == "after_intent_commit":
                reached = True
                raise ControllerCrash("after durable reservation")
        c.supervisor.fault_probe = crash
        with pytest.raises(ControllerCrash):
            _launch(c)
        assert reached and not c.marker.exists()
        before = c.accounting()
        assert len(before["authority_launch_intents"]) == 2
        assert before["authority_run_limits"][0]["dispatch_used"] == 2
        assert before["authority_run_limits"][0]["token_committed"] == 13
        reserved = next(r for r in before["authority_launch_intents"] if r["id"] != c.origin_handle.intent_id)
        assert reserved["child_pid"] is None
        restart = Supervisor(c.store, c.token, evidence_root=c.supervisor.evidence_root)
        original = subprocess.Popen
        spawns = []
        def observe_popen(argv, *args, **kwargs):
            if "run_state.supervisor" in argv and "_child" in argv:
                spawns.append(tuple(argv))
            return original(argv, *args, **kwargs)
        monkeypatch.setattr(subprocess, "Popen", observe_popen)
        with pytest.raises(REFUSALS) as replay:
            _launch(c, supervisor=restart)
        assert replay.value.code == "INTENT_RECONCILIATION_REQUIRED"
        assert not spawns and not c.marker.exists()
        assert c.accounting() == before
    _exercise(tmp_path, monkeypatch, case)


@pytest.mark.parametrize("corruption", ["wrapper_run", "wrapper_activity", "payload_hash"])
def test_allocation_receipt_authentication_refuses_tamper_before_launch(tmp_path, monkeypatch, corruption):
    def case(c):
        # The real originating worker created a valid allocation, not a synthetic receipt.
        assert c.supervisor.consume_delegate_request(c.event_id) == c.allocation
        key = "delegate-allocation:" + str(c.event_id)
        with c.store.transaction() as tx:
            receipt = tx.execute(
                "SELECT k.event_id,k.payload_hash,e.payload FROM authority_event_keys k "
                "JOIN control_events e ON e.id=k.event_id WHERE k.activity_id=? AND k.idempotency_key=?",
                (c.origin.id, key),
            ).fetchone()
            payload = json.loads(receipt["payload"])
            original_data = json.dumps(payload["data"], sort_keys=True)
            assert payload["data"] == c.allocation
            if corruption == "payload_hash":
                tx.execute("UPDATE authority_event_keys SET payload_hash=? WHERE activity_id=? AND idempotency_key=?",
                           ("0" * 64, c.origin.id, key))
            else:
                field = "run_id" if corruption == "wrapper_run" else "activity_id"
                assert field in payload
                payload[field] = "unrelated-durable-authority"
                assert json.dumps(payload["data"], sort_keys=True) == original_data
                tx.execute("UPDATE control_events SET payload=? WHERE id=?",
                           (json.dumps(payload, sort_keys=True), receipt["event_id"]))
            after = tx.execute("SELECT payload FROM control_events WHERE id=?", (receipt["event_id"],)).fetchone()
            assert json.dumps(json.loads(after["payload"])["data"], sort_keys=True) == original_data
        before = c.accounting()
        try:
            handle = _launch(c)
        except REFUSALS:
            assert c.accounting() == before
            assert not c.marker.exists()
        else:
            assert handle.process.wait(timeout=10) == 0
            assert c.marker.read_text() == "real child"
            pytest.fail("tampered allocation receipt admitted and executed real child: " + corruption)
    _exercise(tmp_path, monkeypatch, case)



def test_concurrent_same_event_supervisors_launch_exactly_one_real_child(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    def case(c):
        supervisors = [Supervisor(c.store, c.token, evidence_root=c.supervisor.evidence_root)
                       for _ in range(2)]
        barrier = Barrier(2)
        def attempt(supervisor):
            barrier.wait(timeout=10)
            try:
                return ("launched", (supervisor, _launch(c, supervisor=supervisor)))
            except REFUSALS as error:
                return ("refused", error)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt, supervisor) for supervisor in supervisors]
            outcomes = [future.result(timeout=40) for future in futures]
        launched = [value for status, value in outcomes if status == "launched"]
        refused = [value for status, value in outcomes if status == "refused"]
        assert len(launched) == 1 and len(refused) == 1
        assert refused[0].code == "INTENT_RECONCILIATION_REQUIRED"
        winner, handle = launched[0]
        assert handle.process.wait(timeout=10) == 0
        assert c.marker.read_text() == "real child"
        assert handle.stdout_path.read_text().splitlines() == ["delegate-output"]
        after = c.accounting()
        assert len(after["authority_launch_intents"]) == len(c.before["authority_launch_intents"]) + 1
        assert after["authority_run_limits"][0]["dispatch_used"] == c.before["authority_run_limits"][0]["dispatch_used"] + 1
        assert after["authority_run_limits"][0]["token_committed"] == 13
        reservations = [row for row in after["authority_launch_accounting"]
                        if row["intent_id"] != c.origin_handle.intent_id]
        assert len(reservations) == 1 and reservations[0]["token_reservation"] == 13
        child_before = next(row for row in c.before["authority_activities"] if row["id"] == c.allocation["activity_id"])
        child_after = next(row for row in after["authority_activities"] if row["id"] == c.allocation["activity_id"])
        assert child_after["remaining_retry_budget"] == child_before["remaining_retry_budget"] - 1
        assert winner.finish(handle, timeout=10, token_usage=13)["returncode"] == 0
    _exercise(tmp_path, monkeypatch, case)
