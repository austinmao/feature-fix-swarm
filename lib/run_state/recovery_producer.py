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
REPAIR_PREFIX = "Repair request:"
_TEXT_LIMIT = 8 * 1024      # bytes of the diagnosis carried into the trial prompt
_TERMINAL = {"succeeded", "failed", "aborted"}
_Settled = namedtuple("_Settled", "handle receipt action_id workspace receipt_hash")
_Diagnosis = namedtuple("_Diagnosis", "receipt text")


@dataclass(frozen=True)
class _Kind:
    """What differs between a recovery child and an ordinary repair child; every other step is shared."""

    family: str         # the key family and the prefix of the child's typed refusals
    role: str           # the child's qualification and binding role
    receipt_role: str   # the role of the receipt that child's settled process records
    reconcile: str      # the typed refusal for an intent issued under an earlier owner fence
    ambiguous: str


_RECOVERY = _Kind("recovery", "recovery", "recovery", "RECOVERY_RECONCILIATION_REQUIRED", "RECOVERY_ACTION_AMBIGUOUS")
_REPAIR = _Kind("repair", "worker", "execution", "REPAIR_RECONCILIATION_REQUIRED", "REPAIR_ACTION_AMBIGUOUS")


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
    checks: int                # frozen checks one trial's candidate is measured against
    kind: _Kind = _RECOVERY
    ordinal: int | None = None  # the ordinal of the repair this call runs (a repair only)

    @property
    def tag(self) -> str:
        return self.binding.input_hash[:16]

    def key(self, name: str) -> str:
        """The logical key of one child: ``recovery:<tag>:<name>``, or ``repair:<tag>:<ordinal>``."""
        return f"repair:{self.tag}:{self.ordinal}" if self.kind is _REPAIR else f"recovery:{self.tag}:{name}"

    @property
    def repair_demand(self) -> int:
        """Launches one repair costs: its qualification probes, its launch and the checks that follow it."""
        return self.probes + 1 + self.checks

    @property
    def probes(self) -> int:
        return len(_QUALIFICATION_PROBE_ORDER)

    @property
    def trial_demand(self) -> int:
        """Launches one trial child costs: its qualification probes, its launch and its checks."""
        return self.probes + 1 + self.checks

    @property
    def overhead(self) -> int:
        """Launches still to come once the diagnosis child is qualified: its launch and the trial."""
        return 1 + self.trial_demand


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


def produce_repair(store, token, *, supervisor, controller, seam, parent_activity_id: str, preparation,
                   failed_criteria, timeout_seconds=None) -> None:
    """Run or resume one ordinary repair of the current candidate: the ``LifecycleProducers.repair`` callable.

    The repair child (role ``worker``, receipt role ``execution``) runs in an isolated copy of the CURRENT
    candidate.  Its harvested patch is merged through the journaled integration path under ``repair:<action_id>``
    and the repaired candidate is bound with the candidate it was repaired from as its parent; the lifecycle then
    re-runs the frozen checks.  An empty patch integrates nothing: the child settles, its grant stays spent, and the
    lifecycle repairs again or hands back.  When the remaining launch budget cannot cover the repair this refuses
    ``REPAIR_BUDGET_INFEASIBLE`` before anything is reserved or charged, and the lifecycle hands back instead.

    Ordering is the contract, as for a recovery child: the child is captured, qualified and bound before the grant
    is reserved; every step is keyed and idempotent; a repair issued under an earlier owner fence is never
    relaunched, re-verified or recorded here (``REPAIR_RECONCILIATION_REQUIRED``).
    """
    try:
        cycle = _context(store, token, supervisor, controller, seam, parent_activity_id, preparation,
                         {"failed_criteria": sorted(set(failed_criteria))}, timeout_seconds, _REPAIR)
        cycle = replace(cycle, ordinal=_repair_ordinal(cycle))
        settled = _child(cycle, "repair", "repair", lambda: _repair_prompt(cycle), cycle.repair_demand)
        if settled is not None:
            _land_repair(cycle, settled)
    except RecoveryRefused as error:
        raise SupervisorRefused(error.code) from error


