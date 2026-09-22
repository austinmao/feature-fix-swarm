"""Stage-resumable frontend lifecycle consumer over the existing authority.

This driver owns no launcher and no authority of its own.  It reads the durable
frontend stage and sequences the existing components: mapped checks
(``FrontendPolicyController.run_mapped_checks``), the bounded handback, the
journaled recovery-winner integration and the DONE completion gate.  Every
process still crosses ``Supervisor``; producers supply the host-specific work
(execution, ordinary repair, final review, recovery cycle) and must reserve
their own named policy actions.  Re-entering after a crash resumes from the
retained stage; nothing here resets an allowance.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .frontend_policy import FrontendPolicyController, FrontendPolicyRefused
from .ownership import OwnershipRefused
from .run_policy import action_limit

TERMINAL_STAGES = frozenset({"DONE", "NEEDS_DECISION", "CAPABILITY_FAILURE", "CANCELLED"})
BLOCKED_SCHEMA = "ffs.frontend-lifecycle-blocked/v1"


@dataclass(frozen=True)
class LifecycleProducers:
    """Host-specific stage work.  Each callable routes its children through Supervisor."""

    execute: Callable            # (frozen) -> None; integrates and binds the executed candidate
    final_review: Callable       # (frozen) -> None; record_final_review, SupervisorRefused on a failed review
    recover: Callable            # (handback_packet) -> RecoveryDecision
    repair: Callable | None = None   # (frozen, failed_criteria) -> None; reserves one ``repair`` action
    settle: Callable | None = None   # () -> None; terminal-transition owned activities before DONE


def _used(store, token, action: str) -> list[str]:
    with store.read_transaction() as tx:
        return [row[0] for row in tx.execute(
            "SELECT id FROM authority_policy_actions WHERE repository_id=? AND run_id=? AND action=? "
            "AND state<>'cancelled' ORDER BY created_at,id", (token.repository_id, token.run_id, action))]


def _review_recorded(store, token, acceptance_hash: str) -> bool:
    """A durable review receipt (passed or refused) is the only proof the grant was spent.

    A reserved action whose launch was interrupted before its receipt must be
    re-entered by the producer, which resumes the retained intent rather than
    reserving a second broad review.
    """
    with store.read_transaction() as tx:
        return tx.execute(
            "SELECT 1 FROM authority_acceptance_receipts WHERE repository_id=? AND run_id=? AND acceptance_hash=? "
            "AND json_extract(receipt_json,'$.role')='review'",
            (token.repository_id, token.run_id, acceptance_hash)).fetchone() is not None


def _failed_criteria(sealed, checks: dict) -> list[str]:
    return sorted(criterion["id"] for criterion in sealed.material["criteria"]
                  if any(checks.get(check["id"], {}).get("status") != "passed" for check in criterion["checks"]))


def drive_frontend_lifecycle(store, token, *, supervisor, controller, workspace: str,
                             parent_activity_id: str, producers: LifecycleProducers,
                             max_steps: int = 32) -> str:
    """Advance the sealed run to a terminal stage; returns that stage."""
    if not isinstance(controller, FrontendPolicyController) or not isinstance(producers, LifecycleProducers):
        raise FrontendPolicyRefused("FRONTEND_LIFECYCLE_INPUT_INVALID")
    from .recovery_integration import integrate_recovery_winner
    from .candidate_chain import resolve_current_frontend_candidate
    from .supervisor import SupervisorRefused

    def state():
        return store.get_frontend_policy_state(repository_id=token.repository_id, run_id=token.run_id)

    def move(expected, new, decision=None):
        store.transition_frontend_policy(token, expected_stage=expected, new_stage=new, decision=decision)

    def consumed() -> list[str]:
        # RecoveryController refuses a FINAL_REVIEW handback that does not name the spent review grant.
        return _used(store, token, "repair") + (["final_review"] if _used(store, token, "final_review") else [])

    def checked(stage) -> bool:
        """Run frozen checks on the current candidate; hand back on failure."""
        candidate = resolve_current_frontend_candidate(store, token)
        checks = controller.run_mapped_checks(workspace=workspace if candidate is None else candidate.workspace,
            supervisor=supervisor, parent_activity_id=parent_activity_id if candidate is None else candidate.parent_activity_id)
        failed = _failed_criteria(sealed, checks)
        if not failed:
            return True
        repairs = _used(store, token, "repair")
        if producers.repair is not None and len(repairs) < action_limit("repair", budget.tier):
            producers.repair(controller.sealed(), failed)
            # One issued repair consumes one grant regardless of outcome; an
            # uncharged producer would make this loop unbounded.
            if len(_used(store, token, "repair")) <= len(repairs):
                raise FrontendPolicyRefused("FRONTEND_REPAIR_UNCHARGED")
            return False
        controller.handback(saved_stage=stage, failed_criteria=failed, consumed_attempts=consumed(), choices=[])
        return False

    sealed = store.get_sealed_acceptance(repository_id=token.repository_id, run_id=token.run_id)
    budget = store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id)
    if sealed is None or budget is None or state() is None:
        raise FrontendPolicyRefused("ACCEPTANCE_SEAL_REQUIRED")
    for _step in range(max_steps):
        current = state()
        stage = current.stage
        if stage in TERMINAL_STAGES:
            return stage
        if stage == "SEALED":
            move("SEALED", "EXECUTE")
        elif stage == "EXECUTE":
            # Execution runs once: an advanced candidate, a retained recovery
            # continuation or an issued repair proves it already happened.
            if (current.candidate_hash == sealed.material["candidate_hash"] and current.decision_json is None
                    and not _used(store, token, "repair")):
                producers.execute(controller.sealed())
            if checked("EXECUTE"):
                move("EXECUTE", "FINAL_REVIEW")
        elif stage == "FINAL_REVIEW":
            if not checked("FINAL_REVIEW"):
                continue
            if not _review_recorded(store, token, sealed.acceptance_hash):
                try:
                    producers.final_review(controller.sealed())
                except SupervisorRefused as error:
                    if not error.code.startswith("FINAL_REVIEW_"):
                        raise
                    # The refusal does not name criteria; every sealed criterion stays an obligation.
                    controller.handback(saved_stage="FINAL_REVIEW", consumed_attempts=consumed(),
                                        failed_criteria=[item["id"] for item in sealed.material["criteria"]], choices=[])
                    continue
            if producers.settle is not None:
                producers.settle()
            try:
                move("FINAL_REVIEW", "DONE")
            except OwnershipRefused as error:
                if error.code != "FRONTEND_COMPLETION_REVIEW_REQUIRED":
                    raise
                # ponytail: the single final-review grant is consumed and the DONE gate still demands a review
                # receipt for the post-recovery candidate; fail closed until the gate accepts the design's
                # deterministic post-repair acceptance (checks + evidence, no second broad review).
                move("FINAL_REVIEW", "NEEDS_DECISION", {"schema": BLOCKED_SCHEMA, "code": error.code,
                                                        "candidate_hash": state().candidate_hash})
        elif stage == "RECOVER":
            decision = producers.recover(current.decision_json)
            if decision.winner is not None:
                candidate = resolve_current_frontend_candidate(store, token)
                integrate_recovery_winner(store, token, supervisor=supervisor, decision=decision,
                                          workspace=workspace if candidate is None else candidate.workspace)
            else:
                move("RECOVER", "NEEDS_DECISION", {
                    "schema": BLOCKED_SCHEMA, "code": "RECOVERY_CYCLE_WITHOUT_WINNER",
                    "cycle_action_id": decision.cycle.action_id,
                    "diagnosis_receipt_hash": decision.diagnosis_receipt_hash})
        else:
            raise FrontendPolicyRefused("FRONTEND_LIFECYCLE_STAGE_UNSUPPORTED")
    raise FrontendPolicyRefused("FRONTEND_LIFECYCLE_STEP_LIMIT")
