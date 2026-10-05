"""spec-014 E8 prerequisite 3b: a repair lands on a candidate a journaled GSD wave advanced in place.

Real task-swarm execution journals its gsd-wave integrations into the outer's preparation and
``bind_wave_execution_candidate`` advances the frontend candidate on that preparation; the preparation's own
first input stays the sealed input.  This builds the production shape at the store level (a real monitored
wave, as ``test_recovery_wave_candidate`` does) and drives ``produce_repair`` itself through the production host
seam over the lifecycle assembly's fixture host: a frozen check fails on the wave's candidate, the repair child
copies that ADVANCED candidate, and its patch is journaled onto the same preparation under ``repair:<id>``.
The second test then tampers with each fact the repair journal and the candidate chain bind, one at a time.
The wave worker, the final review and the host are Python stand-ins with fixture runtime receipts: not a native
repair or host.
"""
import hashlib
import json
from contextlib import contextmanager
from dataclasses import replace
import time
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from test_recovery_wave_candidate import _adapter_bytes, _chain, requires_local_confinement

pytestmark = requires_local_confinement

from run_state.candidate_chain import resolve_current_frontend_candidate, verify_candidate_chain
from run_state.frontend_lifecycle import LifecycleProducers, drive_frontend_lifecycle
from run_state.frontend_policy import FrontendPolicyController
from run_state.host_request import parse_codex_host_request
from run_state.managed import build_frontend_acceptance_draft, prepare_managed_run
from run_state.managed_admission import ManagedAdmissionQueue
from run_state.ownership import OwnershipRefused
from run_state.resource_observation import ResourceObservation
from run_state.sealed_review import record_final_review
from run_state.shared_resources import SharedResourceCoordinator, cold_start_demand
from run_state.state import qualified_runtime_tuple_hash
from run_state.supervisor import DispatchRequest, Supervisor, prepare_managed_codex_session
from run_state.wave_candidate import bind_wave_execution_candidate
from run_state.wave_execution import _head, capture_prelaunch_snapshot
from run_state.workspace import begin_child_workspace_preparation, inspect_workspace, prepare_workspace
from recovery_fixture import host_script
from test_frontend_lifecycle import _REVIEW
from test_managed_lifecycle_assembly import _fixture_host
from test_managed_production_ingress import _setup
from test_runtime_receipt_authority import _qualified
from test_supervised_process import _allocate_registered_child, _receipt_bound_request
from test_wave_consumer import wave_fixture


def _live(store, token, preparation_id, activity_id, digest, role) -> bool:
    """Whether the journal's binding accepts ``digest`` as the input a ``role`` child ran on, on this preparation."""
    from run_state.integration_journal import _recovery_input_is_live_tx
    with store.read_transaction() as tx:
        workspace = tx.execute("SELECT * FROM context_workspaces WHERE preparation_id=?", (preparation_id,)).fetchone()
        return _recovery_input_is_live_tx(tx, token, workspace, activity_id, digest, role=role)


