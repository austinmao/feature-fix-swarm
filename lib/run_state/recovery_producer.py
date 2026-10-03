"""Production recovery (diagnosis and trial) producer for one retained frontend handback.

``produce_recovery`` is the ``LifecycleProducers.recover`` callable.  The
``RecoveryController`` only reads the store, so this module creates, through the
managed host seam and the existing authority, exactly what it reads: one issued
``recovery_cycle_<mode>`` action bound to the current candidate, one succeeded
``diagnosis`` child, and one isolated ``recovery_trial`` child whose checks
``run_isolated_trial_checks`` retains.  Nothing here selects a winner or touches
the shared candidate; ``integrate_recovery_winner`` does that, under its own
journal key.

Ordering is the contract.  The diagnosis child is captured and qualified before
the cycle is reserved, so a host or qualification refusal leaves the handback
retained with no cycle spent; the cycle is reserved with the launch demand of
the whole cycle, so an infeasible cycle refuses before it is spent.  Every step
is keyed and idempotent.  A retained intent follows the final-review rule: one
never acknowledged is settled only by owner-fence reconciliation, and one issued
under an earlier fence is never relaunched, re-verified or recorded here (its
receipts, workspace and trial records bind that fence, and the controller's
generation checks are never weakened): both refuse typed.
"""
from __future__ import annotations

from collections import namedtuple
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import uuid

from .frontend_producers import (
    _current_candidate, _current_reviewer, _release, _retained_action, _reviewer_workspace,
)
from .ownership import OwnershipRefused
from .recovery_controller import (
    ControlStoreRecoveryAuthority, FrozenRecoveryBinding, RecoveryController, RecoveryRefused, RecoveryTrial,
)
from .sealed_review import _host_final_text
from .state import _QUALIFICATION_PROBE_ORDER
from .supervisor import SupervisorRefused, _read_evidence
from .workspace import WorkspaceRefused, _from_row

DIAGNOSIS_PREFIX = "Recovery diagnosis request:"
TRIAL_PREFIX = "Recovery trial request:"
_TEXT_LIMIT = 8 * 1024      # bytes of the diagnosis carried into the trial prompt
_TERMINAL = {"succeeded", "failed", "aborted"}
_Settled = namedtuple("_Settled", "handle receipt action_id workspace")
_Diagnosis = namedtuple("_Diagnosis", "receipt text")


@dataclass(frozen=True)
class _Cycle:
    """Everything one recovery cycle derives once from the handback and the current candidate."""

    store: object
    token: object
    supervisor: object
    controller: object
    seam: object
    timeout_seconds: object
    packet: dict
    sealed: object
    frozen: object
    candidate: object          # the live preparation of the current candidate
    parent_id: str             # the activity the current candidate (and so each child) parents under
    runtime: str               # the runtime identity the candidate was captured under
    binding: FrozenRecoveryBinding
    overhead: int              # launches still to come once the diagnosis child is qualified

    @property
    def tag(self) -> str:
        return self.binding.input_hash[:16]


def produce_recovery(store, token, *, supervisor, controller, seam, parent_activity_id: str, preparation,
                     packet: dict, timeout_seconds=None):
    """Run or resume the one recovery cycle of a RECOVER handback; returns the controller's ``RecoveryDecision``."""
    try:
        cycle = _context(store, token, supervisor, controller, seam, parent_activity_id, preparation, packet,
                         timeout_seconds)
        diagnosis = _diagnose(cycle)
        # ponytail: one trial per cycle (the authority allows RECOVERY_TRIALS_PER_CYCLE = 3, and each extra trial
        # costs a full qualification); fan out to one trial per hypothesis when single-trial winners prove rare.
        trial = _trial(cycle, diagnosis.text)
        # documents: none until a diagnosis demonstrably needs version-bound docs.
        recovery = RecoveryController(packet, cycle.binding, (), ControlStoreRecoveryAuthority(store, token, cycle.binding))
        return recovery.consume(_reserve_cycle(cycle).id, diagnosis_receipt=diagnosis.receipt,
                                trials=[] if trial is None else [RecoveryTrial(trial.action_id, trial.receipt)])
    except RecoveryRefused as error:
        raise SupervisorRefused(error.code) from error


