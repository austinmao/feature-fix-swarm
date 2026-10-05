"""spec-014 E8 prerequisite 3c: what a resume finds at every durable step of one native spec review.

Each point crashes the managed Codex lifecycle (fixture host, real ControlStore, real child workspace preparation with
a real Git worktree, real CLI entry) with an uncaught ``BaseException``; the owner's fence is then left to a dead
process, as ``kill -9`` leaves it, and the identical request resumes under a new fence.

Every point is pre-launch for the OUTER orchestrator (the review runs between its qualification and its launch), and the
fixture host seam replays that retained qualification without a staging check or a generation check.  So no point resumes
to DONE: the resume is refused, and each pinned outcome below is ONE exact refusal.  PRODUCTION REFUSES EARLIER than this
fixture does, for the same reason, and the two production-resume files pin that: on the Codex host the strict reuse
check of the outer's staged home refuses ``RETAINED_RUNTIME_NOT_REUSABLE`` (``test_spec_review_production_resume``); on
the Claude host the replayed qualification refuses ``ADMISSION_CONFLICT``, mapped to ``HOST_CAPABILITY_UNQUALIFIED``
(``test_spec_review_production_resume_claude``).  The remedy in both is a new request key, which repeats the review
grant.  What this sweep proves is the part that does not depend on that refusal: whatever the resume meets, no second
review is launched, nothing more is charged, no outer is launched, and the primary checkout never moves.

* before the review intent exists the resume leaves the dead owner's workspace and child as retained evidence (an
  earlier-fence reviewer is aborted, never re-qualified) and releases an unspent grant, then meets the stale outer;
* from the intent on, the proof and the workspace of the earlier fence cannot bind to this one, so the resume refuses
  typed (``INTENT_RECONCILIATION_REQUIRED`` or ``SPEC_REVIEW_RECONCILIATION_REQUIRED``) and touches nothing;
* once the review is recorded the resume does not review again; it seals that exact draft and meets the stale outer
  only at the outer's own launch.

Fixture-level proof only: not native host qualification and not E8.
"""
from __future__ import annotations

import pytest

from recovery_fixture import (
    CHECK_ALWAYS_PASSES, SPEC_REVIEW_POINTS, arm, ledger, seals, spec_review_records, world,
)
from test_final_review_resume import _held_resources, _outer_intents
from test_managed_lifecycle_assembly import _last_envelope, requires_local_confinement

pytestmark = requires_local_confinement


def _point(outcome, *, stage=None, grants=None, records=0, sealed=0, held=None):
    """``outcome`` is ``(code, exit status, recovery action)``; the rest is the end state the resume leaves."""
    return {"outcome": outcome, "stage": stage, "grants": {} if grants is None else grants, "records": records,
            "seals": sealed, "held": held}


# The stale outer the fixture seam leaves behind is met at the review's workspace capture (78) or at the outer's own
# prelaunch capture (5); an earlier-fence reviewer workspace is met as FENCE_REVOKED (4).
STALE_OUTER_AT_CAPTURE = ("WAVE_ADMISSION_MISMATCH", 78, "qualify_host_adapter")
STALE_OUTER_AT_LAUNCH = ("WAVE_ADMISSION_MISMATCH", 5, "correct_request")
ONE_GRANT = {"spec_review": 1}
EXPECTED = {
    "spec-review-entered": _point(STALE_OUTER_AT_CAPTURE),
    "spec-reviewer-workspace-begun": _point(STALE_OUTER_AT_CAPTURE),
    "spec-reviewer-workspace-ready": _point(("FENCE_REVOKED", 4, "correct_request")),
    "spec-review-qualified": _point(STALE_OUTER_AT_CAPTURE),
    # The unspent grant is released (cancelled) and none is reserved afresh: the run nets no grant at all.
    "spec-review-action-reserved": _point(STALE_OUTER_AT_CAPTURE),
    # A kill between the intent commit and the child's acknowledgement leaves the admission it took held by the dead
    # owner; the resume refuses before touching admission.
    "spec-review-intent-committed": _point(("INTENT_RECONCILIATION_REQUIRED", 78, "reconcile_intent"),
                                           grants=ONE_GRANT, held=1),
    "spec-review-completed-before-record": _point(
        ("SPEC_REVIEW_RECONCILIATION_REQUIRED", 78, "inspect_retained_spec_review"), grants=ONE_GRANT),
    # Recorded: the resume seals that exact draft, moves SEALED -> EXECUTE, and only the outer's launch is refused.
    "spec-review-recorded-before-seal": _point(STALE_OUTER_AT_LAUNCH, stage="EXECUTE", grants=ONE_GRANT, records=1,
                                               sealed=1),
    # Sealed but no lifecycle state: the base `state is None` fall-through attempts the unsealed single launch, which
    # only the stale outer refuses.  (Production never gets this far, see above; the base behavior is unchanged.)
    "spec-review-sealed-before-initialized": _point(STALE_OUTER_AT_LAUNCH, grants=ONE_GRANT, records=1, sealed=1),
    "sealed-before-execute": _point(STALE_OUTER_AT_LAUNCH, stage="EXECUTE", grants=ONE_GRANT, records=1, sealed=1),
}


def _held(tmp_path):
    """Admissions still held in the shared queue; nothing was ever admitted when the queue was never created."""
    if not (tmp_path / "managed-admission" / "admission.sqlite3").exists():
        return {}
    return _held_resources(tmp_path)


def test_the_sweep_covers_every_expected_point():
    assert set(EXPECTED) == set(SPEC_REVIEW_POINTS)


@pytest.mark.parametrize("point", SPEC_REVIEW_POINTS)
def test_a_crash_at_each_spec_review_step_is_refused_on_resume_without_a_relaunch_or_a_charge(
        tmp_path, monkeypatch, capsys, point):
    expected = EXPECTED[point]
    w = world(tmp_path, monkeypatch, check=CHECK_ALWAYS_PASSES, spec_review="accept")
    head = w.head()
    fired = arm(monkeypatch, point)
    w.crash()
    assert fired == [point]
    crashed = ledger(w)

    capsys.readouterr()
    result = w.run()
    envelope = _last_envelope(capsys)
    led = ledger(w)
    # Exactly one refusal per point: its code, exit status and recovery action.
    assert (envelope["code"], result, envelope["recovery_action"]["action"]) == expected["outcome"], (point, led.stage)
    # The end state the resume leaves, and what it did not do: no second review, no charge, no outer launch.
    assert (led.stage, led.actions, len(spec_review_records(w)), len(seals(w))) == (
        expected["stage"], expected["grants"], expected["records"], expected["seals"])
    assert led.charged == crashed.charged and led.launches == crashed.launches
    assert led.launches.get("spec_review", 0) <= 1 and _outer_intents(led) == []
    assert all(count <= 1 for _action, count in led.intents)
    assert w.head() == head
    held = _held(tmp_path)
    if expected["held"] is None:
        assert held == {}
    else:
        assert list(held) == ["admissions"] and len(held["admissions"]) == expected["held"]