def _drive(tmp_path, monkeypatch, after, *, interrupted=None):
    """Execute one wave, fail its frozen check, repair through ``produce_repair``, reach DONE, then call ``after``.

    ``interrupted`` is a ``RuntimeError`` message: the first repair integration is interrupted by it, once, before
    its journal exists; the same owner retries the repair producer directly (the lifecycle itself would call a
    producer that added no new grant "uncharged") and then re-drives the lifecycle, whose stage ``after`` sees.
    """
    from run_state.recovery_producer import produce_repair
    from run_state.state import ControlStore
    primary, authority, _repository_id, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    seen = []

    def execute(store, token, context):
        store.bind_runtime(token, context.activity_id, "b" * 64)
        parent, ready = _allocate_registered_child(store, token, key="wave-parent")
        store.transition_activity(token, parent.id, expected="pending", new="active", reason="fixture wave parent")
        runtime = qualified_runtime_tuple_hash(_qualified(Path(ready.path)))
        legacy = store.get_acceptance_contract(repository_id=token.repository_id, run_id=token.run_id)
        criterion = legacy.accepted_requirement_ids[0]
        controller = FrontendPolicyController(store, token, command_mode="feature-implement")
        material = build_frontend_acceptance_draft(
            objective_digest=legacy.material["objective_digest"],
            criteria=[{"id": criterion, "objective_clause": "the wave result records the repair",
                       "checks": [{"id": "repaired", "kind": "command",
                                   "locator": "/usr/bin/grep -q repaired result-0.txt"}],
                       "evidence_rules": [{"id": "check-process", "kind": "test", "required": True}]}],
            exclusions=[{"id": "other-source", "reason": "fixture scope"}],
            global_invariants=[{"id": "no-commit", "reason": "retain HEAD"}],
            requested_runtime_hash=runtime, effective_runtime_hash=runtime,
            candidate_hash=ready.input_digest, generation=legacy.generation, command_mode="feature-implement")
        controller.freeze(draft_id="wave-seal", revision=1, material=material)
        sealed = store.get_sealed_acceptance(repository_id=token.repository_id, run_id=token.run_id)
        store.transition_frontend_policy(token, expected_stage="SEALED", new_stage="EXECUTE")
        queue = ManagedAdmissionQueue(tmp_path / "wave-resources", observation_provider=lambda:
            ResourceObservation(time.monotonic_ns(), 4, 4 << 30, 4 << 30, 100, 100, {}, "fixture"))

        def supervisor():
            return Supervisor(store, token, evidence_root=authority / "wave-evidence",
                              shared_resource_coordinator=SharedResourceCoordinator(store, token, queue=queue),
                              resource_demand_policy=cold_start_demand)

        first_input = ready.input_digest
        wave_supervisor = supervisor()
        wave_request = DispatchRequest(parent.id, "wave-0", (sys.executable, "-c", "pass"), str(ready.path),
                                       ready.base_commit, "b" * 64, contract_hash="d" * 64)
        command = (sys.executable, "-c", "from pathlib import Path; Path('result-0.txt').write_text('done')")
        outer_command = (sys.executable, "-c", "from pathlib import Path; import time\n"
                         "while not Path('result-0.txt').exists() or Path('result-0.txt').read_text() != 'done': "
                         "time.sleep(.02)\n")

        def launch_outer(bound):
            return wave_supervisor.launch(wave_supervisor.reserve_request_action(
                replace(bound, monitor_result=True), action="execute"))

        with wave_fixture(tmp_path, monkeypatch, plans=1, commands=[command], wave=1,
                          owner=(wave_supervisor, store, wave_request), outer_command=outer_command,
                          request_key="wave-0", launch_outer=launch_outer) as wave:
            reply = wave.consumer(wave.event)
            assert reply["results"][0]["status"] == "complete"
            _adapter_bytes(wave, reply, parent.id, ready.base_commit)
            result = wave_supervisor.finish(wave.outer, timeout=30)
            _recorded, bound = bind_wave_execution_candidate(
                store, token, sealed=sealed, handle=wave.outer, request_key=wave.request.request_key,
                ready=ready, process_evidence=result["evidence"])
        advanced = bound.candidate_hash
        assert advanced != first_input and _chain(store) == {advanced: first_input}
        # Before any repair the live candidate is a valid input for either role.  The preparation's first input is
        # one only for the activity whose own execution receipt produced the live candidate from it: the wave
        # worker (role execution), never a recovery child.
        for role in ("execution", "recovery"):
            assert _live(store, token, ready.id, parent.id, advanced, role) is True
            assert _live(store, token, ready.id, parent.id, first_input, role) is (role == "execution")
            assert _live(store, token, ready.id, "another-activity", first_input, role) is False
            assert _live(store, token, ready.id, parent.id, "e" * 64, role) is False

        runtime_home, fake, _catalog = _fixture_host(tmp_path, monkeypatch)
        mode_path = tmp_path / "recovery-mode.json"
        mode_path.write_text(json.dumps({"repair": "repaired\n", "repair_file": "result-0.txt"}))
        fake.write_text(f"#!{sys.executable}\n" + host_script(mode_path))
        host_request = parse_codex_host_request(
            runtime_home=str(runtime_home), binary=str(fake), model_request_json='{"kind":"tier","name":"execution"}',
            sandbox="workspace-write", network_enabled=False, token_reservation=100, timeout_seconds=30)
        session = prepare_managed_codex_session(store, token, context, ("/gsd-execute-phase", "1"),
                                                "repair-seam", host_request)
        try:
            review_supervisor = supervisor()
            calls = {"repair": 0, "review": 0}
            retained = {}

            def repair(_frozen, failed):
                calls["repair"] += 1
                assert failed == [criterion]
                produce_repair(store, token, supervisor=review_supervisor, controller=controller,
                               seam=session.seam, parent_activity_id=parent.id, preparation=ready,
                               failed_criteria=failed, timeout_seconds=30)

            def final_review(current):
                calls["review"] += 1
                candidate = resolve_current_frontend_candidate(store, token)
                retained["resolved"] = candidate.candidate_hash
                live = inspect_workspace(store, candidate.workspace_preparation_id)
                snapshot = capture_prelaunch_snapshot(store, token, live, activity_id=candidate.parent_activity_id,
                                                      runtime_identity=candidate.runtime_identity,
                                                      evidence_root=review_supervisor.evidence_root)
                pending = begin_child_workspace_preparation(
                    store, token, parent_activity_id=candidate.parent_activity_id, request_key="final-review",
                    role="reviewer", base_commit=live.base_commit, selected_input_manifest=snapshot.manifest,
                    repository_path=live.repository_path)
                review_ready = prepare_workspace(store, token, pending, input_snapshot=snapshot)
                reviewer = store.create_child_activity(
                    token, parent_activity_id=candidate.parent_activity_id, role="reviewer",
                    request_key="final-review-activity", candidate_hash=review_ready.input_digest,
                    contract_hash=current.acceptance_hash, runtime_identity=runtime,
                    workspace_binding=str(review_ready.path), workspace_preparation_id=review_ready.id, retry_budget=1)
                store.transition_activity(token, reviewer.id, expected="pending", new="active", reason="fixture review")
                review_request = _receipt_bound_request(store, token, DispatchRequest(
                    reviewer.id, "final-review-launch",
                    (sys.executable, "-c", _REVIEW, current.acceptance_hash, review_ready.input_digest, criterion,
                     "passed"), str(review_ready.path), review_ready.base_commit, "b" * 64,
                    contract_hash=current.acceptance_hash))
                handle = review_supervisor.launch(controller.reserve_stage(
                    review_supervisor, review_request, action="final_review"))
                review_supervisor.finish(handle, timeout=30, token_usage=0)
                retained["review"] = handle.result["evidence"]
                try:
                    record_final_review(review_supervisor, handle, acceptance_hash=current.acceptance_hash)
                finally:
                    store.transition_activity(token, reviewer.id, expected="active", new="succeeded",
                                              result=handle.result["evidence"])

            def settle():
                if store.get_activity(parent.id).state == "active":
                    store.transition_activity(token, parent.id, expected="active", new="succeeded",
                                              result=retained["review"], reason="candidate accepted")

            producers = LifecycleProducers(
                execute=lambda _frozen: pytest.fail("the wave already advanced the candidate: execution must not rerun"),
                final_review=final_review,
                recover=lambda _packet: pytest.fail("the repair fixes the check: no recovery"),
                settle=settle, repair=repair)
            if interrupted is not None:
                register, raised = ControlStore.register_integration_intent_tx, []

                def register_integration_intent_tx(self, tx, token_, wave_key, *args, **kwargs):
                    if wave_key.startswith("repair:") and not raised:
                        raised.append(wave_key)
                        raise RuntimeError(interrupted)
                    return register(self, tx, token_, wave_key, *args, **kwargs)

                monkeypatch.setattr(ControlStore, "register_integration_intent_tx", register_integration_intent_tx)
            drive = dict(supervisor=review_supervisor, controller=controller, workspace=str(ready.path),
                         parent_activity_id=parent.id, producers=producers)
            if interrupted is None:
                stage = drive_frontend_lifecycle(store, token, **drive)
            else:
                with pytest.raises(RuntimeError, match=interrupted):
                    drive_frontend_lifecycle(store, token, **drive)
                retained["interrupted_calls"] = dict(calls)
                repair(None, [criterion])
                stage = drive_frontend_lifecycle(store, token, **drive)
            after(SimpleNamespace(
                store=store, token=token, stage=stage, calls=calls, retained=retained, ready=ready, parent=parent,
                advanced=advanced, first_input=first_input, criterion=criterion, controller=controller,
                supervisor=review_supervisor, producers=producers))
            seen.append(stage)
            return 0
        finally:
            session.close(None, None, None)

    result = prepare_managed_run(objective=env["FFS_OBJECTIVE"], state_root=authority,
        selection_manifest=env["FFS_SELECTION_MANIFEST"], upstream_runtime_manifest=env["FFS_UPSTREAM_RUNTIME_MANIFEST"],
        upstream_runtime_sha256=env["FFS_UPSTREAM_RUNTIME_SHA256"], request_key=env["FFS_REQUEST_KEY"],
        run_id=env["GSD_RUN_ID"], command=("/gsd-execute-phase", "1"), activity="execute", scope="1",
        dispatch_limit=24, token_limit=1000, worker_capacity=2, on_ready=execute)
    assert result == 0 and seen == ["DONE"]


