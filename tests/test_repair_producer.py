"""spec-014 E8 prerequisite 3b: the production ordinary repair producer, against a failed frozen check.

Until this producer, ``drive_managed_session`` passed the lifecycle no ``repair``: a failed frozen check handed
straight back to RECOVER.  Each test drives the real ``frontend-start`` CLI entry over the real ControlStore and
Supervisor with the lifecycle assembly's fixture host (synthetic credentials).  The repair child is an isolated
copy of the current candidate; its harvested patch is merged through the journaled integration path under a
``repair:<action_id>`` key and the candidate is bound with the previous one as its parent (R-direct).
Fixture-level proof of the production assembly only: not native host qualification, not E8.
"""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

import run_state.managed_qualification as managed_qualification
import run_state.recovery_producer as recovery_producer
from run_state.supervisor import SupervisorRefused
from run_state.run_policy import action_limit
from run_state.state import ControlStore
from recovery_fixture import (
    FULL_ACTIONS, LAST_REPAIR, arm, assert_recovered_once, assert_repaired_once, candidate_chain, ledger,
    policy_tier, repair_ids, repair_journals, sealed_candidate, watch_binds, watch_repair, world,
)
from test_final_review_resume import _held_resources
from test_managed_lifecycle_assembly import _last_envelope, requires_local_confinement

pytestmark = requires_local_confinement


def test_p1_a_failed_check_is_repaired_integrated_bound_and_the_run_reaches_done(tmp_path, monkeypatch, capsys):
    w = world(tmp_path, monkeypatch, repair=True)
    head = w.head()
    resolved = watch_binds(monkeypatch)
    capsys.readouterr()
    assert w.run() == 0, capsys.readouterr().out[-1500:]
    led = assert_repaired_once(w)
    assert w.head() == head
    # The repaired candidate descends from the previous one: one link, and the sealed input is its parent.
    assert candidate_chain(w) == {led.candidate: sealed_candidate(w)}
    # The chain resolver accepted the bound candidate the moment it was bound.
    assert resolved == [(led.candidate, led.candidate)]
    ((_key, _state, workspace),) = repair_journals(w)
    assert (Path(workspace) / "src/input.txt").read_text() == "base-input\nselected edit\nrepaired\n"
    with led.store.read_transaction() as tx:
        roles = sorted(json.loads(row[0])["role"] for row in tx.execute(
            "SELECT receipt_json FROM authority_acceptance_receipts"))
    assert roles == ["execution", "review"]
    # A terminal run replays without a producer call, an event or a charge.
    events, charged = led.events, led.charged
    assert w.run() == 0
    again = ledger(w)
    assert (again.events, again.charged, again.actions) == (events, charged, led.actions)


def test_p2_a_repair_that_writes_nothing_spends_the_tier_limit_then_recovery_reaches_a_truthful_terminal(
        tmp_path, monkeypatch, capsys):
    w = world(tmp_path, monkeypatch, repair=True, mode={"repair": "", "trial": ""})
    capsys.readouterr()
    assert w.run() == 78
    assert _last_envelope(capsys)["code"] == "FRONTEND_LIFECYCLE_NEEDS_DECISION"
    led = ledger(w)
    limit = action_limit("repair", policy_tier(w))
    assert led.stage == "NEEDS_DECISION" and led.decision["code"] == "RECOVERY_CYCLE_WITHOUT_WINNER"
    # Every repair grant was consumed, each launched exactly once; the recovery cycle then ran its one diagnosis and trial.
    assert led.actions == {"execute": 1, "repair": limit, "recovery_cycle_normal": 1, "diagnosis": 1,
                           "recovery_trial": 1}
    assert led.launches["repair"] == limit and all(count <= 1 for _action, count in led.intents)
    # Nothing was integrated or bound, and no review ran.
    assert led.candidate == sealed_candidate(w) and candidate_chain(w) == {} and repair_journals(w) == []
    assert led.native == 0 and _held_resources(tmp_path) == {}
    events, charged, launches = led.events, led.charged, led.launches
    capsys.readouterr()
    assert w.run() == 78
    again = ledger(w)
    assert (again.events, again.charged, again.launches) == (events, charged, launches)


