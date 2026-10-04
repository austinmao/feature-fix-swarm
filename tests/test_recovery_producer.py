"""spec-014 E8 prerequisite 3a: the production recovery (diagnosis/trial) producer, against retained handbacks.

Until this producer, ``drive_managed_session`` assembled ``recover`` as a stub that refused
``RECOVERY_PRODUCER_UNAVAILABLE``: a retained handback at stage RECOVER could never finish and its
replay refused ``REQUEST_ALREADY_COMPLETED``.  Each test drives the real ``frontend-start`` CLI entry
over the real ControlStore and Supervisor with the lifecycle assembly's fixture host (synthetic
credentials), crashes or refuses at a named point, and resumes the identical request under a new
owner fence.  Fixture-level proof of the production assembly only: not native host qualification,
not a native diagnosis or trial, and not E8.
"""
from __future__ import annotations

import json

import pytest

import run_state.managed_qualification as managed_qualification
from run_state.frontend_completion import post_repair_review_tx
from run_state.state import ControlStore
from recovery_fixture import (
    CHECK_ALWAYS_PASSES, arm, assert_recovered_once, candidate_chain, ledger, recovery_workspaces, sealed_candidate,
    world,
)
from test_final_review_resume import _held_resources
from test_managed_lifecycle_assembly import _last_envelope, requires_local_confinement

pytestmark = requires_local_confinement


def _stop_at_the_handback(w, monkeypatch):
    """Run 1: the check fails, there is no repair producer, and the owner dies as RECOVER is entered."""
    fired = arm(monkeypatch, "recover-entered")
    w.crash()
    assert fired == ["recover-entered"]
    led = ledger(w)
    assert led.stage == "RECOVER" and led.cycles == [] and led.actions == {"execute": 1}
    return led


def test_r1_retained_execute_handback_completes_under_a_new_owner_to_done(tmp_path, monkeypatch, capsys):
    w = world(tmp_path, monkeypatch)
    head = w.head()
    handback = _stop_at_the_handback(w, monkeypatch)
    assert handback.decision["saved_stage"] == "EXECUTE"
    capsys.readouterr()
    assert w.run() == 0, capsys.readouterr().out[-1500:]
    led = assert_recovered_once(w)
    assert w.head() == head
    # The winner descends from the sealed candidate: one link, and the sealed input is its parent.
    assert candidate_chain(w) == {led.candidate: sealed_candidate(w)}
    # A terminal run replays without a producer call, an event or a charge.
    events, charged = led.events, led.charged
    assert w.run() == 0
    again = ledger(w)
    assert (again.events, again.charged, again.actions) == (events, charged, led.actions)


def test_r2_retained_final_review_handback_reaches_done_without_a_second_broad_review(tmp_path, monkeypatch, capsys):
    w = world(tmp_path, monkeypatch, check=CHECK_ALWAYS_PASSES, mode={"review": "failed"})
    handback = _stop_at_the_handback_after_review(w, monkeypatch)
    assert handback.decision["saved_stage"] == "FINAL_REVIEW"
    assert "final_review" in handback.decision["consumed_attempts"]
    capsys.readouterr()
    assert w.run() == 0, capsys.readouterr().out[-1500:]
    led = assert_recovered_once(w)
    assert led.actions["final_review"] == 1 and led.launches["final_review"] == 1
    # The only review is the refused one, recorded on an ancestor of the recovered candidate.
    acceptance_hash = _acceptance_hash(w)
    with led.store.read_transaction() as tx:
        proof = post_repair_review_tx(led.store, tx, _token(w), acceptance_hash, led.candidate)
    assert proof is not None and proof[1].completion_status == "failed"


def _stop_at_the_handback_after_review(w, monkeypatch):
    fired = arm(monkeypatch, "recover-entered")
    w.crash()
    assert fired == ["recover-entered"]
    led = ledger(w)
    assert led.stage == "RECOVER" and led.cycles == [] and (led.native, led.reviews) == (1, 1)
    return led


