"""Actual-process coverage for the explicit supervisor result monitor."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from process_identity import LIVE, ProcessIdentity, probe_identity
from run_state.state import ControlStore
from run_state.supervisor import Supervisor, SupervisorRefused, _publish
from run_state.ownership import StartRequest, reserve_resources
from test_supervised_process import setup_owner


def test_monitored_process_retains_native_identity_and_publishes_result(tmp_path):
    supervisor, store, request = setup_owner(tmp_path)
    command = (sys.executable, "-c", "from pathlib import Path; Path('ran').write_text('once'); print('native')")
    handle = supervisor.launch(replace(request, command=command, monitor_result=True))
    assert handle.monitored
    assert handle.monitor_identity is not None
    assert handle.identity.pid != handle.process.pid
    result = supervisor.finish(handle, timeout=10, token_usage=0)
    assert result["returncode"] == 0
    assert (Path(request.workspace) / "ran").read_text() == "once"
    receipt = json.loads(Path(result["evidence"]["locator"]).read_text())
    assert receipt["identity"]["pid"] == handle.identity.pid
    with store.read_transaction() as tx:
        intent = tx.execute("SELECT generation,child_pid,permit_id FROM authority_launch_intents WHERE id=?", (handle.intent_id,)).fetchone()
    assert intent["child_pid"] == handle.identity.pid
    assert intent["permit_id"]


def test_default_dispatch_retains_legacy_request_material_shape(tmp_path):
    supervisor, store, request = setup_owner(tmp_path)
    handle = supervisor.launch(request)
    supervisor.finish(handle, timeout=10, token_usage=0)
    with store.read_transaction() as tx:
        event = tx.execute(
            "SELECT e.payload FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id "
            "WHERE k.idempotency_key='dispatch-request:first'",
        ).fetchone()
    assert "transport" not in json.loads(event["payload"])["data"]["request"]


def test_result_publication_keeps_interrupted_stage_and_no_partial_final(tmp_path, monkeypatch):
    def interrupted(*_args, **_kwargs):
        raise OSError("injected link interruption")
    monkeypatch.setattr(os, "link", interrupted)
    with pytest.raises(OSError, match="injected"):
        _publish(tmp_path, "result.json", {"intent_id": "one"})
    assert not (tmp_path / "result.json").exists()
    stages = list(tmp_path.glob(".result.json.*.tmp"))
    assert len(stages) == 1
    assert json.loads(stages[0].read_text()) == {"intent_id": "one"}


def test_resume_monitored_observes_original_attempt_without_new_debit(tmp_path):
    supervisor, store, request = setup_owner(tmp_path)
    command = (sys.executable, "-c", "import time; time.sleep(.25); print('native')")
    launched = supervisor.launch(replace(request, command=command, monitor_result=True))
    with store.read_transaction() as tx:
        before = tx.execute("SELECT dispatch_used,token_committed FROM authority_run_limits").fetchone()
    resumed = Supervisor(ControlStore(store.db_path), supervisor.token, evidence_root=supervisor.evidence_root)
    handle = resumed.resume_monitored(launched.intent_id)
    # This test process still owns the original monitor Popen, so reap that
    # wrapper after its native result is published.  A SIGKILL owner case has
    # init perform this reap; the successor only observes the receipt.
    launched.process.wait(timeout=10)
    result = resumed.finish(handle, token_usage=0)
    with store.read_transaction() as tx:
        after = tx.execute("SELECT dispatch_used,token_committed FROM authority_run_limits").fetchone()
    assert result["returncode"] == 0
    assert tuple(after) == tuple(before)
    with pytest.raises(SupervisorRefused, match="REQUEST_ALREADY_COMPLETED"):
        resumed.launch(replace(request, command=command, monitor_result=True))


def test_sigkill_owner_successor_harvests_the_same_monitored_native_attempt(tmp_path):
    """An owner SIGKILL cannot replace or release a waiting native attempt."""
    metadata = tmp_path / "owner-metadata.json"
    native_script = tmp_path / "native-wait.py"
    native_script.write_text("""
from pathlib import Path
import json
import os
import time
Path('native-ready.json').write_text(json.dumps({'pid': os.getpid()}))
while not Path('test-owned-release').exists():
    time.sleep(.01)
with Path('native-effects.jsonl').open('a') as stream:
    stream.write(json.dumps({'pid': os.getpid()}) + '\\n')
print('native-once')
""")
    owner_program = """
from dataclasses import replace
from pathlib import Path
import json, os, sys, time
from test_supervised_process import setup_owner
Path(sys.argv[1]).mkdir(parents=True, exist_ok=True)
supervisor, store, request = setup_owner(Path(sys.argv[1]))
command = (sys.executable, sys.argv[3])
handle = supervisor.launch(replace(request, command=command, monitor_result=True, token_reservation=7))
deadline = time.monotonic() + 20
while not (Path(request.workspace) / 'native-ready.json').exists():
    assert time.monotonic() < deadline
    time.sleep(.01)
with store.read_transaction() as tx:
    intent = tx.execute("SELECT generation,child_pid,child_host_id,child_boot_id,child_start_token,permit_id,acknowledgement_id FROM authority_launch_intents WHERE id=?", (handle.intent_id,)).fetchone()
    limits = tx.execute("SELECT dispatch_used,token_committed,token_used FROM authority_run_limits").fetchone()
    activity = tx.execute("SELECT remaining_retry_budget FROM authority_activities WHERE id=?", (request.activity_id,)).fetchone()
    counts = {
        'attempts': tx.execute("SELECT count(*) FROM authority_launch_intents WHERE activity_id=?", (request.activity_id,)).fetchone()[0],
        'accounting': tx.execute("SELECT count(*) FROM authority_launch_accounting WHERE intent_id=?", (handle.intent_id,)).fetchone()[0],
        'acks': tx.execute("SELECT count(*) FROM authority_event_keys WHERE activity_id=? AND idempotency_key=?", (request.activity_id, 'child-ack:' + handle.intent_id)).fetchone()[0],
    }
