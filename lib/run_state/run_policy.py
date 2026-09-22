"""Pure validation and transition guards for managed acceptance policy.

This module intentionally owns no database connection and has no knowledge of
dispatch envelopes.  ``state.ControlStore`` persists the immutable objects it
validates; supervisor/accounting code can therefore reuse these guards without
gaining a second writer or a second source of budget truth.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Mapping
from contextlib import contextmanager


@contextmanager
def productive_work(store, token, *, kind: str):
    """Bracket local supervisor work without holding a database transaction."""
    budget = store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id)
    if budget is None:
        yield
        return
    interval = store.begin_policy_work(token, kind=kind)
    try:
        yield
    finally:
        store.end_policy_work(token, interval)


# These are cumulative authority allowances, deliberately not scheduler
# settings.  Keeping them here makes the storage adapter and future frontends
# share one small, inspectable vocabulary.
TIER_LIMITS = {
    "small": (12, 1 * 60 * 60 * 1_000_000_000),
    "medium": (48, 4 * 60 * 60 * 1_000_000_000),
    "large": (192, 12 * 60 * 60 * 1_000_000_000),
}
ACTION_LIMITS = {
    "spec_review": (1, 2, 3),
    "repair": (2, 4, 6),
    "final_review": (1, 1, 1),
    "recovery_cycle_normal": (1, 1, 1),
    "recovery_cycle_autonomous": (2, 2, 2),
}
RECOVERY_TRIALS_PER_CYCLE = 3
_NON_MUTATING_ACTIONS = {
    "check", "spec_review", "final_review", "diagnosis", "qualification",
    "recovery_cycle_normal", "recovery_cycle_autonomous",
}
_MUTATING_ACTIONS = {"execute", "repair", "recovery_trial"}


@dataclass(frozen=True)
class PolicyTier:
    name: str
    launch_limit: int
    active_limit_ns: int


def classify_ceremony(estimate: object = None) -> str:
    """Reuse the frozen ceremony thresholds; unknown scope stays medium."""
    if estimate is None:
        return "medium"
    if not isinstance(estimate, dict) or set(estimate) != {"files", "loc", "protected"}:
        raise RunPolicyRefused("POLICY_ESTIMATE_INVALID")
    if (any(type(estimate[key]) is not int or estimate[key] < 0 for key in ("files", "loc"))
            or type(estimate["protected"]) is not bool):
        raise RunPolicyRefused("POLICY_ESTIMATE_INVALID")
    if estimate["protected"] or estimate["files"] > 20 or estimate["loc"] > 1500:
        return "large"
    if estimate["files"] < 5 and estimate["loc"] < 200:
        return "small"
    return "medium"


def policy_tier(value: object) -> PolicyTier:
    if not isinstance(value, str) or value not in TIER_LIMITS:
        raise RunPolicyRefused("POLICY_TIER_INVALID")
    launch_limit, active_limit_ns = TIER_LIMITS[value]
    return PolicyTier(value, launch_limit, active_limit_ns)


def action_limit(action: object, tier: object) -> int | None:
    """Return the named cumulative allowance, or refuse unknown authority.

    ``diagnosis`` has no separate count: it is still a launch/time-consuming
    action, but receives no mutation authority.  Recovery trials are bounded
    by their durable cycle rather than a run-wide total.
    """
    tier_value = policy_tier(tier)
    if not isinstance(action, str) or not action:
        raise RunPolicyRefused("POLICY_ACTION_INVALID")
    if action in {"execute", "check", "diagnosis", "qualification"}:
        return None
    if action == "recovery_trial":
        return RECOVERY_TRIALS_PER_CYCLE
    limits = ACTION_LIMITS.get(action)
    if limits is None:
        raise RunPolicyRefused("POLICY_ACTION_INVALID")
    return limits[("small", "medium", "large").index(tier_value.name)]


def action_can_mutate(action: object) -> bool:
    if not isinstance(action, str) or action not in _NON_MUTATING_ACTIONS | _MUTATING_ACTIONS:
        raise RunPolicyRefused("POLICY_ACTION_INVALID")
    if action in _NON_MUTATING_ACTIONS:
        return False
    return True


def recovery_mode(value: object) -> str:
    if value not in {"normal", "autonomous"}:
        raise RunPolicyRefused("POLICY_RECOVERY_MODE_INVALID")
    return value


POLICY_DRAFT_SCHEMA = "ffs.run-policy-draft/v2"
POLICY_RECEIPT_SCHEMA = "ffs.run-policy-receipt/v1"
HEX_DIGEST_LENGTH = 64


class RunPolicyRefused(ValueError):
    """A structurally invalid policy input, carrying a stable refusal code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class LifecycleState(str, Enum):
    SPEC_DRAFT = "SPEC_DRAFT"
    SPEC_REVIEW = "SPEC_REVIEW"
    SEALED = "SEALED"
    EXECUTE = "EXECUTE"
    FINAL_REVIEW = "FINAL_REVIEW"
    RECOVER = "RECOVER"
    DONE = "DONE"
    NEEDS_DECISION = "NEEDS_DECISION"
    CAPABILITY_FAILURE = "CAPABILITY_FAILURE"
    CANCELLED = "CANCELLED"


