"""Managed frontend lifecycle over the existing ControlStore authority.

The controller deliberately has no launcher.  ``Supervisor.reserve_request_action``
and the qualified host adapters remain the sole route to a child process.  This
module freezes frontend scope, maps deterministic checks, and exposes the exact
stage/candidate/allowance material that CLI and supervisor glue must bind.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from .run_policy import (
    RunPolicyRefused, action_limit, classify_finding,
    productive_work,
)


class FrontendPolicyRefused(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class FrozenFrontendContract:
    acceptance_hash: str
    candidate_hash: str
    generation: int
    command_mode: str


def _digest(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise FrontendPolicyRefused("FRONTEND_POLICY_INPUT_INVALID")
    return value


def _canonical(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise FrontendPolicyRefused("FRONTEND_POLICY_INPUT_INVALID") from error


class FrontendPolicyController:
    """One durable policy controller shared by all managed frontends."""

    def __init__(self, store, token, *, command_mode: str) -> None:
        if command_mode not in {"feature-spec", "feature-implement", "fix", "code-uplift", "task-swarm"}:
            raise FrontendPolicyRefused("FRONTEND_MODE_INVALID")
        self.store, self.token, self.command_mode = store, token, command_mode

    def freeze(self, *, draft_id: str, revision: int, material: dict) -> FrozenFrontendContract:
        """Persist and seal exactly the supplied objective-mapped draft."""
        legacy = self.store.get_acceptance_contract(
            repository_id=self.token.repository_id, run_id=self.token.run_id,
        )
        if legacy is None:
            raise FrontendPolicyRefused("ACCEPTANCE_CONTRACT_REQUIRED")
        if material.get("command_mode") != self.command_mode:
            raise FrontendPolicyRefused("FRONTEND_MODE_BINDING_INVALID")
        try:
            self.store.create_acceptance_draft(
                self.token, draft_id=draft_id, revision=revision,
                acceptance_contract_hash=legacy.contract_hash, material=material,
            )
            sealed = self.store.seal_acceptance_draft(
                self.token, draft_id=draft_id, revision=revision,
                acceptance_contract_hash=legacy.contract_hash,
            )
            state = self.store.initialize_frontend_policy(
                self.token, acceptance_hash=sealed.acceptance_hash, stage="SEALED",
            )
        except ValueError as error:
            raise FrontendPolicyRefused(getattr(error, "code", "FRONTEND_FREEZE_REFUSED")) from error
        return FrozenFrontendContract(sealed.acceptance_hash, state.candidate_hash,
                                      state.generation, self.command_mode)

    def sealed(self) -> FrozenFrontendContract:
        state = self.store.get_frontend_policy_state(
            repository_id=self.token.repository_id, run_id=self.token.run_id,
        )
        if state is None:
            raise FrontendPolicyRefused("ACCEPTANCE_SEAL_REQUIRED")
        sealed = self.store.get_sealed_acceptance(
            repository_id=self.token.repository_id, run_id=self.token.run_id,
        )
        if sealed is None or sealed.acceptance_hash != state.acceptance_hash:
            raise FrontendPolicyRefused("FRONTEND_POLICY_BINDING_INVALID")
        if sealed.material["command_mode"] != self.command_mode:
            raise FrontendPolicyRefused("FRONTEND_MODE_BINDING_INVALID")
        return FrozenFrontendContract(state.acceptance_hash, state.candidate_hash,
                                      state.generation, self.command_mode)

    def reserve_stage(self, supervisor, request, *, action: str,
                      qualification_contract: dict | None = None,
                      recovery_cycle: int | None = None):
        """Route one stage through Supervisor's exact dispatch-hash reservation.

        The controller intentionally does not recreate the supervisor digest:
        native session/auth/workspace material is security-sensitive and every
        transport must bind its freshly observed value there.  E4 already
        accounts for qualification launch overhead when the supervisor binds
        the request.
        """
        frozen = self.sealed()
        state = self.store.get_frontend_policy_state(repository_id=self.token.repository_id, run_id=self.token.run_id)
        permitted = {
            "SEALED": {"execute", "qualification"}, "EXECUTE": {"execute", "repair", "check", "final_review"},
            "FINAL_REVIEW": {"final_review", "repair", "check"}, "RECOVER": {"diagnosis", "recovery_trial", "check"},
        }
        if state is None or action not in permitted.get(state.stage, set()):
            raise FrontendPolicyRefused("FRONTEND_STAGE_ACTION_INVALID")
        if action != "qualification" and request.contract_hash != frozen.acceptance_hash:
            raise FrontendPolicyRefused("FRONTEND_DISPATCH_CONTRACT_STALE")
        with self.store.read_transaction() as tx:
            child = tx.execute("SELECT candidate_hash FROM authority_child_bindings WHERE activity_id=?",
                               (request.activity_id,)).fetchone()
        if child is None or (action != "qualification" and child["candidate_hash"] != frozen.candidate_hash):
            raise FrontendPolicyRefused("FRONTEND_DISPATCH_CANDIDATE_STALE")
        return supervisor.reserve_request_action(
            request, action=action, qualification_contract=qualification_contract,
            recovery_cycle=recovery_cycle,
        )

    def advance_candidate(self, *, candidate_hash: str, receipt_hash: str,
                          parent_candidate_hash: str | None = None) -> FrozenFrontendContract:
        frozen = self.sealed()
        try:
            state = self.store.bind_frontend_candidate(
                self.token, acceptance_hash=frozen.acceptance_hash, candidate_hash=_digest(candidate_hash),
                receipt_hash=_digest(receipt_hash), parent_candidate_hash=parent_candidate_hash,
            )
        except ValueError as error:
            raise FrontendPolicyRefused(getattr(error, "code", "FRONTEND_CANDIDATE_REFUSED")) from error
        return FrozenFrontendContract(state.acceptance_hash, state.candidate_hash,
                                      state.generation, self.command_mode)

    def record_integration(self, *, receipt_hash: str, candidate_hash: str,
                           integration_evidence: dict, no_commit_evidence: dict) -> None:
        try:
            self.store.record_frontend_integration(
                self.token, receipt_hash=_digest(receipt_hash), candidate_hash=_digest(candidate_hash),
                integration_evidence=integration_evidence, no_commit_evidence=no_commit_evidence,
            )
        except ValueError as error:
            raise FrontendPolicyRefused(getattr(error, "code", "FRONTEND_INTEGRATION_REFUSED")) from error

    def run_mapped_checks(self, *, workspace: str, supervisor, parent_activity_id: str) -> dict[str, dict]:
        """Run every frozen check through an isolated, supervised local child.

        The current candidate is captured once. Each check receives a separate
        registered copy, exact sealed command and an existing launch debit;
        results come only from the supervisor's physical process evidence.
        """
        from .supervisor import Supervisor
        from .workspace import inspect_workspace
        from .wave_execution import capture_prelaunch_snapshot

        if (not isinstance(supervisor, Supervisor) or supervisor.store is not self.store
                or supervisor.token != self.token):
            raise FrontendPolicyRefused('FRONTEND_CHECK_SUPERVISOR_REQUIRED')
        frozen = self.sealed()
        sealed = self.store.get_sealed_acceptance(
            repository_id=self.token.repository_id, run_id=self.token.run_id)
        assert sealed is not None
        checks = {check['id']: check for criterion in sealed.material['criteria'] for check in criterion['checks']}
        with self.store.read_transaction() as tx:
            parent = tx.execute('SELECT a.runtime_tuple_hash,b.workspace_preparation_id,b.workspace_binding '
                'FROM authority_activities a JOIN authority_child_bindings b ON b.activity_id=a.id '
                'WHERE a.id=? AND a.repository_id=? AND a.run_id=?',
                (parent_activity_id, self.token.repository_id, self.token.run_id)).fetchone()
            retained = tx.execute('SELECT check_id,status,evidence_json FROM authority_frontend_policy_checks '
                'WHERE repository_id=? AND run_id=? AND acceptance_hash=? AND candidate_hash=?',
                (self.token.repository_id, self.token.run_id, frozen.acceptance_hash, frozen.candidate_hash)).fetchall()
        if parent is None or parent['workspace_binding'] != workspace or not parent['runtime_tuple_hash']:
            raise FrontendPolicyRefused('FRONTEND_CHECK_WORKSPACE_INVALID')
        ready = inspect_workspace(self.store, parent['workspace_preparation_id'])
        with productive_work(self.store, self.token, kind='check'):
            snapshot = capture_prelaunch_snapshot(self.store, self.token, ready,
                activity_id=parent_activity_id, runtime_identity=parent['runtime_tuple_hash'],
                evidence_root=supervisor.evidence_root)
        if snapshot.input_digest != frozen.candidate_hash:
            raise FrontendPolicyRefused('FRONTEND_CHECK_CANDIDATE_STALE')
        if retained:
            if {row['check_id'] for row in retained} != set(checks):
                raise FrontendPolicyRefused('FRONTEND_CHECK_RECONCILIATION_REQUIRED')
            output = {}
            for row in retained:
                evidence = json.loads(row['evidence_json'])
                for item in evidence:
                    self.store._verified_evidence(item)
                output[row['check_id']] = {'status': row['status'], 'evidence': evidence, 'reused': True}
            return output
        results, output = run_sealed_checks(
            self.store, self.token, supervisor, acceptance_hash=frozen.acceptance_hash,
            candidate_hash=frozen.candidate_hash, checks=checks, parent_activity_id=parent_activity_id,
            runtime_identity=parent['runtime_tuple_hash'], ready=ready, snapshot=snapshot,
            key_prefix='sealed-check:')
        self.store.record_frontend_check_results(self.token, acceptance_hash=frozen.acceptance_hash,
            candidate_hash=frozen.candidate_hash, results=results)
        return output

    def classify_findings(self, findings: list[dict]) -> list[dict]:
        """Classify reviewer output by frozen code/criterion/evidence, not prose."""
        frozen = self.sealed()
        sealed = self.store.get_sealed_acceptance(
            repository_id=self.token.repository_id, run_id=self.token.run_id,
        )
        assert sealed is not None
        criteria = {item["id"]: tuple(check["id"] for check in item["checks"])
                    for item in sealed.material["criteria"]}
        invariants = tuple(item["id"] for item in sealed.material["global_invariants"])
        persisted: list[dict] = []
        for finding in findings:
            if not isinstance(finding, dict) or set(finding) != {
                "acceptance_hash", "candidate_hash", "runtime_hash", "criterion_ids", "check_ids",
                "invariant_ids", "evidence",
            } or not isinstance(finding["evidence"], list):
                raise FrontendPolicyRefused("FINDING_INVALID")
            # The old pure predicate receives a verified fact only after every
            # content-addressed item has been independently hashed here.
            try:
                evidence = [self.store._verified_evidence(item) for item in finding["evidence"]]
                if not evidence:
                    raise FrontendPolicyRefused("FINDING_EVIDENCE_REQUIRED")
                known_evidence = {rule["id"] for item in sealed.material["criteria"]
                                  for rule in item["evidence_rules"]} | set(
                                      check for checks in criteria.values() for check in checks)
                if not {item["id"] for item in evidence}.intersection(known_evidence | set(finding["invariant_ids"])):
                    raise FrontendPolicyRefused("FINDING_EVIDENCE_SCOPE_INVALID")
                decision = classify_finding(
                    {key: finding[key] for key in ("acceptance_hash", "candidate_hash", "runtime_hash",
                     "criterion_ids", "check_ids", "invariant_ids")} | {"evidence_valid": True},
                    acceptance_hash=frozen.acceptance_hash, candidate_hash=frozen.candidate_hash,
                    runtime_hash=sealed.material["runtime"]["effective_hash"], criteria=criteria,
                    invariants=invariants,
                )
            except (RunPolicyRefused, ValueError) as error:
                raise FrontendPolicyRefused(getattr(error, "code", "FINDING_INVALID")) from error
            canonical = _canonical(finding)
            finding_hash = hashlib.sha256(canonical.encode()).hexdigest()
            record = {
                "finding_hash": finding_hash, "classification": decision.classification.value,
                "criterion_ids": list(decision.criterion_ids), "invariant_ids": list(decision.invariant_ids),
                "evidence": evidence, "code": decision.code,
            }
            self._persist_finding(frozen, record)
            persisted.append(record)
        return persisted

    def _persist_finding(self, frozen: FrozenFrontendContract, record: dict) -> None:
        from .ownership import assert_owner
        self.store.ensure_frontend_policy_schema()
        with self.store.transaction() as tx:
            assert_owner(tx, self.token)
            state = tx.execute("SELECT candidate_hash,acceptance_hash FROM authority_frontend_policy_states WHERE repository_id=? AND run_id=?",
                               (self.token.repository_id, self.token.run_id)).fetchone()
            if state is None or (state["candidate_hash"], state["acceptance_hash"]) != (frozen.candidate_hash, frozen.acceptance_hash):
                raise FrontendPolicyRefused("FINDING_CANDIDATE_STALE")
            tx.execute(
                "INSERT OR IGNORE INTO authority_frontend_policy_findings "
                "(repository_id,run_id,acceptance_hash,candidate_hash,finding_hash,classification,criterion_ids_json,invariant_ids_json,evidence_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (self.token.repository_id, self.token.run_id, frozen.acceptance_hash, frozen.candidate_hash,
                 record["finding_hash"], record["classification"], json.dumps(record["criterion_ids"]),
                 json.dumps(record["invariant_ids"]), _canonical(record["evidence"]), self.store._now()),
            )

    def handback(self, *, saved_stage: str, failed_criteria: list[str], consumed_attempts: list[str],
                choices: list[dict]) -> dict:
        """Persist E7's exact continuation interface; it never claims recovery ran."""
        frozen = self.sealed()
        budget = self.store.get_run_policy_budget(repository_id=self.token.repository_id, run_id=self.token.run_id)
        if budget is None:
            raise FrontendPolicyRefused("RUN_POLICY_REQUIRED")
        limits = {action: action_limit(action, budget.tier) for action in
                  ("spec_review", "repair", "final_review", "recovery_cycle_normal", "recovery_cycle_autonomous")}
        with self.store.read_transaction() as tx:
            used = {action: tx.execute(
                "SELECT COUNT(*) FROM authority_policy_actions WHERE repository_id=? AND run_id=? AND action=? AND state<>'cancelled'",
                (self.token.repository_id, self.token.run_id, action),
            ).fetchone()[0] for action in limits}
            outstanding = [row[0] for row in tx.execute(
                "SELECT id FROM authority_activities WHERE repository_id=? AND run_id=? AND state NOT IN ('succeeded','failed','aborted')",
                (self.token.repository_id, self.token.run_id),
            ).fetchall()]
        state = self.store.get_frontend_policy_state(repository_id=self.token.repository_id, run_id=self.token.run_id)
        if state is None or saved_stage != state.stage or saved_stage not in {"EXECUTE", "FINAL_REVIEW"}:
            raise FrontendPolicyRefused("FRONTEND_SAVED_STAGE_INVALID")
        sealed = self.store.get_sealed_acceptance(repository_id=self.token.repository_id, run_id=self.token.run_id)
        assert sealed is not None
        packet = {
            "schema": "ffs.frontend-recovery-continuation/v1", "acceptance_hash": frozen.acceptance_hash,
            "candidate_hash": frozen.candidate_hash, "saved_stage": saved_stage,
            "failed_criteria": sorted(set(failed_criteria)), "consumed_attempts": list(consumed_attempts),
            "remaining_allowances": {action: limit - used[action] for action, limit in limits.items()},
            "remaining_launches": budget.launch_limit - budget.launch_charged,
            "remaining_active_ns": budget.active_limit_ns - budget.active_ns,
            "generation": frozen.generation, "runtime_hash": sealed.material["runtime"]["effective_hash"],
            "base_candidate_hash": sealed.material["candidate_hash"], "outstanding_activity_ids": outstanding,
            "required_obligation_ids": list(sealed.material["criteria"][i]["id"] for i in range(len(sealed.material["criteria"]))),
            "choices": choices,
        }
        recovery_left = limits["recovery_cycle_" + budget.recovery_mode] - used["recovery_cycle_" + budget.recovery_mode]
        next_stage = "RECOVER" if recovery_left > 0 else "NEEDS_DECISION"
        try:
            self.store.transition_frontend_policy(
                self.token, expected_stage=state.stage, new_stage=next_stage, decision=packet,
            )
        except ValueError as error:
            raise FrontendPolicyRefused(getattr(error, "code", "FRONTEND_HANDOFF_REFUSED")) from error
        return packet