def _repair_rows(store) -> SimpleNamespace:
    """The one repair's action, launch intent, child and execution receipt, read from the durable store."""
    with store.read_transaction() as tx:
        action = tx.execute("SELECT * FROM authority_policy_actions WHERE action='repair'").fetchone()
        intent = tx.execute("SELECT i.* FROM authority_policy_action_attempts p JOIN authority_launch_intents i "
                            "ON i.id=p.intent_id WHERE p.action_id=?", (action["id"],)).fetchone()
        child = tx.execute("SELECT b.*,a.state AS activity_state FROM authority_child_bindings b "
                           "JOIN authority_activities a ON a.id=b.activity_id WHERE b.activity_id=?",
                           (intent["activity_id"],)).fetchone()
        receipt = tx.execute("SELECT receipt_hash FROM authority_acceptance_receipts "
                             "WHERE json_extract(receipt_json,'$.activity_id')=?", (intent["activity_id"],)).fetchone()
    return SimpleNamespace(action=dict(action), intent=dict(intent), child=dict(child),
                           receipt_hash=receipt["receipt_hash"])


def test_p6_a_repair_lands_on_a_wave_advanced_candidate_and_reaches_done(tmp_path, monkeypatch):
    def after(ctx):
        store, token = ctx.store, ctx.token
        assert ctx.stage == "DONE" and ctx.calls == {"repair": 1, "review": 1}
        chain = _chain(store)
        (repaired,) = [candidate for candidate, parent in chain.items() if parent == ctx.advanced]
        assert chain == {ctx.advanced: ctx.first_input, repaired: ctx.advanced}
        rows = _repair_rows(store)
        with store.read_transaction() as tx:
            digests = [row[0] for row in tx.execute(
                "SELECT s.input_digest FROM authority_child_bindings b JOIN context_input_snapshots s "
                "ON s.preparation_id=b.workspace_preparation_id WHERE b.role='worker' AND b.activity_id=?",
                (rows.child["activity_id"],))]
            actions = {row[0]: row[1] for row in tx.execute(
                "SELECT action,count(*) FROM authority_policy_actions WHERE action<>'check' GROUP BY action")}
            journals = [tuple(row) for row in tx.execute(
                "SELECT substr(wave_key,1,instr(wave_key,':')-1),state FROM authority_workspace_integrations "
                "ORDER BY event_id")]
        # The repair child copied the ADVANCED candidate, not the preparation's first input.
        assert digests == [ctx.advanced]
        assert actions == {"execute": 2, "repair": 1, "final_review": 1}
        assert journals == [("gsd-wave", "published"), ("repair", "published")]
        assert rows.child["role"] == "worker" and rows.action["state"] in {"dispatched", "completed_valid"}
        activity = rows.child["activity_id"]
        # Once the repair's output is current, its input is accepted only through its own execution receipt.
        assert _live(store, token, ctx.ready.id, activity, ctx.advanced, "execution") is True
        assert _live(store, token, ctx.ready.id, activity, ctx.advanced, "recovery") is False
        assert _live(store, token, ctx.ready.id, ctx.parent.id, ctx.advanced, "execution") is False
        assert _live(store, token, ctx.ready.id, activity, ctx.first_input, "execution") is False
        assert _live(store, token, ctx.ready.id, activity, "e" * 64, "execution") is False
        assert _live(store, token, rows.child["workspace_preparation_id"], activity, repaired, "execution") is False
        state = store.get_frontend_policy_state(repository_id=token.repository_id, run_id=token.run_id)
        assert state.stage == "DONE" and state.candidate_hash == repaired
        assert ctx.retained["resolved"] == repaired
        assert (ctx.ready.path / "result-0.txt").read_text() == "donerepaired\n"
        assert _head(ctx.ready.path) == ctx.ready.base_commit
        # Terminal replay: no producer call.
        assert drive_frontend_lifecycle(store, token, supervisor=ctx.supervisor, controller=ctx.controller,
                                        workspace=str(ctx.ready.path), parent_activity_id=ctx.parent.id,
                                        producers=ctx.producers) == "DONE"
        assert ctx.calls == {"repair": 1, "review": 1}

    _drive(tmp_path, monkeypatch, after)