def _context(store, token, supervisor, controller, seam, parent_activity_id, preparation, packet, timeout_seconds):
    frozen = controller.sealed()
    sealed = store.get_sealed_acceptance(repository_id=token.repository_id, run_id=token.run_id)
    if sealed is None or sealed.acceptance_hash != frozen.acceptance_hash:
        raise SupervisorRefused("ACCEPTANCE_SEAL_REQUIRED")
    candidate, parent_id, runtime = _current_candidate(store, token, parent_activity_id=parent_activity_id,
                                                       preparation=preparation)
    # The binding names the CURRENT candidate (its live base and the digest the handback froze), never the
    # session's first input: a task-swarm wave integration advances the candidate before a handback.
    binding = FrozenRecoveryBinding(
        candidate.base_commit, frozen.candidate_hash, frozen.candidate_hash, sealed.material["candidate_hash"],
        frozen.acceptance_hash, sealed.material["runtime"]["effective_hash"], frozen.acceptance_hash)
    checks = sum(len(criterion["checks"]) for criterion in sealed.material["criteria"])
    # The diagnosis launch, the trial's qualification probes and launch, and the trial's checks.
    overhead = 1 + len(_QUALIFICATION_PROBE_ORDER) + 1 + checks
    return _Cycle(store, token, supervisor, controller, seam, timeout_seconds, packet, sealed, frozen, candidate,
                  parent_id, runtime, binding, overhead)


def _reserve_cycle(cycle: _Cycle):
    """The one issued cycle of this binding; a replay returns the same row (and is never re-priced)."""
    store, token = cycle.store, cycle.token
    mode = store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id).recovery_mode
    with store.read_transaction() as tx:
        issued = [dict(row) for row in tx.execute(
            "SELECT recovery_cycle,input_hash FROM authority_policy_actions WHERE repository_id=? AND run_id=? "
            "AND action IN ('recovery_cycle_normal','recovery_cycle_autonomous') AND state<>'cancelled' "
            "ORDER BY recovery_cycle", (token.repository_id, token.run_id))]
    mine = [row for row in issued if row["input_hash"] == cycle.binding.input_hash]
    try:
        return store.reserve_policy_action(
            token, action="recovery_cycle_" + mode, logical_key=f"recovery:{cycle.tag}:cycle",
            input_hash=cycle.binding.input_hash, recovery_cycle=mine[0]["recovery_cycle"] if mine else len(issued) + 1,
            required_launch_overhead=cycle.overhead)
    except OwnershipRefused as error:
        raise SupervisorRefused(error.code) from error


def _diagnose(cycle: _Cycle):
    settled = _child(cycle, "diagnosis", "diagnosis", lambda: _diagnosis_prompt(cycle))
    if settled is None:
        raise SupervisorRefused("RECOVERY_DIAGNOSIS_FAILED")
    text = _final_text(settled.handle).replace("\0", "").strip()
    if not text:
        _end_child(cycle, settled.handle.activity_id, "failed", settled.handle.result["evidence"])
        raise SupervisorRefused("RECOVERY_DIAGNOSIS_FAILED")
    _end_child(cycle, settled.handle.activity_id, "succeeded", settled.handle.result["evidence"])
    return _Diagnosis(settled.receipt, text[:_TEXT_LIMIT])



def _trial(cycle: _Cycle, diagnosis_text: str):
    """One isolated trial; ``None`` when it produced no usable patch (its launch stays charged)."""
    settled = _child(cycle, "trial", "recovery_trial", lambda: _trial_prompt(cycle, diagnosis_text))
    if settled is None:
        return None
    from .recovery_trial_checks import run_isolated_trial_checks, trial_checks_key
    activity_id, evidence = settled.handle.activity_id, settled.handle.result["evidence"]
    if cycle.store.get_activity(activity_id).state in _TERMINAL and not _retained_event(
            cycle.store, activity_id, trial_checks_key(settled.action_id)):
        return None     # a replay of a trial already excluded for an empty patch
    try:
        run_isolated_trial_checks(
            cycle.store, cycle.token, supervisor=cycle.supervisor, cycle_action_id=_reserve_cycle(cycle).id,
            trial_action_id=settled.action_id, trial_activity_id=activity_id, workspace=settled.workspace,
            expected_input_digest=cycle.frozen.candidate_hash)
    except RecoveryRefused as error:
        if error.code != "RECOVERY_TRIAL_PATCH_EMPTY":
            raise
        _end_child(cycle, activity_id, "succeeded", evidence)
        return None
    _end_child(cycle, activity_id, "succeeded", evidence)
    return settled


