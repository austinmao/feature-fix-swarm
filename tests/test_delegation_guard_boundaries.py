"""Independent fault barriers around real authenticated delegation consumption.

Fixtures use disposable Git repositories and real waiting children. SQL writes
below deliberately corrupt retained evidence; they never invent live owners,
process identities, launch acknowledgements, or successful execution results.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest

from run_state.cli import _cmd_fixture_start
from run_state.supervisor import DispatchRequest, Supervisor, SupervisorRefused
from run_state.worker_channel import WorkerChannelServer
from run_state.workspace import (
    _verify_snapshot_complete, begin_child_workspace_preparation,
    inspect_workspace, parse_input_selection, prepare_workspace, snapshot_inputs,
)
from test_managed_preparation import _args
from test_m4_upstream_context_acceptance import _repository
from test_m4_workspace_acceptance import _copy, _selection
from test_registered_child_execution import git
from test_run_context_acceptance import INHERITED_CONTEXT_KEYS


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _exercise(tmp_path, monkeypatch, case):
    primary = _repository(tmp_path)
    monkeypatch.chdir(primary)
    for key in INHERITED_CONTEXT_KEYS:
        monkeypatch.delenv(key, raising=False)
    base = git(primary, "rev-parse", "HEAD")
    original_consume = Supervisor.consume_delegate_request
    entered, proceed = threading.Event(), threading.Event()
    control = SimpleNamespace(primary=primary, tmp=tmp_path, event_id=None)

    def barrier(supervisor, event_id):
        control.event_id = event_id
        entered.set()
        assert proceed.wait(20), "consumer barrier was not released"
        return original_consume(supervisor, event_id)

    monkeypatch.setattr(Supervisor, "consume_delegate_request", barrier)

    def execute(store, token, context):
        store.configure_run_limits(token, dispatch_limit=3, token_limit=100, worker_capacity=2)
        store.bind_runtime(token, context.activity_id, "b" * 64)
        selected = (primary / "src/input.txt").read_bytes()
        snapshot = snapshot_inputs(
            primary, parse_input_selection(_selection(
                primary, token.repository_id, entries=[_copy("src/input.txt", selected)],
            )), tmp_path / "retained-input",
        )
        preparation = begin_child_workspace_preparation(
            store, token, parent_activity_id=context.activity_id, request_key="origin-tree",
            role="worker", base_commit=base, selected_input_manifest=snapshot.manifest,
            repository_path=primary,
        )
        ready = prepare_workspace(store, token, preparation, input_snapshot=snapshot)
        child = store.create_child_activity(
            token, parent_activity_id=context.activity_id, role="worker", request_key="origin",
            candidate_hash=ready.input_digest, contract_hash="d" * 64, runtime_identity="b" * 64,
            workspace_binding=str(ready.path), workspace_preparation_id=ready.id,
        )
        body = dict(parent_activity_id=child.id, role="worker", candidate_hash=ready.input_digest,
                    contract_hash="d" * 64, runtime_identity="b" * 64)
        command = (sys.executable, "-c", f"""
import json,os,time
from pathlib import Path
from run_state.worker_channel import request
scope=json.loads(os.environ['FFS_WORKER_SCOPE'])
deadline=time.monotonic()+60
Path('worker-ready').touch()
while not Path('request-go').exists():
    if time.monotonic()>deadline: raise RuntimeError('request gate expired')
    time.sleep(.01)
response=request(os.environ['FFS_WORKER_ENDPOINT'],scope,request_key='boundary',
 operation='delegate-request',body={body!r},timeout=30)
Path('response.json').write_text(json.dumps(response))
while not Path('worker-release').exists():
    if time.monotonic()>deadline: raise RuntimeError('release gate expired')
    time.sleep(.01)