def test_p2b_the_repairs_spent_on_nothing_are_consumed_attempts_of_the_recovery_that_then_wins(
        tmp_path, monkeypatch, capsys):
    w = world(tmp_path, monkeypatch, repair=True, mode={"repair": ""})
    packets, produce_recovery = [], recovery_producer.produce_recovery

    def recovery(*args, **kwargs):
        packets.append(kwargs["packet"])
        return produce_recovery(*args, **kwargs)

    monkeypatch.setattr(recovery_producer, "produce_recovery", recovery)
    capsys.readouterr()
    assert w.run() == 0, capsys.readouterr().out[-1500:]
    led = ledger(w)
    limit = action_limit("repair", policy_tier(w))
    assert led.stage == "DONE"
    assert led.actions == {**FULL_ACTIONS, "repair": limit}
    assert len(packets) == 1 and packets[0]["saved_stage"] == "EXECUTE"
    assert sorted(packets[0]["consumed_attempts"]) == sorted(repair_ids(w))
    assert packets[0]["remaining_allowances"]["repair"] == 0
    # Only the recovery winner advanced the candidate.
    assert candidate_chain(w) == {led.candidate: sealed_candidate(w)} and repair_journals(w) == []
    assert _held_resources(tmp_path) == {}


def test_p3_a_repair_producer_that_charges_nothing_is_refused_not_looped(tmp_path, monkeypatch, capsys):
    w = world(tmp_path, monkeypatch, repair=True)
    calls = []
    monkeypatch.setattr(recovery_producer, "produce_repair", lambda *args, **kwargs: calls.append(1))
    capsys.readouterr()
    assert w.run() == 78
    assert _last_envelope(capsys)["code"] == "FRONTEND_REPAIR_UNCHARGED"
    led = ledger(w)
    assert calls == [1]
    assert led.stage == "EXECUTE" and led.actions == {"execute": 1} and led.cycles == []


def test_p4_an_infeasible_repair_is_refused_before_anything_is_reserved_and_the_run_falls_through_to_recovery(
        tmp_path, monkeypatch, capsys):
    from run_state.state import _QUALIFICATION_PROBE_ORDER
    w = world(tmp_path, monkeypatch, repair=True)
    fired = arm(monkeypatch, "repair-entered")
    w.crash()
    assert fired == ["repair-entered"]
    crashed = ledger(w)
    assert crashed.stage == "EXECUTE" and crashed.actions == {"execute": 1}
    # One launch short of a repair: its qualification probes, its launch and the one frozen check.
    demand = len(_QUALIFICATION_PROBE_ORDER) + 1 + 1
    store = ControlStore(w.authority / "control.sqlite3")
    with store.transaction() as tx:
        tx.execute("UPDATE authority_run_policy_budgets SET launch_limit=launch_charged+?", (demand - 1,))
    seen = watch_repair(monkeypatch, w)
    capsys.readouterr()
    assert w.run() == 78
    # The producer refused before reserving anything; the lifecycle took the handback, so the 3a recovery path
    # ran and ended on its own typed refusal (its cycle does not fit either) rather than on a raw repair refusal.
    assert seen == [("REPAIR_BUDGET_INFEASIBLE", [])]
    assert _last_envelope(capsys)["code"] == "POLICY_STAGE_INFEASIBLE"
    led = ledger(w)
    assert led.stage == "RECOVER" and led.cycles == [] and led.actions == {"execute": 1}
    assert led.decision["saved_stage"] == "EXECUTE" and led.decision["consumed_attempts"] == []
    assert (led.charged, led.launches) == (crashed.charged, crashed.launches)
    # A later owner with room completes the retained handback through recovery, with no repair at all.
    with store.transaction() as tx:
        tx.execute("UPDATE authority_run_policy_budgets SET launch_limit=launch_limit+100")
    capsys.readouterr()
    assert w.run() == 0, capsys.readouterr().out[-1500:]
    assert_recovered_once(w)
    assert seen == [("REPAIR_BUDGET_INFEASIBLE", [])]