def _repair_state(cycle: _Cycle) -> tuple[int, bool]:
    """``(ordinal, unfinished)`` of the latest repair of the current candidate; ``(0, False)`` when it has none.

    A repair is unfinished while it has no launch yet, while its child is still active, or while its child ended
    with a retained record that the candidate it was repaired from has not yet absorbed (the candidate is unchanged,
    so its integration did not finish).  Cancelled grants are never counted.
    """
    from .repair_integration import repair_record_key
    store, token, prefix = cycle.store, cycle.token, f"repair:{cycle.tag}:"
    with store.read_transaction() as tx:
        rows = [dict(row) for row in tx.execute(
            "SELECT a.id,a.logical_key,a.intent_id,i.activity_id,t.state AS activity_state "
            "FROM authority_policy_actions a LEFT JOIN authority_launch_intents i ON i.id=a.intent_id "
            "LEFT JOIN authority_activities t ON t.id=i.activity_id "
            "WHERE a.repository_id=? AND a.run_id=? AND a.action='repair' AND a.state<>'cancelled' "
            "AND a.logical_key LIKE ?", (token.repository_id, token.run_id, prefix + "%"))]
    if not rows:
        return 0, False
    last = max(rows, key=lambda row: int(row["logical_key"][len(prefix):].split(":")[0]))
    ordinal = int(last["logical_key"][len(prefix):].split(":")[0])
    unfinished = (last["intent_id"] is None or last["activity_state"] not in _TERMINAL
                  or (last["activity_state"] == "succeeded"
                      and _retained_event(store, last["activity_id"], repair_record_key(last["id"]))))
    return ordinal, unfinished


def _repair_ordinal(cycle: _Cycle) -> int:
    """The ordinal of the repair this call runs for the current candidate: the unfinished one, else the next."""
    ordinal, unfinished = _repair_state(cycle)
    return ordinal if unfinished else ordinal + 1


def repair_unfinished(store, token, *, supervisor, controller, seam, parent_activity_id: str, preparation) -> bool:
    """Whether the current candidate has a repair ``produce_repair`` must resume or replace.

    That is a grant reserved and never launched, or an issued repair not yet integrated.  The lifecycle asks this
    once the tier's repair allowance is spent: that allowance counts such a repair, so without the producer a
    replay would hand back to recovery past a stale grant, or past the earlier-fence refusal an issued repair owes.
    """
    cycle = _context(store, token, supervisor, controller, seam, parent_activity_id, preparation,
                     {"failed_criteria": []}, None, _REPAIR)
    return _repair_state(cycle)[1]


def _land_repair(cycle: _Cycle, settled) -> None:
    """Harvest the settled repair's patch, end its child, then integrate and bind the repaired candidate."""
    from .repair_integration import integrate_repair, repair_record_key, retain_repair_record
    activity_id, evidence = settled.handle.activity_id, settled.handle.result["evidence"]
    if cycle.store.get_activity(activity_id).state in _TERMINAL and not _retained_event(
            cycle.store, activity_id, repair_record_key(settled.action_id)):
        return      # a replay of a repair already excluded for an empty patch
    retained = retain_repair_record(
        cycle.store, cycle.token, supervisor=cycle.supervisor, action_id=settled.action_id, activity_id=activity_id,
        workspace=settled.workspace, expected_input_digest=cycle.frozen.candidate_hash)
    _end_child(cycle, activity_id, "succeeded", evidence)
    if retained:
        integrate_repair(cycle.store, cycle.token, supervisor=cycle.supervisor, action_id=settled.action_id,
                         activity_id=activity_id, receipt_hash=settled.receipt_hash,
                         workspace=str(cycle.candidate.path), acceptance_hash=cycle.frozen.acceptance_hash)


def _context(store, token, supervisor, controller, seam, parent_activity_id, preparation, packet, timeout_seconds,
             kind=_RECOVERY):
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
    return _Cycle(store, token, supervisor, controller, seam, timeout_seconds, packet, sealed, frozen, candidate,
                  parent_id, runtime, binding, checks, kind)


def _issued_cycles(cycle: _Cycle) -> list[dict]:
    store, token = cycle.store, cycle.token
    with store.read_transaction() as tx:
        return [dict(row) for row in tx.execute(
            "SELECT recovery_cycle,input_hash FROM authority_policy_actions WHERE repository_id=? AND run_id=? "
            "AND action IN ('recovery_cycle_normal','recovery_cycle_autonomous') AND state<>'cancelled' "
            "ORDER BY recovery_cycle", (token.repository_id, token.run_id))]


def _reserve_cycle(cycle: _Cycle):
    """The one issued cycle of this binding; a replay returns the same row."""
    store, token = cycle.store, cycle.token
    mode = store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id).recovery_mode
    issued = _issued_cycles(cycle)
    mine = [row for row in issued if row["input_hash"] == cycle.binding.input_hash]
    try:
        return store.reserve_policy_action(
            token, action="recovery_cycle_" + mode, logical_key=f"recovery:{cycle.tag}:cycle",
            input_hash=cycle.binding.input_hash, recovery_cycle=mine[0]["recovery_cycle"] if mine else len(issued) + 1,
            required_launch_overhead=cycle.overhead)
    except OwnershipRefused as error:
        raise SupervisorRefused(error.code) from error


