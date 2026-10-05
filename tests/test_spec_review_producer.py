"""spec-014 E8 prerequisite 3c: the production native spec-review producer over the real Supervisor authority chain.

The reviewer executable is the final-review producer test's Python telemetry fixture with a spec-review reply
(``ffs.spec-review/v1``) added beside its sealed-review one: it contacts no model and uses no real credential.
Runtime qualification is that file's fixture seam, so this proves the producer's sequencing, its single-grant replay
discipline and its terminal outcomes before a draft is sealed; it is not native host qualification and not E8.
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

requires_local_confinement = pytest.mark.skipif(
    sys.platform != "darwin" or not os.path.isfile("/usr/bin/sandbox-exec"),
    reason="needs Darwin sandbox-exec local-check confinement (non-Darwin fails closed; covered by test_local_check_transport)",
)
pytestmark = requires_local_confinement

import test_frontend_final_review_producer as final_review_tests
from run_state.managed import build_frontend_acceptance_draft, prepare_managed_run
from run_state.managed_admission import ManagedAdmissionQueue
from run_state.resource_observation import ResourceObservation
from run_state.shared_resources import SharedResourceCoordinator, cold_start_demand
from run_state.state import qualified_runtime_tuple_hash
from run_state.supervisor import Supervisor, SupervisorRefused
from test_managed_production_ingress import _setup
from test_native_review_supervisor_dispatch import _commit_same_activity_runtime
from test_runtime_receipt_authority import _qualified

# The sealed-review reply of ``test_frontend_final_review_producer._COMMON``, which needs a final review's checks.
_FINAL_REPLY = '''check = next(iter(context["checks"].values()))["evidence"][0]
status = "failed" if "FAIL" in pathlib.Path("src/input.txt").read_text() else "passed"
criteria = {cid: {"status": status, "evidence": [{"id": rid, **check}
            for rid in spec["required_evidence_ids_for_pass"]]} for cid, spec in contract["criteria"].items()}
text = json.dumps({**contract["fixed_fields"], "criteria": criteria, "findings": []},
                  sort_keys=True, separators=(",", ":"))
'''
_SPEC_REPLY = '''if contract["fixed_fields"]["schema"] == "ffs.spec-review/v1":
    verdict = json.load(open(%r))["spec_review"]
    marks = {cid: {"status": "revise" if verdict == "revise" else "acceptable", "reason": "fixture " + verdict}
             for cid in contract["criteria"]}
    text = "not json" if verdict == "malformed" else json.dumps(
        {**contract["fixed_fields"], "verdict": "revise" if verdict == "revise" else "accept",
         "criteria": marks, "notes": []}, sort_keys=True, separators=(",", ":"))
else:
''' + "".join("    " + line + "\n" for line in _FINAL_REPLY.splitlines())


def _spec_script(original, host: str, mode_path) -> bytes:
    """The final-review fixture host with a spec-review reply, sharing its credential-revocation protocol."""
    script = original(host).decode()
    assert script.count(_FINAL_REPLY) == 1
    return script.replace(_FINAL_REPLY, _SPEC_REPLY % str(mode_path)).encode()


def _operator(case, revision: int = 1, note: str = "fixture") -> dict:
    """The operator's draft as ``seal_from_draft`` receives it; a different ``note`` is a different draft hash."""
    criterion = case.legacy.accepted_requirement_ids[0]
    return {"draft_id": "spec", "revision": revision, "criteria": [{
        "id": criterion, "objective_clause": "review the retained fixture input",
        "checks": [{"id": "fixture-check", "kind": "command",
                    "locator": "/bin/bash --noprofile --norc -c 'printf fixture-check-stdout'"}],
        "evidence_rules": [{"id": "fixture-evidence", "kind": "log", "required": True}]}],
        "exclusions": [{"id": "none", "reason": note}], "global_invariants": [{"id": "no-commit", "reason": "fixture"}]}