def _acceptance_hash(w) -> str:
    store = ControlStore(w.authority / "control.sqlite3")
    return store.get_sealed_acceptance(repository_id=w.repository_id, run_id=w.run_id).acceptance_hash


def _token(w):
    """A read-only owner token for the run (the DONE gate's proof reader needs the repository and run)."""
    from types import SimpleNamespace
    return SimpleNamespace(repository_id=w.repository_id, run_id=w.run_id)


def test_r2b_final_review_handback_caused_by_a_refusal_code_ends_at_needs_decision_without_a_second_review(
        tmp_path, monkeypatch, capsys):
    """The M3 attempt 33 shape: the review output is invalid, so a handback exists but no failed review receipt.

    The recovery winner continues at FINAL_REVIEW, but the single final-review grant is spent and its review
    settled without a receipt, so the lifecycle cannot re-enter that review.  The one truthful terminal is
    NEEDS_DECISION with FRONTEND_COMPLETION_REVIEW_REQUIRED, and nothing is launched beyond the grants.
    """
    w = world(tmp_path, monkeypatch, check=CHECK_ALWAYS_PASSES, mode={"review": "malformed"})
    capsys.readouterr()
    assert w.run() == 78
    assert _last_envelope(capsys)["code"] == "FRONTEND_LIFECYCLE_NEEDS_DECISION"
    led = ledger(w)
    assert led.stage == "NEEDS_DECISION"
    assert led.decision["code"] == "FRONTEND_COMPLETION_REVIEW_REQUIRED" and led.decision["candidate_hash"] == led.candidate
    assert led.reviews == 0
    assert led.actions == {"execute": 1, "final_review": 1, "recovery_cycle_normal": 1, "diagnosis": 1,
                           "recovery_trial": 1}
    assert [led.launches[name] for name in ("execute", "final_review", "diagnosis", "recovery_trial")] == [1, 1, 1, 1]
    assert all(count <= 1 for _action, count in led.intents)
    assert _held_resources(tmp_path) == {}
    # The recovered candidate descends from the sealed one (the winner was integrated before the stop).
    assert candidate_chain(w) == {led.candidate: sealed_candidate(w)}
    # Replaying the terminal launches, reserves and charges nothing and records no event.
    capsys.readouterr()
    assert w.run() == 78
    assert _last_envelope(capsys)["code"] == "FRONTEND_LIFECYCLE_NEEDS_DECISION"
    again = ledger(w)
    assert (again.launches, again.charged, again.actions, again.events) == (
        led.launches, led.charged, led.actions, led.events)


def test_a_review_crashed_in_flight_with_no_recovery_continuation_still_re_enters_the_final_review(
        tmp_path, monkeypatch, capsys):
    """F51 is untouched: only a FINAL_REVIEW that follows a recovery continuation skips the spent review."""
    import run_state.frontend_producers as frontend_producers
    from test_final_review_resume import _Killed
    w = world(tmp_path, monkeypatch, check=CHECK_ALWAYS_PASSES)

    def killed_before_record(supervisor, handle, *, acceptance_hash):
        raise _Killed()

    real = frontend_producers.record_final_review
    monkeypatch.setattr(frontend_producers, "record_final_review", killed_before_record)
    w.crash()
    crashed = ledger(w)
    assert (crashed.stage, crashed.decision, crashed.native, crashed.reviews) == ("FINAL_REVIEW", None, 1, 0)
    assert crashed.actions["final_review"] == 1 and "recovery_cycle_normal" not in crashed.actions
    monkeypatch.setattr(frontend_producers, "record_final_review", real)
    capsys.readouterr()
    assert w.run() == 78
    # The producer was re-entered for the retained review (its proof binds the earlier fence); the lifecycle did
    # not decide, and nothing was launched or reserved.
    assert _last_envelope(capsys)["code"] == "REVIEW_RECONCILIATION_REQUIRED"
    led = ledger(w)
    assert (led.stage, led.decision, led.native, led.reviews) == ("FINAL_REVIEW", None, 1, 0)
    assert (led.actions, led.launches, led.charged) == (crashed.actions, crashed.launches, crashed.charged)