class FindingClass(str, Enum):
    CONTRACT_FAILURE = "CONTRACT_FAILURE"
    INVARIANT_VIOLATION = "INVARIANT_VIOLATION"
    FOLLOW_UP = "FOLLOW_UP"
    INVALID = "INVALID"


@dataclass(frozen=True)
class AcceptanceDraft:
    """Canonical, unsealed acceptance material bound by ``ControlStore``."""

    material: dict
    material_json: str
    material_hash: str


@dataclass(frozen=True)
class RoleReceipt:
    """Validated role result, deliberately separate from a launch envelope."""

    role: str
    request_key: str
    activity_id: str
    intent_id: str
    fence_generation: int
    acceptance_hash: str
    candidate_hash: str
    runtime_hash: str
    workspace_preparation_hash: str
    evidence: tuple[dict, ...]
    completion_status: str
    process_identity: dict
    review_dimensions: tuple[str, ...]
    receipt_json: str
    receipt_hash: str


@dataclass(frozen=True)
class FindingDecision:
    classification: FindingClass
    criterion_ids: tuple[str, ...]
    invariant_ids: tuple[str, ...]
    code: str


@dataclass(frozen=True)
class TransitionGuard:
    allowed: bool
    next_state: LifecycleState | None
    code: str


def _canonical(value: object, *, code: str) -> tuple[dict, str, str]:
    if not isinstance(value, dict) or not value:
        raise RunPolicyRefused(code)
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise RunPolicyRefused(code) from error
    return dict(value), encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _identifier(value: object, *, code: str) -> str:
    if (
        not isinstance(value, str) or not value or value != value.strip()
        or len(value.encode("utf-8")) > 256 or not value.isprintable()
    ):
        raise RunPolicyRefused(code)
    return value