def _persist(case, revision: int = 1, note: str = "fixture"):
    """The unsealed draft row ``seal_from_draft`` persists before it asks for the review."""
    operator = _operator(case, revision, note)
    material = build_frontend_acceptance_draft(
        objective_digest=case.legacy.material["objective_digest"], criteria=operator["criteria"],
        exclusions=operator["exclusions"], global_invariants=operator["global_invariants"],
        requested_runtime_hash=case.tuple_hash, effective_runtime_hash=case.tuple_hash,
        candidate_hash=case.ready.input_digest, generation=case.legacy.generation, command_mode="feature-implement")
    return case.store.create_acceptance_draft(
        case.token, draft_id="spec", revision=revision, acceptance_contract_hash=case.legacy.contract_hash,
        material=material)


def _run_case(tmp_path, monkeypatch, host, check, *, verdict="accept", estimate=None):
    mode = tmp_path / "spec-review-mode.json"
    mode.write_text(json.dumps({"spec_review": verdict}))
    original = final_review_tests._script
    monkeypatch.setattr(final_review_tests, "_script", lambda name: _spec_script(original, name, mode))
    primary, authority, _repository_id, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    monkeypatch.setenv("FFS_FIXTURE_PARENT_SECRET", "fixture-only")
    seen = []

    def execute(store, token, context):
        try:
            return _execute(store, token, context)
        except Exception:
            # The managed ingress maps every failure to a typed refusal; keep the fixture cause visible.
            import traceback
            traceback.print_exc()
            raise

    def _execute(store, token, context):
        store.bind_runtime(token, context.activity_id, "b" * 64)
        queue = ManagedAdmissionQueue(tmp_path / "resource-registry", observation_provider=lambda:
            ResourceObservation(time.monotonic_ns(), 4, 4 << 30, 4 << 30, 100, 100, {host: 4}, "fixture"))

        def new_supervisor(fault_probe=None):
            return Supervisor(store, token, evidence_root=authority / "review-evidence", fault_probe=fault_probe,
                shared_resource_coordinator=SharedResourceCoordinator(store, token, queue=queue),
                resource_demand_policy=cold_start_demand)

        supervisor = new_supervisor()
        parent, ready = final_review_tests._candidate(store, token, supervisor,
                                                      text=b"base-input\nfixture reviewed candidate\n")
        worker_runtime = _qualified(ready.path)
        tuple_hash = qualified_runtime_tuple_hash(worker_runtime)
        legacy = store.get_acceptance_contract(repository_id=token.repository_id, run_id=token.run_id)
        # The outer orchestrator: qualified, unlaunched and not yet sealed to anything.
        worker = store.create_child_activity(
            token, parent_activity_id=parent["activity_id"], role="worker", request_key="executed:activity",
            candidate_hash=ready.input_digest, contract_hash=legacy.contract_hash, runtime_identity=tuple_hash,
            workspace_binding=str(ready.path), workspace_preparation_id=ready.id, retry_budget=2)
        _commit_same_activity_runtime(store, token, worker, worker_runtime)
        seam = final_review_tests._fixture_seam(tmp_path, host, store, token)
        check(SimpleNamespace(store=store, token=token, supervisor=supervisor, new_supervisor=new_supervisor,
                              seam=seam, worker=worker, ready=ready, legacy=legacy, tuple_hash=tuple_hash,
                              mode=mode))
        seen.append(True)
        return 0

    result = prepare_managed_run(objective=env["FFS_OBJECTIVE"], state_root=authority,
        selection_manifest=env["FFS_SELECTION_MANIFEST"],
        upstream_runtime_manifest=env["FFS_UPSTREAM_RUNTIME_MANIFEST"],
        upstream_runtime_sha256=env["FFS_UPSTREAM_RUNTIME_SHA256"], request_key=env["FFS_REQUEST_KEY"],
        run_id=env["GSD_RUN_ID"], command=("/gsd-plan-phase", "1"), activity="plan", scope="1",
        dispatch_limit=8, token_limit=1000, ceremony_estimate=estimate, on_ready=execute)
    assert result == 0 and seen == [True]


SMALL = {"files": 1, "loc": 1, "protected": False}


def _produce(case, draft, *, supervisor=None, seam=None):
    from run_state.frontend_producers import produce_spec_review
    return produce_spec_review(
        case.store, case.token, supervisor=supervisor or case.supervisor, seam=seam or case.seam,
        parent_activity_id=case.worker.id, preparation=case.ready, draft=draft, timeout_seconds=15)


