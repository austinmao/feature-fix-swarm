"""Real CLI owner reacquisition keeps child authority separate from parent."""

import pytest

from run_state.cli import _cmd_fixture_start
from run_state.ownership import release_owner
from run_state.workspace import (
    WorkspaceRefused, begin_child_workspace_preparation, prepare_workspace, revalidate_ready_fence,
)
from test_managed_preparation import _args
from test_m4_upstream_context_acceptance import _repository
from test_registered_child_execution import git
from test_run_context_acceptance import INHERITED_CONTEXT_KEYS


@pytest.mark.parametrize("path", ["replay", "fresh", "interrupted"])
@pytest.mark.parametrize("child_state", ["pending", "terminal", "uncertain"])
def test_cli_resume_preserves_child_authority(tmp_path, monkeypatch, path, child_state):
    primary = _repository(tmp_path)
    monkeypatch.chdir(primary)
    for key in INHERITED_CONTEXT_KEYS:
        monkeypatch.delenv(key, raising=False)
    retained = {}
    def children(store):
        with store.read_transaction() as tx:
            return {
                "activity": dict(tx.execute("SELECT * FROM authority_activities WHERE id=?", (retained["child_id"],)).fetchone()),
                "preparation": dict(tx.execute("SELECT * FROM context_workspaces WHERE preparation_id=?", (retained["preparation_id"],)).fetchone()),
                **{table: [dict(row) for row in tx.execute(f"SELECT * FROM {table}")]
                   for table in ("authority_child_bindings", "authority_run_limits", "authority_launch_intents",
                                 "authority_launch_accounting", "authority_budget_debits")},
            }
    def first(store, token, context):
        store.configure_run_limits(token, dispatch_limit=3, token_limit=100, worker_capacity=2)
        store.bind_runtime(token, context.activity_id, "b" * 64)
        pending = begin_child_workspace_preparation(
            store, token, parent_activity_id=context.activity_id, request_key="resume-workspace",
            role="worker", base_commit=git(primary, "rev-parse", "HEAD"),
            selected_input_manifest={"entries": []}, repository_path=primary,
        )
        ready = prepare_workspace(store, token, pending)
        child = store.create_child_activity(
            token, parent_activity_id=context.activity_id, role="worker", request_key="resume-child",
            candidate_hash=ready.input_digest, contract_hash="d" * 64, runtime_identity="b" * 64,
            workspace_binding=str(ready.path), workspace_preparation_id=ready.id,
        )
        if child_state == "terminal":
            store.transition_activity(token, child.id, expected="pending", new="succeeded",
                                      result={"locator": "fixture-child-result", "sha256": "a" * 64})
        elif child_state == "uncertain":
            store.reserve_launch(child.id, token, token_reservation=7, request_key="unresolved-launch",
                                 request_payload={"fixture": "committed before spawn"})
        retained.update(parent_id=context.activity_id, child_id=child.id, preparation_id=ready.id,
                        generation=token.generation, store=store)
        retained["before"] = children(store)
        # Explicit fixture owner release obtains a genuine new generation on
        # the next CLI call. The unresolved intent has no spawned child and is
        # retained conservatively; release does not refund any allowance.
        with store.transaction() as tx:
            release_owner(tx, token)
        return 0
    args = _args(tmp_path / "authority", activity="execute")
    assert _cmd_fixture_start(args, on_ready=first) == 0
    if path == "interrupted":
        with retained["store"].transaction() as tx:
            tx.execute("UPDATE context_requests SET result_json=NULL WHERE request_key=?", (args.request_key,))
    received = []
    def resumed(store, token, context):
        assert token.generation > retained["generation"]
        assert context.activity_id == retained["parent_id"]
        assert children(store) == retained["before"]
        with store.read_transaction() as tx:
            parent = tx.execute("SELECT * FROM authority_activities WHERE id=?", (context.activity_id,)).fetchone()
            assert parent["generation"] == token.generation
            row = tx.execute("SELECT * FROM context_runs").fetchone()
            assert row["activity_id"] == retained["parent_id"]
            assert row["writer_version"] is None
            assert tx.execute("SELECT COUNT(*) FROM authority_activities").fetchone()[0] == 2
        before_recovery = children(store)
        with store.read_transaction() as tx:
            parent_context = dict(tx.execute("SELECT * FROM context_runs").fetchone())
        if child_state == "uncertain":
            with pytest.raises(WorkspaceRefused, match="INTENT_RECONCILIATION_REQUIRED"):
                revalidate_ready_fence(store, token, retained["preparation_id"])
            assert children(store) == before_recovery
        else:
            recovered = revalidate_ready_fence(store, token, retained["preparation_id"])
            assert recovered.generation == token.generation and recovered.ready
            after_recovery = children(store)
            assert after_recovery["preparation"]["generation"] == token.generation
            expected_preparation = dict(before_recovery["preparation"])
            expected_preparation["generation"] = token.generation
            expected_preparation["updated_at"] = after_recovery["preparation"]["updated_at"]
            assert after_recovery["preparation"] == expected_preparation
            assert {key: value for key, value in after_recovery.items() if key != "preparation"} == {
                key: value for key, value in before_recovery.items() if key != "preparation"
            }
        with store.read_transaction() as tx:
            assert dict(tx.execute("SELECT * FROM context_runs").fetchone()) == parent_context
        received.append(context.activity_id)
        return 0
    resume = args if path in {"replay", "interrupted"} else _args(tmp_path / "authority", activity="execute", request_key="fresh-resume", resume=True)
    assert _cmd_fixture_start(resume, on_ready=resumed) == 0
    assert received == [retained["parent_id"]]


