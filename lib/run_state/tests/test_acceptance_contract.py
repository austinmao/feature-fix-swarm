"""Acceptance-contract persistence and activation boundaries."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from process_identity import ProcessIdentity
from run_state.ownership import OwnershipRefused, StartRequest, reserve_resources
from run_state.state import ControlStore


def _managed_run(tmp_path):
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
        "managed-run", str(workspace), "objective", ProcessIdentity.current(),
        repository_id="repository", planning_scope="scope",
    ))
    material = {
        "repository_id": "repository", "run_id": "managed-run",
        "objective_digest": "a" * 64, "input_digest": "b" * 64,
        "request_key": "managed-request", "request_digest": "c" * 64,
    }
    with store.transaction() as tx:
        tx.execute(
            "INSERT INTO context_runs "
            "(repository_id,run_id,objective_digest,objective_text,planning_scope,workspace,workspace_key,"
            "evidence_root,state,generation,activity_id,activity_kind,input_digest,request_key,request_digest,"
            "writer_version,upstream_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("repository", "managed-run", material["objective_digest"], "objective", "scope",
             str(workspace), str(workspace), "/evidence", "preparing", owned.token.generation,
             "activity", "plan", material["input_digest"], material["request_key"],
             material["request_digest"], "ffs-supervisor/1", "{}", "now", "now"),
        )
        tx.execute(
            "INSERT INTO context_requests(repository_id,request_key,request_digest,run_id,created_at) "
            "VALUES(?,?,?,?,?)",
            ("repository", material["request_key"], material["request_digest"], "managed-run", "now"),
        )
    store.configure_run_limits(owned.token, dispatch_limit=7, token_limit=99, worker_capacity=2)
    return store, owned.token, material


def _amendment_grant(store, token, *, target: str, key: str) -> str:
    return store.create_grant(
        token, action="acceptance-amend", target=target,
        provenance={"operator": "fixture", "reason": key},
        expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        idempotency_key=f"acceptance-amend-grant:{key}",
    ).id


def test_initial_contract_is_one_time_and_bound_to_managed_material(tmp_path):
    store, token, material = _managed_run(tmp_path)

    contract = store.create_initial_acceptance_contract(
        token, accepted_requirement_ids=["REQ-2", "REQ-1"], material=material,
    )

    assert contract.generation == 1
    assert contract.material == material
    assert contract.accepted_requirement_ids == ("REQ-1", "REQ-2")
    assert contract.active_obligation_ids == ()
    assert len(contract.contract_hash) == 64
    assert store.get_acceptance_contract(repository_id="repository", run_id="managed-run") == contract

    with pytest.raises(OwnershipRefused, match="ACCEPTANCE_INITIAL_REPLAY"):
        store.create_initial_acceptance_contract(
            token, accepted_requirement_ids=["REQ-1", "REQ-2"], material=material,
        )
    with pytest.raises(OwnershipRefused, match="ACCEPTANCE_MATERIAL_CONFLICT"):
        store.create_initial_acceptance_contract(
            token, accepted_requirement_ids=["REQ-1"],
            material={**material, "input_digest": "d" * 64},
        )


def test_proposals_stay_non_blocking_until_operator_amendment_activates_them(tmp_path):
    store, token, material = _managed_run(tmp_path)
    initial = store.create_initial_acceptance_contract(
        token, accepted_requirement_ids=["REQ-1", "REQ-2"], material=material,
    )
    proposal = store.append_proposed_acceptance_obligation(
        token, obligation_id="proof", requirement_ids=["REQ-2"],
        material={"kind": "proof", "locator": "evidence://proof"},
    )
    assert proposal.active is False
    assert store.get_acceptance_contract(repository_id="repository", run_id="managed-run") == initial

    with store.read_transaction() as tx:
        budgets_before = dict(tx.execute(
            "SELECT dispatch_limit,token_limit,worker_capacity,dispatch_used,token_committed,token_used "
            "FROM authority_run_limits WHERE repository_id=? AND run_id=?",
            ("repository", "managed-run"),
        ).fetchone())

    amended = store.amend_acceptance_contract(
        token, amendment_id="operator-001",
        amendment={"operator": "lumin", "reason": "proof is required"},
        grant_id=_amendment_grant(store, token, target=initial.contract_hash, key="operator-001"),
        activate_obligation_ids=["proof"],
    )
    assert amended.generation == 2
    assert amended.parent_contract_hash == initial.contract_hash
    assert amended.contract_hash != initial.contract_hash
    assert amended.active_obligation_ids == ("proof",)
    assert store.list_acceptance_obligations(repository_id="repository", run_id="managed-run")[0].active

    with store.read_transaction() as tx:
        budgets_after = dict(tx.execute(
            "SELECT dispatch_limit,token_limit,worker_capacity,dispatch_used,token_committed,token_used "
            "FROM authority_run_limits WHERE repository_id=? AND run_id=?",
            ("repository", "managed-run"),
        ).fetchone())
    assert budgets_after == budgets_before


def test_amendment_rejects_unfrozen_references_and_replay_without_partial_activation(tmp_path):
    store, token, material = _managed_run(tmp_path)
    store.create_initial_acceptance_contract(
        token, accepted_requirement_ids=["REQ-1"], material=material,
    )
    store.append_proposed_acceptance_obligation(
        token, obligation_id="scope-creep", requirement_ids=["REQ-NOT-ACCEPTED"],
        material={"kind": "review", "label": "scope creep"},
    )

    with pytest.raises(OwnershipRefused, match="ACCEPTANCE_REQUIREMENT_REFERENCE_INVALID"):
        store.amend_acceptance_contract(
            token, amendment_id="operator-invalid", amendment={"operator": "op", "reason": "bad"},
            grant_id=_amendment_grant(
                store, token,
                target=store.get_acceptance_contract(repository_id="repository", run_id="managed-run").contract_hash,
                key="operator-invalid",
            ),
            activate_obligation_ids=["scope-creep"],
        )
    assert store.get_acceptance_contract(repository_id="repository", run_id="managed-run").generation == 1
    assert not store.list_acceptance_obligations(repository_id="repository", run_id="managed-run")[0].active

    store.append_proposed_acceptance_obligation(
        token, obligation_id="valid", requirement_ids=["REQ-1"],
        material={"kind": "review", "label": "valid"},
    )
    store.amend_acceptance_contract(
        token, amendment_id="operator-valid", amendment={"operator": "op", "reason": "valid"},
        grant_id=_amendment_grant(
            store, token,
            target=store.get_acceptance_contract(repository_id="repository", run_id="managed-run").contract_hash,
            key="operator-valid",
        ),
        activate_obligation_ids=["valid"],
    )
    with pytest.raises(OwnershipRefused, match="ACCEPTANCE_AMENDMENT_REPLAY"):
        store.amend_acceptance_contract(
            token, amendment_id="operator-valid", amendment={"operator": "op", "reason": "valid"},
            grant_id=_amendment_grant(
                store, token,
                target=store.get_acceptance_contract(repository_id="repository", run_id="managed-run").contract_hash,
                key="operator-valid-replay",
            ),
            activate_obligation_ids=[],
        )


def test_reading_a_legacy_authority_does_not_install_acceptance_schema(tmp_path):
    authority = tmp_path / "authority"
    authority.mkdir(mode=0o700)
    store = ControlStore(authority / "control.sqlite3")
    before = store.db_path.read_bytes()

    assert store.get_acceptance_contract(repository_id="repository", run_id="missing") is None
    assert store.list_acceptance_obligations(repository_id="repository", run_id="missing") == ()
    assert store.db_path.read_bytes() == before
    with store.read_transaction() as tx:
        assert not tx.execute(
            "SELECT 1 FROM sqlite_master WHERE name='authority_acceptance_contracts'"
        ).fetchone()


def test_amendment_requires_a_live_single_use_grant_for_the_current_contract(tmp_path):
    store, token, material = _managed_run(tmp_path)
    initial = store.create_initial_acceptance_contract(
        token, accepted_requirement_ids=["REQ-1"], material=material,
    )
    store.append_proposed_acceptance_obligation(
        token, obligation_id="valid", requirement_ids=["REQ-1"], material={"kind": "proof"},
    )

    with pytest.raises(OwnershipRefused, match="ACCEPTANCE_AMENDMENT_GRANT_REQUIRED"):
        store.amend_acceptance_contract(
            token, amendment_id="missing", amendment={"operator": "op"}, grant_id="",
            activate_obligation_ids=["valid"],
        )
    wrong = _amendment_grant(store, token, target="f" * 64, key="wrong")
    with pytest.raises(OwnershipRefused, match="GRANT_MISMATCH"):
        store.amend_acceptance_contract(
            token, amendment_id="wrong", amendment={"operator": "op"}, grant_id=wrong,
            activate_obligation_ids=["valid"],
        )
    expired = _amendment_grant(store, token, target=initial.contract_hash, key="expired")
    with store.transaction() as tx:
        tx.execute("UPDATE authority_grants SET expires_at='1970-01-01T00:00:00Z' WHERE id=?", (expired,))
    with pytest.raises(OwnershipRefused, match="GRANT_EXPIRED"):
        store.amend_acceptance_contract(
            token, amendment_id="expired", amendment={"operator": "op"}, grant_id=expired,
            activate_obligation_ids=["valid"],
        )
    assert store.get_acceptance_contract(repository_id="repository", run_id="managed-run").generation == 1
    assert not store.list_acceptance_obligations(repository_id="repository", run_id="managed-run")[0].active

    grant = _amendment_grant(store, token, target=initial.contract_hash, key="single-use")
    store.amend_acceptance_contract(
        token, amendment_id="single-use", amendment={"operator": "op"}, grant_id=grant,
        activate_obligation_ids=["valid"],
    )
    with pytest.raises(OwnershipRefused, match="IDEMPOTENCY_CONFLICT"):
        store.amend_acceptance_contract(
            token, amendment_id="second-use", amendment={"operator": "op"}, grant_id=grant,
            activate_obligation_ids=[],
        )
