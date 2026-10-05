"""Fixture world for the production recovery (diagnosis/trial) producer tests.

The host executable is the lifecycle assembly's Python telemetry fixture with a
recovery prelude: a diagnosis prompt prints a fixed finding, a trial prompt
appends a configurable marker to ``src/input.txt`` (or writes nothing), and the
native review answers per a mode file.  Every launch still crosses the real
ControlStore/Supervisor authority through the real ``frontend-start`` CLI entry.
Credentials are synthetic.  This is fixture proof of the production assembly,
not native host qualification and not E8.
"""
from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import run_state.frontend_policy as frontend_policy
import run_state.frontend_producers as frontend_producers
import run_state.managed_qualification as managed_qualification
import run_state.recovery_producer as recovery_producer
import run_state.supervisor as supervisor_module
import run_state.workspace as workspace
from run_state.recovery_controller import RecoveryController
from run_state.run_policy import action_limit
from run_state.state import ControlStore
from run_state.supervisor import Supervisor, SupervisorRefused
from test_final_review_resume import _Killed, _crash, _held_resources, _outer_intents  # noqa: F401
from test_managed_lifecycle_assembly import _REVIEW, _draft, _fixture_host, _frontend_start, _setup

# Fails on the selected overlay; a trial that appends the marker makes it pass.
CHECK_NEEDS_MARKER = "/usr/bin/grep -q repaired src/input.txt"
CHECK_ALWAYS_PASSES = "/usr/bin/grep -q base-input src/input.txt"
FULL_ACTIONS = {"execute": 1, "recovery_cycle_normal": 1, "diagnosis": 1, "recovery_trial": 1, "final_review": 1}

_PRELUDE = '''
MODE = json.load(open(MODE_PATH))
def _emit(text):
    print(json.dumps({"type":"thread.started","thread_id":"recovery-thread"}), flush=True)
    print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":text}}), flush=True)
    print(json.dumps({"type":"turn.completed","usage":{"input_tokens":7,"cached_input_tokens":2,"cache_write_input_tokens":1,"output_tokens":3,"reasoning_output_tokens":2}}), flush=True)
if prompt.startswith("Recovery diagnosis request:"):
    _emit(MODE["diagnosis"])
    raise SystemExit(0)
if prompt.startswith("Repair request:"):
    counter = pathlib.Path(MODE_PATH + ".repairs")
    launches = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(launches))
    if MODE.get("repair") and launches >= MODE.get("repair_from", 1):
        target = pathlib.Path(MODE.get("repair_file", "src/input.txt"))
        target.write_text(target.read_text() + MODE["repair"])
    _emit("repair applied")
    raise SystemExit(0)
if prompt.startswith("Recovery trial request:"):
    if MODE.get("trial"):
        target = pathlib.Path(MODE.get("trial_file", "src/input.txt"))
        target.write_text(target.read_text() + MODE["trial"])
    _emit("trial applied")
    raise SystemExit(0)
'''


def host_script(mode_path) -> str:
    """The lifecycle assembly's host script with the recovery prelude and mode-driven review verdicts."""
    script = _REVIEW
    for old, new in (
        ('"status": "passed"', '"status": ("failed" if MODE.get("review") == "failed" else "passed")'),
        ('"text":text}', '"text":("not json" if MODE.get("review") == "malformed" else text)}'),
    ):
        assert script.count(old) == 1, old
        script = script.replace(old, new)
    head = "prompt = sys.argv[-1]\n"
    assert script.count(head) == 1
    return script.replace(head, head + _PRELUDE.replace("MODE_PATH", repr(str(mode_path))))


def _no_repair(*_args, **_kwargs):
    """The pre-3b world: no repair is attempted, so a failed frozen check hands straight back to recovery."""
    raise SupervisorRefused("REPAIR_BUDGET_INFEASIBLE")