def _record_event(store, activity_id, key):
    with store.read_transaction() as tx:
        row = tx.execute("SELECT k.event_id,e.payload FROM authority_event_keys k JOIN control_events e "
                         "ON e.id=k.event_id WHERE k.activity_id=? AND k.idempotency_key=?", (activity_id, key)).fetchone()
    return row["event_id"], json.loads(row["payload"])


@contextmanager
def _changed(store, sql, params, restore_sql, restore_params):
    with store.transaction() as tx:
        tx.execute(sql, params)
    try:
        yield
    finally:
        with store.transaction() as tx:
            tx.execute(restore_sql, restore_params)


@contextmanager
def _record_changed(store, activity_id, key, mutate):
    """Rewrite the repair record event with ``mutate`` applied, hash kept consistent so only the binding can refuse."""
    event_id, original = _record_event(store, activity_id, key)
    tampered = json.loads(json.dumps(original))
    mutate(tampered["data"])

    def write(value):
        canonical = json.dumps(value["data"], sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        with store.transaction() as tx:
            tx.execute("UPDATE control_events SET payload=? WHERE id=?", (json.dumps(value), event_id))
            tx.execute("UPDATE authority_event_keys SET payload_hash=? WHERE event_id=?",
                       (hashlib.sha256(canonical).hexdigest(), event_id))
    write(tampered)
    try:
        yield
    finally:
        write(original)


def test_p5_the_repair_journal_and_chain_branch_refuse_each_tampered_fact_exactly(tmp_path, monkeypatch):
    from run_state.integration_journal import validate_completed_publication_tx
    from run_state.repair_integration import REPAIR_SCHEMA, repair_journal_key, repair_record_key

    def after(ctx):
        store, token = ctx.store, ctx.token
        rows = _repair_rows(store)
        action_id, activity_id = rows.action["id"], rows.child["activity_id"]
        key, record_key = repair_journal_key(action_id), repair_record_key(action_id)
        _event_id, record = _record_event(store, activity_id, record_key)
        assert record["data"]["schema"] == REPAIR_SCHEMA and record["data"]["action_id"] == action_id
        assert record["data"]["issuing_intent_id"] == rows.intent["id"]
        assert set(record["data"]) == {"schema", "action_id", "issuing_intent_id", "acceptance_hash", "input_digest",
                                       "workspace_preparation_id", "base_commit", "patch"}
        assert set(record["data"]["patch"]) == {"locator", "sha256", "size", "changed_files"}
        _id, integration = _record_event(store, activity_id, "frontend-integration:" + rows.receipt_hash)
        integration = integration["data"]
        state = store.get_frontend_policy_state(repository_id=token.repository_id, run_id=token.run_id)
        proof = dict(receipt_hash=rows.receipt_hash, candidate_hash=state.candidate_hash,
                     integration_evidence=integration["integration_evidence"], no_commit_evidence=None)

        def journal():
            with store.read_transaction() as tx:
                return validate_completed_publication_tx(store, tx, token, wave_key=key, intent_id=rows.intent["id"])

        def codes():
            outcome = []
            for call in (journal, lambda: verify_candidate_chain(store, token, **proof)):
                try:
                    call()
                except OwnershipRefused as error:
                    outcome.append(error.code)
                else:
                    outcome.append("accepted")
            return tuple(outcome)

        # The untouched chain verifies, journal and whole.
        assert codes() == ("accepted", "accepted")
        assert verify_candidate_chain(store, token, **proof) == integration["candidate_chain"]
        binding = "WAVE_INTEGRATION_BINDING_INVALID"
        cases = {
            "tampered-patch-sha": (_record_changed(store, activity_id, record_key,
                                   lambda data: data["patch"].update(sha256="0" * 64)),
                                   ("FRONTEND_INTEGRATION_PUBLICATION_INVALID", "FRONTEND_INTEGRATION_PUBLICATION_INVALID")),
            "wrong-schema": (_record_changed(store, activity_id, record_key,
                             lambda data: data.update(schema="ffs.recovery-trial-checks/v1")), (binding, binding)),
            "input-digest-not-the-live-candidate": (_record_changed(
                store, activity_id, record_key, lambda data: data.update(input_digest="e" * 64)), (binding, binding)),
            "non-repair-action": (_changed(
                store, "UPDATE authority_policy_actions SET action='recovery_trial' WHERE id=?", (action_id,),
                "UPDATE authority_policy_actions SET action='repair' WHERE id=?", (action_id,)), (binding, binding)),
            "non-worker-role": (_changed(
                store, "UPDATE authority_child_bindings SET role='recovery' WHERE activity_id=?", (activity_id,),
                "UPDATE authority_child_bindings SET role='worker' WHERE activity_id=?", (activity_id,)),
                (binding, "ACCEPTANCE_RECEIPT_BINDING_INVALID")),
            "unsettled-intent": (_changed(
                store, "UPDATE authority_launch_intents SET state='released_to_execute' WHERE id=?", (rows.intent["id"],),
                "UPDATE authority_launch_intents SET state=? WHERE id=?",
                (rows.intent["state"], rows.intent["id"])), (binding, binding)),
        }
        for name, (tamper, expected) in cases.items():
            with tamper:
                assert codes() == expected, name
            assert codes() == ("accepted", "accepted"), name
        # A changed patch file is refused by the chain's physical evidence check, whatever the records say.
        patch = Path(record["data"]["patch"]["locator"])
        original = patch.read_bytes()
        patch.chmod(0o600)
        patch.write_bytes(original + b"\n")
        try:
            assert codes() == ("accepted", "EVIDENCE_INVALID")
        finally:
            patch.write_bytes(original)
        assert codes() == ("accepted", "accepted")

    _drive(tmp_path, monkeypatch, after)


def test_p7_a_repair_interrupted_before_its_journal_resumes_on_the_same_owner_without_a_second_launch(
        tmp_path, monkeypatch):
    def after(ctx):
        store = ctx.store
        assert ctx.stage == "DONE"
        # The producer was called twice, but the second call resumed the one retained repair.
        assert ctx.retained["interrupted_calls"] == {"repair": 1, "review": 0} and ctx.calls == {"repair": 2, "review": 1}
        rows = _repair_rows(store)
        with store.read_transaction() as tx:
            repairs = [tuple(row) for row in tx.execute(
                "SELECT a.state,count(p.intent_id) FROM authority_policy_actions a LEFT JOIN "
                "authority_policy_action_attempts p ON p.action_id=a.id WHERE a.action='repair' GROUP BY a.id")]
            journals = [tuple(row) for row in tx.execute(
                "SELECT substr(wave_key,1,instr(wave_key,':')-1),state FROM authority_workspace_integrations "
                "ORDER BY event_id")]
        assert repairs == [("dispatched", 1)]
        assert journals == [("gsd-wave", "published"), ("repair", "published")]
        assert rows.child["role"] == "worker"
        assert (ctx.ready.path / "result-0.txt").read_text() == "donerepaired\n"

    _drive(tmp_path, monkeypatch, after, interrupted="interrupted before the repair journal")
