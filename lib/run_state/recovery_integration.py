"""Integrate one verified recovery winner into the shared frontend candidate.

The retained trial patch is applied through the existing fenced workspace
journal under a second key family, ``recovery-trial:<trial_action_id>``; its
authority is the trial's check record, never a fabricated GSD wave.  The new
candidate is bound through the trial's own recovery receipt and the saved
stage resumes with only the unfinished obligations of the handback packet.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .ownership import assert_owner
from .recovery_controller import RecoveryDecision, RecoveryRefused

CONTINUATION_SCHEMA = "ffs.frontend-recovery-continuation-result/v1"


def recovery_journal_key(trial_action_id: str) -> str:
    return "recovery-trial:" + trial_action_id


def _retained(tx, activity_id, key):
    row = tx.execute("SELECT k.payload_hash,e.payload FROM authority_event_keys k JOIN control_events e "
                     "ON e.id=k.event_id WHERE k.activity_id=? AND k.idempotency_key=?", (activity_id, key)).fetchone()
    return None if row is None else (json.loads(row["payload"])["data"], row["payload_hash"])


def integrate_recovery_winner(store, token, *, supervisor, decision, workspace) -> dict:
    """Journal, apply and bind ``decision.winner``; replay returns the retained continuation."""
    from run_context import workspace_effect_lock
    from .recovery_trial_checks import trial_checks_key
    from .run_policy import productive_work
    from .supervisor import Supervisor, _read_evidence

    if (not isinstance(supervisor, Supervisor) or supervisor.store is not store or supervisor.token != token):
        raise RecoveryRefused("RECOVERY_INTEGRATION_SUPERVISOR_REQUIRED")
    if not isinstance(decision, RecoveryDecision) or decision.winner is None or not decision.winner.get("passed"):
        raise RecoveryRefused("RECOVERY_WINNER_REQUIRED")
    winner, workspace = decision.winner, Path(workspace)
    key = recovery_journal_key(winner["action_id"])
    state = store.get_frontend_policy_state(repository_id=token.repository_id, run_id=token.run_id)
    if state is None:
        raise RecoveryRefused("RECOVERY_INTEGRATION_STAGE_INVALID")
    continued = state.decision_json if isinstance(state.decision_json, dict) else {}
    if (state.stage == decision.continuation and continued.get("schema") == CONTINUATION_SCHEMA
            and continued.get("winner_action_id") == winner["action_id"]
            and continued.get("candidate_hash") == state.candidate_hash):
        return continued
    if state.stage != "RECOVER" or continued.get("saved_stage") != decision.continuation:
        raise RecoveryRefused("RECOVERY_INTEGRATION_STAGE_INVALID")
    with store.read_transaction() as tx:
        record = _retained(tx, winner["activity_id"], trial_checks_key(winner["action_id"]))
        rows = tx.execute("SELECT preparation_id,common_dir FROM context_workspaces WHERE repository_id=? AND run_id=? "
                          "AND path=? AND state='ready'", (token.repository_id, token.run_id, str(workspace))).fetchall()
    if record is None or len(rows) != 1:
        raise RecoveryRefused("RECOVERY_INTEGRATION_BINDING_INVALID")
    record, record_hash = record
    patch = _read_evidence(Path(record["patch"]["locator"]))
    if (record["patch"]["sha256"] != winner["patch_sha256"] or hashlib.sha256(patch).hexdigest() != winner["patch_sha256"]
            or record["trial_candidate_hash"] != winner["trial_candidate_hash"]
            or record["input_digest"] != continued.get("candidate_hash")):
        raise RecoveryRefused("RECOVERY_INTEGRATION_BINDING_INVALID")
    results = [{"status": "complete", "patch": patch.decode("utf-8"),
                "changed_files": list(record["patch"]["changed_files"])}]
    with (productive_work(store, token, kind="integration"),
          workspace_effect_lock(Path(rows[0]["common_dir"]), repository_id=token.repository_id,
                                run_id=token.run_id, preparation_id=rows[0]["preparation_id"])):
        output, evidence = _apply(store, token, supervisor.evidence_root, key, winner["activity_id"], workspace,
                                  record, record_hash, results)
    store.record_frontend_integration(token, receipt_hash=winner["receipt_hash"], candidate_hash=output,
                                      integration_evidence=evidence, no_commit_evidence=None)
    store.bind_frontend_candidate(token, acceptance_hash=state.acceptance_hash, candidate_hash=output,
                                  receipt_hash=winner["receipt_hash"])
    # Only what the handback left unfinished continues; consumed attempts are never re-granted.
    continuation = {
        "schema": CONTINUATION_SCHEMA, "cycle_action_id": decision.cycle.action_id,
        "winner_action_id": winner["action_id"], "winner_receipt_hash": winner["receipt_hash"],
        "parent_candidate_hash": record["input_digest"], "candidate_hash": output,
        "saved_stage": decision.continuation, "remaining_obligation_ids": list(continued["failed_criteria"]),
        "consumed_attempts": list(continued["consumed_attempts"]),
    }
    store.transition_frontend_policy(token, expected_stage="RECOVER", new_stage=decision.continuation,
                                     decision=continuation)
    return continuation


def _apply(store, token, evidence_root, key, activity_id, workspace, record, record_hash, results):
    """The wave consumer's journal sequence for one retained patch; caller holds the effect lock."""
    from .integration_journal import quarantine, read_intent
    from .supervisor import _read_evidence
    from .wave_execution import (
        _canonical_integration, _current_material, _head, _material_record, _write_integration_evidence,
        capture_integration_candidate, integrate_wave_patches, prepare_integration_material,
    )
    base = record["base_commit"]
    journal = read_intent(store, token, wave_key=key)
    if journal is None:
        intent = prepare_integration_material(workspace, base, results, evidence_root)
    elif journal["state"] == "quarantined":
        raise RecoveryRefused("RECOVERY_INTEGRATION_RECONCILIATION_REQUIRED")
    else:
        contract = json.loads(journal["contract_json"])
        intent = {"before": contract["before"], "expected_after": contract["expected_after"]}

    def observe():
        return {path: _material_record(_current_material(workspace, path)) for path in intent["before"]}

    applied = journal is not None and observe() == intent["expected_after"]
    if _head(workspace) != base or (observe() != intent["before"] and not applied):
        if journal is not None:
            quarantine(store, token, wave_key=key, reason="mixed-or-changed-material")
        raise RecoveryRefused("RECOVERY_INTEGRATION_RECONCILIATION_REQUIRED")
    with store.transaction() as tx:
        assert_owner(tx, token)
        store._record_event_once_tx(tx, token, activity_id, key + ":prepared", {
            "plans": [{"activity_id": activity_id, "workspace_preparation_id": record["workspace_preparation_id"]}],
            "input_digest": record["input_digest"], "trial_checks_sha256": record_hash})
        integrated = _retained(tx, activity_id, key + ":integrated")
        journal = store.register_integration_intent_tx(
            tx, token, key, str(workspace), intent["before"], intent["expected_after"],
            {"initial_head": base, "trial_action_id": record["trial_action_id"],
             "patch_sha256": record["patch"]["sha256"]})
        store.mark_workspace_integration_pending_tx(tx, token, journal, str(workspace))
    if integrated is not None:
        evidence, material = integrated[0]["evidence"], integrated[0]["material"]
        if json.loads(_read_evidence(Path(evidence["locator"]))) != material:
            raise RecoveryRefused("RECOVERY_INTEGRATION_EVIDENCE_INVALID")
    elif applied:
        # Crash after git apply, before the record: re-derive the same content-addressed evidence.
        material = {"schema": "ffs.wave-integration/v1", "workspace": str(workspace), "initial_head": base,
                    "patch_sha256": record["patch"]["sha256"], "changed_files": sorted(intent["before"]),
                    "before": intent["before"], "after": intent["expected_after"]}
        path, digest = _write_integration_evidence(evidence_root, _canonical_integration(material))
        evidence = {"locator": str(path), "sha256": digest}
    else:
        integration = integrate_wave_patches(workspace, base, results, evidence_root)
        evidence = {"locator": integration["locator"], "sha256": integration["sha256"]}
        material = integration["material"]
    if _head(workspace) != base or observe() != intent["expected_after"]:
        quarantine(store, token, wave_key=key, reason="post-apply-material-changed")
        raise RecoveryRefused("RECOVERY_INTEGRATION_RECONCILIATION_REQUIRED")
    output = capture_integration_candidate(store, token, journal, evidence_root)
    with store.transaction() as tx:
        assert_owner(tx, token)
        store._record_event_once_tx(tx, token, activity_id, key + ":candidate-output", output)
        store.mark_integration_applied_tx(tx, token, journal, intent["expected_after"])
        store._record_event_once_tx(tx, token, activity_id, key + ":integrated",
                                    {"event_id": journal["event_id"], "evidence": evidence, "material": material})
        store.publish_integration_tx(tx, token, journal, material["after"])
    return output["output_digest"], evidence
