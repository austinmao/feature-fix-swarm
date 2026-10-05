"""spec-014 E8 prerequisite 3b: the production ordinary repair producer, against a failed frozen check.

Until this producer, ``drive_managed_session`` passed the lifecycle no ``repair``: a failed frozen check handed
straight back to RECOVER.  Each test drives the real ``frontend-start`` CLI entry over the real ControlStore and
Supervisor with the lifecycle assembly's fixture host (synthetic credentials).  The repair child is an isolated
copy of the current candidate; its harvested patch is merged through the journaled integration path under a
``repair:<action_id>`` key and the candidate is bound with the previous one as its parent (R-direct).
Fixture-level proof of the production assembly only: not native host qualification, not E8.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import run_state.recovery_producer as recovery_producer
from run_state.run_policy import action_limit
from run_state.state import ControlStore
from recovery_fixture import (
    FULL_ACTIONS, arm, assert_recovered_once, assert_repaired_once, candidate_chain, ledger,
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
