"""spec-014 E8 prerequisite 3b: every durable step of one ordinary repair either resumes to DONE once or refuses typed.

Each point crashes the managed Codex lifecycle (fixture host, real ControlStore, real child workspace
preparation with a real Git worktree, real CLI entry) with an uncaught ``BaseException``; the owner's fence is
then left to a dead process, as ``kill -9`` leaves it, and the identical request resumes under a new fence.
The producer's promises are checked at every point:

* before the repair intent exists, the resume leaves the dead owner's workspace, child and unlaunched grant as
  retained evidence and completes the repair once (a stale grant is released and one new grant reserved, so the
  run nets exactly one);
* from the intent on, the receipt, workspace and record of the earlier fence cannot bind to this one, so the
  resume refuses typed and launches, reserves and charges nothing further;
* once the repaired candidate is bound, the resume continues from it and never repairs again.

Fixture-level proof only: not native host qualification and not E8.
"""
from __future__ import annotations

import pytest

from recovery_fixture import (
    REPAIR_POINTS, REPAIRED_ACTIONS, arm, candidate_chain, ledger, repair_journals, sealed_candidate, world,
)
from test_final_review_resume import _held_resources
from test_managed_lifecycle_assembly import _last_envelope, requires_local_confinement

pytestmark = requires_local_confinement

DONE = ("DONE", 0, None)
INTENT = ("INTENT_RECONCILIATION_REQUIRED", 78, "reconcile_intent")
RECONCILE = ("REPAIR_RECONCILIATION_REQUIRED", 78, "inspect_retained_repair")
# Once the repair's patch is applied to the shared workspace and its candidate is not yet bound, the resume meets
# the unbound change before the repair producer: the pending journal refuses at the first workspace guard, and a
# published one leaves the shared workspace ahead of the bound candidate, which the frozen checks refuse.
PENDING = ("WORKSPACE_INTEGRATION_PENDING", 5, "correct_request")
STALE = ("FRONTEND_CHECK_CANDIDATE_STALE", 78, "correct_request")
EXPECTED = {
    "repair-entered": DONE,
    "repair-workspace-begun": DONE,
    "repair-qualified": DONE,
    "repair-action-reserved": DONE,
    "repair-intent-committed": INTENT,
    "repair-completed-before-harvest": RECONCILE,
    "repair-harvested-before-journal": RECONCILE,
    "repair-applied-before-capture": PENDING,
    "repair-applied-before-integration-record": STALE,
    "repair-recorded-before-bind": STALE,
    "repair-bound-before-transition": DONE,
}
# A kill between the intent commit and the child's acknowledgement leaves the admission it took held by the dead
# owner; the resume refuses before touching admission.
HELD_BY_THE_DEAD_OWNER = {"repair-intent-committed"}
_BASELINE = {}


def _uninterrupted(tmp_path_factory):
    """The same run with no crash: the counts every resumed-to-DONE point must equal."""
    if not _BASELINE:
        with pytest.MonkeyPatch.context() as patch:
            w = world(tmp_path_factory.mktemp("baseline"), patch, repair=True)
            assert w.run() == 0
            led = ledger(w)
            _BASELINE.update(stage=led.stage, charged=led.charged, actions=led.actions, launches=led.launches)
    return _BASELINE


def test_the_sweep_covers_every_expected_point():
    assert set(EXPECTED) == set(REPAIR_POINTS)


@pytest.mark.parametrize("point", REPAIR_POINTS)
def test_a_crash_at_each_repair_step_resumes_to_done_once_or_refuses_typed_without_a_relaunch(
        tmp_path, tmp_path_factory, monkeypatch, capsys, point):
    baseline = _uninterrupted(tmp_path_factory)
    assert baseline["stage"] == "DONE" and baseline["actions"] == REPAIRED_ACTIONS
    w = world(tmp_path, monkeypatch, repair=True)
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
        # The resume spent exactly what the uninterrupted run spent: no double charge, one repair grant, one launch.
        assert led.stage == "DONE" and led.cycles == []
        assert (led.charged, led.actions, led.launches) == (baseline["charged"], baseline["actions"],
                                                            baseline["launches"])
        assert led.launches["repair"] == 1
        # The chain verifies: one link from the previous candidate, one published repair journal.
        assert candidate_chain(w) == {led.candidate: sealed_candidate(w)}
        ((_key, state, _workspace),) = repair_journals(w)
        assert state == "published"
    else:
        # A typed refusal changes nothing: the retained stage stays put and nothing launches, reserves or charges.
        assert led.stage == crashed.stage
        assert (led.charged, led.actions, led.launches, led.cycles) == (
            crashed.charged, crashed.actions, crashed.launches, crashed.cycles)
