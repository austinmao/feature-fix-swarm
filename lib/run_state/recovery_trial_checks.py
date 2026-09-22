"""Isolated recovery-trial checks bound to one exact post-patch candidate.

A trial never touches the shared frontend candidate.  This producer measures
the trial workspace, harvests the exact patch relative to the trial's immutable
input preparation, runs every frozen check through ``run_sealed_checks`` and
retains one authority event under the trial activity.  ``RecoveryController``
only reads those records; nothing here selects a winner or integrates a patch.
"""
from __future__ import annotations

import json

from .ownership import assert_owner
from .recovery_controller import RecoveryRefused

TRIAL_CHECKS_SCHEMA = "ffs.recovery-trial-checks/v1"


def trial_checks_key(trial_action_id: str) -> str:
    return "recovery-trial-checks:" + trial_action_id


def _trial_rows_tx(tx, token, *, cycle_action_id, trial_action_id, trial_activity_id, acceptance_hash):
    """Resolve the issued cycle/trial actions and the trial child under one read."""
    cycle = tx.execute("SELECT * FROM authority_policy_actions WHERE id=? AND repository_id=? AND run_id=?",
                       (cycle_action_id, token.repository_id, token.run_id)).fetchone()
    trial = tx.execute("SELECT * FROM authority_policy_actions WHERE id=? AND repository_id=? AND run_id=?",
                       (trial_action_id, token.repository_id, token.run_id)).fetchone()
    if (cycle is None or trial is None
            or cycle["action"] not in {"recovery_cycle_normal", "recovery_cycle_autonomous"}
            or cycle["state"] not in {"reserved", "dispatched", "completed_valid"}
            or trial["action"] != "recovery_trial" or trial["recovery_cycle"] != cycle["recovery_cycle"]
            or trial["state"] not in {"dispatched", "completed_valid"}):
        raise RecoveryRefused("RECOVERY_TRIAL_ACTION_INVALID")
    intent = tx.execute("SELECT p.intent_id FROM authority_policy_action_attempts p JOIN authority_launch_intents i "
                        "ON i.id=p.intent_id WHERE p.action_id=? AND i.activity_id=? ORDER BY p.created_at DESC",
                        (trial_action_id, trial_activity_id)).fetchone()
    child = tx.execute("SELECT b.*,a.runtime_tuple_hash,a.generation AS activity_generation,a.state AS activity_state "
                       "FROM authority_child_bindings b JOIN authority_activities a ON a.id=b.activity_id "
                       "WHERE b.activity_id=? AND a.repository_id=? AND a.run_id=?",
                       (trial_activity_id, token.repository_id, token.run_id)).fetchone()
    if (intent is None or child is None or child["role"] != "recovery"
            or child["contract_hash"] != acceptance_hash or not child["runtime_tuple_hash"]
            or child["activity_generation"] != token.generation):
        raise RecoveryRefused("RECOVERY_TRIAL_BINDING_INVALID")
    return cycle, trial, intent["intent_id"], child