def _retained_event(store, activity_id: str, key: str) -> bool:
    with store.read_transaction() as tx:
        return tx.execute("SELECT 1 FROM authority_event_keys WHERE activity_id=? AND idempotency_key=?",
                          (activity_id, key)).fetchone() is not None


def _child(cycle: _Cycle, name: str, action: str, make_prompt):
    """Run, or resume, one recovery child; ``None`` when its process did not complete usably."""
    key = f"recovery:{cycle.tag}:{name}"
    launch_key = key + ":launch"
    row, intent = _retained_action(cycle.store, cycle.token, launch_key, action, "RECOVERY_ACTION_AMBIGUOUS")
    if intent is not None:
        if not intent["permit_id"] or intent["child_pid"] is None:
            # Reserved but never acknowledged: only owner-fence reconciliation may settle it.
            raise SupervisorRefused("INTENT_RECONCILIATION_REQUIRED")
        if intent["generation"] != cycle.token.generation:
            # Its completion, receipt and workspace bind the fence that issued them: never relaunched or re-bound.
            raise SupervisorRefused("RECOVERY_RECONCILIATION_REQUIRED")
        return _settle(cycle, cycle.supervisor.resume_monitored(intent["id"]))
    if row is not None:
        # Reserved, never launched.  A re-bind builds new launch material (a new private TMPDIR or session), so
        # this grant can never be reused: release it unspent (it holds no intent and no attempt).
        try:
            cycle.store.cancel_unlaunched_policy_review(cycle.token, action_id=row["id"])
        except OwnershipRefused as error:
            raise SupervisorRefused(error.code) from error
    return _launch(cycle, key, launch_key, action, make_prompt)


def _launch(cycle: _Cycle, key: str, launch_key: str, action: str, make_prompt):
    qualified, ready = _qualified_child(cycle, key)
    ordinal = _reserve_cycle(cycle).recovery_cycle
    request, adapter = cycle.seam.bind(qualified, make_prompt(), ready, cycle.frozen.acceptance_hash, launch_key)
    material = request.codex_material or request.claude_material
    if material is None:
        _release(adapter, material)
        raise SupervisorRefused("HOST_CAPABILITY_UNQUALIFIED")
    launched, handle = False, None
    try:
        try:
            request = cycle.controller.reserve_stage(cycle.supervisor, replace(request, monitor_result=True),
                                                     action=action, recovery_cycle=ordinal)
        except OwnershipRefused as error:
            raise SupervisorRefused(error.code) from error
        launched = True
        handle = cycle.supervisor.launch(request)
        return _settle(cycle, handle)
    finally:
        # Private launch material goes only once nothing can still be running on it.
        if not launched or (handle is not None and handle.recorded):
            _release(adapter, material)


def _qualified_child(cycle: _Cycle, key: str):
    """Capture the current candidate into a fresh recovery child workspace and qualify it (replay-safe)."""
    try:
        child_key, retained_activity, retained_preparation = _current_reviewer(
            cycle.store, cycle.token, parent_activity_id=cycle.parent_id, base_key=key + ":child", action=None,
            reconciliation_code="RECOVERY_RECONCILIATION_REQUIRED")
        ready = _reviewer_workspace(
            cycle.store, cycle.token, cycle.supervisor, preparation=cycle.candidate, parent_activity_id=cycle.parent_id,
            runtime_identity=cycle.runtime, reviewer_key=child_key,
            candidate_hash=cycle.frozen.candidate_hash, retained_preparation_id=retained_preparation,
            retained_activity_id=retained_activity, role="recovery")
    except WorkspaceRefused as error:
        raise SupervisorRefused(error.code) from error
    # The outer orchestrator's prepaid group has ended by now: qualify on this channel-less supervisor.
    qualified = cycle.seam.qualify(retained_activity or str(uuid.uuid4()), ready, child_key, cycle.parent_id,
                                   cycle.frozen.acceptance_hash, "recovery", supervisor=cycle.supervisor)
    return qualified, ready


def _settle(cycle: _Cycle, handle):
    result = cycle.supervisor.finish(handle, timeout=cycle.timeout_seconds)
    if result["returncode"] != 0 or result.get("host_receipt", {}).get("status") != "complete":
        _end_child(cycle, handle.activity_id, "failed", result["evidence"])
        return None
    receipt, action_id, workspace = _record_receipt(cycle, handle)
    return _Settled(handle, receipt, action_id, workspace)