def _counting(seam):
    """The same seam, counting every qualification it is asked for (each one is four probes in production)."""
    calls, real = [], seam.qualify

    def qualify(*args, **kwargs):
        calls.append(args[0])
        return real(*args, **kwargs)
    return dataclasses.replace(seam, qualify=qualify), calls


_NOT_A_PROBE = "NOT EXISTS (SELECT 1 FROM authority_qualification_launches q WHERE q.intent_id=i.id)"


def _counts(store, parent_id):
    with store.read_transaction() as tx:
        ids = [row[0] for row in tx.execute(
            "SELECT activity_id FROM authority_child_bindings WHERE parent_activity_id=? AND role='reviewer'",
            (parent_id,)).fetchall()]
        marks = ",".join("?" for _ in ids) or "''"
        intents = tx.execute(f"SELECT count(*) FROM authority_launch_intents i WHERE i.activity_id IN ({marks}) "
                             f"AND {_NOT_A_PROBE}", ids).fetchone()[0]
        actions = tx.execute("SELECT count(*) FROM authority_policy_actions "
                             "WHERE action='spec_review' AND state<>'cancelled'").fetchone()[0]
        attempts = tx.execute(f"SELECT count(*) FROM authority_policy_action_attempts p JOIN authority_launch_intents i "
                              f"ON i.id=p.intent_id WHERE i.activity_id IN ({marks}) AND {_NOT_A_PROBE}",
                              ids).fetchone()[0]
        receipts = tx.execute("SELECT count(*) FROM authority_acceptance_receipts").fetchone()[0]
        final_reviews = tx.execute("SELECT count(*) FROM authority_policy_actions "
                                   "WHERE action='final_review'").fetchone()[0]
    return {"reviewers": len(ids), "intents": intents, "actions": actions, "attempts": attempts,
            "receipts": receipts, "final_reviews": final_reviews}


_ONE = {"reviewers": 1, "intents": 1, "actions": 1, "attempts": 1, "receipts": 0, "final_reviews": 0}
_NONE = {"reviewers": 0, "intents": 0, "actions": 0, "attempts": 0, "receipts": 0, "final_reviews": 0}


def _reviewers(case):
    with case.store.read_transaction() as tx:
        return [tuple(row) for row in tx.execute(
            "SELECT a.request_key,a.state FROM authority_activities a JOIN authority_child_bindings b "
            "ON b.activity_id=a.id WHERE b.parent_activity_id=? AND b.role='reviewer' ORDER BY a.created_at",
            (case.worker.id,))]


def _sealed(case):
    return case.store.get_sealed_acceptance(repository_id=case.token.repository_id, run_id=case.token.run_id)


def _policy_state(case):
    return case.store.get_frontend_policy_state(repository_id=case.token.repository_id, run_id=case.token.run_id)


def _retained(case, draft):
    from run_state.spec_review import retained_spec_review
    return retained_spec_review(case.store, case.token, draft)


@pytest.mark.parametrize("host", ["codex", "claude"])
def test_producer_reviews_the_draft_natively_with_one_spec_review_grant(tmp_path, monkeypatch, host):
    def check(case):
        draft = _persist(case)
        record = _produce(case, draft)
        assert (record["verdict"], record["draft_hash"], record["candidate_hash"]) == (
            "accept", draft.draft_hash, case.ready.input_digest)
        assert _counts(case.store, case.worker.id) == _ONE
        assert [state for _key, state in _reviewers(case)] == ["succeeded"]
        assert _reviewers(case)[0][0] == "spec-review:" + draft.draft_hash[:16] + ":reviewer"
        # Nothing is sealed, no lifecycle stage exists, and the review is not an acceptance receipt.
        assert _sealed(case) is None and _policy_state(case) is None
        with case.store.read_transaction() as tx:
            usage = [row[0] for row in tx.execute(
                "SELECT token_usage FROM authority_launch_intents WHERE completion_status='succeeded'").fetchall()]
        assert (8 if host == "codex" else 7) in usage
        assert _retained(case, draft)["verdict"] == "accept"
        # Replay after the record exists spends and launches nothing.
        assert _produce(case, draft, supervisor=case.new_supervisor()) == record
        assert _counts(case.store, case.worker.id) == _ONE
    _run_case(tmp_path, monkeypatch, host, check)


