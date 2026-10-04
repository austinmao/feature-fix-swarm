"""spec-014 E8 prerequisite 3a: a recovery winner lands on a candidate a journaled GSD wave advanced in place.

Real task-swarm execution journals its gsd-wave integrations into the outer's preparation and
``bind_wave_execution_candidate`` advances the frontend candidate on that preparation; the preparation's
own first input stays the sealed input.  The assembly fixture's execution is planning-only, so this builds
the production shape at the store level (a real monitored wave, as ``test_policy_production_slice`` does) and
then runs the whole lifecycle: a frozen check fails on the wave's candidate, one recovery cycle runs with
Python children, and its winner must be journaled onto the already-advanced candidate and reach DONE.
Python children and fixture runtime receipts are explicit: not a native diagnosis, trial or host.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from dataclasses import replace

import pytest

requires_local_confinement = pytest.mark.skipif(
    sys.platform != "darwin" or not os.path.isfile("/usr/bin/sandbox-exec"),
    reason="needs Darwin sandbox-exec local-check confinement (non-Darwin fails closed; covered by test_local_check_transport)",
)
pytestmark = requires_local_confinement

from run_state.candidate_chain import resolve_current_frontend_candidate
from run_state.frontend_lifecycle import LifecycleProducers, drive_frontend_lifecycle
from run_state.frontend_policy import FrontendPolicyController
from run_state.managed import build_frontend_acceptance_draft, prepare_managed_run
from run_state.managed_admission import ManagedAdmissionQueue
from run_state.recovery_controller import (
    ControlStoreRecoveryAuthority, FrozenRecoveryBinding, RecoveryController, RecoveryTrial,
)
from run_state.recovery_trial_checks import run_isolated_trial_checks
from run_state.resource_observation import ResourceObservation
from run_state.sealed_review import record_final_review
from run_state.shared_resources import SharedResourceCoordinator, cold_start_demand
from run_state.state import qualified_runtime_tuple_hash
from run_state.supervisor import DispatchRequest, Supervisor
from run_state.wave_candidate import bind_wave_execution_candidate
from run_state.wave_execution import _head, capture_prelaunch_snapshot
from run_state.workspace import begin_child_workspace_preparation, inspect_workspace, prepare_workspace
from test_frontend_lifecycle import _REVIEW
from test_managed_production_ingress import _setup
from test_recovery_controller import docs
from test_recovery_trial_checks import _launch, _recovery_child
from test_runtime_receipt_authority import _qualified
from test_supervised_process import _allocate_registered_child, _receipt_bound_request
from test_wave_consumer import wave_fixture


def _adapter_bytes(wave, reply, activity_id, base_commit) -> None:
    """The real adapter's exact no-commit schema, as the production slice writes it (fixture bytes)."""
    root = wave.parent / ".planning/.ffs-supervised/waves" / activity_id
    root.mkdir(parents=True, mode=0o700)
    prefix = root / f"wave-{wave.manifest['wave']}"
    manifest_raw = json.dumps(wave.manifest, sort_keys=True, separators=(",", ":")).encode()
    result_raw = (json.dumps(reply, indent=2) + "\n").encode()
    completion = {"schema": "ffs.gsd-no-commit-completion/v1",
                  "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
                  "result_sha256": hashlib.sha256(result_raw).hexdigest(),
                  "initial_head": base_commit, "commit_mode": "patches"}
    for suffix, raw in ((".manifest.json", manifest_raw), (".result.json", result_raw),
                        (".result.json.receipt.json", (json.dumps(completion) + "\n").encode())):
        path = Path(str(prefix) + suffix)
        path.write_bytes(raw)
        path.chmod(0o600)


def _chain(store) -> dict:
    with store.read_transaction() as tx:
        return {row[0]: row[1] for row in tx.execute(
            "SELECT candidate_hash,parent_candidate_hash FROM authority_frontend_policy_candidates")}


def _live(store, token, preparation_id, activity_id, digest) -> bool:
    """Whether the journal's recovery binding accepts ``digest`` as a trial input on this preparation."""
    from run_state.integration_journal import _recovery_input_is_live_tx
    with store.read_transaction() as tx:
        workspace = tx.execute("SELECT * FROM context_workspaces WHERE preparation_id=?", (preparation_id,)).fetchone()
        return _recovery_input_is_live_tx(tx, token, workspace, activity_id, digest)


