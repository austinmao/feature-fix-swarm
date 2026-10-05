"""spec-014 E8 prerequisite 3c: every durable step of one native spec review either resumes to DONE once or refuses typed.

Each point crashes the managed Codex lifecycle (fixture host, real ControlStore, real child workspace preparation with
a real Git worktree, real CLI entry) with an uncaught ``BaseException``; the owner's fence is then left to a dead
process, as ``kill -9`` leaves it, and the identical request resumes under a new fence.  The producer's promises are
checked at every point:

* before the review intent exists, the resume leaves the dead owner's workspace, child and unlaunched grant as retained
  evidence and completes the review once (a stale grant is released and one new grant reserved, so the run nets one);
* from the intent on, the proof and the workspace of the earlier fence cannot bind to this one, so the resume refuses
  typed and launches, reserves and charges nothing further;
* once the review is recorded, the resume seals that exact draft and never reviews again.

Fixture-level proof only: not native host qualification and not E8.
"""
from __future__ import annotations

import pytest

from recovery_fixture import (
    CHECK_ALWAYS_PASSES, SPEC_REVIEW_ACTIONS, SPEC_REVIEW_POINTS, arm, ledger, seals, spec_review_records, world,
)
from test_final_review_resume import _held_resources
from test_managed_lifecycle_assembly import _last_envelope, requires_local_confinement

pytestmark = requires_local_confinement

DONE = ("DONE", 0, None)
INTENT = ("INTENT_RECONCILIATION_REQUIRED", 78, "reconcile_intent")
RECONCILE = ("SPEC_REVIEW_RECONCILIATION_REQUIRED", 78, "inspect_retained_spec_review")
EXPECTED = {
    "spec-review-entered": DONE,
    "spec-reviewer-workspace-begun": DONE,
    "spec-reviewer-workspace-ready": DONE,
    "spec-review-qualified": DONE,
    "spec-review-action-reserved": DONE,
    "spec-review-intent-committed": INTENT,
    "spec-review-completed-before-record": RECONCILE,
    "spec-review-recorded-before-seal": DONE,
    "sealed-before-execute": DONE,
}
# A kill between the intent commit and the child's acknowledgement leaves the admission it took held by the dead
# owner; the resume refuses before touching admission.
HELD_BY_THE_DEAD_OWNER = {"spec-review-intent-committed"}
_BASELINE = {}


def _uninterrupted(tmp_path_factory):
    """The same run with no crash: the counts every resumed-to-DONE point must equal."""
    if not _BASELINE:
        with pytest.MonkeyPatch.context() as patch:
            w = world(tmp_path_factory.mktemp("baseline"), patch, check=CHECK_ALWAYS_PASSES, spec_review="accept")
            assert w.run() == 0
            led = ledger(w)
            _BASELINE.update(stage=led.stage, charged=led.charged, actions=led.actions, launches=led.launches)
    return _BASELINE


def test_the_sweep_covers_every_expected_point():
    assert set(EXPECTED) == set(SPEC_REVIEW_POINTS)


@pytest.mark.parametrize("point", SPEC_REVIEW_POINTS)
def test_a_crash_at_each_spec_review_step_resumes_to_done_once_or_refuses_typed_without_a_relaunch(
        tmp_path, tmp_path_factory, monkeypatch, capsys, point):
    baseline = _uninterrupted(tmp_path_factory)
    assert baseline["stage"] == "DONE" and baseline["actions"] == SPEC_REVIEW_ACTIONS
    w = world(tmp_path, monkeypatch, check=CHECK_ALWAYS_PASSES, spec_review="accept")
    head = w.head()
    fired = arm(monkeypatch, point)
    w.crash()
    assert fired == [point]
    crashed = ledger(w)

    capsys.readouterr()
    result = w.run()
    envelope = None if result == 0 else _last_envelope(capsys)
    outcome = DONE if result == 0 else (envelope["code"], result, envelope["recovery_action"]["action"])
    led = ledger(w)
    assert outcome == EXPECTED[point], (point, result, led.stage)
    # At most one intent per action, whatever the point; the primary checkout's HEAD never moves.
    assert all(count <= 1 for _action, count in led.intents)
    assert w.head() == head
    held = _held_resources(tmp_path)
    if point in HELD_BY_THE_DEAD_OWNER:
        assert list(held) == ["admissions"] and len(held["admissions"]) == 1
    else:
        assert held == {}
    if EXPECTED[point] == DONE:
        # The resume spent exactly what the uninterrupted run spent: one review grant, one launch, one seal.
        assert led.stage == "DONE" and led.cycles == []
        assert (led.charged, led.actions, led.launches) == (baseline["charged"], baseline["actions"],
                                                            baseline["launches"])
        assert led.launches["spec_review"] == 1
        (record,) = spec_review_records(w)
        assert seals(w) == [("assembly", 1, record["draft_hash"])]
    else:
        # A typed refusal changes nothing: the run stays unsealed and nothing launches, reserves or charges.
        assert led.stage == crashed.stage is None and seals(w) == []
        assert (led.charged, led.actions, led.launches, led.cycles) == (
            crashed.charged, crashed.actions, crashed.launches, crashed.cycles)