def test_accepted_record_lets_freeze_seal_the_same_draft_and_refuses_a_revised_one(tmp_path, monkeypatch):
    from run_state.frontend_producers import seal_from_draft

    def check(case):
        def seal(operator, review):
            return seal_from_draft(case.store, case.token, command_mode="feature-implement", draft=operator,
                                   runtime_hash=case.tuple_hash, candidate_hash=case.ready.input_digest,
                                   review=review)
        captured = []
        # A review that reviewed nothing is no review: the draft is persisted and not sealed.
        with pytest.raises(SupervisorRefused, match=r"^SPEC_REVIEW_REQUIRED$"):
            seal(_operator(case), captured.append)
        assert _sealed(case) is None and _policy_state(case) is None
        draft = captured[0]
        record = _produce(case, draft)
        # A revised draft is a new hash with no record: it is not sealed on the strength of the first one.
        with pytest.raises(SupervisorRefused, match=r"^SPEC_REVIEW_REQUIRED$"):
            seal(_operator(case, revision=2, note="a revised exclusion"), lambda _row: None)
        assert _sealed(case) is None and _policy_state(case) is None
        seal(_operator(case), lambda _row: None)
        sealed = _sealed(case)
        assert (sealed.draft_hash, sealed.draft_revision) == (draft.draft_hash, 1) == (record["draft_hash"], 1)
        assert _policy_state(case).stage == "SEALED"
        assert _counts(case.store, case.worker.id) == _ONE
    _run_case(tmp_path, monkeypatch, "codex", check)


def test_revise_verdict_spends_the_grant_retains_the_record_and_refuses_typed_on_replay(tmp_path, monkeypatch):
    from run_state.frontend_producers import seal_from_draft

    def check(case):
        draft = _persist(case)
        with pytest.raises(SupervisorRefused, match=r"^SPEC_REVIEW_REJECTED$"):
            _produce(case, draft)
        assert _counts(case.store, case.worker.id) == _ONE
        # The spent grant's verdict is durable, and the child that gave it is ended.
        assert _retained(case, draft)["verdict"] == "revise"
        assert [state for _key, state in _reviewers(case)] == ["succeeded"]
        with pytest.raises(SupervisorRefused, match=r"^SPEC_REVIEW_REJECTED$"):
            _produce(case, draft, supervisor=case.new_supervisor())
        assert _counts(case.store, case.worker.id) == _ONE
        # Through the seal path the same verdict refuses, and nothing is sealed.
        with pytest.raises(SupervisorRefused, match=r"^SPEC_REVIEW_REJECTED$"):
            seal_from_draft(case.store, case.token, command_mode="feature-implement", draft=_operator(case),
                            runtime_hash=case.tuple_hash, candidate_hash=case.ready.input_digest,
                            review=lambda row: _produce(case, row, supervisor=case.new_supervisor()))
        assert _sealed(case) is None and _policy_state(case) is None
        assert _counts(case.store, case.worker.id) == _ONE
    _run_case(tmp_path, monkeypatch, "codex", check, verdict="revise")


def test_malformed_output_refuses_without_a_record_and_replays_without_a_second_launch(tmp_path, monkeypatch):
    def check(case):
        draft = _persist(case)
        with pytest.raises(SupervisorRefused, match=r"^SPEC_REVIEW_OUTPUT_INVALID$"):
            _produce(case, draft)
        assert _counts(case.store, case.worker.id) == _ONE
        assert _retained(case, draft) is None
        assert [state for _key, state in _reviewers(case)] == ["failed"]
        with pytest.raises(SupervisorRefused, match=r"^SPEC_REVIEW_OUTPUT_INVALID$"):
            _produce(case, draft, supervisor=case.new_supervisor())
        assert _counts(case.store, case.worker.id) == _ONE
        assert _retained(case, draft) is None
    _run_case(tmp_path, monkeypatch, "codex", check, verdict="malformed")


