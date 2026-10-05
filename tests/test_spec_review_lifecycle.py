"""spec-014 E8 prerequisite 3c: a draft that opts in is reviewed natively, then sealed, executed and reviewed again.

The managed Codex lifecycle runs end to end (fixture host, real ControlStore, real child workspace preparation with a
real Git worktree, real CLI entry).  The opt-in is the draft's own ``"spec_review": "native"`` key; a draft without it
runs exactly as it always did.  Fixture-level proof only: not native host qualification and not E8.
"""
from __future__ import annotations

import json

from recovery_fixture import (
    CHECK_ALWAYS_PASSES, SPEC_REVIEW_ACTIONS, charge_qualification, drafts, ledger, seals, set_mode,
    spec_review_records, spec_reviewers, world,
)
from test_final_review_resume import _held_resources, _outer_intents
from test_managed_lifecycle_assembly import _facts, _last_envelope, requires_local_confinement

pytestmark = requires_local_confinement

SMALL = ("--ceremony-estimate", json.dumps({"files": 1, "loc": 1, "protected": False}))


def _revise(w, *, revision: int, reason: str) -> None:
    """The operator's next revision of the same draft: one exclusion reworded, so a new draft hash."""
    document = json.loads(w.draft.read_text())
    document["revision"], document["exclusions"] = revision, [{"id": "none", "reason": reason}]
    w.draft.write_text(json.dumps(document))


def _refusal(w, capsys, *extra):
    capsys.readouterr()
    result = w.run(*extra)
    envelope = _last_envelope(capsys)
    return result, envelope["code"], envelope["recovery_action"]["action"]


def test_draft_with_native_spec_review_is_reviewed_sealed_executed_reviewed_and_done(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, check=CHECK_ALWAYS_PASSES, spec_review="accept")
    assert w.run() == 0
    led = ledger(w)
    assert led.stage == "DONE"
    assert led.actions == SPEC_REVIEW_ACTIONS
    assert led.launches["spec_review"] == 1 and led.launches["execute"] == 1 and led.launches["final_review"] == 1
    assert all(count <= 1 for _action, count in led.intents)
    # The spec reviewer and the final reviewer are two children; only the final review is an acceptance receipt.
    assert (led.native, led.reviews) == (2, 1)
    assert _outer_intents(led) == [("completed_succeeded", "succeeded")]
    (record,) = spec_review_records(w)
    assert record["verdict"] == "accept"
    # The one seal is the draft the record reviewed.
    assert seals(w) == [("assembly", 1, record["draft_hash"])] == drafts(w)
    assert [state for _key, state in spec_reviewers(w)] == ["succeeded"]
    assert _held_resources(tmp_path) == {}
    # A terminal run replays to the same facts without a new launch, grant or record.
    assert w.run() == 0
    again = ledger(w)
    assert (again.stage, again.charged, again.actions, again.launches, again.native, again.reviews) == (
        led.stage, led.charged, led.actions, led.launches, led.native, led.reviews)
    assert spec_review_records(w) == [record]


def test_revise_verdict_leaves_the_run_unsealed_and_refuses_typed(tmp_path, monkeypatch, capsys):
    w = world(tmp_path, monkeypatch, check=CHECK_ALWAYS_PASSES, spec_review="revise")
    assert _refusal(w, capsys) == (78, "SPEC_REVIEW_REJECTED", "revise_acceptance_draft")
    facts = _facts(w.authority, w.repository_id, w.run_id)
    assert (facts.outer, facts.stage, facts.reviews) == (0, None, 0)
    assert seals(w) == [] and len(drafts(w)) == 1
    led = ledger(w)
    assert led.actions == {"spec_review": 1} and led.launches == {"spec_review": 1}
    # The same draft replays to the same refusal and spends nothing.
    assert _refusal(w, capsys) == (78, "SPEC_REVIEW_REJECTED", "revise_acceptance_draft")
    again = ledger(w)
    assert (again.actions, again.launches, again.charged) == (led.actions, led.launches, led.charged)
    # A revision is a new draft: the medium tier allows a second review, which accepts and the run reaches DONE.
    set_mode(w, spec_review="accept")
    _revise(w, revision=2, reason="a revised exclusion")
    assert w.run() == 0
    done = ledger(w)
    assert done.stage == "DONE" and done.actions == {**SPEC_REVIEW_ACTIONS, "spec_review": 2}
    records = spec_review_records(w)
    assert [record["verdict"] for record in records] == ["revise", "accept"]
    assert seals(w) == [("assembly", 2, records[1]["draft_hash"])]
    assert [state for _key, state in spec_reviewers(w)] == ["succeeded", "succeeded"]


def test_a_spent_small_tier_allowance_refuses_a_revision_before_any_probe(tmp_path, monkeypatch, capsys):
    charge_qualification(monkeypatch)
    w = world(tmp_path, monkeypatch, check=CHECK_ALWAYS_PASSES, spec_review="revise")
    assert _refusal(w, capsys, *SMALL) == (78, "SPEC_REVIEW_REJECTED", "revise_acceptance_draft")
    led = ledger(w)
    # The one review cost its four probes and its launch.
    assert led.actions == {"spec_review": 1} and led.charged == 5
    set_mode(w, spec_review="accept")
    _revise(w, revision=2, reason="a revised exclusion")
    assert _refusal(w, capsys, *SMALL) == (78, "POLICY_ACTION_LIMIT_EXHAUSTED", "qualify_host_adapter")
    after = ledger(w)
    assert (after.charged, after.actions, after.launches) == (led.charged, led.actions, led.launches)
    assert [state for _key, state in spec_reviewers(w)] == ["succeeded"] and len(drafts(w)) == 2
    assert seals(w) == []


def test_draft_without_the_key_runs_exactly_as_before(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, check=CHECK_ALWAYS_PASSES)
    assert w.run() == 0
    led = ledger(w)
    assert led.stage == "DONE" and led.actions == {"execute": 1, "final_review": 1}
    assert (led.native, led.reviews) == (1, 1)
    assert spec_review_records(w) == [] and spec_reviewers(w) == []


def test_draft_with_an_unknown_spec_review_value_is_refused_before_anything_launches(tmp_path, monkeypatch, capsys):
    w = world(tmp_path, monkeypatch, check=CHECK_ALWAYS_PASSES)
    document = json.loads(w.draft.read_text())
    document["spec_review"] = "auto"
    w.draft.write_text(json.dumps(document))
    assert _refusal(w, capsys) == (78, "ACCEPTANCE_DRAFT_INVALID", "qualify_host_adapter")
    facts = _facts(w.authority, w.repository_id, w.run_id)
    assert (facts.outer, facts.stage, facts.native) == (0, None, 0)
    assert seals(w) == [] and drafts(w) == []