def _assert_fits(cycle: _Cycle, demand: int) -> None:
    """Refuse, before any probe is charged, when the launches this child still needs no longer fit.

    A reused cycle was priced when it was reserved, but a new owner abandons the earlier qualified child
    and qualifies a fresh one (its probes are charged again), so the reservation's price is not enough.
    A repair has no cycle to reserve: it is checked every time, fresh or reused, before anything is charged.
    """
    budget = cycle.store.get_run_policy_budget(repository_id=cycle.token.repository_id, run_id=cycle.token.run_id)
    if budget.launch_limit - budget.launch_charged < demand:
        raise SupervisorRefused("REPAIR_BUDGET_INFEASIBLE" if cycle.kind is _REPAIR else "POLICY_STAGE_INFEASIBLE")


def _diagnose(cycle: _Cycle):
    # Under a reused cycle the diagnosis child's own probes are charged again, on top of the cycle's price.
    settled = _child(cycle, "diagnosis", "diagnosis", lambda: _diagnosis_prompt(cycle),
                     cycle.probes + cycle.overhead)
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
    settled = _child(cycle, "trial", "recovery_trial", lambda: _trial_prompt(cycle, diagnosis_text),
                     cycle.trial_demand)
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


def _child(cycle: _Cycle, name: str, action: str, make_prompt, demand: int):
    """Run, or resume, one recovery child; ``None`` when its process did not complete usably.

    ``demand`` is the launches this child and everything after it still need if it has to be qualified afresh.
    """
    key = cycle.key(name)
    launch_key = key + ":launch"
    row, intent = _retained_action(cycle.store, cycle.token, launch_key, action, cycle.kind.ambiguous)
    if intent is not None:
        if not intent["permit_id"] or intent["child_pid"] is None:
            # Reserved but never acknowledged: only owner-fence reconciliation may settle it.
            raise SupervisorRefused("INTENT_RECONCILIATION_REQUIRED")
        if intent["generation"] != cycle.token.generation:
            # Its completion, receipt and workspace bind the fence that issued them: never relaunched or re-bound.
            raise SupervisorRefused(cycle.kind.reconcile)
        return _settle(cycle, cycle.supervisor.resume_monitored(intent["id"]))
    if cycle.kind is _REPAIR or any(item["input_hash"] == cycle.binding.input_hash for item in _issued_cycles(cycle)):
        _assert_fits(cycle, demand)
    if row is not None:
        # Reserved, never launched.  A re-bind builds new launch material (a new private TMPDIR or session), so
        # this grant can never be reused: release it unspent (it holds no intent and no attempt).
        release = (cycle.store.cancel_unlaunched_repair if cycle.kind is _REPAIR
                   else cycle.store.cancel_unlaunched_policy_review)
        try:
            release(cycle.token, action_id=row["id"])
        except OwnershipRefused as error:
            raise SupervisorRefused(error.code) from error
    return _launch(cycle, key, launch_key, action, make_prompt)


def _launch(cycle: _Cycle, key: str, launch_key: str, action: str, make_prompt):
    qualified, ready = _qualified_child(cycle, key)
    # Bind and validate the launch material BEFORE the cycle (or the repair grant) is reserved: a host refusal
    # here must leave the handback (or the failed check) retained with nothing spent.
    request, adapter = cycle.seam.bind(qualified, make_prompt(), ready, cycle.frozen.acceptance_hash, launch_key)
    material = request.codex_material or request.claude_material
    if material is None:
        raise SupervisorRefused("HOST_CAPABILITY_UNQUALIFIED")
    launched, handle = False, None
    try:
        try:
            ordinal = None if cycle.kind is _REPAIR else _reserve_cycle(cycle).recovery_cycle
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
    """Capture the current candidate into a fresh child workspace and qualify it (replay-safe)."""
    try:
        child_key, retained_activity, retained_preparation = _current_reviewer(
            cycle.store, cycle.token, parent_activity_id=cycle.parent_id, base_key=key + ":child", action=None,
            reconciliation_code=cycle.kind.reconcile)
        ready = _reviewer_workspace(
            cycle.store, cycle.token, cycle.supervisor, preparation=cycle.candidate, parent_activity_id=cycle.parent_id,
            runtime_identity=cycle.runtime, reviewer_key=child_key,
            candidate_hash=cycle.frozen.candidate_hash, retained_preparation_id=retained_preparation,
            retained_activity_id=retained_activity, role=cycle.kind.role)
    except WorkspaceRefused as error:
        raise SupervisorRefused(error.code) from error
    # The outer orchestrator's prepaid group has ended by now: qualify on this channel-less supervisor.
    qualified = cycle.seam.qualify(retained_activity or str(uuid.uuid4()), ready, child_key, cycle.parent_id,
                                   cycle.frozen.acceptance_hash, cycle.kind.role, supervisor=cycle.supervisor)
    return qualified, ready


