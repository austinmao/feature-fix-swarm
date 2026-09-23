"""Child allocation rejects conflicting identities without stateful side effects."""
import json

import pytest

from run_state.ownership import OwnershipRefused
from run_state.selection import SelectionRefused, parse_input_selection
from run_state.state import ControlStore, ControlStoreRefused
from run_state.workspace import (
    WorkspaceRefused, begin_child_workspace_preparation, inspect_workspace, prepare_workspace,
)
from test_child_workspace_recovery import _invoke
from test_m4_workspace_acceptance import _copy, _pure_manifest
from test_registered_child_execution import git


@pytest.mark.parametrize("partial", [False, True])
def test_legacy_child_schema_reader_is_exact_and_does_not_migrate(tmp_path, partial):
    root = tmp_path / "authority"
    root.mkdir(mode=0o700)
    store = ControlStore(root / "control.sqlite3")
    store.ensure_context_schema()
    columns = ["parent_preparation_id", "parent_activity_id", "child_role", "child_request_key", "native_identity_json"]
    with store.transaction() as tx:
        tx.execute("DROP INDEX context_child_request_unique")
        for column in columns[:1] if partial else columns:
            tx.execute(f"ALTER TABLE context_workspaces DROP COLUMN {column}")
    before = store.db_path.read_bytes()
    if partial:
        with pytest.raises(ControlStoreRefused, match="CORRUPT_STORE"):
            store.validate_context_schema()
    else:
        store.validate_context_schema()
    assert store.db_path.read_bytes() == before


def test_selection_cannot_overlay_registered_children():
    with pytest.raises(SelectionRefused, match="RESERVED_SELECTION_PATH"):
        parse_input_selection(_pure_manifest(entries=[_copy(".ffs-children/owned/input.txt", b"x")]))


def test_legacy_parent_preparation_remains_readable_without_ownership_upgrade(tmp_path, monkeypatch):
    def execute(primary, store, token, context):
        with store.transaction() as tx:
            parent_id = tx.execute("SELECT preparation_id FROM context_runs").fetchone()[0]
            tx.execute("DROP INDEX context_child_request_unique")
            for column in ("parent_preparation_id", "parent_activity_id", "child_role",
                           "child_request_key", "native_identity_json"):
                tx.execute(f"ALTER TABLE context_workspaces DROP COLUMN {column}")
        before = store.db_path.read_bytes()
        store.validate_context_schema()
        retained = inspect_workspace(store, parent_id)
        assert retained.ready and str(retained.path) == token.workspace
        assert retained.parent_preparation_id is None and retained.native_identity is None
        assert store.db_path.read_bytes() == before
    _invoke(tmp_path, monkeypatch, execute)


def test_allocation_replay_binds_full_snapshot_material(tmp_path, monkeypatch):
    def execute(primary, store, token, context):
        manifest = {"schema": "ffs.input-snapshot/v1", "selection_manifest_hash": "a" * 64,
                    "input_digest": "b" * 64, "capture": {"locator": "/captured-one"}}
        kwargs = dict(parent_activity_id=context.activity_id, request_key="immutable-child",
                      role="worker", base_commit=git(primary, "rev-parse", "HEAD"),
                      repository_path=primary)
        first = begin_child_workspace_preparation(store, token, selected_input_manifest=manifest, **kwargs)
        changed = json.loads(json.dumps(manifest))
        changed["capture"]["locator"] = "/captured-two"
        with pytest.raises(WorkspaceRefused, match="INPUT_SELECTION_CHANGED"):
            begin_child_workspace_preparation(store, token, selected_input_manifest=changed, **kwargs)
        replay = begin_child_workspace_preparation(store, token, selected_input_manifest=manifest, **kwargs)
        assert replay.id == first.id
    _invoke(tmp_path, monkeypatch, execute)


def test_workspace_cannot_bind_two_activity_requests(tmp_path, monkeypatch):
    def execute(primary, store, token, context):
        store.configure_run_limits(token, dispatch_limit=3, token_limit=100)
        store.bind_runtime(token, context.activity_id, "b" * 64)
        pending = begin_child_workspace_preparation(
            store, token, parent_activity_id=context.activity_id, request_key="one-workspace",
            role="worker", base_commit=git(primary, "rev-parse", "HEAD"),
            repository_path=primary, selected_input_manifest={"entries": []},
        )
        ready = prepare_workspace(store, token, pending)
        kwargs = dict(parent_activity_id=context.activity_id, role="worker",
                      candidate_hash=ready.input_digest, contract_hash="d" * 64,
                      runtime_identity="b" * 64, workspace_binding=str(ready.path),
                      workspace_preparation_id=ready.id)
        first = store.create_child_activity(token, request_key="first-activity", **kwargs)
        assert store.create_child_activity(token, request_key="first-activity", **kwargs).id == first.id
        with pytest.raises(OwnershipRefused, match="WORKSPACE_ACTIVITY_BOUND"):
            store.create_child_activity(token, request_key="second-activity", **kwargs)
    _invoke(tmp_path, monkeypatch, execute)
