"""Child preparation recovery preserves its registered parent authority."""
import subprocess
import sys

import pytest

from run_state.cli import _cmd_fixture_start
from run_state.supervisor import DispatchRequest, Supervisor, SupervisorRefused
from run_state.workspace import (
    adopt_unstarted_workspace_preparation_fence, adopt_workspace_preparation_fence,
    begin_child_workspace_preparation, finalize_ready_unlock, prepare_workspace,
    recover_workspace_preparation,
)
from test_managed_preparation import _args
from test_m4_upstream_context_acceptance import _repository
from test_registered_child_execution import git
from test_run_context_acceptance import INHERITED_CONTEXT_KEYS


def _parent(store):
    with store.read_transaction() as tx:
        return dict(tx.execute("SELECT * FROM context_runs").fetchone())


def _invoke(tmp_path, monkeypatch, callback):
    primary = _repository(tmp_path)
    monkeypatch.chdir(primary)
    for key in INHERITED_CONTEXT_KEYS:
        monkeypatch.delenv(key, raising=False)
    def execute(store, token, context):
        callback(primary, store, token, context)
        return 0
    assert _cmd_fixture_start(_args(tmp_path / "authority", activity="execute"), on_ready=execute) == 0


@pytest.mark.parametrize("interrupted", [False, True])
def test_child_recovery_preserves_entire_parent_context(tmp_path, monkeypatch, interrupted):
    def execute(primary, store, token, context):
        before = _parent(store)
        pending = begin_child_workspace_preparation(
            store, token, parent_activity_id=context.activity_id,
            request_key="recover-child", role="worker", base_commit=git(primary, "rev-parse", "HEAD"),
            selected_input_manifest={"entries": []}, repository_path=primary,
        )
        if interrupted:
            def interrupt(_workspace):
                raise RuntimeError("simulated interruption before READY")
            with pytest.raises(RuntimeError, match="simulated interruption"):
                prepare_workspace(store, token, pending, before_ready=interrupt)
            assert pending.path.is_dir()
            native = git(primary, "worktree", "list", "--porcelain")
            assert f"locked ffs-preparation:{pending.id}" in native
            adopted = adopt_workspace_preparation_fence(store, token, pending.id)
            ready = recover_workspace_preparation(store, token, adopted.id)
            finalize_ready_unlock(store, token, ready.id)
        else:
            assert not pending.path.exists()
            adopted = adopt_unstarted_workspace_preparation_fence(
                store, token, repository_path=primary, preparation_id=pending.id,
            )
            assert adopted.id == pending.id
            ready = prepare_workspace(store, token, adopted)
        assert ready.ready
        assert git(ready.path, "rev-parse", "HEAD") == pending.base_commit
        records = git(primary, "worktree", "list", "--porcelain").split("\n\n")
        record = next(row for row in records if row.startswith(f"worktree {ready.path}\n"))
        assert not any(line.startswith("locked") for line in record.splitlines())
        assert _parent(store) == before
    _invoke(tmp_path, monkeypatch, execute)


def test_foreign_clone_at_identical_head_refuses_before_debit(tmp_path, monkeypatch):
    def execute(primary, store, token, context):
        store.configure_run_limits(token, dispatch_limit=3, token_limit=100, worker_capacity=2)
        store.bind_runtime(token, context.activity_id, "b" * 64)
        before_parent = _parent(store)
        base = git(primary, "rev-parse", "HEAD")
        pending = begin_child_workspace_preparation(
            store, token, parent_activity_id=context.activity_id,
            request_key="replace-child", role="worker", base_commit=base,
            selected_input_manifest={"entries": []}, repository_path=primary,
        )
        ready = prepare_workspace(store, token, pending)
        child = store.create_child_activity(
            token, parent_activity_id=context.activity_id, role="worker", request_key="replace-activity",
            candidate_hash=ready.input_digest, contract_hash="d" * 64, runtime_identity="b" * 64,
            workspace_binding=str(ready.path), workspace_preparation_id=ready.id,
        )
        retained = tmp_path / "retained-original-child"
        ready.path.rename(retained)
        subprocess.run(["rtk", "proxy", "git", "clone", "--no-hardlinks", "-q", str(primary), str(ready.path)], check=True)
        assert git(ready.path, "rev-parse", "HEAD") == base
        assert git(ready.path, "rev-parse", "--git-common-dir") == ".git"
        def accounting():
            with store.read_transaction() as tx:
                return {table: [dict(row) for row in tx.execute(f"SELECT * FROM {table}")]
                        for table in ("authority_run_limits", "authority_launch_intents",
                                      "authority_launch_accounting", "authority_budget_debits",
                                      "authority_activities")}
        before = accounting()
        supervisor = Supervisor(store, token, evidence_root=tmp_path / "authority/evidence")
        request = DispatchRequest(child.id, "foreign-launch", (sys.executable, "-c", "raise SystemExit(0)"),
                                  str(ready.path), base, "b" * 64, contract_hash="d" * 64)
        with pytest.raises(SupervisorRefused, match="WORKSPACE_BINDING_MISMATCH"):
            supervisor.launch(request)
        assert accounting() == before
        assert retained.is_dir() and (retained / "src/input.txt").is_file()
        assert _parent(store) == before_parent
    _invoke(tmp_path, monkeypatch, execute)