def test_next_parent_revision_follows_all_child_revisions(tmp_path, monkeypatch):
    primary = _repository(tmp_path)
    monkeypatch.chdir(primary)
    for key in INHERITED_CONTEXT_KEYS:
        monkeypatch.delenv(key, raising=False)
    def first(store, token, context):
        store.configure_run_limits(token, dispatch_limit=3, token_limit=100)
        pending = begin_child_workspace_preparation(
            store, token, parent_activity_id=context.activity_id, request_key="revision-child",
            role="worker", base_commit=git(primary, "rev-parse", "HEAD"),
            selected_input_manifest={"entries": []}, repository_path=primary,
        )
        ready = prepare_workspace(store, token, pending)
        child = store.create_child_activity(
            token, parent_activity_id=context.activity_id, role="worker", request_key="revision-activity",
            candidate_hash=ready.input_digest, contract_hash="d" * 64, runtime_identity="b" * 64,
            workspace_binding=str(ready.path), workspace_preparation_id=ready.id,
        )
        parent = store.get_activity(context.activity_id)
        store.transition_activity(token, parent.id, expected="active", new="succeeded",
                                  result={"locator": "parent-result", "sha256": "a" * 64})
        selected = store.select_activity(token, kind="review", input_digest=parent.input_digest, resume=True, revise=False)
        assert selected.id not in {child.id, parent.id}
        assert selected.revision == child.revision + 1
        return 0
    assert _cmd_fixture_start(_args(tmp_path / "authority", activity="execute"), on_ready=first) == 0


@pytest.mark.parametrize("fault,code", [("binding", "FENCE_REVOKED"), ("pointer", "PARENT_ACTIVITY_INVALID")])
def test_resume_refuses_invalid_child_bindings_before_owner(tmp_path, monkeypatch, capsys, fault, code):
    primary = _repository(tmp_path)
    monkeypatch.chdir(primary)
    for key in INHERITED_CONTEXT_KEYS:
        monkeypatch.delenv(key, raising=False)
    retained = {}
    def first(store, token, context):
        store.configure_run_limits(token, dispatch_limit=3, token_limit=100)
        pending = begin_child_workspace_preparation(
            store, token, parent_activity_id=context.activity_id, request_key="invalid-child",
            role="worker", base_commit=git(primary, "rev-parse", "HEAD"),
            selected_input_manifest={"entries": []}, repository_path=primary,
        )
        ready = prepare_workspace(store, token, pending)
        child = store.create_child_activity(
            token, parent_activity_id=context.activity_id, role="worker", request_key="invalid-activity",
            candidate_hash=ready.input_digest, contract_hash="d" * 64, runtime_identity="b" * 64,
            workspace_binding=str(ready.path), workspace_preparation_id=ready.id,
        )
        with store.transaction() as tx:
            if fault == "binding":
                tx.execute("UPDATE authority_child_bindings SET workspace_binding=? WHERE activity_id=?",
                           (str(tmp_path / "foreign"), child.id))
            else:
                tx.execute("UPDATE context_runs SET activity_id=?", (child.id,))
            release_owner(tx, token)
        retained["store"] = store
        return 0
    args = _args(tmp_path / "authority", activity="execute", resume=True)
    assert _cmd_fixture_start(args, on_ready=first) == 0
    with retained["store"].read_transaction() as tx:
        before = [dict(row) for row in tx.execute("SELECT * FROM control_reservations")]
    assert _cmd_fixture_start(args, on_ready=lambda *_: pytest.fail("invalid resume callback")) != 0
    assert code in capsys.readouterr().out
    with retained["store"].read_transaction() as tx:
        assert [dict(row) for row in tx.execute("SELECT * FROM control_reservations")] == before
