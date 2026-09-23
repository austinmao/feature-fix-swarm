"""Focused E6 policy boundaries; managed preparation is covered by the slice."""
import pytest

from run_state.frontend_policy import FrontendPolicyController, FrontendPolicyRefused
from run_state.run_policy import RunPolicyRefused, validate_role_receipt


def _receipt(role="execution"):
    return {
        "schema": "ffs.run-policy-receipt/v1", "role": role, "request_key": "request",
        "activity_id": "activity", "intent_id": "intent", "fence_generation": 1,
        "acceptance_hash": "a" * 64, "candidate_hash": "b" * 64,
        "runtime_hash": "c" * 64, "workspace_preparation_hash": "d" * 64,
        "evidence": [{"id": "actual-bytes", "sha256": "e" * 64, "locator": "/evidence"}],
        "completion_status": "succeeded",
        "process_identity": {"host_id": "host", "boot_id": "boot", "pid": 1, "start_token": "start"},
        "review_dimensions": [],
    }


def test_receipt_role_is_a_whitelist_not_caller_prose():
    with pytest.raises(RunPolicyRefused, match="POLICY_RECEIPT_ROLE_INVALID"):
        validate_role_receipt(_receipt("approval"))
    assert validate_role_receipt(_receipt()).role == "execution"


@pytest.mark.parametrize("mode", ["", "review-gate", "feature_spec"])
def test_controller_refuses_unmanaged_or_renamed_frontend_modes(mode):
    with pytest.raises(FrontendPolicyRefused, match="FRONTEND_MODE_INVALID"):
        FrontendPolicyController(None, None, command_mode=mode)

