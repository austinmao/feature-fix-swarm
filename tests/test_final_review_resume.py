"""F51: a managed run that crashes after its outer launch settled resumes to DONE.

M5 (``e2e-m5a-phase02``) killed ``frontend-start`` at policy stage FINAL_REVIEW
while the native reviewer's qualification probes were in flight.  The outer
orchestrator's launch had already settled ``completed_succeeded``; the same-build
resume (same run id, same request key) refused ``REQUEST_ALREADY_COMPLETED`` and
the run could never finish.  The resume must continue the lifecycle from the
retained stage instead: never relaunch the settled outer launch, never launch a
second final review, and leave no admission, lease or group slot behind.

Real ControlStore, Supervisor, admission queue and CLI entry throughout; the
Codex binary, private staging and qualification observer are the lifecycle
assembly's fixture seams.  A crash is an uncaught ``BaseException`` at the named
point (no typed refusal or settle path runs), after which any fence the owner
still holds is left to a dead process, as ``kill -9`` leaves it.
"""
from __future__ import annotations

import sqlite3
import subprocess

import pytest

import run_state.frontend_producers as frontend_producers
import run_state.managed_qualification as managed_qualification
from run_state.state import ControlStore
from test_final_review_standalone_lease import _scripted_probe
from test_managed_lifecycle_assembly import (
    _draft, _facts, _fixture_host, _frontend_start, _last_envelope, _setup, requires_local_confinement,
)


class _Killed(BaseException):
    """Stands in for ``kill -9`` of ``frontend-start`` at one point of the lifecycle."""


def _start(tmp_path, monkeypatch):
    primary, authority, repository_id, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    for key in ("GSD_RUN_ID", "FFS_RUN_ID", "GSD_RESUME"):
        monkeypatch.delenv(key, raising=False)
    runtime, fake, catalog = _fixture_host(tmp_path, monkeypatch)
    import run_state.supervisor as supervisor_module
    monkeypatch.setattr(supervisor_module, "_gsd_wave_completion_code", lambda *_args, require_wave=True: None)
    draft = _draft(tmp_path)

    def run(*extra):
        return _frontend_start(env, authority, "rs", runtime, fake, catalog, draft, "task-swarm",
                               "--scope", "1", *extra)

    return authority, repository_id, run


def _crash(run, authority):
    with pytest.raises(_Killed):
        run()
    # A SIGKILLed owner leaves its fence held by a dead process; the resume reclaims it.
    dead = subprocess.Popen(["/usr/bin/true"])
    dead.wait()
    with ControlStore(authority / "control.sqlite3").transaction() as tx:
        tx.execute("UPDATE control_reservations SET pid=? WHERE held=1", (dead.pid,))


def _outer_intents(facts):
    with facts.store.read_transaction() as tx:
        return [tuple(row) for row in tx.execute(
            "SELECT state,completion_status FROM authority_launch_intents WHERE capacity_exempt=1 "
            "AND NOT EXISTS (SELECT 1 FROM authority_qualification_launches q "
            "WHERE q.intent_id=authority_launch_intents.id)").fetchall()]


def _held_resources(tmp_path) -> dict:
    """Every admission, standalone lease or prepaid group slot still held in the shared queue."""
    with sqlite3.connect(f"file:{tmp_path / 'managed-admission' / 'admission.sqlite3'}?mode=ro", uri=True) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # Not vacuous: the run admitted its outer group, checks and reviewer through this queue.
        assert db.execute("SELECT count(*) FROM managed_admissions").fetchone()[0] > 0
        held = {"admissions": db.execute("SELECT sequence,status FROM managed_admissions "
                                         "WHERE status NOT IN ('released','reclaimed')").fetchall()}
        if "resource_parent_groups" in tables:
            held["groups"] = db.execute("SELECT group_id,state FROM resource_parent_groups "
                                        "WHERE state IN ('staging','reserved')").fetchall()
            held["claims"] = db.execute("SELECT group_id,request_key FROM resource_parent_group_claims "
                                        "WHERE state='active'").fetchall()
    return {key: value for key, value in held.items() if value}