def run_sealed_checks(store, token, supervisor, *, acceptance_hash, candidate_hash, checks,
                      parent_activity_id, runtime_identity, ready, snapshot, key_prefix):
    """Run frozen checks in isolated supervised children of one captured candidate.

    Shared by the mapped-check path and isolated recovery trials; the caller
    owns which candidate/keys are recorded, this helper only produces the
    supervisor's physical process evidence.
    """
    from .supervisor import DispatchRequest
    from .workspace import begin_child_workspace_preparation, prepare_workspace
    results, output = [], {}
    for check_id in sorted(checks):
        key = key_prefix + hashlib.sha256(_canonical(
            [acceptance_hash, candidate_hash, check_id]).encode()).hexdigest()
        retained = _completed_check(store, token, acceptance_hash=acceptance_hash,
                                    candidate_hash=candidate_hash, check_id=check_id, key=key)
        if retained is not None:
            results.append(retained)
            output[check_id] = {'status': retained['status'], 'evidence': retained['evidence'], 'reconstructed': True}
            continue
        with productive_work(store, token, kind='preparation'):
            pending = begin_child_workspace_preparation(store, token,
                parent_activity_id=parent_activity_id, request_key=key + ':workspace', role='inventory',
                base_commit=ready.base_commit, selected_input_manifest=snapshot.manifest,
                repository_path=ready.repository_path)
            check_ready = prepare_workspace(store, token, pending, input_snapshot=snapshot)
            child = store.create_child_activity(token, parent_activity_id=parent_activity_id,
                role='inventory', request_key=key + ':activity', candidate_hash=check_ready.input_digest,
                contract_hash=acceptance_hash, runtime_identity=runtime_identity,
                workspace_binding=str(check_ready.path), workspace_preparation_id=check_ready.id, retry_budget=1)
        # The launcher replaces this inert placeholder from the sealed
        # locator itself; callers never select a different executable.
        request = DispatchRequest(child.id, key + ':launch', ('/usr/bin/true',),
            str(check_ready.path), check_ready.base_commit, runtime_identity,
            token_reservation=0, contract_hash=acceptance_hash, monitor_result=True)
        handle = supervisor.launch_sealed_check(request, acceptance_hash=acceptance_hash, check_id=check_id)
        result = supervisor.finish(handle, timeout=300)
        host = result.get('host_receipt', {})
        if (host.get('schema') != 'ffs.local-check-invocation/v1'
                or host.get('acceptance_hash') != acceptance_hash or host.get('check_id') != check_id
                or type(result.get('returncode')) is not int):
            raise FrontendPolicyRefused('FRONTEND_CHECK_RESULT_UNCERTAIN')
        evidence = [dict(result['evidence'])]
        value = {'check_id': check_id, 'status': 'passed' if result['returncode'] == 0 else 'failed',
                 'evidence': evidence}
        results.append(value)
        output[check_id] = {'status': value['status'], 'evidence': evidence}
        store.transition_activity(token, child.id, expected='active',
            new='succeeded' if result['returncode'] == 0 else 'failed', result=dict(result['evidence']),
            reason='sealed deterministic check completed')
    return results, output