def _digest(value: object, *, code: str) -> str:
    if (
        not isinstance(value, str) or len(value) != HEX_DIGEST_LENGTH
        or value != value.lower() or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RunPolicyRefused(code)
    return value


def _identifier_list(value: object, *, code: str, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise RunPolicyRefused(code)
    values = tuple(_identifier(item, code=code) for item in value)
    if (not allow_empty and not values) or len(values) != len(set(values)):
        raise RunPolicyRefused(code)
    return tuple(sorted(values))


def _validate_rule_list(value: object, *, code: str, required_key: str) -> tuple[dict, ...]:
    if not isinstance(value, list) or not value:
        raise RunPolicyRefused(code)
    seen: set[str] = set()
    rules: list[dict] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"id", "kind", required_key}:
            raise RunPolicyRefused(code)
        identifier = _identifier(item["id"], code=code)
        if identifier in seen:
            raise RunPolicyRefused(code)
        seen.add(identifier)
        _identifier(item["kind"], code=code)
        if required_key == "locator":
            _identifier(item[required_key], code=code)
        elif not isinstance(item[required_key], bool):
            raise RunPolicyRefused(code)
        rules.append(dict(item))
    return tuple(sorted(rules, key=lambda item: item["id"]))


def build_draft_material(
    *, objective_digest: str, criteria: list[dict], exclusions: list[dict],
    global_invariants: list[dict], requested_runtime_hash: str,
    effective_runtime_hash: str, candidate_hash: str, generation: int,
    command_mode: str, follow_up_policy: str = "record-and-continue",
    required_review_dimensions: tuple[str, ...] = ("correctness", "security", "regression"),
) -> dict:
    """Build the only supported draft shape; validation remains explicit."""
    return {
        "schema": POLICY_DRAFT_SCHEMA,
        "objective_digest": objective_digest,
        "criteria": criteria,
        "exclusions": exclusions,
        "global_invariants": global_invariants,
        "runtime": {
            "requested_hash": requested_runtime_hash,
            "effective_hash": effective_runtime_hash,
        },
        "candidate_hash": candidate_hash,
        "generation": generation,
        "command_mode": command_mode,
        "follow_up_policy": follow_up_policy,
        "required_review_dimensions": list(required_review_dimensions),
    }


def validate_draft_material(value: object) -> AcceptanceDraft:
    """Validate immutable executable criteria, evidence and identity bindings.

    A draft has no dispatch-envelope field by design.  It is intentionally a
    closed schema so a future control-significant field cannot be smuggled in
    without a versioned validator and migration.
    """
    material, _encoded, _hash = _canonical(value, code="POLICY_DRAFT_INVALID")
    fields = {
        "schema", "objective_digest", "criteria", "exclusions", "global_invariants",
        "runtime", "candidate_hash", "generation", "command_mode", "follow_up_policy",
    }
    if material.get("schema") == POLICY_DRAFT_SCHEMA:
        fields.add("required_review_dimensions")
        _identifier_list(material.get("required_review_dimensions"), code="POLICY_DRAFT_INVALID")
    elif material.get("schema") != "ffs.run-policy-draft/v1":
        raise RunPolicyRefused("POLICY_DRAFT_INVALID")
    if set(material) != fields:
        raise RunPolicyRefused("POLICY_DRAFT_INVALID")
    _digest(material["objective_digest"], code="POLICY_DRAFT_INVALID")
    if isinstance(material["generation"], bool) or not isinstance(material["generation"], int) or material["generation"] < 1:
        raise RunPolicyRefused("POLICY_DRAFT_INVALID")
    _identifier(material["command_mode"], code="POLICY_DRAFT_INVALID")
    if material["follow_up_policy"] != "record-and-continue":
        raise RunPolicyRefused("POLICY_DRAFT_INVALID")
    runtime = material["runtime"]
    if not isinstance(runtime, dict) or set(runtime) != {"requested_hash", "effective_hash"}:
        raise RunPolicyRefused("POLICY_DRAFT_INVALID")
    _digest(runtime["requested_hash"], code="POLICY_DRAFT_INVALID")
    _digest(runtime["effective_hash"], code="POLICY_DRAFT_INVALID")
    _digest(material["candidate_hash"], code="POLICY_DRAFT_INVALID")

    if not isinstance(material["criteria"], list) or not material["criteria"]:
        raise RunPolicyRefused("POLICY_DRAFT_INVALID")
    criteria: list[dict] = []
    criterion_ids: set[str] = set()
    check_ids: set[str] = set()
    evidence_ids: set[str] = set()
    for item in material["criteria"]:
        if not isinstance(item, dict) or set(item) != {"id", "objective_clause", "checks", "evidence_rules"}:
            raise RunPolicyRefused("POLICY_DRAFT_INVALID")
        criterion_id = _identifier(item["id"], code="POLICY_DRAFT_INVALID")
        _identifier(item["objective_clause"], code="POLICY_DRAFT_INVALID")
        if criterion_id in criterion_ids:
            raise RunPolicyRefused("POLICY_DRAFT_INVALID")
        criterion_ids.add(criterion_id)
        checks = _validate_rule_list(item["checks"], code="POLICY_DRAFT_INVALID", required_key="locator")
        evidence_rules = _validate_rule_list(item["evidence_rules"], code="POLICY_DRAFT_INVALID", required_key="required")
        if check_ids.intersection(rule["id"] for rule in checks) or evidence_ids.intersection(rule["id"] for rule in evidence_rules):
            raise RunPolicyRefused("POLICY_DRAFT_INVALID")
        check_ids.update(rule["id"] for rule in checks)
        evidence_ids.update(rule["id"] for rule in evidence_rules)
        criteria.append({
            "id": criterion_id, "objective_clause": item["objective_clause"],
            "checks": list(checks), "evidence_rules": list(evidence_rules),
        })

    def validate_labeled(items: object) -> tuple[dict, ...]:
        if not isinstance(items, list):
            raise RunPolicyRefused("POLICY_DRAFT_INVALID")
        seen: set[str] = set()
        result: list[dict] = []
        for item in items:
            if not isinstance(item, dict) or set(item) != {"id", "reason"}:
                raise RunPolicyRefused("POLICY_DRAFT_INVALID")
            identifier = _identifier(item["id"], code="POLICY_DRAFT_INVALID")
            _identifier(item["reason"], code="POLICY_DRAFT_INVALID")
            if identifier in seen:
                raise RunPolicyRefused("POLICY_DRAFT_INVALID")
            seen.add(identifier)
            result.append(dict(item))
        return tuple(sorted(result, key=lambda item: item["id"]))

    exclusions = validate_labeled(material["exclusions"])
    invariants = validate_labeled(material["global_invariants"])
    canonical = {
        **material,
        "criteria": sorted(criteria, key=lambda item: item["id"]),
        "exclusions": list(exclusions),
        "global_invariants": list(invariants),
    }
    canonical, encoded, digest = _canonical(canonical, code="POLICY_DRAFT_INVALID")
    return AcceptanceDraft(canonical, encoded, digest)


def validate_role_receipt(value: object, *, required_review_dimensions: tuple[str, ...] = ()) -> RoleReceipt:
    """Validate a receipt before any lifecycle or accounting decision.

    ``required_review_dimensions`` is supplied by the sealed contract caller;
    it keeps role validation pure and makes an incomplete clean review fail
    closed.
    """
    receipt, encoded, digest = _canonical(value, code="POLICY_RECEIPT_INVALID")
    fields = {
        "schema", "role", "request_key", "activity_id", "intent_id", "fence_generation",
        "acceptance_hash", "candidate_hash", "runtime_hash", "workspace_preparation_hash",
        "evidence", "completion_status", "process_identity", "review_dimensions",
    }
    if set(receipt) != fields or receipt["schema"] != POLICY_RECEIPT_SCHEMA:
        raise RunPolicyRefused("POLICY_RECEIPT_INVALID")
    role = _identifier(receipt["role"], code="POLICY_RECEIPT_INVALID")
    # Receipt roles are authority labels, not caller-provided prose.  Keep the
    # whitelist synchronized with ControlStore's child-role binding.
    if role not in {"execution", "review", "recovery", "qualification"}:
        raise RunPolicyRefused("POLICY_RECEIPT_ROLE_INVALID")
    for name in ("request_key", "activity_id", "intent_id"):
        _identifier(receipt[name], code="POLICY_RECEIPT_INVALID")
    if isinstance(receipt["fence_generation"], bool) or not isinstance(receipt["fence_generation"], int) or receipt["fence_generation"] < 1:
        raise RunPolicyRefused("POLICY_RECEIPT_INVALID")
    for name in ("acceptance_hash", "candidate_hash", "runtime_hash", "workspace_preparation_hash"):
        _digest(receipt[name], code="POLICY_RECEIPT_INVALID")
    if receipt["completion_status"] not in {"succeeded", "failed", "timed_out", "crashed", "rejected"}:
        raise RunPolicyRefused("POLICY_RECEIPT_INVALID")
    if not isinstance(receipt["evidence"], list):
        raise RunPolicyRefused("POLICY_RECEIPT_INVALID")
    evidence: list[dict] = []
    for item in receipt["evidence"]:
        if not isinstance(item, dict) or set(item) != {"id", "sha256", "locator"}:
            raise RunPolicyRefused("POLICY_RECEIPT_INVALID")
        _identifier(item["id"], code="POLICY_RECEIPT_INVALID")
        _digest(item["sha256"], code="POLICY_RECEIPT_INVALID")
        _identifier(item["locator"], code="POLICY_RECEIPT_INVALID")
        evidence.append(dict(item))
    if len({item["id"] for item in evidence}) != len(evidence):
        raise RunPolicyRefused("POLICY_RECEIPT_INVALID")
    if not isinstance(receipt["process_identity"], dict) or set(receipt["process_identity"]) != {"host_id", "boot_id", "pid", "start_token"}:
        raise RunPolicyRefused("POLICY_RECEIPT_INVALID")
    for name in ("host_id", "boot_id", "start_token"):
        _identifier(receipt["process_identity"][name], code="POLICY_RECEIPT_INVALID")
    if isinstance(receipt["process_identity"]["pid"], bool) or not isinstance(receipt["process_identity"]["pid"], int) or receipt["process_identity"]["pid"] < 1:
        raise RunPolicyRefused("POLICY_RECEIPT_INVALID")
    dimensions = _identifier_list(receipt["review_dimensions"], code="POLICY_RECEIPT_INVALID", allow_empty=True)
    required = tuple(sorted({_identifier(item, code="POLICY_RECEIPT_INVALID") for item in required_review_dimensions}))
    if role == "review" and receipt["completion_status"] == "succeeded" and not set(required).issubset(dimensions):
        raise RunPolicyRefused("POLICY_REVIEW_DIMENSIONS_MISSING")
    return RoleReceipt(
        role, receipt["request_key"], receipt["activity_id"], receipt["intent_id"], receipt["fence_generation"],
        receipt["acceptance_hash"], receipt["candidate_hash"], receipt["runtime_hash"],
        receipt["workspace_preparation_hash"], tuple(evidence), receipt["completion_status"],
        dict(receipt["process_identity"]), dimensions, encoded, digest,
    )


def guard_transition(
    state: LifecycleState | str, event: str, *, acceptance_sealed: bool = False,
    reservation_valid: bool = False, verified_receipt: bool = False,
    capability_failure: bool = False, uncertainty: bool = False, cancelled: bool = False,
) -> TransitionGuard:
    """Return a deterministic lifecycle decision without consulting storage.

    Reservations, retries, clocks and fence settlement are represented by the
    inputs so E4 can compose this guard; their durable accounting is purposely
    not implemented here.
    """
    try:
        current = LifecycleState(state)
    except ValueError:
        return TransitionGuard(False, None, "POLICY_STATE_INVALID")
    if current in {LifecycleState.DONE, LifecycleState.NEEDS_DECISION, LifecycleState.CAPABILITY_FAILURE, LifecycleState.CANCELLED}:
        return TransitionGuard(False, None, "POLICY_TERMINAL")
    if capability_failure or uncertainty:
        return TransitionGuard(True, LifecycleState.CAPABILITY_FAILURE, "CAPABILITY_FAILURE")
    if cancelled:
        return TransitionGuard(True, LifecycleState.CANCELLED, "CANCELLED")
    if event == "seal":
        return TransitionGuard(current in {LifecycleState.SPEC_DRAFT, LifecycleState.SPEC_REVIEW} and acceptance_sealed,
                               LifecycleState.SEALED if acceptance_sealed else None,
                               "OK" if acceptance_sealed else "ACCEPTANCE_SEAL_REQUIRED")
    if event == "dispatch":
        return TransitionGuard(current == LifecycleState.SEALED and acceptance_sealed and reservation_valid,
                               LifecycleState.EXECUTE if acceptance_sealed and reservation_valid else None,
                               "OK" if acceptance_sealed and reservation_valid else "RESERVATION_REQUIRED")
    if event == "finalize":
        return TransitionGuard(current == LifecycleState.FINAL_REVIEW and verified_receipt,
                               LifecycleState.DONE if verified_receipt else None,
                               "OK" if verified_receipt else "ACCEPTANCE_RECEIPT_REQUIRED")
    return TransitionGuard(False, None, "POLICY_EVENT_INVALID")


def classify_finding(
    finding: Mapping[str, object], *, acceptance_hash: str, candidate_hash: str,
    runtime_hash: str, criteria: Mapping[str, tuple[str, ...]], invariants: tuple[str, ...],
) -> FindingDecision:
    """Fail closed unless a finding names a sealed check or invariant.

    The criterion mapping is the immutable criterion-id -> executable-check-id
    map from a sealed draft.  A claimed criterion without an actual sealed
    check is therefore an invalid finding, rather than a scope-expanding one.
    """
    required = {"acceptance_hash", "candidate_hash", "runtime_hash", "criterion_ids", "check_ids", "invariant_ids", "evidence_valid"}
    if set(finding) != required or finding.get("evidence_valid") is not True:
        return FindingDecision(FindingClass.INVALID, (), (), "FINDING_EVIDENCE_INVALID")
    if any(finding.get(name) != expected for name, expected in (
        ("acceptance_hash", acceptance_hash), ("candidate_hash", candidate_hash), ("runtime_hash", runtime_hash),
    )):
        return FindingDecision(FindingClass.INVALID, (), (), "FINDING_BINDING_INVALID")
    try:
        criterion_ids = _identifier_list(finding["criterion_ids"], code="FINDING_INVALID", allow_empty=True)
        check_ids = _identifier_list(finding["check_ids"], code="FINDING_INVALID", allow_empty=True)
        invariant_ids = _identifier_list(finding["invariant_ids"], code="FINDING_INVALID", allow_empty=True)
    except RunPolicyRefused:
        return FindingDecision(FindingClass.INVALID, (), (), "FINDING_INVALID")
    known_invariants = tuple(sorted(set(invariant_ids).intersection(invariants)))
    if known_invariants:
        return FindingDecision(FindingClass.INVARIANT_VIOLATION, (), known_invariants, "INVARIANT_MATCHED")
    matched = tuple(criterion for criterion in criterion_ids if criterion in criteria and set(criteria[criterion]).intersection(check_ids))
    if matched:
        return FindingDecision(FindingClass.CONTRACT_FAILURE, tuple(sorted(matched)), (), "CRITERION_CHECK_MATCHED")
    if criterion_ids or check_ids or invariant_ids:
        return FindingDecision(FindingClass.INVALID, (), (), "FINDING_SCOPE_INVALID")
    return FindingDecision(FindingClass.FOLLOW_UP, (), (), "FOLLOW_UP")