Path(sys.argv[2]).write_text(json.dumps({
    'db': str(store.db_path), 'intent_id': handle.intent_id,
    'evidence_root': str(supervisor.evidence_root), 'workspace': request.workspace,
    'issuing_generation': supervisor.token.generation, 'native': {
        'host_id': intent['child_host_id'], 'boot_id': intent['child_boot_id'],
        'pid': intent['child_pid'], 'start_token': intent['child_start_token'],
    }, 'permit_id': intent['permit_id'], 'acknowledgement_id': intent['acknowledgement_id'],
    'monitor': {
        'host_id': handle.monitor_identity.host_id, 'boot_id': handle.monitor_identity.boot_id,
        'pid': handle.monitor_identity.pid, 'start_token': handle.monitor_identity.start_token,
    },
    'limits': list(limits), 'remaining_retry_budget': activity['remaining_retry_budget'], 'counts': counts,
}))
while True: time.sleep(1)
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "lib") + os.pathsep + str(Path(__file__).parent)
    owner = subprocess.Popen(
        [sys.executable, "-c", owner_program, str(tmp_path / "owner"), str(metadata), str(native_script)],
        env=environment,
    )
    payload = None
    released = False
    try:
        deadline = time.monotonic() + 20
        while not metadata.exists():
            assert owner.poll() is None and time.monotonic() < deadline
            time.sleep(.01)
        payload = json.loads(metadata.read_text())
        ready = json.loads((Path(payload["workspace"]) / "native-ready.json").read_text())
        assert ready == {"pid": payload["native"]["pid"]}
        assert not (Path(payload["workspace"]) / "native-effects.jsonl").exists()
        owner.kill()
        assert owner.wait(timeout=10) != 0  # owner is reaped before test release
        store = ControlStore(Path(payload["db"]))
        with store.read_transaction() as tx:
            run = tx.execute("SELECT * FROM context_runs").fetchone()
        successor = reserve_resources(store, StartRequest(
            run["run_id"], run["workspace"], run["objective_digest"], __import__('process_identity').ProcessIdentity.current(),
            repository_id=run["repository_id"], planning_scope=run["planning_scope"],
        )).token
        resumed = Supervisor(store, successor, evidence_root=Path(payload["evidence_root"]))
        handle = resumed.resume_monitored(payload["intent_id"])
        assert handle.identity.pid == payload["native"]["pid"]
        assert not (Path(payload["workspace"]) / "native-effects.jsonl").exists()
        # Only the test releases the already permitted native process, after
        # the issuing owner is gone and the successor has reconstructed it.
        (Path(payload["workspace"]) / "test-owned-release").write_text("release\n")
        released = True
        deadline = time.monotonic() + 20
        while True:
            try:
                result = resumed.finish(handle, token_usage=0)
                break
            except SupervisorRefused as error:
                assert error.code == "MONITOR_RESULT_PENDING" and time.monotonic() < deadline
                time.sleep(.02)
        assert result["returncode"] == 0
        with store.read_transaction() as tx:
            intent = tx.execute("SELECT generation,state,child_pid,permit_id,acknowledgement_id FROM authority_launch_intents WHERE id=?", (payload["intent_id"],)).fetchone()
            event = tx.execute("SELECT payload FROM control_events WHERE event_type='launch_completed' ORDER BY id DESC LIMIT 1").fetchone()
            limits = tx.execute("SELECT dispatch_used,token_committed,token_used FROM authority_run_limits").fetchone()
            activity = tx.execute("SELECT remaining_retry_budget FROM authority_activities WHERE id=?", (result["activity_id"],)).fetchone()
            attempts = tx.execute("SELECT count(*) FROM authority_launch_intents WHERE activity_id=?", (result["activity_id"],)).fetchone()[0]
            accounting = tx.execute("SELECT count(*) FROM authority_launch_accounting WHERE intent_id=?", (payload["intent_id"],)).fetchone()[0]
            acks = tx.execute("SELECT count(*) FROM authority_event_keys WHERE activity_id=? AND idempotency_key=?", (result["activity_id"], 'child-ack:' + payload["intent_id"])).fetchone()[0]
        assert intent["generation"] == payload["issuing_generation"]
        assert intent["state"] == "completed_succeeded"
        assert intent["child_pid"] == payload["native"]["pid"]
        assert intent["permit_id"] == payload["permit_id"]
        assert intent["acknowledgement_id"] == payload["acknowledgement_id"]
        assert tuple(limits) == (payload["limits"][0], 0, payload["limits"][2])
        assert activity["remaining_retry_budget"] == payload["remaining_retry_budget"]
        assert (attempts, accounting, acks) == (payload["counts"]["attempts"], payload["counts"]["accounting"], payload["counts"]["acks"])
        effects = (Path(payload["workspace"]) / "native-effects.jsonl").read_text().splitlines()
        assert [json.loads(line) for line in effects] == [{"pid": payload["native"]["pid"]}]
        assert json.loads(event["payload"])["data"]["settlement_generation"] == successor.generation
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)
        if payload is not None and not released:
            # The test owns only the exact process incarnations emitted by its
            # own monitor transport. Never use a PID-only cleanup fallback.
            for name in ("native", "monitor"):
                identity = ProcessIdentity(**payload[name])
                if probe_identity(identity) == LIVE:
                    try:
                        os.kill(identity.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