def world(tmp_path, monkeypatch, *, check=CHECK_NEEDS_MARKER, mode=None, run_id="rs", draft_mode=None, repair=False):
    """A fresh managed repository, the fixture host and a ``run()`` that replays the identical request.

    ``repair=False`` keeps the recovery tests' premise (a failed check hands back at once) by refusing the repair
    producer as infeasible before anything is reserved; ``repair=True`` runs the production repair producer.
    """
    if not repair:
        monkeypatch.setattr(recovery_producer, "produce_repair", _no_repair, raising=False)
    primary, authority, repository_id, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    for key in ("GSD_RUN_ID", "FFS_RUN_ID", "GSD_RESUME"):
        monkeypatch.delenv(key, raising=False)
    runtime, fake, catalog = _fixture_host(tmp_path, monkeypatch)
    mode_path = tmp_path / "recovery-mode.json"
    mode_path.write_text(json.dumps({"diagnosis": "the input lacks the repaired marker", "trial": "repaired\n",
                                     "review": "passed", "repair": "repaired\n", **(mode or {})}))
    fake.write_text(f"#!{sys.executable}\n" + host_script(mode_path))
    # The fixture host cannot open a real GSD wave.
    monkeypatch.setattr(supervisor_module, "_gsd_wave_completion_code", lambda *_a, require_wave=True: None)
    draft = _draft(tmp_path, check=check, mode=draft_mode)

    def run(*extra):
        return _frontend_start(env, authority, run_id, runtime, fake, catalog, draft, "task-swarm", "--scope", "1",
                               "--dispatch-limit", "80", "--token-limit", "100000", *extra)

    return SimpleNamespace(tmp_path=tmp_path, primary=primary, authority=authority, repository_id=repository_id,
                           run_id=run_id, run=run, mode_path=mode_path, runtime=runtime, fake=fake, catalog=catalog,
                           env=env, crash=lambda: _crash(run, authority), head=lambda: git_head(primary))


def charge_qualification(monkeypatch) -> int:
    """Make the fixture qualify charge what the real four probes charge (one launch each); returns that cost.

    The fixture qualify launches no probe, but a real recovery or repair child's qualification does, and each new
    owner requalifies a new child.  Charged once per child activity, as a retained qualification charges nothing.
    """
    from run_state.state import _QUALIFICATION_PROBE_ORDER
    real, charged, cost = managed_qualification.qualify_managed_runtime, set(), len(_QUALIFICATION_PROBE_ORDER)

    def qualify(store, token, **kwargs):
        result = real(store, token, **kwargs)
        child = kwargs["role"] == "recovery" or kwargs["activity_request_key"].startswith("repair:")
        if child and kwargs["activity_id"] not in charged:
            charged.add(kwargs["activity_id"])
            with store.transaction() as tx:
                tx.execute("UPDATE authority_run_policy_budgets SET launch_charged=launch_charged+? "
                           "WHERE repository_id=? AND run_id=?", (cost, token.repository_id, token.run_id))
        return result

    monkeypatch.setattr(managed_qualification, "qualify_managed_runtime", qualify)
    return cost


def git_head(path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True,
                          text=True).stdout.strip()


def set_mode(w, **values) -> None:
    current = json.loads(w.mode_path.read_text())
    w.mode_path.write_text(json.dumps({**current, **values}))


def ledger(w):
    """Everything the producer's invariants are stated over, read from the durable store."""
    store = ControlStore(w.authority / "control.sqlite3")
    state = store.get_frontend_policy_state(repository_id=w.repository_id, run_id=w.run_id)
    budget = store.get_run_policy_budget(repository_id=w.repository_id, run_id=w.run_id)
    with store.read_transaction() as tx:
        actions = {row[0]: row[1] for row in tx.execute(
            "SELECT action,count(*) FROM authority_policy_actions WHERE state<>'cancelled' AND action<>'check' "
            "GROUP BY action")}
        cycles = [tuple(row) for row in tx.execute(
            "SELECT recovery_cycle,action,input_hash FROM authority_policy_actions "
            "WHERE action LIKE 'recovery_cycle_%' AND state<>'cancelled' ORDER BY recovery_cycle")]
        intents = [tuple(row) for row in tx.execute(
            "SELECT a.action,COUNT(DISTINCT p.intent_id) FROM authority_policy_actions a "
            "LEFT JOIN authority_policy_action_attempts p ON p.action_id=a.id "
            "WHERE a.state<>'cancelled' AND a.action<>'check' GROUP BY a.id")]
        launches = {row[0]: row[1] for row in tx.execute(
            "SELECT a.action,COUNT(*) FROM authority_policy_action_attempts p JOIN authority_policy_actions a "
            "ON a.id=p.action_id GROUP BY a.action")}
        # Owner-fence bookkeeping of a replayed request is not a producer effect.
        events = tx.execute("SELECT COUNT(*) FROM control_events WHERE event_type NOT IN "
                            "('resources_reserved','resources_released','READY_REVALIDATED')").fetchone()[0]
        native = tx.execute("SELECT count(*) FROM authority_launch_intents i JOIN authority_child_bindings b "
                            "ON b.activity_id=i.activity_id WHERE b.role='reviewer'").fetchone()[0]
        reviews = tx.execute("SELECT count(*) FROM authority_acceptance_receipts "
                             "WHERE json_extract(receipt_json,'$.role')='review'").fetchone()[0]
    return SimpleNamespace(store=store, stage=None if state is None else state.stage,
                           decision=None if state is None else state.decision_json,
                           candidate=None if state is None else state.candidate_hash,
                           charged=None if budget is None else budget.launch_charged, actions=actions, cycles=cycles,
                           intents=intents, launches=launches, events=events, native=native, reviews=reviews)


