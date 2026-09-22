"""Restricted-review integration with the existing Supervisor authority.

The supplemental proof belongs to one completed review. It is never a reusable
qualified runtime and never authorizes a second model invocation.
"""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat

from process_identity import ProcessIdentity
from .native_review_runtime import (
    NativeReviewRuntimeRefused, verify_claude_review_evidence, verify_codex_review_evidence,
)
from .native_review_transport import (
    NativeReviewLaunchMaterial, NativeReviewTransportRefused,
    read_native_review_launch, validate_native_review_launch_material,
)
from .ownership import OwnershipRefused
from .supervisor import SupervisorRefused, _canonical, _publish
from .workspace import _open_directory_chain_raw


def material_locator(supervisor, material):
    return supervisor.evidence_root / "native-review-material" / (material.material_sha256() + ".json")


def _sealed_binding(supervisor, activity_id, material, acceptance_hash, candidate_hash):
    from .sealed_review import final_review_input_context, final_review_output_contract
    store, token = supervisor.store, supervisor.token
    sealed = store.get_sealed_acceptance(repository_id=token.repository_id, run_id=token.run_id)
    state = store.get_frontend_policy_state(repository_id=token.repository_id, run_id=token.run_id)
    with store.read_transaction() as tx:
        child = tx.execute("SELECT * FROM authority_child_bindings WHERE activity_id=?", (activity_id,)).fetchone()
    if (sealed is None or state is None or child is None
            or sealed.acceptance_hash != acceptance_hash or state.acceptance_hash != acceptance_hash
            or child["role"] != "reviewer" or child["contract_hash"] != acceptance_hash
            or child["candidate_hash"] != candidate_hash or state.candidate_hash != candidate_hash
            or child["workspace_binding"] != material.native.workspace
            or child["runtime_identity"] != material.runtime_tuple_hash
            or state.stage != "FINAL_REVIEW"):
        raise SupervisorRefused("NATIVE_REVIEW_BINDING_INVALID")
    expected = final_review_output_contract(sealed, candidate_hash=candidate_hash)
    if material.artifact.output_contract_json != _canonical(expected).decode():
        raise SupervisorRefused("NATIVE_REVIEW_CONTRACT_INVALID")
    context = final_review_input_context(store, token, acceptance_hash=acceptance_hash,
                                        candidate_hash=candidate_hash, reviewer_activity_id=activity_id,
                                        selected_artifacts=material.artifact.selected_artifacts)
    if material.artifact.review_context_json != _canonical(context).decode():
        raise SupervisorRefused("NATIVE_REVIEW_CONTEXT_INVALID")


def validate_request(supervisor, request):
    material = request.native_review_material
    if type(material) is not NativeReviewLaunchMaterial:
        raise SupervisorRefused("NATIVE_REVIEW_MATERIAL_INVALID")
    try:
        validate_native_review_launch_material(material, expected_material_sha256=material.material_sha256())
    except NativeReviewTransportRefused as error:
        raise SupervisorRefused("NATIVE_REVIEW_MATERIAL_INVALID") from error
    if (not request.monitor_result or supervisor.worker_channel is not None
            or request.command != material.native.argv or request.workspace != material.native.workspace
            or request.runtime_identity != material.runtime_tuple_hash
            or request.runtime_receipt_sha256 != material.runtime_receipt_sha256
            or request.runtime_receipt_sha256 != material.ordinary_runtime_sha256
            or request.managed_input_sha256 is None or request.local_check_receipt_sha256 is not None):
        raise SupervisorRefused("NATIVE_REVIEW_MATERIAL_INVALID")
    _sealed_binding(supervisor, request.activity_id, material, request.contract_hash,
                    request.managed_input_sha256)
    with supervisor.store.read_transaction() as tx:
        activity = supervisor.store._assert_activity_binding(tx, supervisor.token, request.activity_id)
        if activity["state"] != "active":
            raise SupervisorRefused("NATIVE_REVIEW_BINDING_INVALID")
        # Same fresh receipt validator is repeated atomically in reserve_launch.
        supervisor.store._validate_runtime_receipt_tx(
            tx, supervisor.token, activity, request.runtime_receipt_sha256,
            request.managed_input_sha256, ProcessIdentity.current(), datetime.now(timezone.utc))
        if request.policy_action_id is not None:
            action = tx.execute("SELECT * FROM authority_policy_actions WHERE id=?",
                                (request.policy_action_id,)).fetchone()
            if (action is None or action["action"] != "final_review"
                    or action["repository_id"] != supervisor.token.repository_id
                    or action["run_id"] != supervisor.token.run_id
                    or action["logical_key"] != request.request_key
                    or action["input_hash"] != hashlib.sha256(_canonical(supervisor._dispatch_material(request))).hexdigest()):
                raise SupervisorRefused("NATIVE_REVIEW_ACTION_INVALID")