""")
        # Only this small socket directory uses internal disk (Unix path limit).
        with tempfile.TemporaryDirectory(prefix="ffs-boundary-", dir="/tmp") as endpoint_dir:
            server = WorkerChannelServer(store, token, Path(endpoint_dir).resolve() / "ipc").start()
            supervisor = Supervisor(store, token, evidence_root=tmp_path / "authority/evidence", worker_channel=server)
            handle = None
            control.__dict__.update(store=store, token=token, context=context, ready=ready,
                                    snapshot=snapshot, child=child, supervisor=supervisor,
                                    live_path=ready.path, server=server)

            def wait_file(name):
                deadline = time.monotonic() + 30
                while not (control.live_path / name).exists():
                    assert handle.process.poll() is None, handle.stderr_path.read_text()
                    assert time.monotonic() < deadline, name
                    time.sleep(.01)
                return control.live_path / name

            def response():
                proceed.set()
                return json.loads(wait_file("response.json").read_text())

            def rows(table):
                with store.read_transaction() as tx:
                    return [dict(row) for row in tx.execute(f"SELECT * FROM {table}")]

            control.rows = rows
            control.response = response
            control.consume = lambda: original_consume(supervisor, control.event_id)
            control.receipts = lambda: [row for row in rows("authority_event_keys")
                                       if row["idempotency_key"].startswith("delegate-allocation:")]
            try:
                handle = supervisor.launch(DispatchRequest(
                    child.id, "origin-launch", command, str(ready.path), base, "b" * 64,
                    contract_hash="d" * 64,
                ))
                control.handle = handle
                wait_file("worker-ready")
                (ready.path / "request-go").touch()
                assert entered.wait(10), handle.stderr_path.read_text()
                control.initial_workspaces = rows("context_workspaces")
                control.initial_activities = rows("authority_activities")
                control.initial_debits = rows("authority_budget_debits")
                control.initial_accounting = rows("authority_launch_accounting")
                control.initial_limits = rows("authority_run_limits")
                assert len(control.initial_accounting) == 1
                assert control.initial_limits[0]["dispatch_used"] == 1
                case(control)
                assert rows("authority_budget_debits") == control.initial_debits
                assert rows("authority_launch_accounting") == control.initial_accounting
                assert rows("authority_run_limits") == control.initial_limits
                assert len(rows("authority_launch_intents")) == 1
                return 0
            finally:
                proceed.set()
                (control.live_path / "worker-release").touch()
                if handle is not None:
                    try:
                        handle.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        handle.process.terminate()
                        handle.process.wait(timeout=5)
                server.close()

    assert _cmd_fixture_start(_args(tmp_path / "authority", activity="execute"), on_ready=execute) == 0


@pytest.mark.parametrize("corruption", ["request-body", "registration-body", "intent-key"])
def test_authenticated_event_corruption_refuses_before_allocation(tmp_path, monkeypatch, corruption):
    def case(c):
        with c.store.transaction() as tx:
            event_id = c.event_id
            if corruption == "registration-body":
                event_id = tx.execute(
                    "SELECT event_id FROM authority_event_keys WHERE activity_id=? AND idempotency_key=?",
                    (c.child.id, "worker-registration:" + c.handle.intent_id),
                ).fetchone()["event_id"]
            row = tx.execute("SELECT payload FROM control_events WHERE id=?", (event_id,)).fetchone()
            payload = json.loads(row["payload"])
            if corruption == "request-body":
                payload["data"]["body"]["role"] = "reviewer"
            elif corruption == "registration-body":
                original_roles = payload["data"]["allowed_roles"]
                payload["data"]["allowed_roles"] = [*original_roles, "inventory"]
                assert payload["data"]["allowed_roles"] != original_roles
            else:
                wrong_key = "worker-request:unrelated-intent:boundary"
                tx.execute("UPDATE control_events SET event_type=? WHERE id=?", (wrong_key, event_id))
                tx.execute("UPDATE authority_event_keys SET idempotency_key=? WHERE event_id=?", (wrong_key, event_id))
            if corruption != "intent-key":
                tx.execute("UPDATE control_events SET payload=? WHERE id=?", (_json(payload), event_id))
        assert c.response() == {"ok": False, "code": "IPC_SCOPE_MISMATCH"}
        assert c.rows("context_workspaces") == c.initial_workspaces
        assert c.rows("authority_activities") == c.initial_activities
        assert not c.receipts()
    _exercise(tmp_path, monkeypatch, case)


def _replace_tree(c, path, name):
    retained = c.tmp / name
    path.rename(retained)
    subprocess.run(["git", "clone", "--no-hardlinks", "-q", str(c.primary), str(path)], check=True)
    return retained


def test_origin_inode_replacement_after_read_cannot_begin_child(tmp_path, monkeypatch):
    def case(c):
        original = c.supervisor._read_delegate_request
        def replace_after_read(event_id):
            value = original(event_id)
            c.live_path = _replace_tree(c, c.ready.path, "retained-origin")
            return value
        monkeypatch.setattr(c.supervisor, "_read_delegate_request", replace_after_read)
        assert c.response() == {"ok": False, "code": "WORKSPACE_BINDING_MISMATCH"}
        assert c.rows("context_workspaces") == c.initial_workspaces
        assert c.rows("authority_activities") == c.initial_activities
        assert not c.receipts()
    _exercise(tmp_path, monkeypatch, case)


def test_ancestor_abort_after_request_read_cannot_begin_child(tmp_path, monkeypatch):
    def case(c):
        original = c.supervisor._read_delegate_request
        def abort_after_read(event_id):
            value = original(event_id)
            current = c.store.get_activity(c.context.activity_id)
            c.store.transition_activity(c.token, current.id, expected=current.state, new="aborted",
                                        reason="deterministic boundary test")
            return value
        monkeypatch.setattr(c.supervisor, "_read_delegate_request", abort_after_read)
        response = c.response()
        assert response == {"ok": False, "code": "PARENT_ACTIVITY_INVALID"}
        assert c.rows("context_workspaces") == c.initial_workspaces
        assert len(c.rows("authority_activities")) == len(c.initial_activities)
        assert not c.receipts()
    _exercise(tmp_path, monkeypatch, case)


def test_derived_tree_replaced_after_activity_commit_cannot_publish_receipt(tmp_path, monkeypatch):
    def case(c):
        original = c.store.create_child_activity
        def replace_after_commit(*args, **kwargs):
            child = original(*args, **kwargs)
            c.retained_derived = _replace_tree(c, Path(kwargs["workspace_binding"]), "retained-derived")
            return child
        monkeypatch.setattr(c.store, "create_child_activity", replace_after_commit)
        assert c.response() == {"ok": False, "code": "WORKSPACE_BINDING_MISMATCH"}
        assert len(c.rows("context_workspaces")) == len(c.initial_workspaces) + 1
        assert len(c.rows("authority_activities")) == len(c.initial_activities) + 1
        assert c.retained_derived.is_dir()
        assert not c.receipts()
    _exercise(tmp_path, monkeypatch, case)


def test_replay_rejects_self_consistent_different_full_manifest(tmp_path, monkeypatch):
    def case(c):
        response = c.response()
        assert response["ok"]
        allocation = response["result"]
        preparation_id = allocation["workspace_preparation_id"]
        with c.store.transaction() as tx:
            row = tx.execute("SELECT selected_manifest_json FROM context_workspaces WHERE preparation_id=?",
                             (preparation_id,)).fetchone()
            manifest = json.loads(row["selected_manifest_json"])
            manifest["required_context"] = [{"path": "src/input.txt", "reason": "substituted scope"}]
            encoded = _json(manifest)
            tx.execute("UPDATE context_workspaces SET selected_manifest_json=? WHERE preparation_id=?",
                       (encoded, preparation_id))
            tx.execute("UPDATE context_input_snapshots SET full_manifest_hash=? WHERE preparation_id=?",
                       (hashlib.sha256(encoded.encode()).hexdigest(), preparation_id))
        # Its local receipt/capture is internally consistent. It still is not
        # the exact source manifest that authorized this delegated allocation.
        _verify_snapshot_complete(c.store, inspect_workspace(c.store, preparation_id))
        with pytest.raises(SupervisorRefused, match="REQUEST_BINDING_UNKNOWN"):
            c.consume()
        assert len(c.receipts()) == 1
        assert len(c.rows("context_workspaces")) == len(c.initial_workspaces) + 1
    _exercise(tmp_path, monkeypatch, case)


def test_ready_before_unlock_interruption_resumes_same_unlocked_child(tmp_path, monkeypatch):
    from run_state import workspace

    def case(c):
        original = workspace._unlock_workspace_locked

        def interrupted(*_args, **_kwargs):
            raise workspace.WorkspaceRefused("INJECTED_BEFORE_UNLOCK")

        monkeypatch.setattr(workspace, "_unlock_workspace_locked", interrupted)
        assert c.response() == {"ok": False, "code": "INJECTED_BEFORE_UNLOCK"}
        monkeypatch.setattr(workspace, "_unlock_workspace_locked", original)
        delegated = [row for row in c.rows("context_workspaces")
                     if row["child_request_key"] == f"delegate-allocation:{c.event_id}"]
        assert len(delegated) == 1 and delegated[0]["state"] == "ready"
        row = delegated[0]
        initial_identity = row["native_identity_json"]

        def native_record():
            return next(record for record in git(c.primary, "worktree", "list", "--porcelain").split("\n\n")
                        if record.startswith(f"worktree {row['path']}\n"))

        assert any(line.startswith("locked") for line in native_record().splitlines())
        assert not c.receipts()
        allocation = c.consume()
        assert allocation["workspace_preparation_id"] == row["preparation_id"]
        assert allocation["workspace"] == row["path"]
        final = next(item for item in c.rows("context_workspaces") if item["preparation_id"] == row["preparation_id"])
        assert final["native_identity_json"] == initial_identity
        assert not any(line.startswith("locked") for line in native_record().splitlines())
        assert len(c.rows("context_workspaces")) == len(c.initial_workspaces) + 1
        assert len(c.rows("authority_activities")) == len(c.initial_activities) + 1
        assert c.consume() == allocation
    _exercise(tmp_path, monkeypatch, case)


def test_replay_rejects_derived_replacement_after_physical_probe(tmp_path, monkeypatch):
    from run_state import supervisor as implementation

    def case(c):
        response = c.response()
        assert response["ok"]
        allocation = response["result"]
        original = implementation._verify_snapshot_complete
        replaced = False

        def replace_after_probe(store, preparation, **kwargs):
            nonlocal replaced
            original(store, preparation, **kwargs)
            if preparation.id == allocation["workspace_preparation_id"] and not replaced:
                replaced = True
                _replace_tree(c, preparation.path, "retained-after-probe")

        monkeypatch.setattr(implementation, "_verify_snapshot_complete", replace_after_probe)
        with pytest.raises(SupervisorRefused, match="REQUEST_BINDING_UNKNOWN"):
            c.consume()
        assert replaced
        assert len(c.receipts()) == 1
        assert len(c.rows("context_workspaces")) == len(c.initial_workspaces) + 1
    _exercise(tmp_path, monkeypatch, case)