@pytest.mark.parametrize("trial", [None, "unrelated\n"], ids=["trial-writes-nothing", "trial-fails-the-checks"])
def test_r3_no_winner_hands_back_needs_decision_and_leaves_the_shared_candidate_alone(
        tmp_path, monkeypatch, capsys, trial):
    w = world(tmp_path, monkeypatch, mode={"trial": trial})
    _stop_at_the_handback(w, monkeypatch)
    capsys.readouterr()
    assert w.run() == 78
    assert _last_envelope(capsys)["code"] == "FRONTEND_LIFECYCLE_NEEDS_DECISION"
    led = ledger(w)
    assert led.stage == "NEEDS_DECISION" and led.decision["code"] == "RECOVERY_CYCLE_WITHOUT_WINNER"
    assert led.candidate == sealed_candidate(w) and candidate_chain(w) == {}
    assert len(led.cycles) == 1 and led.actions["diagnosis"] == 1 and led.actions["recovery_trial"] == 1
    assert "final_review" not in led.actions and led.native == 0
    assert _held_resources(tmp_path) == {}
    # Terminal replay: no event, no charge, no new launch.
    events, charged, launches = led.events, led.charged, led.launches
    capsys.readouterr()
    assert w.run() == 78
    again = ledger(w)
    assert (again.events, again.charged, again.launches) == (events, charged, launches)


def test_r4_the_binding_uses_the_candidate_that_advanced_before_the_handback(tmp_path, monkeypatch, capsys):
    """Cycle 1 advances the candidate, the refused final review hands back again, and cycle 2 binds the advanced one."""
    w = world(tmp_path, monkeypatch, mode={"review": "failed"})
    _stop_at_the_handback(w, monkeypatch)
    store = ControlStore(w.authority / "control.sqlite3")
    with store.transaction() as tx:
        tx.execute("UPDATE authority_run_policy_budgets SET recovery_mode='autonomous'")
    capsys.readouterr()
    assert w.run() == 0, capsys.readouterr().out[-1500:]
    led = ledger(w)
    assert led.stage == "DONE"
    assert [cycle[:2] for cycle in led.cycles] == [(1, "recovery_cycle_autonomous"), (2, "recovery_cycle_autonomous")]
    assert led.cycles[0][2] != led.cycles[1][2]
    assert led.actions["diagnosis"] == 2 and led.actions["recovery_trial"] == 2 and led.actions["final_review"] == 1
    sealed = sealed_candidate(w)
    chain = candidate_chain(w)
    (first,) = [candidate for candidate, parent in chain.items() if parent == sealed]
    (second,) = [candidate for candidate, parent in chain.items() if parent == first]
    assert chain == {first: sealed, second: first} and led.candidate == second
    digests = [digest for _key, _state, digest in recovery_workspaces(w)]
    # Cycle 1 children copy the sealed input; cycle 2 children copy the advanced candidate, not the sealed input.
    assert digests.count(sealed) == 2 and digests.count(first) == 2
    # Both trials' checks are retained: the controller consumed cycle 2 against the advanced candidate.
    with store.read_transaction() as tx:
        retained = [json.loads(row[0])["data"]["input_digest"] for row in tx.execute(
            "SELECT e.payload FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id "
            "WHERE k.idempotency_key LIKE 'recovery-trial-checks:%' ORDER BY e.id")]
    assert retained == [sealed, first]
    assert _held_resources(tmp_path) == {}