def run_isolated_trial_checks(store, token, *, supervisor, cycle_action_id, trial_action_id,
                              trial_activity_id, workspace, expected_input_digest) -> dict:
    """Produce bound check evidence for one issued trial; replay returns the retained record."""
    from .frontend_policy import run_sealed_checks
    from .run_policy import productive_work
    from .supervisor import Supervisor
    from .wave_execution import (
        _head, _inventory, _material_entries, capture_prelaunch_snapshot, harvest_scoped_patch,
    )
    from .workspace import inspect_workspace, load_input_snapshot

    if (not isinstance(supervisor, Supervisor) or supervisor.store is not store
            or supervisor.token != token):
        raise RecoveryRefused("RECOVERY_TRIAL_SUPERVISOR_REQUIRED")
    sealed = store.get_sealed_acceptance(repository_id=token.repository_id, run_id=token.run_id)
    state = store.get_frontend_policy_state(repository_id=token.repository_id, run_id=token.run_id)
    if (sealed is None or state is None or state.stage != "RECOVER"
            or state.acceptance_hash != sealed.acceptance_hash):
        raise RecoveryRefused("RECOVERY_TRIAL_STAGE_INVALID")
    acceptance_hash = sealed.acceptance_hash
    key = trial_checks_key(trial_action_id)
    with store.read_transaction() as tx:
        _cycle, _trial, issuing_intent_id, child = _trial_rows_tx(
            tx, token, cycle_action_id=cycle_action_id, trial_action_id=trial_action_id,
            trial_activity_id=trial_activity_id, acceptance_hash=acceptance_hash)
        retained = tx.execute("SELECT e.payload FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id "
                              "WHERE k.activity_id=? AND k.idempotency_key=?", (trial_activity_id, key)).fetchone()
    if child["workspace_binding"] != workspace:
        raise RecoveryRefused("RECOVERY_TRIAL_WORKSPACE_INVALID")
    if retained is not None:
        payload = json.loads(retained["payload"])["data"]
        verify_trial_checks(store, token, payload, cycle_action_id=cycle_action_id,
                            trial_action_id=trial_action_id, trial_activity_id=trial_activity_id,
                            issuing_intent_id=issuing_intent_id, expected_input_digest=expected_input_digest)
        return payload
    ready = inspect_workspace(store, child["workspace_preparation_id"])
    if ready.input_digest != expected_input_digest:
        raise RecoveryRefused("RECOVERY_TRIAL_INPUT_INVALID")
    input_snapshot = load_input_snapshot(store, ready)
    if input_snapshot is None:
        raise RecoveryRefused("RECOVERY_TRIAL_INPUT_INVALID")
    with productive_work(store, token, kind="check"):
        measured = capture_prelaunch_snapshot(store, token, ready, activity_id=trial_activity_id,
                                              runtime_identity=child["runtime_tuple_hash"],
                                              evidence_root=supervisor.evidence_root)
    before = {entry["path"]: entry for entry in input_snapshot.manifest["entries"]}
    after = {entry["path"]: entry for entry in measured.manifest["entries"]}
    changed = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
    modified = [path for path in changed if (ready.path / path).exists()]
    deleted = [path for path in changed if not (ready.path / path).exists()]
    if not changed:
        raise RecoveryRefused("RECOVERY_TRIAL_PATCH_EMPTY")
    with productive_work(store, token, kind="check"):
        patch = harvest_scoped_patch(ready.path, ready.base_commit, modified, deleted,
                                     supervisor.evidence_root / "recovery-trials", baseline_snapshot=input_snapshot)
    entries = tuple(sorted(measured.manifest["entries"], key=lambda item: item["path"]))
    if (not patch.patch or _head(ready.path) != ready.base_commit
            or _material_entries(ready.path, ready.base_commit, _inventory(ready.path, ready.base_commit)) != entries):
        raise RecoveryRefused("RECOVERY_TRIAL_CANDIDATE_CHANGED")
    checks = {check["id"]: check for criterion in sealed.material["criteria"] for check in criterion["checks"]}
    results, _output = run_sealed_checks(
        store, token, supervisor, acceptance_hash=acceptance_hash, candidate_hash=measured.input_digest,
        checks=checks, parent_activity_id=trial_activity_id, runtime_identity=child["runtime_tuple_hash"],
        ready=ready, snapshot=measured, key_prefix="trial-check:" + trial_action_id + ":")
    payload = {
        "schema": TRIAL_CHECKS_SCHEMA, "cycle_action_id": cycle_action_id, "trial_action_id": trial_action_id,
        "issuing_intent_id": issuing_intent_id, "acceptance_hash": acceptance_hash,
        "input_digest": ready.input_digest, "workspace_preparation_id": ready.id, "base_commit": ready.base_commit,
        "trial_candidate_hash": measured.input_digest,
        "patch": {"locator": str(patch.evidence_path), "sha256": patch.sha256,
                  "size": len(patch.patch.encode("utf-8")), "changed_files": list(patch.changed_files)},
        "results": results,
    }
    with store.transaction() as tx:
        assert_owner(tx, token)
        _trial_rows_tx(tx, token, cycle_action_id=cycle_action_id, trial_action_id=trial_action_id,
                       trial_activity_id=trial_activity_id, acceptance_hash=acceptance_hash)
        for result in results:
            store._frontend_check_execution_tx(tx, token, acceptance_hash=acceptance_hash,
                                               candidate_hash=measured.input_digest, check_id=result["check_id"],
                                               status=result["status"], evidence=result["evidence"])
        store._record_event_once_tx(tx, token, trial_activity_id, key, payload)
    return payload


def verify_trial_checks(store, token, payload, *, cycle_action_id, trial_action_id, trial_activity_id,
                        issuing_intent_id, expected_input_digest) -> dict:
    """Re-verify one retained trial record against authority rows and physical evidence."""
    sealed = store.get_sealed_acceptance(repository_id=token.repository_id, run_id=token.run_id)
    if sealed is None:
        raise RecoveryRefused("RECOVERY_TRIAL_STAGE_INVALID")
    required = {check["id"] for criterion in sealed.material["criteria"] for check in criterion["checks"]}
    if (not isinstance(payload, dict) or payload.get("schema") != TRIAL_CHECKS_SCHEMA
            or payload.get("cycle_action_id") != cycle_action_id or payload.get("trial_action_id") != trial_action_id
            or payload.get("issuing_intent_id") != issuing_intent_id
            or payload.get("acceptance_hash") != sealed.acceptance_hash
            or payload.get("input_digest") != expected_input_digest
            or not isinstance(payload.get("results"), list)
            or {item.get("check_id") for item in payload["results"]} != required
            or not isinstance(payload.get("patch"), dict)):
        raise RecoveryRefused("RECOVERY_TRIAL_CHECKS_INVALID")
    with store.read_transaction() as tx:
        _trial_rows_tx(tx, token, cycle_action_id=cycle_action_id, trial_action_id=trial_action_id,
                       trial_activity_id=trial_activity_id, acceptance_hash=sealed.acceptance_hash)
        binding = tx.execute("SELECT workspace_preparation_id FROM authority_child_bindings WHERE activity_id=?",
                             (trial_activity_id,)).fetchone()
        if binding is None or binding["workspace_preparation_id"] != payload["workspace_preparation_id"]:
            raise RecoveryRefused("RECOVERY_TRIAL_CHECKS_INVALID")
        for result in payload["results"]:
            try:
                store._frontend_check_execution_tx(tx, token, acceptance_hash=sealed.acceptance_hash,
                                                   candidate_hash=payload["trial_candidate_hash"],
                                                   check_id=result["check_id"], status=result["status"],
                                                   evidence=result["evidence"])
            except Exception as error:
                raise RecoveryRefused("RECOVERY_TRIAL_CHECK_AUTHORITY_MISSING") from error
    try:
        for result in payload["results"]:
            for item in result["evidence"]:
                store._verified_evidence(item)
        store._verified_evidence({"locator": payload["patch"]["locator"], "sha256": payload["patch"]["sha256"]})
    except Exception as error:
        raise RecoveryRefused("RECOVERY_TRIAL_EVIDENCE_INVALID") from error
    return {"action_id": trial_action_id, "trial_candidate_hash": payload["trial_candidate_hash"],
            "patch_sha256": payload["patch"]["sha256"], "patch_size": payload["patch"]["size"],
            "passed": all(item["status"] == "passed" for item in payload["results"])}