def _completed_check(store, token, *, acceptance_hash, candidate_hash, check_id, key):
    """Reconstruct one check whose child already ran but whose result was never published.

    An owner interrupted between physical completion and publication must not
    relaunch (double debit) nor guess: a completed intent is re-verified through
    the same local-receipt/dispatch binding used at publication; anything else
    that already exists for this key is refused for explicit reconciliation.
    """
    with store.read_transaction() as tx:
        child = tx.execute('SELECT id FROM authority_activities WHERE repository_id=? AND run_id=? AND request_key=?',
                           (token.repository_id, token.run_id, key + ':activity')).fetchone()
        if child is None:
            return None
        intent = tx.execute('SELECT state,completion_status,completion_evidence_json FROM authority_launch_intents '
                            'WHERE activity_id=? ORDER BY created_at DESC', (child['id'],)).fetchone()
        if (intent is None or intent['state'] not in {'completed_succeeded', 'completed_failed'}
                or not intent['completion_evidence_json']):
            raise FrontendPolicyRefused('FRONTEND_CHECK_RECONCILIATION_REQUIRED')
        status = 'passed' if intent['state'] == 'completed_succeeded' else 'failed'
        evidence = [json.loads(intent['completion_evidence_json'])]
        try:
            store._frontend_check_execution_tx(tx, token, acceptance_hash=acceptance_hash,
                                               candidate_hash=candidate_hash, check_id=check_id,
                                               status=status, evidence=evidence)
        except Exception as error:
            raise FrontendPolicyRefused('FRONTEND_CHECK_RECONCILIATION_REQUIRED') from error
    for item in evidence:
        store._verified_evidence(item)
    return {'check_id': check_id, 'status': status, 'evidence': evidence}
