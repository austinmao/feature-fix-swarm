"""spec-014 E8 prerequisite 3a: every durable step of one recovery cycle either resumes to DONE once or refuses typed.

Each point crashes the managed Codex lifecycle (fixture host, real ControlStore, real child workspace
preparation with a real Git worktree, real CLI entry) with an uncaught ``BaseException``; the owner's
fence is then left to a dead process, as ``kill -9`` leaves it, and the identical request resumes under a
new fence.  The producer's two promises are checked at every point:

* before the diagnosis intent exists, the resume leaves the dead owner's workspace, child and unlaunched
  grant as retained, evidence and completes the cycle once (the cycle already reserved is reused, never
  duplicated);
* from the diagnosis intent on, the receipts, workspaces and trial records of the earlier fence cannot
  bind to this one (the controller's generation checks are never weakened), so the resume refuses typed
  and launches, reserves and charges nothing further.

Fixture-level proof only: not native host qualification and not E8.
"""
from __future__ import annotations

import pytest

from recovery_fixture import POINTS, arm, ledger, world
from test_final_review_resume import _held_resources
from test_managed_lifecycle_assembly import _last_envelope, requires_local_confinement

pytestmark = requires_local_confinement

DONE = "DONE"
INTENT = "INTENT_RECONCILIATION_REQUIRED"
RECONCILE = "RECOVERY_RECONCILIATION_REQUIRED"
EXPECTED = {
    "recover-entered": DONE,
    "diagnosis-workspace-begun": DONE,
    "diagnosis-worktree-created": DONE,
    "diagnosis-workspace-unpublished": DONE,
    "diagnosis-qualified": DONE,
    "cycle-reserved": DONE,
    "diagnosis-action-reserved": DONE,
    "diagnosis-intent-committed": INTENT,
    "diagnosis-completed-before-receipt": RECONCILE,
    "diagnosis-receipt-recorded": RECONCILE,
    "trial-completed-before-checks": RECONCILE,
    "trial-checks-retained": RECONCILE,
    "winner-applied-before-integration-record": RECONCILE,
    "winner-recorded-before-bind": RECONCILE,
    "continuation-transitioned": DONE,
}
_BASELINE = {}


def _uninterrupted(tmp_path_factory):
    """The same run with no crash: the counts every resumed-to-DONE point must equal."""
    if not _BASELINE:
        with pytest.MonkeyPatch.context() as patch:
            w = world(tmp_path_factory.mktemp("baseline"), patch)
            assert w.run() == 0
            led = ledger(w)
            _BASELINE.update(stage=led.stage, charged=led.charged, actions=led.actions, launches=led.launches)
    return _BASELINE


def test_the_sweep_covers_every_expected_point():
    assert set(EXPECTED) == set(POINTS)


@pytest.mark.parametrize("point", POINTS)
def test_a_crash_at_each_recovery_step_resumes_to_done_once_or_refuses_typed_without_a_relaunch(
        tmp_path, tmp_path_factory, monkeypatch, capsys, point):
    baseline = _uninterrupted(tmp_path_factory)
    assert baseline["stage"] == "DONE"
    w = world(tmp_path, monkeypatch)
    fired = arm(monkeypatch, point)
    w.crash()
    assert fired == [point]
    crashed = ledger(w)

    capsys.readouterr()
    result = w.run()
    outcome = DONE if result == 0 else _last_envelope(capsys)["code"]
    led = ledger(w)
    assert outcome == EXPECTED[point], (point, result, led.stage)
    # At most one intent per action and at most one cycle per binding, whatever the point.
    assert all(count <= 1 for _action, count in led.intents)
    assert len(led.cycles) <= 1 and len({cycle[2] for cycle in led.cycles}) == len(led.cycles)
    assert _held_resources(tmp_path) == {}
    if outcome == DONE:
        # The resume spent exactly what the uninterrupted run spent: no double charge, no second grant.
        assert led.stage == "DONE"
        assert (led.charged, led.actions, led.launches) == (baseline["charged"], baseline["actions"],
                                                            baseline["launches"])
    else:
        # A typed refusal changes nothing: the handback stays retained and nothing launches, reserves or charges.
        assert result == 78 and led.stage == crashed.stage
        assert (led.charged, led.actions, led.launches, led.cycles) == (
            crashed.charged, crashed.actions, crashed.launches, crashed.cycles)