def _record_receipt(cycle: _Cycle, handle):
    """The role-``recovery`` receipt, built from durable rows only and recorded idempotently."""
    store, token, acceptance_hash = cycle.store, cycle.token, cycle.frozen.acceptance_hash
    with store.read_transaction() as tx:
        child = tx.execute("SELECT * FROM authority_child_bindings WHERE activity_id=?", (handle.activity_id,)).fetchone()
        intent = tx.execute("SELECT * FROM authority_launch_intents WHERE id=?", (handle.intent_id,)).fetchone()
        action = tx.execute("SELECT a.* FROM authority_policy_actions a JOIN authority_policy_action_attempts p "
                            "ON p.action_id=a.id WHERE p.intent_id=?", (handle.intent_id,)).fetchone()
        preparation = None if child is None else tx.execute(
            "SELECT * FROM context_workspaces WHERE preparation_id=?", (child["workspace_preparation_id"],)).fetchone()
    if (child is None or intent is None or action is None or preparation is None or child["role"] != "recovery"
            or child["contract_hash"] != acceptance_hash or intent["completion_status"] != "succeeded"):
        raise SupervisorRefused("RECOVERY_BINDING_INVALID")
    receipt = {
        "schema": "ffs.run-policy-receipt/v1", "role": "recovery", "request_key": action["logical_key"],
        "activity_id": handle.activity_id, "intent_id": handle.intent_id, "fence_generation": intent["generation"],
        "acceptance_hash": acceptance_hash, "candidate_hash": child["candidate_hash"],
        "runtime_hash": child["runtime_identity"],
        "workspace_preparation_hash": hashlib.sha256(json.dumps(
            asdict(_from_row(preparation)), default=str, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "evidence": [{"id": "process-result", **handle.result["evidence"]}], "completion_status": "succeeded",
        "process_identity": asdict(handle.identity), "review_dimensions": [],
    }
    store.record_acceptance_receipt(token, acceptance_hash=acceptance_hash, receipt=receipt)
    return receipt, action["id"], child["workspace_binding"]


def _final_text(handle) -> str:
    raw = _read_evidence(handle.stdout_path, handle.stream_identities["stdout"])
    if hashlib.sha256(raw).hexdigest() != handle.result["streams"]["stdout"]["sha256"]:
        raise SupervisorRefused("EVIDENCE_CHANGED")
    try:
        return _host_final_text(handle.result, raw, single=False)
    except (KeyError, TypeError, ValueError, UnicodeError) as error:
        raise SupervisorRefused("RECOVERY_DIAGNOSIS_FAILED") from error


def _end_child(cycle: _Cycle, activity_id: str, new: str, evidence) -> None:
    if cycle.store.get_activity(activity_id).state == "active":
        cycle.store.transition_activity(cycle.token, activity_id, expected="active", new=new, result=evidence,
                                        reason="recovery child settled")


def _brief(cycle: _Cycle) -> str:
    """The failed criteria with their frozen checks: what the child must make true."""
    wanted = set(cycle.packet["failed_criteria"])
    lines = [f"Saved stage: {cycle.packet['saved_stage']}"]
    for criterion in cycle.sealed.material["criteria"]:
        if criterion["id"] in wanted:
            lines.append(f"- {criterion['id']}: {criterion['objective_clause']}")
            lines.extend(f"    frozen check {check['id']}: {check['locator']}" for check in criterion["checks"])
    return "\n".join(lines)


def _diagnosis_prompt(cycle: _Cycle) -> str:
    return "\n".join([
        DIAGNOSIS_PREFIX,
        "A sealed run's frozen checks or final review did not accept its candidate.  Find out why.",
        "Do not modify, create or delete any file: this workspace is a copy and only your written answer is used.",
        _brief(cycle),
        "Reply with a short plain-text diagnosis and the smallest change that would fix it.",
    ])


def _trial_prompt(cycle: _Cycle, diagnosis_text: str) -> str:
    return "\n".join([
        TRIAL_PREFIX,
        "A sealed run's frozen checks or final review did not accept its candidate.  Apply the smallest change in this",
        "workspace that makes the frozen checks pass.  Do not commit, and change nothing the fix does not need.",
        _brief(cycle),
        "Diagnosis:",
        diagnosis_text,
    ])
