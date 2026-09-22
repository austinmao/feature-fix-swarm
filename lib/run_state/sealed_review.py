"""Bind a completed independent review to the frozen acceptance contract.

This is the receipt boundary, not a host launcher or a claim of host
qualification. Native model identity remains in the supervisor's host receipt.
"""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from .supervisor import SupervisorRefused, _read_evidence
from .workspace import _from_row
from .final_review_context import final_review_input_context, validate_native_review_evidence


def final_review_output_contract(sealed, *, candidate_hash: str) -> dict:
    """Describe the sealed response grammar; this is not acceptance evidence."""
    from .run_policy import RunPolicyRefused, validate_draft_material
    from .state import SealedAcceptance
    if type(sealed) is not SealedAcceptance:
        raise ValueError("sealed acceptance is required")
    for digest in (candidate_hash, sealed.acceptance_hash):
        if (not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            raise ValueError("review contract digest is malformed")
    try:
        material = validate_draft_material(sealed.material).material
    except RunPolicyRefused as error:
        raise ValueError("sealed acceptance material is malformed") from error
    dimensions = material.get("required_review_dimensions")
    if dimensions is None:
        raise ValueError("sealed review dimensions are required")
    fixed = {"schema": "ffs.sealed-final-review/v1", "acceptance_hash": sealed.acceptance_hash,
             "candidate_hash": candidate_hash, "review_dimensions": dimensions}
    return {
        "required_fields": ["schema", "acceptance_hash", "candidate_hash", "review_dimensions",
                            "criteria", "findings"],
        "additional_fields": False,
        "fixed_fields": fixed,
        "criterion_result": {
            "required_fields": ["status", "evidence"], "additional_fields": False,
            "status_values": ["passed", "failed"], "evidence_type": "array",
        },
        "criteria": {
            item["id"]: {
                "required_evidence_ids_for_pass": sorted(
                    rule["id"] for rule in item["evidence_rules"] if rule["required"]),
            } for item in material["criteria"]
        },
        "criteria_membership": "Exactly the listed criterion IDs; each value follows criterion_result; no omissions or extra IDs.",
        "evidence": {
            "required_fields": ["id", "locator", "sha256"], "additional_fields": False,
            "id": "Supplied rule/check/invariant ID, never an artifact name; unique across criterion evidence references.",
            "locator": "Absolute path of the supplied retained evidence file.",
            "sha256": "Exact lowercase SHA-256 of that supplied evidence file.",
        },
        "findings": {
            "type": "array", "empty_allowed": True,
            "required_fields": ["acceptance_hash", "candidate_hash", "runtime_hash",
                                "criterion_ids", "check_ids", "invariant_ids", "evidence"],
            "additional_fields": False,
            "fixed_fields": {"acceptance_hash": sealed.acceptance_hash, "candidate_hash": candidate_hash,
                             "runtime_hash": material["runtime"]["effective_hash"]},
            "allowed_criterion_ids": sorted(item["id"] for item in material["criteria"]),
            "allowed_check_ids": sorted(check["id"] for item in material["criteria"] for check in item["checks"]),
            "allowed_invariant_ids": sorted(item["id"] for item in material["global_invariants"]),
            "identifier_fields_type": "Arrays of unique identifiers; empty arrays are allowed.",
            "evidence": "Nonempty array grounded in supplied checks/rules/invariants; source citations follow review_context.source_evidence_policy.",
        },
        "instructions": [
            "Return exactly the required top-level fields and exact fixed values.",
            "Evaluate every criterion; use failed when evidence is missing or the criterion is not met.",
            "Use only supplied evidence references; do not invent locators, hashes or observations.",
            "Report supported findings in the exact finding shape; return an empty findings array when none.",
            "This response is subject to independent evidence, process and authority validation.",
        ],
    }


def _unique_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate review field")
        value[key] = item
    return value


def _review_output(result: dict, raw: bytes) -> dict:
    try:
        host = result.get("host_receipt")
        if host is None:
            value = json.loads(raw, object_pairs_hook=_unique_keys)
        else:
            if host.get("status") != "complete":
                raise ValueError("incomplete host receipt")
            schema = host.get("schema")
            if schema == "ffs.native-review-invocation/v1":
                if (host.get("host") not in {"codex", "claude"}
                        or host.get("qualification_scope") != "one-completed-review"
                        or not isinstance(host.get("observation"), dict)):
                    raise ValueError("incomplete native review receipt")
                schema = "ffs." + host["host"] + "-invocation-receipt/v1"
            records = [json.loads(line, object_pairs_hook=_unique_keys) for line in raw.splitlines() if line.strip()]
            if schema == "ffs.codex-invocation-receipt/v1":
                texts = [record["item"]["text"] for record in records
                         if record.get("type") == "item.completed"
                         and record.get("item", {}).get("type") == "agent_message"]
            elif schema == "ffs.claude-invocation-receipt/v1":
                texts = [record["result"] for record in records if record.get("type") == "result"]
            else:
                raise ValueError("unsupported host receipt")
            if len(texts) != 1:
                raise ValueError("ambiguous review output")
            value = json.loads(texts[0], object_pairs_hook=_unique_keys)
        if not isinstance(value, dict):
            raise ValueError("invalid review output")
        return value
    except (KeyError, TypeError, ValueError, UnicodeError) as error:
        raise SupervisorRefused("FINAL_REVIEW_OUTPUT_INVALID") from error


def record_final_review(supervisor, handle, *, acceptance_hash: str):
    """Record only a complete, dimensionally complete, same-candidate review."""
    if (supervisor._handles.get(handle.intent_id) is not handle or not handle.recorded
            or not isinstance(handle.result, dict) or handle.result.get("returncode") != 0):
        raise SupervisorRefused("FINAL_REVIEW_RESULT_REQUIRED")
    store, token = supervisor.store, supervisor.token
    sealed = store.get_sealed_acceptance(repository_id=token.repository_id, run_id=token.run_id)
    if sealed is None or sealed.acceptance_hash != acceptance_hash:
        raise SupervisorRefused("ACCEPTANCE_SEAL_REQUIRED")
    with store.read_transaction() as tx:
        child = tx.execute("SELECT * FROM authority_child_bindings WHERE activity_id=?", (handle.activity_id,)).fetchone()
        intent = tx.execute("SELECT * FROM authority_launch_intents WHERE id=?", (handle.intent_id,)).fetchone()
        action = tx.execute(
            "SELECT a.* FROM authority_policy_actions a JOIN authority_policy_action_attempts p ON p.action_id=a.id "
            "WHERE p.intent_id=?", (handle.intent_id,),
        ).fetchone()
        if (child is None or child["role"] != "reviewer" or child["contract_hash"] != acceptance_hash
                or intent is None or intent["completion_status"] != "succeeded"
                or action is None or action["action"] != "final_review"):
            raise SupervisorRefused("FINAL_REVIEW_BINDING_INVALID")
        preparation = _from_row(tx.execute("SELECT * FROM context_workspaces WHERE preparation_id=?",
                                          (child["workspace_preparation_id"],)).fetchone())
    raw = _read_evidence(handle.stdout_path, handle.stream_identities["stdout"])
    if hashlib.sha256(raw).hexdigest() != handle.result["streams"]["stdout"]["sha256"]:
        raise SupervisorRefused("EVIDENCE_CHANGED")
    if "native_review_material" in (handle.replay_material or {}):
        from .native_review_supervision import completion
        proof, _usage = completion(supervisor, handle, handle.result, raw)
        if (proof.get("status") != "complete" or json.dumps(proof, sort_keys=True)
                != json.dumps(handle.result.get("host_receipt"), sort_keys=True)):
            raise SupervisorRefused("FINAL_REVIEW_NATIVE_PROOF_INVALID")
    output = _review_output(handle.result, raw)
    if (set(output) != {"schema", "acceptance_hash", "candidate_hash", "review_dimensions", "criteria", "findings"}
            or output["schema"] != "ffs.sealed-final-review/v1"
            or output["acceptance_hash"] != acceptance_hash or output["candidate_hash"] != child["candidate_hash"]
            or not isinstance(output["criteria"], dict) or not isinstance(output["findings"], list)):
        raise SupervisorRefused("FINAL_REVIEW_OUTPUT_INVALID")
    criteria = {item["id"]: item for item in sealed.material["criteria"]}
    if set(output["criteria"]) != set(criteria):
        raise SupervisorRefused("FINAL_REVIEW_CRITERIA_MISSING")
    if "native_review_material" in (handle.replay_material or {}):
        from .native_review_transport import read_native_review_launch
        replay = handle.replay_material["native_review_material"]
        material = read_native_review_launch(Path(replay["material_locator"]),
                                             expected_material_sha256=replay["material_sha256"])
        context = final_review_input_context(store, token, acceptance_hash=acceptance_hash,
                                            candidate_hash=child["candidate_hash"],
                                            reviewer_activity_id=handle.activity_id,
                                            selected_artifacts=material.artifact.selected_artifacts)
        validate_native_review_evidence(output, context)
    evidence = [{"id": "review-process-result", **handle.result["evidence"]}]
    for identifier, criterion in criteria.items():
        checked = output["criteria"][identifier]
        if (not isinstance(checked, dict) or set(checked) != {"status", "evidence"}
                or checked["status"] not in {"passed", "failed"} or not isinstance(checked["evidence"], list)):
            raise SupervisorRefused("FINAL_REVIEW_CRITERION_FAILED")
        required = {rule["id"] for rule in criterion["evidence_rules"] if rule["required"]}
        if checked["status"] == "passed" and not required.issubset(
                {item.get("id") for item in checked["evidence"] if isinstance(item, dict)}):
            raise SupervisorRefused("FINAL_REVIEW_EVIDENCE_MISSING")
        evidence.extend(checked["evidence"])
    # Reviewer prose cannot invent the completion decision.  Findings must be
    # content-hashed, current-candidate records and are classified by frozen
    # criterion/check/invariant mappings.  A valid follow-up is retained but
    # cannot widen this sealed run; contract/invariant findings block here.
    failure = None
    if output["findings"]:
        from .frontend_policy import FrontendPolicyController, FrontendPolicyRefused
        try:
            controller = FrontendPolicyController(
                store, token, command_mode=sealed.material["command_mode"],
            )
            decisions = controller.classify_findings(output["findings"])
        except FrontendPolicyRefused as error:
            raise SupervisorRefused(error.code) from error
        # Invalid/malformed/inconsistent reviewer output is not a clean review.
        if any(item["classification"] in {"CONTRACT_FAILURE", "INVARIANT_VIOLATION", "INVALID"}
               for item in decisions):
            failure = "FINAL_REVIEW_BLOCKING_FINDING"
    if failure is None and any(value["status"] != "passed" for value in output["criteria"].values()):
        failure = "FINAL_REVIEW_CRITERION_FAILED"
    receipt = {
        "schema": "ffs.run-policy-receipt/v1", "role": "review", "request_key": action["logical_key"],
        "activity_id": handle.activity_id, "intent_id": handle.intent_id,
        "fence_generation": intent["generation"], "acceptance_hash": acceptance_hash,
        "candidate_hash": child["candidate_hash"], "runtime_hash": child["runtime_identity"],
        "workspace_preparation_hash": hashlib.sha256(json.dumps(asdict(preparation), default=str,
                                                               sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        # A well-formed refusal is retained as a failed review receipt: the single grant is spent and the
        # DONE gate's post-repair rule needs durable proof that the broad review happened on an ancestor.
        "evidence": evidence, "completion_status": "failed" if failure else "succeeded",
        "process_identity": asdict(handle.identity),
        "review_dimensions": output["review_dimensions"],
    }
    recorded = store.record_acceptance_receipt(token, acceptance_hash=acceptance_hash, receipt=receipt)
    if failure is not None:
        raise SupervisorRefused(failure)
    return recorded
