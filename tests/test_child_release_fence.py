"""Actual waiting children cannot execute under changed ancestry or workspaces."""
import shutil
import sys

import pytest

from run_state.ownership import OwnershipRefused
from run_state.supervisor import DispatchRequest, Supervisor, SupervisorRefused
from run_state.workspace import begin_child_workspace_preparation, prepare_workspace
from test_child_workspace_recovery import _invoke
from test_registered_child_execution import git


def _ready(primary, store, token, context):
    store.configure_run_limits(token, dispatch_limit=3, token_limit=100)
    store.bind_runtime(token, context.activity_id, "b" * 64)
    pending = begin_child_workspace_preparation(
        store, token, parent_activity_id=context.activity_id, request_key="release-child",
        role="worker", base_commit=git(primary, "rev-parse", "HEAD"),
        repository_path=primary, selected_input_manifest={"entries": []},
    )
    ready = prepare_workspace(store, token, pending)
    child = store.create_child_activity(
        token, parent_activity_id=context.activity_id, role="worker", request_key="release-activity",
        candidate_hash=ready.input_digest, contract_hash="d" * 64, runtime_identity="b" * 64,
        workspace_binding=str(ready.path), workspace_preparation_id=ready.id,
    )
    request = DispatchRequest(
        child.id, "release-launch", (sys.executable, "-c",
                                     "from pathlib import Path; Path('executed').touch()"),
        str(ready.path), ready.base_commit, "b" * 64, contract_hash="d" * 64,
    )
    return ready, request


def test_retained_git_pointer_cannot_rebind_ready_directory(tmp_path, monkeypatch):
    def execute(primary, store, token, context):
        ready, request = _ready(primary, store, token, context)
        retained = ready.path.with_name(ready.path.name + "-retained")
        ready.path.rename(retained)
        ready.path.mkdir()
        shutil.copy2(retained / ".git", ready.path / ".git")
        git(ready.path, "reset", "--hard", ready.base_commit)
        assert git(ready.path, "rev-parse", "HEAD") == ready.base_commit
        assert ready.path.stat().st_ino != retained.stat().st_ino
        supervisor = Supervisor(store, token, evidence_root=tmp_path / "evidence")
        with pytest.raises(SupervisorRefused, match="WORKSPACE_BINDING_MISMATCH"):
            supervisor.launch(request)
        assert not (ready.path / "executed").exists()
        assert retained.is_dir()
        with store.read_transaction() as tx:
            assert tx.execute("SELECT dispatch_used FROM authority_run_limits").fetchone()[0] == 0
            assert tx.execute("SELECT COUNT(*) FROM authority_launch_intents").fetchone()[0] == 0
    _invoke(tmp_path, monkeypatch, execute)


@pytest.mark.parametrize("state", ["paused", "aborted"])
@pytest.mark.parametrize("boundary", ["after_ack_before_authorization", "after_authorization_before_release"])
def test_parent_invalidation_before_release_retains_debit_without_execution(
    tmp_path, monkeypatch, state, boundary,
):
    def execute(primary, store, token, context):
        ready, request = _ready(primary, store, token, context)
        def invalidate(point):
            if point == boundary:
                store.transition_activity(token, context.activity_id, expected="active", new=state)
        supervisor = Supervisor(store, token, evidence_root=tmp_path / "evidence", fault_probe=invalidate)
        with pytest.raises((OwnershipRefused, SupervisorRefused)):
            supervisor.launch(request)
        assert not (ready.path / "executed").exists()
        with store.read_transaction() as tx:
            assert tx.execute("SELECT dispatch_used FROM authority_run_limits").fetchone()[0] == 1
            row = tx.execute("SELECT state,child_pid FROM authority_launch_intents").fetchone()
            assert row["state"] in {"acknowledged", "released_to_execute"}
            assert row["child_pid"] is not None
    _invoke(tmp_path, monkeypatch, execute)