def candidate_chain(w) -> dict:
    """Bound candidates as ``{candidate_hash: parent_candidate_hash}`` (the sealed input is not a key)."""
    store = ControlStore(w.authority / "control.sqlite3")
    with store.read_transaction() as tx:
        return {row[0]: row[1] for row in tx.execute(
            "SELECT candidate_hash,parent_candidate_hash FROM authority_frontend_policy_candidates "
            "WHERE repository_id=? AND run_id=?", (w.repository_id, w.run_id))}


def sealed_candidate(w) -> str:
    store = ControlStore(w.authority / "control.sqlite3")
    return store.get_sealed_acceptance(repository_id=w.repository_id, run_id=w.run_id).material["candidate_hash"]


def recovery_workspaces(w) -> list[tuple]:
    """``(child_request_key, state, input_digest)`` of every recovery child workspace, oldest first."""
    store = ControlStore(w.authority / "control.sqlite3")
    with store.read_transaction() as tx:
        return [tuple(row) for row in tx.execute(
            "SELECT w.child_request_key,w.state,s.input_digest FROM context_workspaces w "
            "LEFT JOIN context_input_snapshots s ON s.preparation_id=w.preparation_id "
            "WHERE w.child_request_key LIKE 'recovery:%' ORDER BY w.created_at,w.child_request_key")]


def assert_recovered_once(w, *, held=True) -> object:
    """The invariants of one uninterrupted recovery that reached DONE."""
    led = ledger(w)
    assert led.stage == "DONE"
    assert led.actions == FULL_ACTIONS
    assert all(count <= 1 for _action, count in led.intents)
    assert [led.launches[name] for name in ("execute", "diagnosis", "recovery_trial", "final_review")] == [1, 1, 1, 1]
    assert len(led.cycles) == 1 and (led.native, led.reviews) == (1, 1)
    assert _outer_intents(led) == [("completed_succeeded", "succeeded")]
    if held:
        assert _held_resources(w.tmp_path) == {}
    return led


REPAIRED_ACTIONS = {"execute": 1, "repair": 1, "final_review": 1}


def repair_ids(w) -> set:
    """The non-cancelled repair grants, read from a fresh connection."""
    store = ControlStore(w.authority / "control.sqlite3")
    with store.read_transaction() as tx:
        return {row[0] for row in tx.execute(
            "SELECT id FROM authority_policy_actions WHERE action='repair' AND state<>'cancelled'")}


def repair_journals(w) -> list[tuple]:
    """``(wave_key, state, workspace)`` of every repair journal, oldest first."""
    store = ControlStore(w.authority / "control.sqlite3")
    with store.read_transaction() as tx:
        return [(row[0], row[1], json.loads(row[2])["authority"]["workspace"]) for row in tx.execute(
            "SELECT wave_key,state,contract_json FROM authority_workspace_integrations "
            "WHERE wave_key LIKE 'repair:%' ORDER BY event_id")]


def policy_tier(w) -> str:
    store = ControlStore(w.authority / "control.sqlite3")
    return store.get_run_policy_budget(repository_id=w.repository_id, run_id=w.run_id).tier


def assert_repaired_once(w, *, held=True) -> object:
    """The invariants of one uninterrupted ordinary repair that reached DONE with no recovery."""
    led = ledger(w)
    assert led.stage == "DONE"
    assert led.actions == REPAIRED_ACTIONS
    assert all(count <= 1 for _action, count in led.intents)
    assert [led.launches[name] for name in ("execute", "repair", "final_review")] == [1, 1, 1]
    assert led.cycles == [] and (led.native, led.reviews) == (1, 1)
    assert _outer_intents(led) == [("completed_succeeded", "succeeded")]
    ((key, state, _workspace),) = repair_journals(w)
    assert state == "published" and key == "repair:" + next(iter(repair_ids(w)))
    if held:
        assert _held_resources(w.tmp_path) == {}
    return led


