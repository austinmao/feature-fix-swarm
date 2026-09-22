"""Fail-closed E7 saved-stage recovery authority consumer.

The controller is deliberately a reader of the existing ``ControlStore``.
It neither dispatches a child nor accepts a callback saying that a child or a
check passed. Isolated-trial checks are retained by ``recovery_trial_checks``
as one authority event per issued trial, bound to the cycle, issuing intent,
immutable input preparation, exact harvested patch and measured post-patch
candidate; this reader re-verifies those records and selects the smallest
verified winner. Winner integration remains a separate producer seam.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Mapping, Sequence

from .ownership import OwnerToken
from .recovery_docs import CachedDocument
from .run_policy import RoleReceipt, RunPolicyRefused, validate_role_receipt
from .state import ControlStore

_HEX = re.compile(r"[0-9a-f]{64}\Z")
_GIT = re.compile(r"[0-9a-f]{40}\Z")
_PACKET_SCHEMA = "ffs.frontend-recovery-continuation/v1"


class RecoveryRefused(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _digest(value: object, code: str = "RECOVERY_BINDING_INVALID") -> str:
    if not isinstance(value, str) or _HEX.fullmatch(value) is None:
        raise RecoveryRefused(code)
    return value


def _identifier(value: object, code: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 256 or not value.isprintable():
        raise RecoveryRefused(code)
    return value


def _encoded(value: object, code: str) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise RecoveryRefused(code) from error


@dataclass(frozen=True)
class FrozenRecoveryBinding:
    """Common measured input. Each isolated trial has its own preparation."""

    base_head: str
    overlay_hash: str
    candidate_hash: str
    original_candidate_hash: str
    acceptance_hash: str
    runtime_hash: str
    contract_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.base_head, str) or _GIT.fullmatch(self.base_head) is None:
            raise RecoveryRefused("RECOVERY_BASE_HEAD_INVALID")
        for item in (self.overlay_hash, self.candidate_hash, self.original_candidate_hash,
                     self.acceptance_hash, self.runtime_hash, self.contract_hash):
            _digest(item)
        if self.contract_hash != self.acceptance_hash:
            raise RecoveryRefused("RECOVERY_CONTRACT_BINDING_INVALID")

    @property
    def input_hash(self) -> str:
        material = {"acceptance_hash": self.acceptance_hash, "base_head": self.base_head,
                    "candidate_hash": self.candidate_hash, "contract_hash": self.contract_hash,
                    "original_candidate_hash": self.original_candidate_hash,
                    "overlay_hash": self.overlay_hash, "runtime_hash": self.runtime_hash}
        return hashlib.sha256(_encoded(material, "RECOVERY_BINDING_INVALID").encode()).hexdigest()


@dataclass(frozen=True)
class AuthorityCycle:
    action_id: str
    mode: str
    ordinal: int


@dataclass(frozen=True)
class RecoveryTrial:
    """Caller supplies an issued action ID and the recorded role receipt only."""

    action_id: str
    role_receipt: Mapping[str, object]


@dataclass(frozen=True)
class RecoveryDecision:
    saved_stage: str
    cycle: AuthorityCycle
    diagnosis_receipt_hash: str
    documents: tuple[CachedDocument, ...]
    continuation: str
    winner: dict | None = None
    trials: tuple[dict, ...] = ()


class ControlStoreRecoveryAuthority:
    """Concrete read-only authority reader; no arbitrary success callbacks."""

    def __init__(self, store: ControlStore, token: OwnerToken, frozen: FrozenRecoveryBinding) -> None:
        if not isinstance(store, ControlStore) or not isinstance(token, OwnerToken):
            raise RecoveryRefused("RECOVERY_AUTHORITY_REQUIRED")
        self.store, self.token, self.frozen = store, token, frozen

    def cycle(self, action_id: str) -> AuthorityCycle:
        _identifier(action_id, "RECOVERY_CYCLE_INVALID")
        with self.store.read_transaction() as tx:
            budget = tx.execute("SELECT recovery_mode FROM authority_run_policy_budgets WHERE repository_id=? AND run_id=?",
                                (self.token.repository_id, self.token.run_id)).fetchone()
            row = tx.execute("SELECT * FROM authority_policy_actions WHERE id=? AND repository_id=? AND run_id=?",
                             (action_id, self.token.repository_id, self.token.run_id)).fetchone()
            if budget is None or row is None:
                raise RecoveryRefused("RECOVERY_CYCLE_AUTHORITY_MISSING")
            expected = "recovery_cycle_" + budget["recovery_mode"]
            if (row["action"] != expected or row["input_hash"] != self.frozen.input_hash
                    or row["state"] not in {"reserved", "dispatched", "completed_valid"}):
                raise RecoveryRefused("RECOVERY_CYCLE_BINDING_INVALID")
            # The issued cycle is the same immutable field used when binding
            # trial grants. Wall timestamps can tie or move backwards and
            # must never renumber an earlier cycle during recovery replay.
            ordinal = row['recovery_cycle']
            if type(ordinal) is not int or ordinal < 1 or ordinal > (1 if budget['recovery_mode'] == 'normal' else 2):
                raise RecoveryRefused('RECOVERY_CYCLE_BINDING_INVALID')
            issued = [item[0] for item in tx.execute(
                "SELECT recovery_cycle FROM authority_policy_actions WHERE repository_id=? AND run_id=? "
                "AND action=? AND recovery_cycle<=? AND state<>'cancelled'",
                (self.token.repository_id, self.token.run_id, expected, ordinal))]
        if len(issued) != ordinal or set(issued) != set(range(1, ordinal + 1)):
            raise RecoveryRefused("RECOVERY_CYCLE_LIMIT_EXHAUSTED")
        return AuthorityCycle(action_id, budget["recovery_mode"], ordinal)

    def receipt(self, raw: Mapping[str, object], *, cycle: AuthorityCycle,
                expected_action: str, expected_action_id: str | None = None) -> RoleReceipt:
        try:
            typed = validate_role_receipt(raw)
        except RunPolicyRefused as error:
            raise RecoveryRefused("RECOVERY_RECEIPT_INVALID") from error
        # Runtime receipts are workspace-bound and every isolated trial has its
        # own preparation, so a child's runtime tuple is proven below through
        # its committed ControlStore runtime receipt, not by equality with the
        # single sealed recovery runtime (which the handback packet binds).
        if (typed.role != "recovery" or typed.completion_status != "succeeded"
                or typed.acceptance_hash != self.frozen.acceptance_hash
                or typed.candidate_hash != self.frozen.candidate_hash or not typed.evidence):
            raise RecoveryRefused("RECOVERY_RECEIPT_BINDING_INVALID")
        with self.store.read_transaction() as tx:
            try:
                self.store._acceptance_receipt_execution_tx(tx, self.token, typed)
            except Exception as error:
                raise RecoveryRefused("RECOVERY_RECEIPT_BINDING_INVALID") from error
            saved = tx.execute("SELECT receipt_json FROM authority_acceptance_receipts WHERE repository_id=? AND run_id=? "
                               "AND acceptance_hash=? AND receipt_hash=?", (self.token.repository_id, self.token.run_id,
                               self.frozen.acceptance_hash, typed.receipt_hash)).fetchone()
            action = tx.execute("SELECT a.* FROM authority_policy_actions a JOIN authority_policy_action_attempts p "
                                "ON p.action_id=a.id WHERE p.intent_id=? AND a.repository_id=? AND a.run_id=?",
                                (typed.intent_id, self.token.repository_id, self.token.run_id)).fetchone()
            prep = tx.execute("SELECT w.preparation_id,w.base_commit,w.generation,s.input_digest FROM authority_child_bindings b "
                              "JOIN context_workspaces w ON w.preparation_id=b.workspace_preparation_id "
                              "JOIN context_input_snapshots s ON s.preparation_id=w.preparation_id WHERE b.activity_id=?",
                              (typed.activity_id,)).fetchone()
            runtime = tx.execute("SELECT 1 FROM authority_runtime_receipts WHERE producer_activity_id=? "
                                 "AND runtime_tuple_hash=? AND workspace_preparation_id=?",
                                 (typed.activity_id, typed.runtime_hash,
                                  None if prep is None else prep["preparation_id"])).fetchone()
            if (saved is None or saved["receipt_json"] != typed.receipt_json or action is None or runtime is None
                    or action["action"] != expected_action or action["recovery_cycle"] != cycle.ordinal
                    or expected_action_id is not None and action["id"] != expected_action_id
                    or action["state"] not in {"dispatched", "completed_valid"} or prep is None
                    or prep["base_commit"] != self.frozen.base_head or prep["input_digest"] != self.frozen.overlay_hash
                    or prep["generation"] != self.token.generation):
                raise RecoveryRefused("RECOVERY_RECEIPT_AUTHORITY_MISSING")
        try:
            for evidence in typed.evidence:
                self.store._verified_evidence({"locator": evidence["locator"], "sha256": evidence["sha256"]})
        except Exception as error:
            raise RecoveryRefused("RECOVERY_EVIDENCE_INVALID") from error
        return typed

    def trial_checks(self, typed: RoleReceipt, *, cycle: AuthorityCycle, trial_action_id: str) -> dict:
        """Read one trial's retained check record; a trial without it cannot win."""
        from .recovery_trial_checks import trial_checks_key, verify_trial_checks
        with self.store.read_transaction() as tx:
            row = tx.execute("SELECT k.payload_hash,e.payload FROM authority_event_keys k JOIN control_events e "
                             "ON e.id=k.event_id WHERE k.activity_id=? AND k.idempotency_key=?",
                             (typed.activity_id, trial_checks_key(trial_action_id))).fetchone()
        if row is None:
            raise RecoveryRefused("RECOVERY_TRIAL_CHECKS_REQUIRED")
        payload = json.loads(row["payload"])["data"]
        if hashlib.sha256(_encoded(payload, "RECOVERY_TRIAL_CHECKS_INVALID").encode()).hexdigest() != row["payload_hash"]:
            raise RecoveryRefused("RECOVERY_TRIAL_CHECKS_INVALID")
        verified = verify_trial_checks(self.store, self.token, payload, cycle_action_id=cycle.action_id,
                                       trial_action_id=trial_action_id, trial_activity_id=typed.activity_id,
                                       issuing_intent_id=typed.intent_id, expected_input_digest=self.frozen.overlay_hash)
        return {**verified, "activity_id": typed.activity_id, "receipt_hash": typed.receipt_hash}


