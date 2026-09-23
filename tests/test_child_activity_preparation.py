import pytest

from run_state.ownership import OwnershipRefused
from run_state.tests.test_run_limits import _owned


def test_child_activity_refuses_missing_or_unregistered_preparation(tmp_path):
    store, ownership, parent = _owned(tmp_path)
    store.configure_run_limits(ownership.token, dispatch_limit=2, token_limit=2)
    common = dict(parent_activity_id=parent.id, role="worker", request_key="child",
                  candidate_hash="a" * 64, contract_hash="d" * 64, runtime_identity="b" * 64,
                  workspace_binding=ownership.token.workspace)
    with pytest.raises(OwnershipRefused, match="INVALID_CHILD_ACTIVITY"):
        store.create_child_activity(ownership.token, **common)
    with pytest.raises(OwnershipRefused, match="WORKSPACE_BINDING_MISMATCH"):
        store.create_child_activity(ownership.token, **common, workspace_preparation_id="foreign")


def test_child_activity_replay_cannot_change_preparation_id(tmp_path):
    store, ownership, parent = _owned(tmp_path)
    store.configure_run_limits(ownership.token, dispatch_limit=2, token_limit=2)
    common = dict(parent_activity_id=parent.id, role="worker", request_key="child",
                  candidate_hash="a" * 64, contract_hash="d" * 64, runtime_identity="b" * 64,
                  workspace_binding="/registered/child")
    with pytest.raises(OwnershipRefused, match="WORKSPACE_BINDING_MISMATCH"):
        store.create_child_activity(ownership.token, **common, workspace_preparation_id="one")
    with pytest.raises(OwnershipRefused, match="WORKSPACE_BINDING_MISMATCH"):
        store.create_child_activity(ownership.token, **common, workspace_preparation_id="two")