def publish_material(supervisor, material):
    path = material_locator(supervisor, material)
    directory = _open_directory_chain_raw(Path(path.anchor), path.parent.parts[1:], create=True)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise SupervisorRefused("NATIVE_REVIEW_MATERIAL_INVALID")
    finally:
        os.close(directory)
    try:
        _publish(path.parent, path.name, material.to_dict())
    except FileExistsError:
        retained = read_native_review_launch(path, expected_material_sha256=material.material_sha256(),
                                            credential_required=True)
        if retained != material:
            raise SupervisorRefused("NATIVE_REVIEW_MATERIAL_INVALID")


def _retained_dispatch(supervisor, handle):
    """Join the exact original request, ACK and policy attempt; never caller input."""
    store, token = supervisor.store, supervisor.token
    with store.read_transaction() as tx:
        activity = store._assert_activity_binding(tx, token, handle.activity_id)
        intent = tx.execute("SELECT i.*,a.repository_id,a.run_id FROM authority_launch_intents i "
                            "JOIN authority_activities a ON a.id=i.activity_id WHERE i.id=?",
                            (handle.intent_id,)).fetchone()
        ack = tx.execute("SELECT k.payload_hash,e.payload FROM authority_event_keys k JOIN control_events e "
                         "ON e.id=k.event_id WHERE k.activity_id=? AND k.idempotency_key=?",
                         (handle.activity_id, "child-ack:" + handle.intent_id)).fetchone()
        rows = tx.execute("SELECT k.idempotency_key,k.payload_hash,e.payload FROM authority_event_keys k "
                          "JOIN control_events e ON e.id=k.event_id WHERE k.activity_id=? "
                          "AND k.idempotency_key LIKE 'dispatch-request:%'", (handle.activity_id,)).fetchall()
        action = tx.execute("SELECT a.* FROM authority_policy_actions a JOIN authority_policy_action_attempts p "
                            "ON p.action_id=a.id WHERE p.intent_id=?", (handle.intent_id,)).fetchone()
    dispatches = [(row, json.loads(row["payload"])["data"]) for row in rows]
    matches = [(row, data) for row, data in dispatches if data.get("intent_id") == handle.intent_id]
    if len(matches) != 1 or intent is None or ack is None or action is None:
        raise SupervisorRefused("NATIVE_REVIEW_COMPLETION_INVALID")
    row, dispatch = matches[0]
    binding = json.loads(ack["payload"])["data"]
    request = dispatch["request"]
    if (row["payload_hash"] != hashlib.sha256(_canonical(dispatch)).hexdigest()
            or ack["payload_hash"] != hashlib.sha256(_canonical(binding)).hexdigest()
            or intent["repository_id"] != token.repository_id or intent["run_id"] != token.run_id
            or intent["activity_id"] != handle.activity_id or not intent["permit_id"]
            or intent["generation"] != token.generation or activity["generation"] != token.generation
            or intent["state"] not in {"released_to_execute", "completed_succeeded"}
            or intent["acknowledgement_id"] != binding["acknowledgement_id"]
            or intent["generation"] != binding["issuing_generation"]
            or binding["native"] != asdict(handle.identity)
            or binding["monitor"] != asdict(handle.monitor_identity)
            or binding["transport"] != "supervisor-monitor-v1"
            or binding["intent_id"] != handle.intent_id
            or request != handle.replay_material
            or request["transport"] != "supervisor-monitor-v1"
            or action["action"] != "final_review"
            or action["logical_key"] != row["idempotency_key"].removeprefix("dispatch-request:")
            or action["input_hash"] != hashlib.sha256(_canonical(request)).hexdigest()
            or {"host_id": intent["child_host_id"], "boot_id": intent["child_boot_id"],
                "pid": intent["child_pid"], "start_token": intent["child_start_token"]} != asdict(handle.identity)):
        raise SupervisorRefused("NATIVE_REVIEW_COMPLETION_INVALID")
    return dispatch


