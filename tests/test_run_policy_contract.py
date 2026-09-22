"""E3 draft/seal storage, replay, legacy compatibility, and pure guard tests."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from process_identity import ProcessIdentity
from run_state.ownership import OwnershipRefused, StartRequest, reserve_resources
from run_state.run_policy import (
    FindingClass,
    LifecycleState,
    RunPolicyRefused,
    build_draft_material,
    classify_finding,
    guard_transition,
    validate_draft_material,
)
from run_state.state import ControlStore


def _digest(character: str) -> str:
    return character * 64


def _managed_run(tmp_path, monkeypatch):
    """A wholly disposable authority with the existing managed ingress rows."""
    identity = ProcessIdentity("fixture-host", "fixture-boot", os.getpid(), "fixture-start")
    monkeypatch.setattr(ProcessIdentity, "current", classmethod(lambda cls: identity))
    authority = tmp_path / "authority"
    workspace = tmp_path / "workspace"
    authority.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    store = ControlStore(authority / "control.sqlite3")
    store.ensure_context_schema()
    store.ensure_authority_schema()
    with store.transaction() as tx:
        tx.execute(
            "INSERT INTO context_repositories "
            "(repository_id,marker_id,common_dir,filesystem_id,primary_root,workspace_root,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ("repository", "marker", "/common", "filesystem", "/primary", "/workspaces", "now"),
        )
    owned = reserve_resources(store, StartRequest(
        "managed-run", str(workspace), "objective", identity,
        repository_id="repository", planning_scope="scope",
    ))
    binding = {
        "repository_id": "repository", "run_id": "managed-run",
        "objective_digest": _digest("a"), "input_digest": _digest("b"),
        "request_key": "managed-request", "request_digest": _digest("c"),
    }
    with store.transaction() as tx:
        tx.execute(
            "INSERT INTO context_runs "
            "(repository_id,run_id,objective_digest,objective_text,planning_scope,workspace,workspace_key,"
            "evidence_root,state,generation,activity_id,activity_kind,input_digest,request_key,request_digest,"
            "writer_version,upstream_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("repository", "managed-run", binding["objective_digest"], "objective", "scope",
             str(workspace), str(workspace), "/evidence", "preparing", owned.token.generation,
             "activity", "plan", binding["input_digest"], binding["request_key"],
             binding["request_digest"], "ffs-supervisor/1", "{}", "now", "now"),
        )
        tx.execute(
            "INSERT INTO context_requests(repository_id,request_key,request_digest,run_id,created_at) "
            "VALUES(?,?,?,?,?)",
            ("repository", binding["request_key"], binding["request_digest"], "managed-run", "now"),
        )
    legacy = store.create_initial_acceptance_contract(
        owned.token, accepted_requirement_ids=["REQ-2", "REQ-1"], material=binding,
    )
    return store, owned.token, binding, legacy


def _draft(binding, generation=1, candidate="d"):
    return build_draft_material(
        objective_digest=binding["objective_digest"],
        criteria=[
            {
                "id": "REQ-1", "objective_clause": "repair the requested behavior",
                "checks": [{"id": "check-1", "kind": "command", "locator": "pytest tests/a.py"}],
                "evidence_rules": [{"id": "evidence-1", "kind": "test-output", "required": True}],
            },
            {
                "id": "REQ-2", "objective_clause": "preserve existing behavior",
                "checks": [{"id": "check-2", "kind": "command", "locator": "pytest tests/b.py"}],
                "evidence_rules": [{"id": "evidence-2", "kind": "test-output", "required": True}],
            },
        ],
        exclusions=[{"id": "excluded-docs", "reason": "documentation is outside objective"}],
        global_invariants=[{"id": "no-control-tamper", "reason": "control records are protected"}],
        requested_runtime_hash=_digest("e"), effective_runtime_hash=_digest("f"),
        candidate_hash=_digest(candidate), generation=generation, command_mode="feature-spec",
    )


def _receipt(sealed, *, candidate="d"):
    return {
        "schema": "ffs.run-policy-receipt/v1", "role": "review",
        "request_key": "review-1", "activity_id": "activity-1", "intent_id": "intent-1",
        "fence_generation": 1, "acceptance_hash": sealed.acceptance_hash,
        "candidate_hash": _digest(candidate), "runtime_hash": _digest("f"),
        "workspace_preparation_hash": _digest("9"),
        "evidence": [{"id": "evidence-1", "sha256": _digest("8"), "locator": "evidence://review"}],
        "completion_status": "succeeded",
        "process_identity": {"host_id": "fixture-host", "boot_id": "fixture-boot", "pid": 1, "start_token": "one"},
        "review_dimensions": ["correctness"],
    }


def test_draft_seal_and_receipt_are_versioned_immutable_and_replay_safe(tmp_path, monkeypatch):
    store, token, binding, legacy = _managed_run(tmp_path, monkeypatch)
    material = _draft(binding)

    draft = store.create_acceptance_draft(
        token, draft_id="spec-draft", revision=1,
        acceptance_contract_hash=legacy.contract_hash, material=material,
    )
    replay_draft = store.create_acceptance_draft(
        token, draft_id="spec-draft", revision=1,
        acceptance_contract_hash=legacy.contract_hash, material=material,
    )
    assert replay_draft.reused and replay_draft.draft_hash == draft.draft_hash
    sealed = store.seal_acceptance_draft(
        token, draft_id="spec-draft", revision=1, acceptance_contract_hash=legacy.contract_hash,
    )
    assert sealed.legacy_contract_hash == legacy.contract_hash
    assert sealed.acceptance_hash != legacy.contract_hash
    assert sealed.material["candidate_hash"] == _digest("d")
    assert store.seal_acceptance_draft(
        token, draft_id="spec-draft", revision=1, acceptance_contract_hash=legacy.contract_hash,
    ).reused
    with pytest.raises(OwnershipRefused, match="ACCEPTANCE_SEAL_BINDING_INVALID"):
        store.seal_acceptance_draft(
            token, draft_id="spec-draft", revision=1, acceptance_contract_hash=_digest("0"),
        )
    assert store.get_sealed_acceptance(repository_id="repository", run_id="managed-run") == sealed

    # Structurally valid strings do not constitute a launched reviewer or
    # observed evidence. The production slice covers real receipt replay.
    with pytest.raises(OwnershipRefused, match="ACCEPTANCE_RECEIPT_BINDING_INVALID"):
        store.record_acceptance_receipt(token, acceptance_hash=sealed.acceptance_hash, receipt=_receipt(sealed))
    with pytest.raises(OwnershipRefused, match="ACCEPTANCE_RECEIPT_BINDING_INVALID"):
        store.record_acceptance_receipt(
            token, acceptance_hash=sealed.acceptance_hash, receipt=_receipt(sealed, candidate="7"),
        )
    # The old adapter and its hash retain their original semantics.
    assert store.get_acceptance_contract(repository_id="repository", run_id="managed-run") == legacy


@pytest.mark.parametrize("change", ["check", "exclusion", "invariant"])
def test_changed_draft_cannot_reseal_a_legacy_generation(tmp_path, monkeypatch, change):
    store, token, binding, legacy = _managed_run(tmp_path, monkeypatch)
    first = store.create_acceptance_draft(
        token, draft_id="first", revision=1,
        acceptance_contract_hash=legacy.contract_hash, material=_draft(binding),
    )
    first_seal = store.seal_acceptance_draft(
        token, draft_id=first.draft_id, revision=first.revision,
        acceptance_contract_hash=legacy.contract_hash,
    )
    changed = _draft(binding)
    if change == "check":
        changed["criteria"][0]["checks"][0]["locator"] = "true"
    elif change == "exclusion":
        changed["exclusions"][0]["reason"] = "narrowed after seal"
    else:
        changed["global_invariants"][0]["reason"] = "weakened after seal"
    replacement = store.create_acceptance_draft(
        token, draft_id=f"replacement-{change}", revision=2,
        acceptance_contract_hash=legacy.contract_hash, material=changed,
    )
    with pytest.raises(OwnershipRefused, match="ACCEPTANCE_SEAL_GENERATION_CONFLICT"):
        store.seal_acceptance_draft(
            token, draft_id=replacement.draft_id, revision=replacement.revision,
            acceptance_contract_hash=legacy.contract_hash,
        )
    # The original seal remains the only stable replay for generation one.
    assert store.seal_acceptance_draft(
        token, draft_id=first.draft_id, revision=first.revision,
        acceptance_contract_hash=legacy.contract_hash,
    ).acceptance_hash == first_seal.acceptance_hash


def test_concurrent_distinct_seals_allow_only_one_legacy_generation_winner(tmp_path, monkeypatch):
    store, token, binding, legacy = _managed_run(tmp_path, monkeypatch)
    first = store.create_acceptance_draft(
        token, draft_id="concurrent-first", revision=1,
        acceptance_contract_hash=legacy.contract_hash, material=_draft(binding),
    )
    changed = _draft(binding)
    changed["criteria"][0]["checks"][0]["locator"] = "true"
    second = store.create_acceptance_draft(
        token, draft_id="concurrent-second", revision=2,
        acceptance_contract_hash=legacy.contract_hash, material=changed,
    )

    def seal(draft):
        try:
            return store.seal_acceptance_draft(
                token, draft_id=draft.draft_id, revision=draft.revision,
                acceptance_contract_hash=legacy.contract_hash,
            ).acceptance_hash
        except OwnershipRefused as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = set(executor.map(seal, (first, second)))
    assert "ACCEPTANCE_SEAL_GENERATION_CONFLICT" in outcomes
    assert len(outcomes) == 2
    with store.read_transaction() as tx:
        assert tx.execute("SELECT COUNT(*) FROM authority_sealed_acceptances").fetchone()[0] == 1


def test_draft_must_match_legacy_criteria_and_seal_cannot_follow_an_operator_generation(tmp_path, monkeypatch):
    store, token, binding, legacy = _managed_run(tmp_path, monkeypatch)
    invalid = _draft(binding)
    invalid["criteria"] = invalid["criteria"][:1]
    with pytest.raises(OwnershipRefused, match="ACCEPTANCE_DRAFT_SCOPE_INVALID"):
        store.create_acceptance_draft(
            token, draft_id="bad", revision=1,
            acceptance_contract_hash=legacy.contract_hash, material=invalid,
        )
    sealed_draft = store.create_acceptance_draft(
        token, draft_id="sealed", revision=1,
        acceptance_contract_hash=legacy.contract_hash, material=_draft(binding),
    )
    sealed = store.seal_acceptance_draft(
        token, draft_id=sealed_draft.draft_id, revision=sealed_draft.revision,
        acceptance_contract_hash=legacy.contract_hash,
    )
    stale_draft = store.create_acceptance_draft(
        token, draft_id="stale", revision=1,
        acceptance_contract_hash=legacy.contract_hash, material=_draft(binding),
    )

    store.append_proposed_acceptance_obligation(
        token, obligation_id="operator-proof", requirement_ids=["REQ-1"], material={"kind": "proof"},
    )
    grant = store.create_grant(
        token, action="acceptance-amend", target=legacy.contract_hash,
        provenance={"operator": "fixture"},
        expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        idempotency_key="operator-amend",
    )
    amended = store.amend_acceptance_contract(
        token, amendment_id="operator-amend", amendment={"reason": "explicit grant"},
        grant_id=grant.id, activate_obligation_ids=["operator-proof"],
    )
    assert store.seal_acceptance_draft(
        token, draft_id=sealed_draft.draft_id, revision=sealed_draft.revision,
        acceptance_contract_hash=legacy.contract_hash,
    ).acceptance_hash == sealed.acceptance_hash
    with pytest.raises(OwnershipRefused, match="ACCEPTANCE_SEAL_STALE_DRAFT"):
        store.seal_acceptance_draft(
            token, draft_id=stale_draft.draft_id, revision=stale_draft.revision,
            acceptance_contract_hash=legacy.contract_hash,
        )
    # A post-amendment generation needs material that names its new generation.
    with pytest.raises(OwnershipRefused, match="ACCEPTANCE_DRAFT_BINDING_INVALID"):
        store.create_acceptance_draft(
            token, draft_id="stale", revision=1,
            acceptance_contract_hash=legacy.contract_hash, material=_draft(binding),
        )
    new_draft = store.create_acceptance_draft(
        token, draft_id="operator-generation", revision=1,
        acceptance_contract_hash=amended.contract_hash, material=_draft(binding, generation=2, candidate="7"),
    )
    assert new_draft.acceptance_generation == 2
    new_seal = store.seal_acceptance_draft(
        token, draft_id=new_draft.draft_id, revision=new_draft.revision,
        acceptance_contract_hash=amended.contract_hash,
    )
    assert new_seal.legacy_generation == amended.generation == 2
    assert new_seal.acceptance_generation == 2


def test_legacy_read_does_not_install_e3_policy_tables(tmp_path):
    authority = tmp_path / "authority"
    authority.mkdir(mode=0o700)
    store = ControlStore(authority / "control.sqlite3")
    before = store.db_path.read_bytes()

    assert store.get_sealed_acceptance(repository_id="repository", run_id="missing") is None
    assert store.db_path.read_bytes() == before


def test_policy_validator_rejects_dispatch_hash_laundering_and_pure_guards_fail_closed():
    material = _draft({"objective_digest": _digest("a")})
    material["dispatch_envelope_hash"] = _digest("b")
    with pytest.raises(RunPolicyRefused, match="POLICY_DRAFT_INVALID"):
        validate_draft_material(material)

    finding = classify_finding(
        {
            "acceptance_hash": _digest("1"), "candidate_hash": _digest("2"), "runtime_hash": _digest("3"),
            "criterion_ids": ["REQ-1"], "check_ids": ["wrong-check"], "invariant_ids": [], "evidence_valid": True,
        },
        acceptance_hash=_digest("1"), candidate_hash=_digest("2"), runtime_hash=_digest("3"),
        criteria={"REQ-1": ("check-1",)}, invariants=("no-control-tamper",),
    )
    assert finding.classification is FindingClass.INVALID
    invariant = classify_finding(
        {
            "acceptance_hash": _digest("1"), "candidate_hash": _digest("2"), "runtime_hash": _digest("3"),
            "criterion_ids": [], "check_ids": [], "invariant_ids": ["no-control-tamper"], "evidence_valid": True,
        },
        acceptance_hash=_digest("1"), candidate_hash=_digest("2"), runtime_hash=_digest("3"),
        criteria={}, invariants=("no-control-tamper",),
    )
    assert invariant.classification is FindingClass.INVARIANT_VIOLATION
    assert guard_transition(LifecycleState.SPEC_REVIEW, "seal").allowed is False
    assert guard_transition(LifecycleState.SPEC_REVIEW, "seal", acceptance_sealed=True).next_state is LifecycleState.SEALED
    assert guard_transition(LifecycleState.EXECUTE, "anything", capability_failure=True).next_state is LifecycleState.CAPABILITY_FAILURE