def watch_binds(monkeypatch) -> list:
    """After each candidate bind the chain resolver must accept the bound candidate: record what it resolved."""
    from run_state.candidate_chain import resolve_current_frontend_candidate
    real, resolved = ControlStore.bind_frontend_candidate, []

    def bind_frontend_candidate(self, token, **kwargs):
        state = real(self, token, **kwargs)
        resolved.append((state.candidate_hash, resolve_current_frontend_candidate(self, token).candidate_hash))
        return state

    monkeypatch.setattr(ControlStore, "bind_frontend_candidate", bind_frontend_candidate)
    return resolved


def watch_repair(monkeypatch, w) -> list:
    """Record each call of the repair producer as ``(outcome code, repair grants it added)``."""
    real, seen = recovery_producer.produce_repair, []

    def produce_repair(*args, **kwargs):
        before = repair_ids(w)
        try:
            result = real(*args, **kwargs)
        except SupervisorRefused as error:
            seen.append((error.code, sorted(repair_ids(w) - before)))
            raise
        seen.append(("returned", sorted(repair_ids(w) - before)))
        return result

    monkeypatch.setattr(recovery_producer, "produce_repair", produce_repair)
    return seen


# --- crash points -----------------------------------------------------------------------------------------

POINTS = (
    "recover-entered", "diagnosis-workspace-begun", "diagnosis-worktree-created", "diagnosis-workspace-unpublished",
    "diagnosis-qualified", "cycle-reserved", "diagnosis-action-reserved", "diagnosis-intent-committed",
    "diagnosis-completed-before-receipt", "diagnosis-receipt-recorded", "trial-completed-before-checks",
    "trial-checks-retained", "winner-applied-before-integration-record", "winner-recorded-before-bind",
    "continuation-transitioned",
)


REPAIR_POINTS = (
    "repair-entered", "repair-workspace-begun", "repair-qualified", "repair-action-reserved",
    "repair-intent-committed", "repair-completed-before-harvest", "repair-harvested-before-journal",
    "repair-applied-before-capture", "repair-applied-before-integration-record", "repair-recorded-before-bind",
    "repair-bound-before-transition",
)


# Crash points at the medium tier's LAST repair ordinal, where the allowance is spent once that repair is reserved.
LAST_REPAIR = action_limit("repair", "medium")
LAST_REPAIR_POINTS = (
    "repair-last-action-reserved", "repair-last-intent-committed", "repair-last-completed-before-harvest",
    "repair-last-returned",
)


def _is_diagnosis_key(key) -> bool:
    return bool(key) and key.startswith("recovery:") and ":diagnosis" in key