def test_old_refusals_still_fire_for_a_bare_execute_replay_with_no_issued_repair(tmp_path, monkeypatch, capsys):
    """A crash after the outer settled but before the candidate is bound proves nothing was checked: still refused."""
    w = world(tmp_path, monkeypatch, repair=True)
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
    assert repair_ids(w) == set()


def test_old_refusals_still_fire_for_an_unsealed_legacy_replay(tmp_path, monkeypatch, capsys):
    """The repair predicate never admits an unsealed run's completed outer launch."""
    from test_managed_lifecycle_assembly import _fixture_host, _managed_start, _setup
    primary, authority, _repository_id, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    runtime, fake, catalog = _fixture_host(tmp_path, monkeypatch)
    assert _managed_start(env, authority, "lg", runtime, fake, catalog, None) == 0
    capsys.readouterr()
    assert _managed_start(env, authority, "lg", runtime, fake, catalog, None) == 78
    assert _last_envelope(capsys)["code"] == "REQUEST_ALREADY_COMPLETED"


def test_a_check_that_already_passes_never_reaches_the_repair_producer(tmp_path, monkeypatch, capsys):
    from recovery_fixture import CHECK_ALWAYS_PASSES
    w = world(tmp_path, monkeypatch, repair=True, check=CHECK_ALWAYS_PASSES)
    seen = watch_repair(monkeypatch, w)
    capsys.readouterr()
    assert w.run() == 0
    led = ledger(w)
    assert seen == [] and led.stage == "DONE" and led.actions == {"execute": 1, "final_review": 1}


def _bind_hook(monkeypatch, wrap) -> None:
    """Hand ``produce_repair`` a host seam whose ``bind`` is ``wrap(real_bind)``."""
    real = recovery_producer.produce_repair

    def produce_repair(*args, **kwargs):
        seam = kwargs["seam"]
        return real(*args, **{**kwargs, "seam": replace(seam, bind=wrap(seam.bind))})

    monkeypatch.setattr(recovery_producer, "produce_repair", produce_repair)


@pytest.mark.parametrize("failure", ["bind-refuses", "bind-returns-no-material"])
def test_p8_a_repair_bind_failure_leaves_the_failed_check_retained_and_spends_no_grant(
        tmp_path, monkeypatch, capsys, failure):
    w = world(tmp_path, monkeypatch, repair=True)
    failing = [True]

    def wrap(real):
        def bind(qualified, prompt, ready, contract_hash, launch_key):
            if not failing:
                return real(qualified, prompt, ready, contract_hash, launch_key)
            if failure == "bind-refuses":
                raise SupervisorRefused("HOST_CAPABILITY_UNQUALIFIED")
            request, adapter = real(qualified, prompt, ready, contract_hash, launch_key)
            adapter.release_launch_material(request.codex_material)
            return replace(request, codex_material=None), adapter
        return bind

    _bind_hook(monkeypatch, wrap)
    capsys.readouterr()
    assert w.run() == 78
    assert _last_envelope(capsys)["code"] == "HOST_CAPABILITY_UNQUALIFIED"
    led = ledger(w)
    # The failed check is still retained and nothing was spent: no repair grant, no launch.
    assert led.stage == "EXECUTE" and led.actions == {"execute": 1} and "repair" not in led.launches
    # A later owner whose host binds repairs it: the replay is admitted by the retained failed check alone.
    failing.clear()
    assert w.run() == 0
    assert_repaired_once(w)