def _assert_done_once(authority, repository_id):
    facts = _facts(authority, repository_id, "rs")
    # One outer launch, one native review, one receipt, one grant each: nothing re-ran.
    assert (facts.stage, facts.native, facts.reviews) == ("DONE", 1, 1)
    assert facts.actions["execute"] == 1 and facts.actions["final_review"] == 1
    assert _outer_intents(facts) == [("completed_succeeded", "succeeded")]
    with facts.store.read_transaction() as tx:
        open_activities = tx.execute(
            "SELECT b.role,a.state FROM authority_activities a JOIN authority_child_bindings b ON b.activity_id=a.id "
            "WHERE b.role IN ('worker','reviewer') AND a.state NOT IN ('succeeded','failed','aborted')").fetchall()
    assert [tuple(row) for row in open_activities] == []
    return facts


@requires_local_confinement
@pytest.mark.parametrize("point", ["probes-in-flight", "reviewer-qualified"])
def test_m5_crash_during_reviewer_qualification_resumes_to_done_with_one_review(tmp_path, monkeypatch, point):
    authority, repository_id, run = _start(tmp_path, monkeypatch)
    fixture_qualify = managed_qualification.qualify_managed_runtime
    crashed = []

    def qualify(store, token, **kwargs):
        if kwargs["role"] != "reviewer" or crashed:
            return fixture_qualify(store, token, **kwargs)
        crashed.append(kwargs["activity_id"])
        # One reviewer probe ran on the channel-less review supervisor (a standalone lease, F50).
        supervisor, workspace = kwargs["supervisor"], kwargs["workspace"]
        request, contract = _scripted_probe(
            store, token, parent_activity_id=kwargs["parent_activity_id"], base_commit=workspace.base_commit,
            repository_path=workspace.repository_path, root=tmp_path, key="reviewer-probe")
        result = supervisor.finish(supervisor.launch_qualification(request, qualification_contract=contract),
                                   timeout=15)
        assert result["returncode"] == 0
        store.transition_activity(token, request.activity_id, expected="active", new="succeeded",
                                  result=result["evidence"])
        if point == "reviewer-qualified":
            fixture_qualify(store, token, **kwargs)
        raise _Killed()

    monkeypatch.setattr(managed_qualification, "qualify_managed_runtime", qualify)
    _crash(run, authority)
    crashed_facts = _facts(authority, repository_id, "rs")
    # The M5 store: the outer launch settled, the stage stayed FINAL_REVIEW, no review was launched.
    assert (crashed_facts.stage, crashed_facts.native, crashed_facts.reviews) == ("FINAL_REVIEW", 0, 0)
    assert _outer_intents(crashed_facts) == [("completed_succeeded", "succeeded")]
    assert "final_review" not in crashed_facts.actions
    assert _held_resources(tmp_path) == {}

    # The same-build resume: the identical request (same run id, same request key).
    assert run() == 0
    _assert_done_once(authority, repository_id)
    assert _held_resources(tmp_path) == {}
    # A terminal run replays without preparing or launching anything.
    assert run() == 0
    _assert_done_once(authority, repository_id)