def arm(monkeypatch, point: str) -> list:
    """Raise ``_Killed`` once at ``point`` (a ``kill -9`` stand-in); return the list that records it."""
    fired = []

    def once():
        if not fired:
            fired.append(point)
            raise _Killed()

    def wrap_class(cls, name, before=None, after=None):
        real = getattr(cls, name)

        def wrapper(self, *args, **kwargs):
            if before is not None:
                before(args, kwargs)
            result = real(self, *args, **kwargs)
            if after is not None:
                after(args, kwargs)
            return result
        monkeypatch.setattr(cls, name, wrapper)

    if point == "execute-settled":
        # The outer launch has settled; the candidate is not yet bound and no mapped check has run.
        bind = frontend_producers._bind_executed_candidate

        def bind_executed_candidate(*args, **kwargs):
            once()
            return bind(*args, **kwargs)
        monkeypatch.setattr(frontend_producers, "_bind_executed_candidate", bind_executed_candidate)
    elif point in {"recover-entered", "continuation-transitioned"}:
        def entered(_args, kwargs):
            if point == "recover-entered" and kwargs["new_stage"] == "RECOVER":
                once()
            if (point == "continuation-transitioned" and kwargs["expected_stage"] == "RECOVER"
                    and kwargs["new_stage"] in {"EXECUTE", "FINAL_REVIEW"}):
                once()
        wrap_class(ControlStore, "transition_frontend_policy", after=entered)
    elif point in {"diagnosis-workspace-begun", "diagnosis-worktree-created", "diagnosis-workspace-unpublished"}:
        create, open_chain, publish = (workspace._create_registered_worktree, workspace._open_directory_chain_raw,
                                       workspace.publish_workspace_ready)
        diagnosis = {}

        def create_registered_worktree(store, token, preparation, admin_fd, **kwargs):
            if _is_diagnosis_key(preparation.child_request_key):
                diagnosis[preparation.id] = preparation.path
                if point == "diagnosis-workspace-begun":
                    once()
            return create(store, token, preparation, admin_fd, **kwargs)

        def open_directory_chain_raw(root, parts, *, create):
            from pathlib import Path
            if point == "diagnosis-worktree-created" and Path(root, *parts) in diagnosis.values():
                once()
            return open_chain(root, parts, create=create)

        def publish_workspace_ready(store, token, preparation, *args, **kwargs):
            if point == "diagnosis-workspace-unpublished" and (
                    preparation if isinstance(preparation, str) else preparation.id) in diagnosis:
                once()
            return publish(store, token, preparation, *args, **kwargs)
        monkeypatch.setattr(workspace, "_create_registered_worktree", create_registered_worktree)
        monkeypatch.setattr(workspace, "_open_directory_chain_raw", open_directory_chain_raw)
        monkeypatch.setattr(workspace, "publish_workspace_ready", publish_workspace_ready)
    elif point == "diagnosis-qualified":
        qualify = managed_qualification.qualify_managed_runtime

        def qualify_managed_runtime(store, token, **kwargs):
            result = qualify(store, token, **kwargs)
            if kwargs["role"] == "recovery":
                once()
            return result
        monkeypatch.setattr(managed_qualification, "qualify_managed_runtime", qualify_managed_runtime)
    elif point in {"cycle-reserved", "diagnosis-action-reserved"}:
        def reserved(_args, kwargs):
            action = kwargs["action"]
            if action.startswith("recovery_cycle_") if point == "cycle-reserved" else action == "diagnosis":
                once()
        wrap_class(ControlStore, "reserve_policy_action", after=reserved)
    elif point == "diagnosis-intent-committed":
        armed, fault, reserve = [], Supervisor._fault, Supervisor.reserve_request_action

        def reserve_request_action(self, request, *, action, **kwargs):
            if action == "diagnosis":
                armed.append(request.activity_id)
            return reserve(self, request, action=action, **kwargs)

        def crash_at(self, name):
            if armed and name == "after_intent_commit":
                once()
            return fault(self, name)
        monkeypatch.setattr(Supervisor, "reserve_request_action", reserve_request_action)
        monkeypatch.setattr(Supervisor, "_fault", crash_at)
    elif point in {"diagnosis-completed-before-receipt", "diagnosis-receipt-recorded"}:
        def recovery_receipt_hit(_args, kwargs):
            if kwargs["receipt"]["role"] == "recovery":
                once()
        if point == "diagnosis-completed-before-receipt":
            wrap_class(ControlStore, "record_acceptance_receipt", before=recovery_receipt_hit)
        else:
            wrap_class(ControlStore, "record_acceptance_receipt", after=recovery_receipt_hit)
    elif point == "trial-completed-before-checks":
        checks = frontend_policy.run_sealed_checks

        def run_sealed_checks(*args, **kwargs):
            if str(kwargs.get("key_prefix", "")).startswith("trial-check:"):
                once()
            return checks(*args, **kwargs)
        monkeypatch.setattr(frontend_policy, "run_sealed_checks", run_sealed_checks)
    elif point == "trial-checks-retained":
        wrap_class(RecoveryController, "consume", before=lambda _a, _k: once())
    elif point == "winner-applied-before-integration-record":
        wrap_class(ControlStore, "record_frontend_integration", before=lambda _a, _k: once())
    elif point == "winner-recorded-before-bind":
        wrap_class(ControlStore, "bind_frontend_candidate", before=lambda _a, _k: once())
    elif point in REPAIR_POINTS:
        _arm_repair(monkeypatch, point, once, wrap_class)
    elif point in LAST_REPAIR_POINTS:
        _arm_last_repair(monkeypatch, point, once, wrap_class)
    else:
        raise AssertionError(point)
    return fired