def _settle(cycle: _Cycle, handle):
    result = cycle.supervisor.finish(handle, timeout=cycle.timeout_seconds)
    if result["returncode"] != 0 or result.get("host_receipt", {}).get("status") != "complete":
        _end_child(cycle, handle.activity_id, "failed", result["evidence"])
        return None
    receipt, action_id, workspace, receipt_hash = _record_receipt(cycle, handle)
    return _Settled(handle, receipt, action_id, workspace, receipt_hash)


def _record_receipt(cycle: _Cycle, handle):
    """The child's role receipt (``recovery``, or ``execution`` for a repair), built from durable rows only."""
    store, token, acceptance_hash = cycle.store, cycle.token, cycle.frozen.acceptance_hash
    with store.read_transaction() as tx:
        child = tx.execute("SELECT * FROM authority_child_bindings WHERE activity_id=?", (handle.activity_id,)).fetchone()
        intent = tx.execute("SELECT * FROM authority_launch_intents WHERE id=?", (handle.intent_id,)).fetchone()
        action = tx.execute("SELECT a.* FROM authority_policy_actions a JOIN authority_policy_action_attempts p "
                            "ON p.action_id=a.id WHERE p.intent_id=?", (handle.intent_id,)).fetchone()
        preparation = None if child is None else tx.execute(
            "SELECT * FROM context_workspaces WHERE preparation_id=?", (child["workspace_preparation_id"],)).fetchone()
    if (child is None or intent is None or action is None or preparation is None or child["role"] != cycle.kind.role
            or child["contract_hash"] != acceptance_hash or intent["completion_status"] != "succeeded"):
        raise SupervisorRefused(cycle.kind.family.upper() + "_BINDING_INVALID")
    receipt = {
        "schema": "ffs.run-policy-receipt/v1", "role": cycle.kind.receipt_role, "request_key": action["logical_key"],
        "activity_id": handle.activity_id, "intent_id": handle.intent_id, "fence_generation": intent["generation"],
        "acceptance_hash": acceptance_hash, "candidate_hash": child["candidate_hash"],
        "runtime_hash": child["runtime_identity"],
        "workspace_preparation_hash": hashlib.sha256(json.dumps(
            asdict(_from_row(preparation)), default=str, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "evidence": [{"id": "process-result", **handle.result["evidence"]}], "completion_status": "succeeded",
        "process_identity": asdict(handle.identity), "review_dimensions": [],
    }
    recorded = store.record_acceptance_receipt(token, acceptance_hash=acceptance_hash, receipt=receipt)
    return receipt, action["id"], child["workspace_binding"], recorded.receipt_hash


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
                                        reason=cycle.kind.family + " child settled")


def _failed_lines(cycle: _Cycle) -> list[str]:
    """The failed criteria with their frozen checks: what the child must make true."""
    wanted = set(cycle.packet["failed_criteria"])
    lines = []
    for criterion in cycle.sealed.material["criteria"]:
        if criterion["id"] in wanted:
            lines.append(f"- {criterion['id']}: {criterion['objective_clause']}")
            lines.extend(f"    frozen check {check['id']}: {check['locator']}" for check in criterion["checks"])
    return lines


def _brief(cycle: _Cycle) -> str:
    return "\n".join([f"Saved stage: {cycle.packet['saved_stage']}", *_failed_lines(cycle)])


def _diagnosis_prompt(cycle: _Cycle) -> str:
    return "\n".join([
        DIAGNOSIS_PREFIX,
        "A sealed run's frozen checks or final review did not accept its candidate.  Find out why.",
        "Do not modify, create or delete any file: this workspace is a copy and only your written answer is used.",
        _brief(cycle),
        "Reply with a short plain-text diagnosis and the smallest change that would fix it.",
    ])


def _repair_prompt(cycle: _Cycle) -> str:
    return "\n".join([
        REPAIR_PREFIX,
        "A sealed run's frozen checks did not accept its candidate.  Apply the smallest change in this workspace that",
        "makes them pass.  Do not commit, and change nothing the fix does not need.",
        *_failed_lines(cycle),
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