@requires_local_confinement
@pytest.mark.parametrize(("point", "code"), [
    # Reserved, never acknowledged: only owner-fence reconciliation may settle it.
    ("after_intent_commit", "INTENT_RECONCILIATION_REQUIRED"),
    ("after_ack_before_authorization", "INTENT_RECONCILIATION_REQUIRED"),
    # Settled, not recorded: its native completion proof binds the owner fence that issued it.
    ("settled-unrecorded", "REVIEW_RECONCILIATION_REQUIRED"),
])
def test_crash_inside_the_review_launch_refuses_typed_and_never_launches_a_second_review(
        tmp_path, monkeypatch, capsys, point, code):
    from run_state.supervisor import Supervisor
    authority, repository_id, run = _start(tmp_path, monkeypatch)
    reviewing = []
    if point == "settled-unrecorded":
        def crash_before_record(supervisor, handle, *, acceptance_hash):
            raise _Killed()
        monkeypatch.setattr(frontend_producers, "record_final_review", crash_before_record)
    else:
        launch, fault = Supervisor.launch_native_review, Supervisor._fault

        def launch_native_review(self, request):
            reviewing.append(request.activity_id)
            return launch(self, request)

        def crash_at(self, name):
            if reviewing and name == point:
                raise _Killed()
            return fault(self, name)
        monkeypatch.setattr(Supervisor, "launch_native_review", launch_native_review)
        monkeypatch.setattr(Supervisor, "_fault", crash_at)
    _crash(run, authority)
    crashed = _facts(authority, repository_id, "rs")
    assert (crashed.stage, crashed.native, crashed.reviews) == ("FINAL_REVIEW", 1, 0)
    assert crashed.actions["final_review"] == 1

    capsys.readouterr()
    assert run() == 78
    envelope = _last_envelope(capsys)
    assert envelope["code"] == code
    assert envelope["recovery_action"]["action"] == (
        "inspect_retained_review" if code == "REVIEW_RECONCILIATION_REQUIRED" else "reconcile_intent")
    facts = _facts(authority, repository_id, "rs")
    # The one review intent and grant stay as the crash left them: nothing was launched again.
    assert (facts.stage, facts.native, facts.reviews) == ("FINAL_REVIEW", 1, 0)
    assert facts.actions["final_review"] == 1
    assert _outer_intents(facts) == [("completed_succeeded", "succeeded")]
    if point == "settled-unrecorded":
        # The settled review released its lease before the crash; the refusal holds nothing.
        assert _held_resources(tmp_path) == {}


@requires_local_confinement
def test_crash_after_the_review_recorded_finishes_to_done_idempotently(tmp_path, monkeypatch):
    authority, repository_id, run = _start(tmp_path, monkeypatch)
    settle = frontend_producers._settle_reviewers
    calls = []

    def crash_before_settle(store, token, *, parent_activity_id):
        calls.append(parent_activity_id)
        if len(calls) == 1:
            raise _Killed()
        return settle(store, token, parent_activity_id=parent_activity_id)

    monkeypatch.setattr(frontend_producers, "_settle_reviewers", crash_before_settle)
    _crash(run, authority)
    crashed = _facts(authority, repository_id, "rs")
    assert (crashed.stage, crashed.native, crashed.reviews) == ("FINAL_REVIEW", 1, 1)

    assert run() == 0
    _assert_done_once(authority, repository_id)
    assert _held_resources(tmp_path) == {}


@requires_local_confinement
def test_a_failed_settled_outer_launch_keeps_the_completed_refusal(tmp_path, monkeypatch, capsys):
    authority, repository_id, run = _start(tmp_path, monkeypatch)
    fixture_qualify = managed_qualification.qualify_managed_runtime

    def qualify(store, token, **kwargs):
        if kwargs["role"] == "reviewer":
            raise _Killed()
        return fixture_qualify(store, token, **kwargs)

    monkeypatch.setattr(managed_qualification, "qualify_managed_runtime", qualify)
    _crash(run, authority)
    facts = _facts(authority, repository_id, "rs")
    assert facts.stage == "FINAL_REVIEW"
    with facts.store.transaction() as tx:
        tx.execute("UPDATE authority_launch_intents SET state='completed_failed',completion_status='failed' "
                   "WHERE capacity_exempt=1 AND NOT EXISTS (SELECT 1 FROM authority_qualification_launches q "
                   "WHERE q.intent_id=authority_launch_intents.id)")
    capsys.readouterr()
    assert run() == 78
    envelope = _last_envelope(capsys)
    assert (envelope["code"], envelope["recovery_action"]) == (
        "REQUEST_ALREADY_COMPLETED", {"action": "inspect_completed_launch"})
    again = _facts(authority, repository_id, "rs")
    assert (again.stage, again.native, again.reviews) == ("FINAL_REVIEW", 0, 0)
    assert "final_review" not in again.actions