class RecoveryController:
    """Consume one durable cycle. It never launches or transitions frontend state."""

    def __init__(self, handback: Mapping[str, object], frozen: FrozenRecoveryBinding,
                 documents: Sequence[CachedDocument], authority: ControlStoreRecoveryAuthority) -> None:
        if not isinstance(authority, ControlStoreRecoveryAuthority):
            raise RecoveryRefused("RECOVERY_AUTHORITY_REQUIRED")
        if authority.frozen != frozen:
            raise RecoveryRefused("RECOVERY_AUTHORITY_STALE")
        self.handback = self._packet(handback, frozen)
        self.frozen, self.documents, self.authority = frozen, self._documents(documents), authority

    @staticmethod
    def _packet(packet: Mapping[str, object], frozen: FrozenRecoveryBinding) -> dict:
        required = {"schema", "acceptance_hash", "candidate_hash", "saved_stage", "failed_criteria", "consumed_attempts",
                    "remaining_allowances", "remaining_launches", "remaining_active_ns", "generation", "runtime_hash",
                    "base_candidate_hash", "outstanding_activity_ids", "required_obligation_ids", "choices"}
        value = dict(packet) if isinstance(packet, Mapping) else {}
        if set(value) != required or value["schema"] != _PACKET_SCHEMA or value["saved_stage"] not in {"EXECUTE", "FINAL_REVIEW"}:
            raise RecoveryRefused("RECOVERY_PACKET_INVALID")
        # base_candidate_hash is sealed/original; current candidate may have advanced.
        if (value["acceptance_hash"] != frozen.acceptance_hash or value["candidate_hash"] != frozen.candidate_hash
                or value["base_candidate_hash"] != frozen.original_candidate_hash or value["runtime_hash"] != frozen.runtime_hash
                or type(value["generation"]) is not int or value["generation"] < 1):
            raise RecoveryRefused("RECOVERY_PACKET_STALE")
        if not isinstance(value["consumed_attempts"], list) or (value["saved_stage"] == "FINAL_REVIEW" and "final_review" not in value["consumed_attempts"]):
            raise RecoveryRefused("RECOVERY_FINAL_REVIEW_CONSUMPTION_REQUIRED")
        if not isinstance(value["remaining_allowances"], Mapping):
            raise RecoveryRefused("RECOVERY_PACKET_INVALID")
        _encoded(value, "RECOVERY_PACKET_INVALID")
        return value

    @staticmethod
    def _documents(documents: Sequence[CachedDocument]) -> tuple[CachedDocument, ...]:
        if not isinstance(documents, Sequence) or isinstance(documents, (str, bytes)):
            raise RecoveryRefused("RECOVERY_DOCUMENTS_INVALID")
        result = []
        for document in documents:
            if not isinstance(document, CachedDocument) or not isinstance(document.content, bytes) or not document.content:
                raise RecoveryRefused("RECOVERY_DOCUMENT_BYTES_REQUIRED")
            if (not isinstance(document.metadata, Mapping)
                    or document.metadata.get("content_sha256") != hashlib.sha256(document.content).hexdigest()
                    or any(field not in document.metadata
                           for field in ("dependency", "version", "identity", "source_url", "provenance"))):
                raise RecoveryRefused("RECOVERY_DOCUMENT_INVALID")
            result.append(document)
        return tuple(result)

    def consume(self, cycle_action_id: str, *, diagnosis_receipt: Mapping[str, object],
                trials: Sequence[RecoveryTrial]) -> RecoveryDecision:
        cycle = self.authority.cycle(cycle_action_id)
        remaining = self.handback["remaining_allowances"].get("recovery_cycle_" + cycle.mode)
        if type(remaining) is not int or remaining < 1:
            raise RecoveryRefused("RECOVERY_CYCLE_LIMIT_EXHAUSTED")
        diagnosis = self.authority.receipt(diagnosis_receipt, cycle=cycle, expected_action="diagnosis")
        if not isinstance(trials, Sequence) or isinstance(trials, (str, bytes)) or len(trials) > 3:
            raise RecoveryRefused("RECOVERY_TRIAL_LIMIT_EXHAUSTED")
        action_ids, preparations, verified = set(), set(), []
        for trial in trials:
            if not isinstance(trial, RecoveryTrial) or trial.action_id in action_ids:
                raise RecoveryRefused("RECOVERY_TRIAL_BINDING_INVALID")
            action_ids.add(trial.action_id)
            typed = self.authority.receipt(trial.role_receipt, cycle=cycle, expected_action="recovery_trial",
                                           expected_action_id=trial.action_id)
            if typed.workspace_preparation_hash in preparations:
                raise RecoveryRefused("RECOVERY_TRIAL_ISOLATION_INVALID")
            preparations.add(typed.workspace_preparation_hash)
            verified.append(self.authority.trial_checks(typed, cycle=cycle, trial_action_id=trial.action_id))
        winners = [item for item in verified if item["passed"]]
        winner = min(winners, key=lambda item: (item["patch_size"], item["patch_sha256"])) if winners else None
        continuation = self.handback["saved_stage"] if winner is not None else "NEEDS_DECISION"
        return RecoveryDecision(self.handback["saved_stage"], cycle, diagnosis.receipt_hash, self.documents,
                                continuation, winner, tuple(verified))