def test_p9_a_refused_repair_reservation_releases_the_launch_material_it_bound(tmp_path, monkeypatch, capsys):
    w = world(tmp_path, monkeypatch, repair=True)
    fixture_qualify = managed_qualification.qualify_managed_runtime

    def qualify(store, token, **kwargs):
        result = fixture_qualify(store, token, **kwargs)
        if kwargs["activity_request_key"].startswith("repair:"):
            # A qualification that burned the rest of the launch budget: the pre-check passed, the reservation cannot.
            with store.transaction() as tx:
                tx.execute("UPDATE authority_run_policy_budgets SET launch_charged=launch_limit")
        return result

    monkeypatch.setattr(managed_qualification, "qualify_managed_runtime", qualify)
    real, bound, alive = recovery_producer.produce_repair, [], []

    def produce_repair(*args, **kwargs):
        seam = kwargs["seam"]

        def bind(*arguments):
            request, adapter = seam.bind(*arguments)
            bound.append(request.codex_material.temporary_dir)
            return request, adapter

        try:
            return real(*args, **{**kwargs, "seam": replace(seam, bind=bind)})
        finally:
            alive.extend(Path(directory).exists() for directory in bound)

    monkeypatch.setattr(recovery_producer, "produce_repair", produce_repair)
    capsys.readouterr()
    assert w.run() == 78
    assert _last_envelope(capsys)["code"] == "POLICY_STAGE_INFEASIBLE"
    led = ledger(w)
    assert led.stage == "EXECUTE" and led.actions == {"execute": 1} and "repair" not in led.launches
    # The material is bound before the grant is reserved, so the producer must release it when the reservation refuses.
    assert len(bound) == 1 and alive == [False]


# --- a replay at the tier's repair limit gives an unfinished last repair to the producer (reviewer finding R1-1) --

def _cancelled_repairs(w) -> int:
    store = ControlStore(w.authority / "control.sqlite3")
    with store.read_transaction() as tx:
        return tx.execute("SELECT count(*) FROM authority_policy_actions "
                          "WHERE action='repair' AND state='cancelled'").fetchone()[0]


def test_r1_1a_a_reserved_unlaunched_last_repair_grant_is_replaced_and_the_run_reaches_the_uninterrupted_terminal(
        tmp_path, tmp_path_factory, monkeypatch, capsys):
    """Repairs 1..3 write nothing, the last one fixes it: crashing with the LAST grant reserved changes nothing."""
    mode = {"repair_from": LAST_REPAIR}
    with pytest.MonkeyPatch.context() as patch:
        uninterrupted = world(tmp_path_factory.mktemp("uninterrupted"), patch, repair=True, mode=mode)
        assert uninterrupted.run() == 0
        baseline = ledger(uninterrupted)
    assert baseline.stage == "DONE" and baseline.actions == {"execute": 1, "repair": LAST_REPAIR, "final_review": 1}
    w = world(tmp_path, monkeypatch, repair=True, mode=mode)
    fired = arm(monkeypatch, "repair-last-action-reserved")
    w.crash()
    assert fired == ["repair-last-action-reserved"]
    assert policy_tier(w) == "medium"
    crashed = ledger(w)
    # The allowance is spent on paper: the last grant is reserved and never launched.
    assert crashed.stage == "EXECUTE" and crashed.actions == {"execute": 1, "repair": LAST_REPAIR}
    assert crashed.launches["repair"] == LAST_REPAIR - 1
    capsys.readouterr()
    assert w.run() == 0, capsys.readouterr().out[-1500:]
    led = ledger(w)
    # No handback: the stale grant was released and one new grant reserved, so the run nets exactly the
    # uninterrupted run's grants, launches and charge, and never opened a recovery cycle.
    assert (led.stage, led.charged, led.actions, led.launches) == (
        baseline.stage, baseline.charged, baseline.actions, baseline.launches)
    assert led.cycles == [] and _cancelled_repairs(w) == 1 and len(repair_ids(w)) == LAST_REPAIR
    assert candidate_chain(w) == {led.candidate: sealed_candidate(w)}
    assert _held_resources(tmp_path) == {}