def test_r5_a_refused_diagnosis_qualification_leaves_the_handback_retained_and_spends_no_cycle(
        tmp_path, monkeypatch, capsys):
    w = world(tmp_path, monkeypatch)
    _stop_at_the_handback(w, monkeypatch)
    fixture_qualify = managed_qualification.qualify_managed_runtime
    refusing = [True]

    def qualify(store, token, **kwargs):
        if kwargs["role"] == "recovery" and refusing:
            raise managed_qualification.ManagedQualificationRefused("QUALIFICATION_UNCERTAIN")
        return fixture_qualify(store, token, **kwargs)

    monkeypatch.setattr(managed_qualification, "qualify_managed_runtime", qualify)
    capsys.readouterr()
    assert w.run() == 78
    assert _last_envelope(capsys)["code"] == "HOST_CAPABILITY_UNQUALIFIED"
    led = ledger(w)
    assert led.stage == "RECOVER" and led.cycles == [] and led.actions == {"execute": 1}
    assert "diagnosis" not in led.launches
    # The handback is still retained: a later owner with a qualifying host completes it.
    refusing.clear()
    assert w.run() == 0
    assert_recovered_once(w)


def test_r6_an_infeasible_cycle_refuses_before_it_is_spent(tmp_path, monkeypatch, capsys):
    w = world(tmp_path, monkeypatch)
    _stop_at_the_handback(w, monkeypatch)
    store = ControlStore(w.authority / "control.sqlite3")
    with store.transaction() as tx:
        tx.execute("UPDATE authority_run_policy_budgets SET launch_charged=launch_limit-2")
    capsys.readouterr()
    assert w.run() == 78
    assert _last_envelope(capsys)["code"] == "POLICY_STAGE_INFEASIBLE"
    led = ledger(w)
    assert led.stage == "RECOVER" and led.cycles == [] and led.actions == {"execute": 1}
    assert "diagnosis" not in led.launches and "recovery_trial" not in led.launches


def test_a_diagnosis_with_no_usable_answer_refuses_typed_after_its_cycle_is_spent(tmp_path, monkeypatch, capsys):
    w = world(tmp_path, monkeypatch, mode={"diagnosis": ""})
    capsys.readouterr()
    assert w.run() == 78
    assert _last_envelope(capsys)["code"] == "RECOVERY_DIAGNOSIS_FAILED"
    led = ledger(w)
    # The diagnosis launched and settled, so its cycle and grant are spent; no trial ever starts.
    assert led.stage == "RECOVER" and len(led.cycles) == 1 and led.launches["diagnosis"] == 1
    assert "recovery_trial" not in led.launches and candidate_chain(w) == {}
    # A later owner fence never relaunches it.
    capsys.readouterr()
    assert w.run() == 78
    assert _last_envelope(capsys)["code"] == "RECOVERY_RECONCILIATION_REQUIRED"
    again = ledger(w)
    assert (again.launches, again.charged, again.actions) == (led.launches, led.charged, led.actions)
    assert _held_resources(tmp_path) == {}


def test_old_refusals_still_fire_for_an_unsealed_legacy_replay(tmp_path, monkeypatch, capsys):
    """Widening the resume predicate never admits an unsealed run's completed outer launch."""
    from test_managed_lifecycle_assembly import _fixture_host, _managed_start, _setup
    primary, authority, _repository_id, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    runtime, fake, catalog = _fixture_host(tmp_path, monkeypatch)
    assert _managed_start(env, authority, "lg", runtime, fake, catalog, None) == 0
    capsys.readouterr()
    assert _managed_start(env, authority, "lg", runtime, fake, catalog, None) == 78
    assert _last_envelope(capsys)["code"] == "REQUEST_ALREADY_COMPLETED"


def test_old_refusals_still_fire_for_execute_without_a_recovery_continuation(tmp_path, monkeypatch, capsys):
    """An EXECUTE-stage replay with no continuation never proves the execution happened: it still refuses."""
    w = world(tmp_path, monkeypatch)
    fired = arm(monkeypatch, "execute-settled")
    w.crash()
    assert fired == ["execute-settled"]
    led = ledger(w)
    assert led.stage == "EXECUTE" and led.decision is None
    capsys.readouterr()
    assert w.run() == 78
    assert _last_envelope(capsys)["code"] == "REQUEST_ALREADY_COMPLETED"
    again = ledger(w)
    assert (again.stage, again.actions, again.charged) == ("EXECUTE", led.actions, led.charged)