@pytest.mark.parametrize(("short_by", "outcome"), [(4, "SPEC_REVIEW_BUDGET_INFEASIBLE"), (5, "accept")])
def test_budget_short_of_probes_plus_launch_refuses_before_any_charge(tmp_path, monkeypatch, short_by, outcome):
    """Four qualification probes and the review launch need five launches; one fewer refuses before the first probe."""
    def check(case):
        draft = _persist(case)
        with case.store.transaction() as tx:
            tx.execute("UPDATE authority_run_policy_budgets SET launch_charged=launch_limit-? "
                       "WHERE repository_id=? AND run_id=?", (short_by, case.token.repository_id, case.token.run_id))
        before = case.store.get_run_policy_budget(repository_id=case.token.repository_id, run_id=case.token.run_id)
        seam, qualifications = _counting(case.seam)
        if outcome == "accept":
            assert _produce(case, draft, seam=seam)["verdict"] == "accept"
            assert len(qualifications) == 1
            return
        with pytest.raises(SupervisorRefused, match=r"^SPEC_REVIEW_BUDGET_INFEASIBLE$"):
            _produce(case, draft, seam=seam)
        after = case.store.get_run_policy_budget(repository_id=case.token.repository_id, run_id=case.token.run_id)
        assert qualifications == [] and after.launch_charged == before.launch_charged
        assert _counts(case.store, case.worker.id) == _NONE and _reviewers(case) == []
    _run_case(tmp_path, monkeypatch, "codex", check)


def test_spent_tier_allowance_refuses_before_any_probe(tmp_path, monkeypatch):
    def check(case):
        first = _persist(case)
        with pytest.raises(SupervisorRefused, match=r"^SPEC_REVIEW_REJECTED$"):
            _produce(case, first)
        assert case.store.get_run_policy_budget(
            repository_id=case.token.repository_id, run_id=case.token.run_id).tier == "small"
        revised = _persist(case, revision=2, note="a revised exclusion")
        seam, qualifications = _counting(case.seam)
        before = case.store.get_run_policy_budget(repository_id=case.token.repository_id, run_id=case.token.run_id)
        with pytest.raises(SupervisorRefused, match=r"^POLICY_ACTION_LIMIT_EXHAUSTED$"):
            _produce(case, revised, seam=seam)
        after = case.store.get_run_policy_budget(repository_id=case.token.repository_id, run_id=case.token.run_id)
        assert qualifications == [] and after.launch_charged == before.launch_charged
        assert _counts(case.store, case.worker.id) == _ONE and len(_reviewers(case)) == 1
    _run_case(tmp_path, monkeypatch, "codex", check, verdict="revise", estimate=SMALL)


def test_unacknowledged_intent_refuses_replay_without_a_second_launch(tmp_path, monkeypatch):
    def check(case):
        draft = _persist(case)

        def probe(point):
            if point == "after_intent_commit":
                raise RuntimeError("fixture crash before acknowledgement")
        with pytest.raises(RuntimeError, match="before acknowledgement"):
            _produce(case, draft, supervisor=case.new_supervisor(fault_probe=probe))
        before = _counts(case.store, case.worker.id)
        assert before == _ONE
        with pytest.raises(SupervisorRefused, match=r"^INTENT_RECONCILIATION_REQUIRED$"):
            _produce(case, draft, supervisor=case.new_supervisor())
        assert _counts(case.store, case.worker.id) == before
        with case.store.read_transaction() as tx:
            assert tx.execute(
                "SELECT count(*) FROM authority_launch_intents i JOIN authority_child_bindings b ON b.activity_id=i.activity_id "
                "WHERE b.role='reviewer' AND i.child_pid IS NOT NULL AND " + _NOT_A_PROBE).fetchone()[0] == 0
    _run_case(tmp_path, monkeypatch, "codex", check)


def test_completed_review_of_an_earlier_fence_refuses_typed(tmp_path, monkeypatch):
    import run_state.frontend_producers as producers

    def check(case):
        draft = _persist(case)

        record, crashed = producers.record_spec_review, []

        def killed(*args, **kwargs):
            if not crashed:
                crashed.append(True)
                raise RuntimeError("fixture crash before the record")
            return record(*args, **kwargs)
        monkeypatch.setattr(producers, "record_spec_review", killed)
        with pytest.raises(RuntimeError, match="before the record"):
            _produce(case, draft)
        assert _counts(case.store, case.worker.id) == _ONE and _retained(case, draft) is None
        # The intent now binds an owner fence that is no longer this one: its proof can never be recorded here.
        with case.store.transaction() as tx:
            tx.execute("UPDATE authority_launch_intents SET generation=generation+1 WHERE completion_status='succeeded' "
                       "AND activity_id IN (SELECT activity_id FROM authority_child_bindings WHERE role='reviewer')")
        with pytest.raises(SupervisorRefused, match=r"^SPEC_REVIEW_RECONCILIATION_REQUIRED$"):
            _produce(case, draft, supervisor=case.new_supervisor())
        assert _counts(case.store, case.worker.id) == _ONE and _retained(case, draft) is None
        assert [state for _key, state in _reviewers(case)] == ["active"]
    _run_case(tmp_path, monkeypatch, "codex", check)


