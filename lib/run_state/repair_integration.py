"""Isolated ordinary-repair patch: its retained record and its journaled integration (R-direct).

A repair never touches the shared frontend candidate while it runs.  ``retain_repair_record`` measures the repair
child's isolated workspace against its immutable input, harvests the exact patch and retains one authority event
(``ffs.frontend-repair/v1``) under the child activity; there are no check results in it, because the lifecycle
re-runs the frozen checks on the repaired candidate.  ``integrate_repair`` then merges that patch through the
fenced workspace journal under ``repair:<action_id>`` (the recovery winner's path, ``integrate_retained_patch``),
records the integration against the child's role-``execution`` receipt and binds the new candidate with the
candidate it was repaired from as its parent.  Every step is keyed and idempotent.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .ownership import assert_owner
from .recovery_controller import RecoveryRefused

REPAIR_SCHEMA = "ffs.frontend-repair/v1"
_CODE = "REPAIR"


def repair_record_key(action_id: str) -> str:
    return "repair-record:" + action_id


def repair_journal_key(action_id: str) -> str:
    return "repair:" + action_id


def _repair_rows_tx(tx, token, *, action_id, activity_id, acceptance_hash):
    """Resolve the issued repair action, its launch intent and the repair child under one read."""
    action = tx.execute("SELECT * FROM authority_policy_actions WHERE id=? AND repository_id=? AND run_id=?",
                        (action_id, token.repository_id, token.run_id)).fetchone()
    if action is None or action["action"] != "repair" or action["state"] not in {"dispatched", "completed_valid"}:
        raise RecoveryRefused("REPAIR_ACTION_INVALID")
    intent = tx.execute("SELECT p.intent_id FROM authority_policy_action_attempts p JOIN authority_launch_intents i "
                        "ON i.id=p.intent_id WHERE p.action_id=? AND i.activity_id=? ORDER BY p.created_at DESC",
                        (action_id, activity_id)).fetchone()
    child = tx.execute("SELECT b.*,a.runtime_tuple_hash,a.generation AS activity_generation "
                       "FROM authority_child_bindings b JOIN authority_activities a ON a.id=b.activity_id "
                       "WHERE b.activity_id=? AND a.repository_id=? AND a.run_id=?",
                       (activity_id, token.repository_id, token.run_id)).fetchone()
    if (intent is None or child is None or child["role"] != "worker"
            or child["contract_hash"] != acceptance_hash or not child["runtime_tuple_hash"]
            or child["activity_generation"] != token.generation):
        raise RecoveryRefused("REPAIR_BINDING_INVALID")
    return intent["intent_id"], child


def retain_repair_record(store, token, *, supervisor, action_id, activity_id, workspace, expected_input_digest) -> bool:
    """Harvest one settled repair child's patch and retain its record; ``False`` when it changed nothing.

    Replay re-verifies the retained record against its authority rows and physical evidence.
    """
    from .recovery_trial_checks import harvest_isolated_patch
    sealed = store.get_sealed_acceptance(repository_id=token.repository_id, run_id=token.run_id)
    if sealed is None:
        raise RecoveryRefused("REPAIR_STAGE_INVALID")
    key, acceptance_hash = repair_record_key(action_id), sealed.acceptance_hash
    with store.read_transaction() as tx:
        issuing_intent_id, child = _repair_rows_tx(tx, token, action_id=action_id, activity_id=activity_id,
                                                   acceptance_hash=acceptance_hash)
        retained = tx.execute("SELECT 1 FROM authority_event_keys WHERE activity_id=? AND idempotency_key=?",
                              (activity_id, key)).fetchone()
    if child["workspace_binding"] != workspace:
        raise RecoveryRefused("REPAIR_WORKSPACE_INVALID")
    if retained is not None:
        _verify_record(store, token, activity_id, action_id, issuing_intent_id, acceptance_hash, expected_input_digest)
        return True
    try:
        ready, _measured, patch = harvest_isolated_patch(
            store, token, supervisor=supervisor, activity_id=activity_id, runtime_identity=child["runtime_tuple_hash"],
            preparation_id=child["workspace_preparation_id"], expected_input_digest=expected_input_digest,
            code=_CODE, evidence_directory="repairs")
    except RecoveryRefused as error:
        if error.code == "REPAIR_PATCH_EMPTY":
            return False
        raise
    payload = {
        "schema": REPAIR_SCHEMA, "action_id": action_id, "issuing_intent_id": issuing_intent_id,
        "acceptance_hash": acceptance_hash, "input_digest": ready.input_digest, "workspace_preparation_id": ready.id,
        "base_commit": ready.base_commit,
        "patch": {"locator": str(patch.evidence_path), "sha256": patch.sha256,
                  "size": len(patch.patch.encode("utf-8")), "changed_files": list(patch.changed_files)},
    }
    with store.transaction() as tx:
        assert_owner(tx, token)
        _repair_rows_tx(tx, token, action_id=action_id, activity_id=activity_id, acceptance_hash=acceptance_hash)
        store._record_event_once_tx(tx, token, activity_id, key, payload)
    return True


def _retained_record(tx, activity_id, key):
    row = tx.execute("SELECT k.payload_hash,e.payload FROM authority_event_keys k JOIN control_events e "
                     "ON e.id=k.event_id WHERE k.activity_id=? AND k.idempotency_key=?", (activity_id, key)).fetchone()
    return None if row is None else (json.loads(row["payload"])["data"], row["payload_hash"])


def _verify_record(store, token, activity_id, action_id, issuing_intent_id, acceptance_hash, expected_input_digest):
    with store.read_transaction() as tx:
        retained = _retained_record(tx, activity_id, repair_record_key(action_id))
    payload = None if retained is None else retained[0]
    if (not isinstance(payload, dict) or payload.get("schema") != REPAIR_SCHEMA or payload.get("action_id") != action_id
            or payload.get("issuing_intent_id") != issuing_intent_id or payload.get("acceptance_hash") != acceptance_hash
            or payload.get("input_digest") != expected_input_digest or not isinstance(payload.get("patch"), dict)):
        raise RecoveryRefused("REPAIR_RECORD_INVALID")
    try:
        store._verified_evidence({"locator": payload["patch"]["locator"], "sha256": payload["patch"]["sha256"]})
    except Exception as error:
        raise RecoveryRefused("REPAIR_EVIDENCE_INVALID") from error


def integrate_repair(store, token, *, supervisor, action_id, activity_id, receipt_hash, workspace,
                     acceptance_hash) -> str:
    """Journal, apply and bind one repair's retained patch; returns the repaired candidate hash.

    The candidate is bound through the child's role-``execution`` receipt with the candidate the repair ran on as
    its parent (the chain proof names it).  A replay re-applies nothing: the journal reconstructs its own outcome.
    """
    from .recovery_integration import integrate_retained_patch
    from .supervisor import _read_evidence
    with store.read_transaction() as tx:
        retained = _retained_record(tx, activity_id, repair_record_key(action_id))
    if retained is None:
        raise RecoveryRefused("REPAIR_RECORD_INVALID")
    record, record_hash = retained
    patch = _read_evidence(Path(record["patch"]["locator"]))
    if hashlib.sha256(patch).hexdigest() != record["patch"]["sha256"]:
        raise RecoveryRefused("REPAIR_BINDING_INVALID")
    output, evidence = integrate_retained_patch(
        store, token, evidence_root=supervisor.evidence_root, key=repair_journal_key(action_id),
        activity_id=activity_id, record=record, record_hash=record_hash, workspace=workspace, patch=patch,
        hash_field="repair_record_sha256")
    store.record_frontend_integration(token, receipt_hash=receipt_hash, candidate_hash=output,
                                      integration_evidence=evidence, no_commit_evidence=None)
    store.bind_frontend_candidate(token, acceptance_hash=acceptance_hash, candidate_hash=output,
                                  receipt_hash=receipt_hash)
    return output