@pytest.mark.parametrize(("point", "code", "action", "held"), [
    ("repair-last-completed-before-harvest", "REPAIR_RECONCILIATION_REQUIRED", "inspect_retained_repair", False),
    ("repair-last-intent-committed", "INTENT_RECONCILIATION_REQUIRED", "reconcile_intent", True),
], ids=["completed-last-repair", "intent-without-ack"])
def test_r1_1b_an_issued_last_repair_of_an_earlier_fence_refuses_typed_with_no_handback(
        tmp_path, monkeypatch, capsys, point, code, action, held):
    w = world(tmp_path, monkeypatch, repair=True, mode={"repair": ""})
    fired = arm(monkeypatch, point)
    w.crash()
    assert fired == [point]
    crashed = ledger(w)
    assert crashed.stage == "EXECUTE" and crashed.actions == {"execute": 1, "repair": LAST_REPAIR}
    capsys.readouterr()
    assert w.run() == 78
    envelope = _last_envelope(capsys)
    assert (envelope["code"], envelope["recovery_action"]["action"]) == (code, action)
    led = ledger(w)
    # No handback and no recovery: the lifecycle gave the unfinished last repair to the producer, which refused.
    assert led.stage == "EXECUTE" and led.cycles == []
    assert led.actions == {"execute": 1, "repair": LAST_REPAIR}
    assert (led.charged, led.launches) == (crashed.charged, crashed.launches)
    assert all(count <= 1 for _action, count in led.intents)
    held_now = _held_resources(tmp_path)
    assert (list(held_now) == ["admissions"]) if held else (held_now == {})


def _watch_recovery(monkeypatch) -> list:
    packets, produce_recovery = [], recovery_producer.produce_recovery

    def recovery(*args, **kwargs):
        packets.append(kwargs["packet"])
        return produce_recovery(*args, **kwargs)

    monkeypatch.setattr(recovery_producer, "produce_recovery", recovery)
    return packets


def test_r1_1c_a_last_repair_that_integrated_still_hands_back_to_recovery_at_the_limit(
        tmp_path, monkeypatch, capsys):
    """Every repair integrates an edit that fixes nothing: the limit is spent on four bound candidates, then recovery."""
    w = world(tmp_path, monkeypatch, repair=True, mode={"repair": "unrelated\n"})
    packets = _watch_recovery(monkeypatch)
    capsys.readouterr()
    assert w.run() == 0, capsys.readouterr().out[-1500:]
    led = ledger(w)
    assert led.stage == "DONE" and led.actions == {**FULL_ACTIONS, "repair": LAST_REPAIR}
    assert len(packets) == 1 and packets[0]["saved_stage"] == "EXECUTE"
    assert sorted(packets[0]["consumed_attempts"]) == sorted(repair_ids(w))
    # Four repaired candidates and the recovery winner form one chain from the sealed input.
    chain = candidate_chain(w)
    assert len(chain) == LAST_REPAIR + 1 and led.candidate in chain
    assert sorted(chain.values()).count(sealed_candidate(w)) == 1
    assert len(repair_journals(w)) == LAST_REPAIR and {state for _k, state, _w in repair_journals(w)} == {"published"}
    assert _held_resources(tmp_path) == {}


def test_r1_1c_a_replay_after_the_last_repair_finished_still_hands_back_to_recovery(
        tmp_path, tmp_path_factory, monkeypatch, capsys):
    mode = {"repair": ""}
    with pytest.MonkeyPatch.context() as patch:
        uninterrupted = world(tmp_path_factory.mktemp("uninterrupted"), patch, repair=True, mode=mode)
        assert uninterrupted.run() == 0
        baseline = ledger(uninterrupted)
    w = world(tmp_path, monkeypatch, repair=True, mode=mode)
    fired = arm(monkeypatch, "repair-last-returned")
    w.crash()
    assert fired == ["repair-last-returned"]
    crashed = ledger(w)
    assert crashed.stage == "EXECUTE" and crashed.actions == {"execute": 1, "repair": LAST_REPAIR}
    assert crashed.launches["repair"] == LAST_REPAIR
    capsys.readouterr()
    assert w.run() == 0, capsys.readouterr().out[-1500:]
    led = ledger(w)
    assert (led.stage, led.charged, led.actions, led.launches) == (
        baseline.stage, baseline.charged, baseline.actions, baseline.launches)
    assert led.actions == {**FULL_ACTIONS, "repair": LAST_REPAIR} and len(led.cycles) == 1
    assert _held_resources(tmp_path) == {}
