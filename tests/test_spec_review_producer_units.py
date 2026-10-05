"""spec-014 E8 prerequisite 3c: direct unit tests for the native spec review's contract, record and seal gate.

Each test pins ONE exact outcome (a value, a typed refusal code).  The record is the only thing that lets an
unsealed draft be sealed, so every fact it binds is varied on its own and must refuse ``SPEC_REVIEW_RECORD_INVALID``.
Fixture-level proof only: not native host qualification, not E8.
"""
from __future__ import annotations

import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from process_identity import ProcessIdentity
from run_state.supervisor import SupervisorRefused


@pytest.fixture(autouse=True)
def _stable_test_identity(monkeypatch):
    monkeypatch.setattr(ProcessIdentity, "current", classmethod(
        lambda cls: cls("test-host", "test-boot", os.getpid(), "test-start")))


CANDIDATE = "c" * 64
SCHEMA = "ffs.spec-review/v1"
FINAL_SCHEMA = "ffs.sealed-final-review/v1"


def _material(contract_json):
    return SimpleNamespace(artifact=SimpleNamespace(output_contract_json=contract_json))


def _contract(schema) -> str:
    return json.dumps({"fixed_fields": {"schema": schema}})


def _draft(*, repository="budget-repository", run="budget-run", draft_hash="e" * 64):
    from run_state.run_policy import build_draft_material
    from run_state.state import AcceptanceDraft
    material = build_draft_material(
        objective_digest="a" * 64,
        criteria=[{"id": "objective:one", "objective_clause": "the input is retained",
                   "checks": [{"id": "input-check", "kind": "command", "locator": "/usr/bin/true"}],
                   "evidence_rules": [{"id": "input-evidence", "kind": "log", "required": True}]},
                  {"id": "objective:two", "objective_clause": "the output is retained",
                   "checks": [{"id": "output-check", "kind": "command", "locator": "/usr/bin/true"}],
                   "evidence_rules": [{"id": "output-evidence", "kind": "log", "required": True}]}],
        exclusions=[{"id": "none", "reason": "fixture"}], global_invariants=[{"id": "no-commit", "reason": "fixture"}],
        requested_runtime_hash="b" * 64, effective_runtime_hash="b" * 64, candidate_hash=CANDIDATE, generation=1,
        command_mode="feature-implement")
    return AcceptanceDraft(repository, run, "draft", 1, 1, "d" * 64, draft_hash, material)


# --- the review kind is derived from the hash-bound output contract, never from a caller flag ----------------

@pytest.mark.parametrize("contract_json", [
    _contract(FINAL_SCHEMA), None, "", "not json", "[]", "3", json.dumps({"fixed_fields": 3}),
    json.dumps({"fixed_fields": {}}), json.dumps({"fixed_fields": {"schema": "ffs.spec-review/v2"}}),
], ids=["final-review", "absent", "empty", "not-json", "list", "number", "fixed-fields-not-a-dict", "no-schema",
        "other-version"])
def test_review_action_is_final_review_for_final_review_and_absent_contracts(contract_json):
    from run_state.native_review_supervision import _review_action
    assert _review_action(_material(contract_json)) == "final_review"


def test_review_action_is_spec_review_only_for_the_spec_review_schema():
    from run_state.native_review_supervision import _review_action
    assert _review_action(_material(_contract(SCHEMA))) == "spec_review"


def test_spec_review_output_contract_names_exactly_the_draft_criteria_and_fixed_hashes():
    from run_state.spec_review import spec_review_output_contract
    draft = _draft()
    contract = spec_review_output_contract(draft)
    assert sorted(contract["criteria"]) == ["objective:one", "objective:two"]
    assert contract["fixed_fields"] == {"schema": SCHEMA, "draft_hash": draft.draft_hash, "candidate_hash": CANDIDATE}
    # The contract is a pure function of the draft: the supervisor compares it byte for byte at launch and completion.
    assert spec_review_output_contract(_draft()) == contract
    assert spec_review_output_contract(_draft(draft_hash="f" * 64))["fixed_fields"]["draft_hash"] == "f" * 64


# --- the record binds the exact draft, its material, its action and its evidence ------------------------------

def _world(tmp_path, *, verdict="accept", action="spec_review", edit=None, after=None):
    """A store with one retained spec-review record for ``_draft()``; ``edit`` forges its payload, ``after`` its rows."""
    from run_state.ownership import assert_owner
    from run_state.run_policy import validate_draft_material
    from test_run_policy_budget import INPUT_A, _owned
    store, owner, activity = _owned(tmp_path)
    token, draft = owner.token, _draft()
    reserved = store.reserve_policy_action(token, action=action, logical_key="spec-review:launch", input_hash=INPUT_A)
    intent = store.reserve_launch(activity.id, token, policy_action_id=reserved.id)
    with store.transaction() as tx:
        tx.execute("UPDATE authority_launch_intents SET completion_status='succeeded' WHERE id=?", (intent.id,))
    stdout = tmp_path / "stdout.log"
    stdout.write_bytes(b"reviewer output\n")
    criteria = {item["id"]: {"status": "acceptable" if verdict == "accept" else "revise", "reason": "fixture"}
                for item in draft.material["criteria"]}
    payload = {
        "schema": "ffs.frontend-spec-review/v1", "repository_id": token.repository_id, "run_id": token.run_id,
        "draft_hash": draft.draft_hash, "material_hash": validate_draft_material(draft.material).material_hash,
        "candidate_hash": CANDIDATE, "action_id": reserved.id, "intent_id": intent.id,
        "fence_generation": token.generation, "verdict": verdict, "criteria": criteria, "notes": [],
        "output": {"locator": str(stdout), "sha256": hashlib.sha256(stdout.read_bytes()).hexdigest()},
        "process_identity": {"host_id": "h", "boot_id": "b", "pid": 1, "start_token": "s"},
    }
    if edit is not None:
        edit(payload)
    with store.transaction() as tx:
        assert_owner(tx, token)
        store._record_event_once_tx(tx, token, activity.id, "spec-review:" + draft.draft_hash, payload)
    if after is not None:
        after(store, token, reserved, intent, stdout)
    return store, token, draft