def test_stale_unlaunched_grant_is_released_and_one_new_grant_reserved(tmp_path, monkeypatch):
    def check(case):
        draft = _persist(case)
        crashed = []
        reserve = case.supervisor.reserve_request_action

        def reserve_then_crash(*args, **kwargs):
            value = reserve(*args, **kwargs)
            crashed.append(True)
            raise RuntimeError("fixture crash after the grant")
        monkeypatch.setattr(case.supervisor, "reserve_request_action", reserve_then_crash)
        with pytest.raises(RuntimeError, match="after the grant"):
            _produce(case, draft)
        assert crashed
        with case.store.read_transaction() as tx:
            assert [tuple(row) for row in tx.execute(
                "SELECT state,intent_id FROM authority_policy_actions WHERE action='spec_review'")] == [("reserved", None)]
        record = _produce(case, draft, supervisor=case.new_supervisor())
        assert record["verdict"] == "accept"
        with case.store.read_transaction() as tx:
            states = sorted(row[0] for row in tx.execute(
                "SELECT state FROM authority_policy_actions WHERE action='spec_review'"))
        assert states == ["cancelled", "dispatched"]
        counts = _counts(case.store, case.worker.id)
        assert (counts["actions"], counts["intents"], counts["attempts"]) == (1, 1, 1)
    _run_case(tmp_path, monkeypatch, "codex", check)


def test_restricted_path_refuses_a_spec_review_of_a_sealed_draft(tmp_path, monkeypatch):
    def check(case):
        draft = _persist(case)
        case.store.seal_acceptance_draft(case.token, draft_id="spec", revision=1,
                                         acceptance_contract_hash=draft.acceptance_contract_hash)
        with pytest.raises(SupervisorRefused, match=r"^NATIVE_REVIEW_BINDING_INVALID$"):
            _produce(case, draft)
        # Refused at validation, before a grant, an intent or a record exists.
        assert _counts(case.store, case.worker.id)["actions"] == 0
        assert _counts(case.store, case.worker.id)["intents"] == 0
        assert _retained(case, draft) is None
    _run_case(tmp_path, monkeypatch, "codex", check)


def test_draft_binding_names_the_reviewed_draft_only(tmp_path, monkeypatch):
    """The reviewer child is bound to one draft hash: the same candidate under another draft is not its review."""
    import run_state.native_review_supervision as supervision
    from run_state.native_review_transport import read_native_review_launch

    def check(case):
        draft = _persist(case)
        _produce(case, draft)
        with case.store.read_transaction() as tx:
            reviewer_id = tx.execute("SELECT activity_id FROM authority_child_bindings WHERE role='reviewer'").fetchone()[0]
        published = sorted((case.supervisor.evidence_root / "native-review-material").iterdir())
        assert len(published) == 1
        material = read_native_review_launch(published[0], expected_material_sha256=published[0].stem)
        candidate = case.ready.input_digest
        # The reviewed draft passes while it is unsealed.
        assert supervision._binding(case.supervisor, reviewer_id, material, draft.draft_hash, candidate) is None
        other = _persist(case, revision=2, note="a revised exclusion")
        assert other.draft_hash != draft.draft_hash and other.material["candidate_hash"] == candidate
        for contract_hash, candidate_hash in ((other.draft_hash, candidate), (draft.draft_hash, "f" * 64),
                                              ("f" * 64, candidate)):
            with pytest.raises(SupervisorRefused, match=r"^NATIVE_REVIEW_BINDING_INVALID$"):
                supervision._binding(case.supervisor, reviewer_id, material, contract_hash, candidate_hash)
        # A sealed draft is no longer reviewable, however the review was bound.
        case.store.seal_acceptance_draft(case.token, draft_id="spec", revision=1,
                                         acceptance_contract_hash=draft.acceptance_contract_hash)
        with pytest.raises(SupervisorRefused, match=r"^NATIVE_REVIEW_BINDING_INVALID$"):
            supervision._binding(case.supervisor, reviewer_id, material, draft.draft_hash, candidate)
    _run_case(tmp_path, monkeypatch, "codex", check)