def completion(supervisor, handle, result, stream):
    """Validate one original completed transport; return proof and charged usage."""
    replay = (handle.replay_material or {}).get("native_review_material", {})
    receipt = {"schema": "ffs.native-review-invocation/v1", "status": "uncertain",
               "qualification_scope": "one-completed-review",
               "material_sha256": replay.get("material_sha256"), "exit_code": result["returncode"],
               "telemetry_sha256": hashlib.sha256(stream).hexdigest()}
    try:
        dispatch = _retained_dispatch(supervisor, handle)
        request = dispatch["request"]
        if result.get("auth_revoked") is not True or type(result["returncode"]) is not int or result["returncode"] != 0:
            raise SupervisorRefused("NATIVE_REVIEW_COMPLETION_INVALID")
        material = read_native_review_launch(Path(replay["material_locator"]),
                                            expected_material_sha256=replay["material_sha256"])
        expected = {**material.replay_binding(), "material_locator": str(material_locator(supervisor, material)),
                    "acceptance_hash": request["contract_hash"], "candidate_hash": dispatch["managed_input_sha256"]}
        if (replay != expected or dispatch["runtime_receipt_sha256"] != material.runtime_receipt_sha256
                or request["runtime_identity"] != material.runtime_tuple_hash
                or request["workspace"] != material.native.workspace
                or request["command_sha256"] != hashlib.sha256(_canonical(material.native.argv)).hexdigest()):
            raise SupervisorRefused("NATIVE_REVIEW_COMPLETION_INVALID")
        _sealed_binding(supervisor, handle.activity_id, material, request["contract_hash"],
                        dispatch["managed_input_sha256"])
        observe = verify_codex_review_evidence if material.native.host == "codex" else verify_claude_review_evidence
        observation = observe(material.native, stream, exit_code=result["returncode"])
        usage = dict(observation.telemetry.token_usage)
        charged = (usage["input_tokens"] + usage["cache_write_input_tokens"] + usage["output_tokens"]
                   + usage["reasoning_output_tokens"] if material.native.host == "codex"
                   else usage["input_tokens"] + usage["cache_creation_input_tokens"] + usage["output_tokens"])
        receipt.update(
            status="complete", host=material.native.host, runtime_receipt_sha256=material.runtime_receipt_sha256,
            runtime_tuple_hash=material.runtime_tuple_hash, ordinary_runtime_sha256=material.ordinary_runtime_sha256,
            acceptance_hash=request["contract_hash"], candidate_hash=dispatch["managed_input_sha256"],
            prompt_sha256=material.artifact_prompt_sha256,
            output_contract_sha256=material.artifact.replay_binding()["output_contract_sha256"],
            review_context_sha256=material.artifact.replay_binding()["review_context_sha256"],
            credential_revoked=True, token_usage=usage, observation=asdict(observation),
        )
        return receipt, charged
    except (SupervisorRefused, OwnershipRefused, NativeReviewTransportRefused, NativeReviewRuntimeRefused,
            TypeError, KeyError, ValueError, OSError, OverflowError):
        return receipt, None