def test_a_recovery_winner_lands_on_a_wave_advanced_candidate_and_reaches_done(tmp_path, monkeypatch):
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
                                   "locator": "/usr/bin/grep -qx repaired result-0.txt"}],
                       "evidence_rules": [{"id": "check-process", "kind": "test", "required": True}]}],
            exclusions=[{"id": "other-source", "reason": "fixture scope"}],
            global_invariants=[{"id": "no-commit", "reason": "retain HEAD"}],
            requested_runtime_hash=runtime, effective_runtime_hash=runtime,
            candidate_hash=ready.input_digest, generation=legacy.generation, command_mode="feature-implement")
        frozen = controller.freeze(draft_id="wave-seal", revision=1, material=material)
        sealed = store.get_sealed_acceptance(repository_id=token.repository_id, run_id=token.run_id)
        store.transition_frontend_policy(token, expected_stage="SEALED", new_stage="EXECUTE")
        queue = ManagedAdmissionQueue(tmp_path / "wave-resources", observation_provider=lambda:
            ResourceObservation(time.monotonic_ns(), 4, 4 << 30, 4 << 30, 100, 100, {}, "fixture"))

        def supervisor():
            return Supervisor(store, token, evidence_root=authority / "wave-evidence",
                              shared_resource_coordinator=SharedResourceCoordinator(store, token, queue=queue),
                              resource_demand_policy=cold_start_demand)

        # Execution: one real monitored wave writes the result into the outer's preparation and the candidate
        # advances IN PLACE on it; the preparation's own first input stays the sealed input.
        first_input = ready.input_digest
        wave_supervisor = supervisor()
        request = DispatchRequest(parent.id, "wave-0", (sys.executable, "-c", "pass"), str(ready.path),
                                  ready.base_commit, "b" * 64, contract_hash="d" * 64)
        command = (sys.executable, "-c", "from pathlib import Path; Path('result-0.txt').write_text('done')")
        outer_command = (sys.executable, "-c", "from pathlib import Path; import time\n"
                         "while not Path('result-0.txt').exists() or Path('result-0.txt').read_text() != 'done': "
                         "time.sleep(.02)\n")

        def launch_outer(bound):
            return wave_supervisor.launch(wave_supervisor.reserve_request_action(
                replace(bound, monitor_result=True), action="execute"))

        with wave_fixture(tmp_path, monkeypatch, plans=1, commands=[command], wave=1,
                          owner=(wave_supervisor, store, request), outer_command=outer_command,
                          request_key="wave-0", launch_outer=launch_outer) as wave:
            reply = wave.consumer(wave.event)
            assert reply["results"][0]["status"] == "complete"
            _adapter_bytes(wave, reply, parent.id, ready.base_commit)
            result = wave_supervisor.finish(wave.outer, timeout=30)
            _recorded, bound = bind_wave_execution_candidate(
                store, token, sealed=sealed, handle=wave.outer, request_key=wave.request.request_key,
                ready=ready, process_evidence=result["evidence"])
        advanced = bound.candidate_hash
        assert advanced != first_input and inspect_workspace(store, ready.id).input_digest == first_input
        assert _chain(store) == {advanced: first_input}
        # Before any winner: only the live candidate is a valid trial input; the sealed input the preparation
        # recorded first, and any other digest, are refused.
        assert _live(store, token, ready.id, parent.id, advanced) is True
        assert _live(store, token, ready.id, parent.id, first_input) is False
        assert _live(store, token, ready.id, parent.id, "e" * 64) is False

        review_supervisor = supervisor()
        calls = {"recover": 0, "review": 0}
        retained = {}

        def recover(packet):
            calls["recover"] += 1
            assert packet["saved_stage"] == "EXECUTE" and packet["candidate_hash"] == advanced
            current = resolve_current_frontend_candidate(store, token)
            live = inspect_workspace(store, current.workspace_preparation_id)
            snapshot = capture_prelaunch_snapshot(store, token, live, activity_id=current.parent_activity_id,
                                                  runtime_identity=current.runtime_identity,
                                                  evidence_root=review_supervisor.evidence_root)
            assert snapshot.input_digest == advanced
            binding = FrozenRecoveryBinding(live.base_commit, advanced, advanced, first_input,
                                            frozen.acceptance_hash, runtime, frozen.acceptance_hash)
            cycle = store.reserve_policy_action(token, action="recovery_cycle_normal", logical_key="cycle-1",
                                                input_hash=binding.input_hash, recovery_cycle=1)
            diagnosis, diagnosis_ready = _recovery_child(
                store, token, parent=parent, ready=live, snapshot=snapshot, key="diagnosis",
                acceptance_hash=frozen.acceptance_hash, runtime=runtime)
            _a, diagnosis_receipt, _r, _rt, evidence = _launch(
                store, token, review_supervisor, child=diagnosis, ready=diagnosis_ready, script="pass",
                key="diagnosis-launch", acceptance_hash=frozen.acceptance_hash, action="diagnosis", cycle=1)
            store.transition_activity(token, diagnosis.id, expected="active", new="succeeded", result=evidence)
            child, child_ready = _recovery_child(
                store, token, parent=parent, ready=live, snapshot=snapshot, key="trial",
                acceptance_hash=frozen.acceptance_hash, runtime=runtime)
            script = "from pathlib import Path\nPath('result-0.txt').write_text('repaired')\n"
            retained["trial"] = (child.id, child_ready.id)
            action_id, receipt, _r, _rt, evidence = _launch(
                store, token, review_supervisor, child=child, ready=child_ready, script=script,
                key="trial-launch", acceptance_hash=frozen.acceptance_hash, action="recovery_trial", cycle=1)
            run_isolated_trial_checks(store, token, supervisor=review_supervisor, cycle_action_id=cycle.id,
                trial_action_id=action_id, trial_activity_id=child.id, workspace=str(child_ready.path),
                expected_input_digest=advanced)
            store.transition_activity(token, child.id, expected="active", new="succeeded", result=evidence)
            return RecoveryController(packet, binding, docs(), ControlStoreRecoveryAuthority(store, token, binding)
                                      ).consume(cycle.id, diagnosis_receipt=diagnosis_receipt,
                                                trials=[RecoveryTrial(action_id, receipt)])

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
            final_review=final_review, recover=recover, settle=settle)
        stage = drive_frontend_lifecycle(store, token, supervisor=review_supervisor, controller=controller,
                                         workspace=str(ready.path), parent_activity_id=parent.id, producers=producers)
        assert stage == "DONE" and calls == {"recover": 1, "review": 1}
        chain = _chain(store)
        (winner,) = [candidate for candidate, parent_hash in chain.items() if parent_hash == advanced]
        assert chain == {advanced: first_input, winner: advanced}
        # Once the winner's output is current, its own trial input is accepted only through its own recovery
        # receipt: another activity, a stale input or a foreign preparation are refused.
        trial_id, trial_workspace = retained["trial"]
        assert _live(store, token, ready.id, trial_id, advanced) is True
        assert _live(store, token, ready.id, parent.id, advanced) is False
        assert _live(store, token, ready.id, trial_id, first_input) is False
        assert _live(store, token, ready.id, trial_id, "e" * 64) is False
        assert _live(store, token, trial_workspace, trial_id, winner) is False
        state = store.get_frontend_policy_state(repository_id=token.repository_id, run_id=token.run_id)
        assert state.stage == "DONE" and state.candidate_hash == winner
        # The durable chain resolves to the winner while the outer is live (the review ran against it).
        assert retained["resolved"] == winner
        assert (ready.path / "result-0.txt").read_text() == "repaired"
        assert _head(ready.path) == ready.base_commit
        with store.read_transaction() as tx:
            journals = [tuple(row) for row in tx.execute(
                "SELECT substr(wave_key,1,instr(wave_key,':')-1),state FROM authority_workspace_integrations "
                "ORDER BY event_id")]
        assert journals == [("gsd-wave", "published"), ("recovery-trial", "published")]
        # Terminal replay: no producer call.
        assert drive_frontend_lifecycle(store, token, supervisor=review_supervisor, controller=controller,
                                        workspace=str(ready.path), parent_activity_id=parent.id,
                                        producers=producers) == "DONE"
        assert calls == {"recover": 1, "review": 1}
        seen.append(winner)
        return 0

    result = prepare_managed_run(objective=env["FFS_OBJECTIVE"], state_root=authority,
        selection_manifest=env["FFS_SELECTION_MANIFEST"], upstream_runtime_manifest=env["FFS_UPSTREAM_RUNTIME_MANIFEST"],
        upstream_runtime_sha256=env["FFS_UPSTREAM_RUNTIME_SHA256"], request_key=env["FFS_REQUEST_KEY"],
        run_id=env["GSD_RUN_ID"], command=("/gsd-execute-phase", "1"), activity="execute", scope="1",
        dispatch_limit=24, token_limit=1000, worker_capacity=2, on_ready=execute)
    assert result == 0 and len(seen) == 1