# --- review R1-1: the opt-in is not in the draft's hash, so the review requirement has to outlive the key ------

def _seal_draft(case, operator, review):
    from run_state.frontend_producers import seal_from_draft
    return seal_from_draft(case.store, case.token, command_mode="feature-implement", draft=operator,
                           runtime_hash=case.tuple_hash, candidate_hash=case.ready.input_digest, review=review)


@pytest.mark.parametrize("verdict", ["revise", "accept"])
def test_removing_the_key_from_a_reviewed_draft_never_seals_it(tmp_path, monkeypatch, verdict):
    def check(case):
        draft = _persist(case)
        if verdict == "accept":
            _produce(case, draft)
        else:
            with pytest.raises(SupervisorRefused, match=r"^SPEC_REVIEW_REJECTED$"):
                _produce(case, draft)
        assert _retained(case, draft)["verdict"] == verdict
        # The same draft id and revision, with only the `spec_review` key gone (`_operator` carries none).
        with pytest.raises(SupervisorRefused, match=r"^SPEC_REVIEW_REQUIRED$"):
            _seal_draft(case, _operator(case), None)
        assert _sealed(case) is None and _policy_state(case) is None
        assert _counts(case.store, case.worker.id) == _ONE
    _run_case(tmp_path, monkeypatch, "codex", check, verdict=verdict)


def test_removing_the_key_from_a_draft_whose_review_was_granted_never_seals_it(tmp_path, monkeypatch):
    def check(case):
        draft = _persist(case)

        def probe(point):
            if point == "after_intent_commit":
                raise RuntimeError("fixture crash before acknowledgement")
        with pytest.raises(RuntimeError, match="before acknowledgement"):
            _produce(case, draft, supervisor=case.new_supervisor(fault_probe=probe))
        assert _retained(case, draft) is None and _counts(case.store, case.worker.id)["actions"] == 1
        with pytest.raises(SupervisorRefused, match=r"^SPEC_REVIEW_REQUIRED$"):
            _seal_draft(case, _operator(case), None)
        assert _sealed(case) is None and _policy_state(case) is None
    _run_case(tmp_path, monkeypatch, "codex", check)


def test_a_draft_row_nobody_reviewed_still_seals_without_the_key(tmp_path, monkeypatch):
    def check(case):
        _persist(case)
        _seal_draft(case, _operator(case), None)
        assert _policy_state(case).stage == "SEALED"
        assert _counts(case.store, case.worker.id) == _NONE
    _run_case(tmp_path, monkeypatch, "codex", check)



# --- review R1-6: a sealed draft is refused before the reviewer is captured or qualified -------------------------

def test_a_sealed_draft_is_refused_before_any_workspace_or_probe(tmp_path, monkeypatch):
    def check(case):
        draft = _persist(case)
        case.store.seal_acceptance_draft(case.token, draft_id="spec", revision=1,
                                         acceptance_contract_hash=draft.acceptance_contract_hash)
        seam, qualifications = _counting(case.seam)
        before = case.store.get_run_policy_budget(repository_id=case.token.repository_id, run_id=case.token.run_id)
        with pytest.raises(SupervisorRefused, match=r"^NATIVE_REVIEW_BINDING_INVALID$"):
            _produce(case, draft, seam=seam)
        after = case.store.get_run_policy_budget(repository_id=case.token.repository_id, run_id=case.token.run_id)
        # The restricted path would refuse it too, but only after the reviewer was captured and four probes charged.
        assert qualifications == [] and after.launch_charged == before.launch_charged
        assert _reviewers(case) == [] and _counts(case.store, case.worker.id) == _NONE
        with case.store.read_transaction() as tx:
            assert tx.execute("SELECT count(*) FROM context_workspaces "
                              "WHERE child_request_key LIKE 'spec-review:%'").fetchone()[0] == 0
    _run_case(tmp_path, monkeypatch, "codex", check)