def _arm_repair(monkeypatch, point: str, once, wrap_class) -> None:
    """The ordinary-repair crash points; ``once`` raises ``_Killed`` the first time it is called."""
    import run_state.wave_execution as wave_execution

    def is_repair(key) -> bool:
        return bool(key) and key.startswith("repair:")

    if point == "repair-entered":
        produce_repair = recovery_producer.produce_repair

        def entered(*args, **kwargs):
            once()
            return produce_repair(*args, **kwargs)
        monkeypatch.setattr(recovery_producer, "produce_repair", entered)
    elif point == "repair-workspace-begun":
        create = workspace._create_registered_worktree

        def create_registered_worktree(store, token, preparation, admin_fd, **kwargs):
            if is_repair(preparation.child_request_key):
                once()
            return create(store, token, preparation, admin_fd, **kwargs)
        monkeypatch.setattr(workspace, "_create_registered_worktree", create_registered_worktree)
    elif point == "repair-qualified":
        qualify = managed_qualification.qualify_managed_runtime

        def qualify_managed_runtime(store, token, **kwargs):
            result = qualify(store, token, **kwargs)
            if is_repair(kwargs["activity_request_key"]):
                once()
            return result
        monkeypatch.setattr(managed_qualification, "qualify_managed_runtime", qualify_managed_runtime)
    elif point == "repair-action-reserved":
        wrap_class(ControlStore, "reserve_policy_action",
                   after=lambda _args, kwargs: kwargs["action"] == "repair" and once())
    elif point == "repair-intent-committed":
        armed, fault, reserve = [], Supervisor._fault, Supervisor.reserve_request_action

        def reserve_request_action(self, request, *, action, **kwargs):
            if action == "repair":
                armed.append(request.activity_id)
            return reserve(self, request, action=action, **kwargs)

        def crash_at(self, name):
            if armed and name == "after_intent_commit":
                once()
            return fault(self, name)
        monkeypatch.setattr(Supervisor, "reserve_request_action", reserve_request_action)
        monkeypatch.setattr(Supervisor, "_fault", crash_at)
    elif point == "repair-completed-before-harvest":
        wrap_class(ControlStore, "record_acceptance_receipt",
                   after=lambda _args, kwargs: is_repair(kwargs["receipt"]["request_key"]) and once())
    elif point == "repair-harvested-before-journal":
        wrap_class(ControlStore, "register_integration_intent_tx", before=lambda _args, _kwargs: once())
    elif point == "repair-applied-before-capture":
        capture = wave_execution.capture_integration_candidate

        def capture_integration_candidate(*args, **kwargs):
            once()
            return capture(*args, **kwargs)
        monkeypatch.setattr(wave_execution, "capture_integration_candidate", capture_integration_candidate)
    elif point == "repair-applied-before-integration-record":
        wrap_class(ControlStore, "record_frontend_integration", before=lambda _args, _kwargs: once())
    elif point == "repair-recorded-before-bind":
        wrap_class(ControlStore, "bind_frontend_candidate", before=lambda _args, _kwargs: once())
    else:
        wrap_class(ControlStore, "bind_frontend_candidate", after=lambda _args, _kwargs: once())


def _arm_last_repair(monkeypatch, point: str, once, wrap_class) -> None:
    """Crash on the ``LAST_REPAIR``-th repair only; the earlier repairs run to completion."""
    hits = []

    def last() -> bool:
        hits.append(1)
        return len(hits) == LAST_REPAIR

    if point == "repair-last-action-reserved":
        wrap_class(ControlStore, "reserve_policy_action",
                   after=lambda _args, kwargs: kwargs["action"] == "repair" and last() and once())
    elif point == "repair-last-intent-committed":
        armed, fault, reserve = [], Supervisor._fault, Supervisor.reserve_request_action

        def reserve_request_action(self, request, *, action, **kwargs):
            if action == "repair":
                armed.append(request.activity_id)
            return reserve(self, request, action=action, **kwargs)

        def crash_at(self, name):
            if len(armed) == LAST_REPAIR and name == "after_intent_commit":
                once()
            return fault(self, name)
        monkeypatch.setattr(Supervisor, "reserve_request_action", reserve_request_action)
        monkeypatch.setattr(Supervisor, "_fault", crash_at)
    elif point == "repair-last-completed-before-harvest":
        wrap_class(ControlStore, "record_acceptance_receipt",
                   after=lambda _args, kwargs: kwargs["receipt"]["request_key"].startswith("repair:")
                   and last() and once())
    else:
        produce_repair = recovery_producer.produce_repair

        def returned(*args, **kwargs):
            result = produce_repair(*args, **kwargs)
            if last():
                once()
            return result
        monkeypatch.setattr(recovery_producer, "produce_repair", returned)