def _require(world):
    from run_state.spec_review import require_spec_review_accepted
    store, token, draft = world
    return require_spec_review_accepted(store, token, draft)


def test_an_accepted_record_of_the_exact_draft_verifies(tmp_path):
    record = _require(_world(tmp_path))
    assert (record["verdict"], record["draft_hash"]) == ("accept", "e" * 64)


def _set(key, value):
    return lambda payload: payload.__setitem__(key, value)


def _sql(statement):
    def edit(store, _token, reserved, intent, _stdout):
        with store.transaction() as tx:
            tx.execute(statement, (reserved.id if "policy_actions" in statement else intent.id,))
    return edit


def _rewrite_evidence(_store, _token, _reserved, _intent, stdout):
    stdout.write_bytes(b"tampered\n")


@pytest.mark.parametrize(("edit", "after"), [
    (_set("schema", "ffs.frontend-spec-review/v2"), None),
    (_set("repository_id", "another-repository"), None),
    (_set("run_id", "another-run"), None),
    (_set("draft_hash", "f" * 64), None),
    (_set("material_hash", "f" * 64), None),
    (_set("candidate_hash", "f" * 64), None),
    (_set("action_id", "no-such-action"), None),
    (_set("intent_id", "no-such-intent"), None),
    (_set("verdict", "maybe"), None),
    (None, _sql("UPDATE authority_policy_actions SET state='cancelled' WHERE id=?")),
    (None, _sql("UPDATE authority_launch_intents SET completion_status='failed' WHERE id=?")),
    (None, _rewrite_evidence),
], ids=["schema", "repository", "run", "draft-hash", "material-hash", "candidate-hash", "action", "intent", "verdict",
        "cancelled-action", "unsucceeded-intent", "changed-evidence"])
def test_a_record_that_binds_anything_but_the_exact_draft_refuses_as_invalid(tmp_path, edit, after):
    with pytest.raises(SupervisorRefused, match=r"^SPEC_REVIEW_RECORD_INVALID$"):
        _require(_world(tmp_path, edit=edit, after=after))


def test_a_record_of_another_review_kind_refuses_as_invalid(tmp_path):
    with pytest.raises(SupervisorRefused, match=r"^SPEC_REVIEW_RECORD_INVALID$"):
        _require(_world(tmp_path, action="final_review"))


def test_a_revise_record_never_satisfies_the_seal_gate(tmp_path):
    with pytest.raises(SupervisorRefused, match=r"^SPEC_REVIEW_REQUIRED$"):
        _require(_world(tmp_path, verdict="revise"))


def test_a_draft_with_no_record_never_satisfies_the_seal_gate(tmp_path):
    from dataclasses import replace
    from run_state.spec_review import require_spec_review_accepted
    store, token, draft = _world(tmp_path)
    # The record is keyed by the draft hash: a revision (a new hash) has none.
    with pytest.raises(SupervisorRefused, match=r"^SPEC_REVIEW_REQUIRED$"):
        require_spec_review_accepted(store, token, replace(draft, draft_hash="f" * 64))


# --- the operator's switch ----------------------------------------------------------------------------------

_OPERATOR = {"draft_id": "d", "revision": 1, "criteria": [], "exclusions": [], "global_invariants": []}


def _seal(draft, **kwargs):
    from run_state.frontend_producers import seal_from_draft
    store = SimpleNamespace(get_acceptance_contract=lambda **_kwargs: None)
    token = SimpleNamespace(repository_id="repository", run_id="run")
    return seal_from_draft(store, token, command_mode="feature-implement", draft=draft, runtime_hash="b" * 64,
                           candidate_hash=CANDIDATE, **kwargs)


@pytest.mark.parametrize("value", ["revise", "", "NATIVE", None, True, 1, ["native"]])
def test_seal_from_draft_refuses_an_unknown_spec_review_value(value):
    with pytest.raises(SupervisorRefused, match=r"^ACCEPTANCE_DRAFT_INVALID$"):
        _seal({**_OPERATOR, "spec_review": value}, review=None)


def test_seal_from_draft_ignores_an_absent_spec_review_key():
    # Validation passes and today's path runs on: it stops at the missing legacy contract, as it always did.
    with pytest.raises(SupervisorRefused, match=r"^ACCEPTANCE_CONTRACT_REQUIRED$"):
        _seal(dict(_OPERATOR), review=None)


def test_seal_from_draft_never_seals_a_native_draft_without_a_review():
    with pytest.raises(SupervisorRefused, match=r"^SPEC_REVIEW_REQUIRED$"):
        _seal({**_OPERATOR, "spec_review": "native"}, review=None)


def test_seal_from_draft_accepts_the_native_value_alongside_a_review():
    with pytest.raises(SupervisorRefused, match=r"^ACCEPTANCE_CONTRACT_REQUIRED$"):
        _seal({**_OPERATOR, "spec_review": "native"}, review=lambda _row: None)
