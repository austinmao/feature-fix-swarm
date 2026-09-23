"""Hermetic production ingress -> seal -> monitored wave -> bound evidence.

Python child processes and fixture runtime receipts are explicit here; this
does not qualify authenticated hosts or the later frontend review workflow.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import pytest
from dataclasses import replace
import time
from datetime import datetime, timedelta, timezone

requires_local_confinement = pytest.mark.skipif(
    sys.platform != "darwin" or not os.path.isfile("/usr/bin/sandbox-exec"),
    reason="needs Darwin sandbox-exec local-check confinement (non-Darwin fails closed; covered by test_local_check_transport)",
)

from run_state.frontend_completion import post_repair_review_tx
from run_state.managed_admission import ManagedAdmissionQueue
from run_state.resource_observation import ResourceObservation
from run_state.shared_resources import SharedResourceCoordinator, cold_start_demand
from run_state.state import qualified_runtime_tuple_hash
from run_state.supervisor import SupervisorRefused
from run_state.workspace import load_input_snapshot
from test_runtime_receipt_authority import _qualified
from test_frontend_lifecycle import _REVIEW
from run_state.managed import build_frontend_acceptance_draft, prepare_managed_run
from run_state.supervisor import DispatchRequest, Supervisor
from run_state.ownership import OwnershipRefused
from run_state.state import ControlStoreRefused
from test_managed_production_ingress import _setup
from test_supervised_process import _allocate_registered_child, _receipt_bound_request
from run_state.workspace import begin_child_workspace_preparation, prepare_workspace
from run_state.wave_execution import capture_wave_snapshot
from run_state.sealed_review import record_final_review
from run_state.frontend_policy import FrontendPolicyController
from run_state.frontend_lifecycle import LifecycleProducers, drive_frontend_lifecycle
from run_state.wave_candidate import bind_wave_execution_candidate
from test_wave_consumer import record_wave, wave_fixture
from test_wave_execution import git


@requires_local_confinement
def test_production_ingress_seals_before_wave_and_retains_cumulative_accounting(tmp_path, monkeypatch):
    primary, authority, _, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    observed = {}

    def execute(store, token, context):
        store.bind_runtime(token, context.activity_id, "b" * 64)
        child, ready = _allocate_registered_child(store, token, key="slice-outer")
        supervisor = Supervisor(store, token, evidence_root=authority / "slice-evidence")
        request = DispatchRequest(child.id, "slice", (sys.executable, "-c", "pass"),
                                  str(ready.path), ready.base_commit, "b" * 64, contract_hash="d" * 64)
        sealed = None

        def seal(qualified_request):
            nonlocal sealed
            legacy = store.get_acceptance_contract(repository_id=token.repository_id, run_id=token.run_id)
            material = build_frontend_acceptance_draft(
                objective_digest=legacy.material["objective_digest"],
                criteria=[{"id": legacy.accepted_requirement_ids[0],
                           "objective_clause": "write result-0.txt containing done",
                           "checks": [{"id": "result-content", "kind": "command",
                                       "locator": "/usr/bin/grep -qx done result-0.txt"}],
                           "evidence_rules": [{"id": "wave-result", "kind": "patch", "required": True}]}],
                exclusions=[{"id": "other-files", "reason": "only the declared result file may change"}],
                global_invariants=[{"id": "no-commit", "reason": "retain the original HEAD"}],
                requested_runtime_hash=qualified_request.runtime_identity,
                effective_runtime_hash=qualified_request.runtime_identity,
                candidate_hash=ready.input_digest, generation=legacy.generation, command_mode="feature-implement",
            )
            store.create_acceptance_draft(token, draft_id="slice", revision=1,
                                          acceptance_contract_hash=legacy.contract_hash, material=material)
            sealed = store.seal_acceptance_draft(token, draft_id="slice", revision=1,
                                                acceptance_contract_hash=legacy.contract_hash)
            store.initialize_frontend_policy(token, acceptance_hash=sealed.acceptance_hash)
            store.transition_frontend_policy(token, expected_stage='SEALED', new_stage='EXECUTE')

        outer_command = (sys.executable, "-c",
                         "from pathlib import Path; import time\n"
                         "while not Path('result-0.txt').exists(): time.sleep(.02)\n")
        with wave_fixture(tmp_path, monkeypatch, plans=1, owner=(supervisor, store, request),
                          outer_command=outer_command, before_launch=seal) as wave:
            assert sealed is not None
            reply = wave.consumer(wave.event)
            assert reply["results"][0]["status"] == "complete"
            assert (wave.parent / "result-0.txt").read_bytes() == b"done"
            assert git(wave.parent, "rev-parse", "HEAD") == ready.base_commit
            result = supervisor.finish(wave.outer, timeout=15)
            with store.read_transaction() as tx:
                retained = wave.consumer._retained(tx, child.id, f"gsd-wave:{wave.event}:reply")
            evidence = retained["evidence"]
            assert hashlib.sha256(Path(evidence["locator"]).read_bytes()).hexdigest() == evidence["sha256"]
            assert json.loads(Path(evidence["locator"]).read_bytes()) == reply
            snapshot = capture_wave_snapshot(store, token, ready, wave.manifest, authority / "final-input")
            # Fixture bytes use the real adapter's exact no-commit schema;
            # this does not claim an authenticated GSD adapter invocation.
            completion_root = wave.parent / '.planning/.ffs-supervised/waves' / child.id
            completion_root.mkdir(parents=True, mode=0o700)
            prefix = completion_root / ('wave-' + str(wave.manifest['wave']))
            manifest_raw = json.dumps(wave.manifest, sort_keys=True, separators=(',', ':')).encode()
            result_raw = (json.dumps(reply, indent=2) + '\n').encode()
            completion = {'schema':'ffs.gsd-no-commit-completion/v1',
                'manifest_sha256':hashlib.sha256(manifest_raw).hexdigest(),
                'result_sha256':hashlib.sha256(result_raw).hexdigest(),
                'initial_head':ready.base_commit, 'commit_mode':'patches'}
            for suffix, raw in (('.manifest.json', manifest_raw), ('.result.json', result_raw),
                                ('.result.json.receipt.json', (json.dumps(completion) + '\n').encode())):
                path = Path(str(prefix) + suffix)
                path.write_bytes(raw)
                path.chmod(0o600)
            completion_path = Path(str(prefix) + '.result.json.receipt.json')
            completion_ref = {'locator':str(completion_path),
                              'sha256':hashlib.sha256(completion_path.read_bytes()).hexdigest()}
            recorded, advanced = bind_wave_execution_candidate(
                store, token, sealed=sealed, handle=wave.outer, request_key=wave.request.request_key,
                ready=ready, process_evidence=result["evidence"])
            assert bind_wave_execution_candidate(
                store, token, sealed=sealed, handle=wave.outer, request_key=wave.request.request_key,
                ready=ready, process_evidence=result["evidence"])[0].reused
            receipt = json.loads(recorded.receipt.receipt_json)
            assert [item["id"] for item in receipt["evidence"]] == [
                f"wave-result:gsd-wave:{wave.event}", "process-result"]
            for field, wrong in (
                ("request_key", "fabricated-request"), ("activity_id", "fabricated-activity"),
                ("intent_id", "fabricated-intent"), ("fence_generation", token.generation + 1),
                ("workspace_preparation_hash", "0" * 64), ("role", "review"),
                ("process_identity", {**receipt["process_identity"], "start_token": "wrong-start"}),
            ):
                with pytest.raises(OwnershipRefused, match="ACCEPTANCE_RECEIPT_BINDING_INVALID"):
                    store.record_acceptance_receipt(token, acceptance_hash=sealed.acceptance_hash,
                                                     receipt={**receipt, field: wrong})
            budget = store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id)
            assert budget.launch_charged == 2 and budget.active_ns > 0 and not budget.clock_active
            with store.read_transaction() as tx:
                assert tx.execute("SELECT COUNT(*) FROM authority_policy_action_attempts").fetchone()[0] == 2
                assert tx.execute("SELECT COUNT(*) FROM authority_policy_work_intervals WHERE state='active'").fetchone()[0] == 0
                assert tx.execute("SELECT COUNT(*) FROM authority_sealed_acceptances").fetchone()[0] == 1
            observed.update(receipt_hash=recorded.receipt_hash, seal=sealed.acceptance_hash)
            assert advanced.candidate_hash == snapshot.input_digest
            assert recorded.receipt.candidate_hash == ready.input_digest
            assert store.bind_frontend_candidate(token, acceptance_hash=sealed.acceptance_hash,
                candidate_hash=snapshot.input_digest, receipt_hash=recorded.receipt_hash) == advanced
            original_completion = completion_path.read_bytes()
            completion_path.write_text('{}\n')
            with pytest.raises(OwnershipRefused, match='FRONTEND_NO_COMMIT_RECEIPT_INVALID'):
                store.bind_frontend_candidate(token, acceptance_hash=sealed.acceptance_hash,
                    candidate_hash=snapshot.input_digest, receipt_hash=recorded.receipt_hash)
            completion_path.write_bytes(original_completion)
            pending = begin_child_workspace_preparation(
                store, token, parent_activity_id=child.id, request_key="slice-final-review",
                role="reviewer", base_commit=ready.base_commit, selected_input_manifest=snapshot.manifest,
                repository_path=ready.repository_path,
            )
            review_ready = prepare_workspace(store, token, pending, input_snapshot=snapshot)
            review_child = store.create_child_activity(
                token, parent_activity_id=child.id, role="reviewer", request_key="slice-final-review-activity",
                candidate_hash=review_ready.input_digest, contract_hash=sealed.acceptance_hash,
                runtime_identity="b" * 64, workspace_binding=str(review_ready.path),
                workspace_preparation_id=review_ready.id, retry_budget=2,
            )
            script = """from pathlib import Path
import hashlib,json,sys
path=Path('result-0.txt').resolve()
assert path.read_bytes()==b'done'
print(json.dumps({'schema':'ffs.sealed-final-review/v1','acceptance_hash':sys.argv[1],
 'candidate_hash':sys.argv[2],'review_dimensions':['correctness','security','regression'],
 'criteria':{sys.argv[3]:{'status':'passed','evidence':[{'id':'wave-result','locator':str(path),
 'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}]}},'findings':[]}))
"""
            review_request = _receipt_bound_request(store, token, DispatchRequest(
                review_child.id, "slice-final-review-launch",
                (sys.executable, "-c", script, sealed.acceptance_hash, review_ready.input_digest,
                 sealed.material["criteria"][0]["id"]),
                str(review_ready.path), review_ready.base_commit, "b" * 64,
                contract_hash=sealed.acceptance_hash,
            ))
            store.transition_frontend_policy(token, expected_stage='EXECUTE', new_stage='FINAL_REVIEW')
            review_request = supervisor.reserve_request_action(review_request, action="final_review")
            review_handle = supervisor.launch(review_request)
            supervisor.finish(review_handle, timeout=15)
            final = record_final_review(supervisor, review_handle, acceptance_hash=sealed.acceptance_hash)
            assert final.receipt.role == "review" and final.receipt.candidate_hash == snapshot.input_digest
            assert final.receipt.candidate_hash != sealed.material["candidate_hash"]
            assert record_final_review(supervisor, review_handle, acceptance_hash=sealed.acceptance_hash).reused
            incomplete = json.loads(final.receipt.receipt_json)
            incomplete["review_dimensions"] = ["correctness"]
            with pytest.raises(ControlStoreRefused, match="POLICY_REVIEW_DIMENSIONS_MISSING"):
                store.record_acceptance_receipt(token, acceptance_hash=sealed.acceptance_hash, receipt=incomplete)
            assert store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id).launch_charged == 3
            # A review receipt proves the inspected input, not execution or
            # journaled integration producing a new frontend candidate.
            with pytest.raises(OwnershipRefused, match='FRONTEND_CANDIDATE_RECEIPT_STALE'):
                store.bind_frontend_candidate(
                    token, acceptance_hash=sealed.acceptance_hash, candidate_hash=snapshot.input_digest,
                    receipt_hash=final.receipt_hash,
                )
            result_path = wave.parent / "result-0.txt"
            with pytest.raises(OwnershipRefused, match="FRONTEND_CHECK_EXECUTION_REQUIRED"):
                store.record_frontend_check_results(
                    token, acceptance_hash=sealed.acceptance_hash, candidate_hash=snapshot.input_digest,
                    results=[{"check_id": "result-content", "status": "passed", "evidence": [{
                        "locator": str(result_path),
                        "sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
                    }]}],
                )
            local_supervisor = Supervisor(store, token, evidence_root=authority / 'local-check-evidence',
                shared_resource_coordinator=supervisor.shared_resource_coordinator,
                resource_demand_policy=supervisor.resource_demand_policy)
            checks = FrontendPolicyController(store, token, command_mode='feature-implement').run_mapped_checks(
                workspace=str(wave.parent), supervisor=local_supervisor, parent_activity_id=child.id)
            assert checks['result-content']['status'] == 'passed'
            pending_child, _pending_ready = _allocate_registered_child(store, token, key='unresolved-obligation')
            store.transition_activity(token, review_child.id, expected="active", new="succeeded",
                                      result=review_handle.result["evidence"])
            store.transition_activity(token, child.id, expected="active", new="succeeded", result=result["evidence"])
            with pytest.raises(OwnershipRefused, match='FRONTEND_COMPLETION_OBLIGATIONS_REMAIN'):
                store.transition_frontend_policy(token, expected_stage='FINAL_REVIEW', new_stage='DONE')
            store.transition_activity(token, pending_child.id, expected='pending', new='aborted', reason='fixture obligation resolved')
            check_path = Path(checks['result-content']['evidence'][0]['locator'])
            check_raw = check_path.read_bytes()
            check_path.write_bytes(check_raw + b'\n')
            with pytest.raises(OwnershipRefused, match='EVIDENCE_INVALID'):
                store.transition_frontend_policy(token, expected_stage='FINAL_REVIEW', new_stage='DONE')
            check_path.write_bytes(check_raw)
            completion_path = Path(completion_ref['locator'])
            completion_raw = completion_path.read_bytes()
            completion_path.write_bytes(completion_raw + b'\n')
            with pytest.raises(OwnershipRefused, match='FRONTEND_INTEGRATION_CANDIDATE_INVALID'):
                store.transition_frontend_policy(token, expected_stage='FINAL_REVIEW', new_stage='DONE')
            completion_path.write_bytes(completion_raw)
            done = store.transition_frontend_policy(token, expected_stage='FINAL_REVIEW', new_stage='DONE')
            assert done.stage == 'DONE'
            with store.read_transaction() as tx:
                assert tx.execute('SELECT state FROM authority_activities WHERE id=?', (context.activity_id,)).fetchone()[0] == 'succeeded'
            assert store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id).launch_charged == 4
            observed["final_receipt_hash"] = final.receipt_hash
        return 0

    assert prepare_managed_run(
        objective="write result-0.txt containing done", state_root=authority,
        selection_manifest=env["FFS_SELECTION_MANIFEST"],
        upstream_runtime_manifest=env["FFS_UPSTREAM_RUNTIME_MANIFEST"],
        upstream_runtime_sha256=env["FFS_UPSTREAM_RUNTIME_SHA256"],
        request_key="policy-production-slice", command=("/gsd-execute-phase", "1"),
        dispatch_limit=12, token_limit=100, worker_capacity=2, on_ready=execute,
        run_id="policy-production-slice", activity="execute", scope="1",
    ) == 0
    assert observed["receipt_hash"] != observed["seal"]
    assert observed["final_receipt_hash"] != observed["receipt_hash"]


@requires_local_confinement
def test_failed_review_is_found_across_two_wave_bound_candidates(tmp_path, monkeypatch):
    _wave_descendant_proof(tmp_path, monkeypatch)


@requires_local_confinement
def test_repair_child_integrates_patch_and_replays_without_new_debit(tmp_path, monkeypatch):
    _wave_descendant_proof(tmp_path, monkeypatch, repair=True)


@requires_local_confinement
def test_lifecycle_resolves_repair_child_candidate_despite_stale_caller_context(tmp_path, monkeypatch):
    _wave_descendant_proof(tmp_path, monkeypatch, repair=True, resolve=True)


def _wave_descendant_proof(tmp_path, monkeypatch, *, repair=False, resolve=False):
    """Scripted Python PIDs, fixture resources and adapter bytes; no native qualification."""
    primary, authority, _, env = _setup(tmp_path)
    monkeypatch.chdir(primary)

    def execute(store, token, context):
        store.bind_runtime(token, context.activity_id, 'b' * 64)
        parent, ready = _allocate_registered_child(store, token, key='ancestor-parent')
        stale_workspace, stale_parent_id = str(ready.path), parent.id
        store.transition_activity(token, parent.id, expected='pending', new='active', reason='fixture wave parent')
        runtime = qualified_runtime_tuple_hash(_qualified(Path(ready.path)))
        legacy = store.get_acceptance_contract(repository_id=token.repository_id, run_id=token.run_id)
        if resolve:
            # Acceptance revisions and native owner fences are separate counters.
            grant = store.create_grant(token, action='acceptance-amend', target=legacy.contract_hash,
                provenance={'operator': 'fixture'}, idempotency_key='fixture-acceptance-counter',
                expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ'))
            legacy = store.amend_acceptance_contract(token, amendment_id='fixture-acceptance-counter',
                amendment={'reason': 'fixture distinct acceptance counter'}, grant_id=grant.id)
            assert legacy.generation != token.generation
        criterion = legacy.accepted_requirement_ids[0]
        controller = FrontendPolicyController(store, token, command_mode='feature-implement')
        frozen = controller.freeze(draft_id='ancestor-seal', revision=1,
            material=build_frontend_acceptance_draft(
                objective_digest=legacy.material['objective_digest'],
                criteria=[{'id': criterion, 'objective_clause': 'produce the repaired fixture result',
                    'checks': [{'id': 'repaired', 'kind': 'command',
                                'locator': '/usr/bin/grep -qx repaired result-0.txt'}],
                    'evidence_rules': [{'id': 'check-process', 'kind': 'test', 'required': True}]}],
                exclusions=[{'id': 'other-source', 'reason': 'fixture scope'}],
                global_invariants=[{'id': 'no-commit', 'reason': 'retain HEAD'}],
                requested_runtime_hash=runtime, effective_runtime_hash=runtime,
                candidate_hash=ready.input_digest, generation=legacy.generation,
                command_mode='feature-implement'))
        store.transition_frontend_policy(token, expected_stage='SEALED', new_stage='EXECUTE')
        if resolve:
            from run_state.candidate_chain import resolve_current_frontend_candidate
            assert resolve_current_frontend_candidate(store, token) is None
        queue = ManagedAdmissionQueue(tmp_path / 'ancestor-resources', observation_provider=lambda:
            ResourceObservation(time.monotonic_ns(), 4, 4 << 30, 4 << 30, 100, 100, {}, 'fixture'))

        def supervisor():
            return Supervisor(store, token, evidence_root=authority / 'ancestor-evidence',
                shared_resource_coordinator=SharedResourceCoordinator(store, token, queue=queue),
                resource_demand_policy=cold_start_demand)

        review_supervisor = supervisor()
        snapshot = load_input_snapshot(store, ready)
        pending = begin_child_workspace_preparation(store, token, parent_activity_id=parent.id,
            request_key='ancestor-review', role='reviewer', base_commit=ready.base_commit,
            selected_input_manifest=snapshot.manifest, repository_path=ready.repository_path)
        review_ready = prepare_workspace(store, token, pending, input_snapshot=snapshot)
        reviewer = store.create_child_activity(token, parent_activity_id=parent.id, role='reviewer',
            request_key='ancestor-review-activity', candidate_hash=review_ready.input_digest,
            contract_hash=frozen.acceptance_hash, runtime_identity=runtime,
            workspace_binding=str(review_ready.path), workspace_preparation_id=review_ready.id, retry_budget=1)
        review_request = _receipt_bound_request(store, token, DispatchRequest(reviewer.id, 'ancestor-review-launch',
            (sys.executable, '-c', _REVIEW, frozen.acceptance_hash, ready.input_digest, criterion, 'failed'),
            str(review_ready.path), ready.base_commit, 'b' * 64, contract_hash=frozen.acceptance_hash))
        review_handle = review_supervisor.launch(controller.reserve_stage(
            review_supervisor, review_request, action='final_review'))
        review_supervisor.finish(review_handle, timeout=30, token_usage=0)
        with pytest.raises(SupervisorRefused, match='FINAL_REVIEW_CRITERION_FAILED'):
            record_final_review(review_supervisor, review_handle, acceptance_hash=frozen.acceptance_hash)
        store.transition_activity(token, reviewer.id, expected='active', new='succeeded',
                                  result=review_handle.result['evidence'])
        with store.read_transaction() as tx:
            assert post_repair_review_tx(store, tx, token, frozen.acceptance_hash, ready.input_digest) is None
            failed_review_hash = tx.execute(
                "SELECT receipt_hash FROM authority_acceptance_receipts WHERE json_extract(receipt_json, '$.intent_id')=?",
                (review_handle.intent_id,)).fetchone()[0]
        candidates = [ready.input_digest]
        parents = []
        for index, content in enumerate(('done', 'repaired')):
            current_supervisor = supervisor()
            if index:
                if repair:
                    before_checks = controller.run_mapped_checks(
                        workspace=str(ready.path), supervisor=supervisor(), parent_activity_id=parent.id)
                    assert before_checks['repaired']['status'] == 'failed'
                pending = begin_child_workspace_preparation(store, token, parent_activity_id=parent.id,
                    request_key='repair-parent-workspace', role='worker', base_commit=ready.base_commit,
                    selected_input_manifest=snapshot.manifest, repository_path=ready.repository_path)
                ready = prepare_workspace(store, token, pending, input_snapshot=snapshot)
                parent = store.create_child_activity(token, parent_activity_id=parent.id, role='worker',
                    request_key='repair-parent', candidate_hash=ready.input_digest, contract_hash=frozen.acceptance_hash,
                    runtime_identity=runtime, workspace_binding=str(ready.path),
                    workspace_preparation_id=ready.id, retry_budget=1)
            request = DispatchRequest(parent.id, f'ancestor-wave-{index}', (sys.executable, '-c', 'pass'),
                str(ready.path), ready.base_commit, 'b' * 64,
                contract_hash=frozen.acceptance_hash if index else 'd' * 64)
            outer_command = (sys.executable, '-c', "from pathlib import Path; import time\n"
                f"while not Path('result-0.txt').exists() or Path('result-0.txt').read_text() != {content!r}: time.sleep(.02)\n")

            command = (sys.executable, '-c', f"from pathlib import Path; Path('result-0.txt').write_text({content!r})")
            def launch_outer(bound):
                if repair and index:
                    return current_supervisor.launch(controller.reserve_stage(
                        current_supervisor, replace(bound, monitor_result=True), action='repair'))
                return current_supervisor.launch(current_supervisor.reserve_request_action(
                    replace(bound, monitor_result=True), action='execute'))

            with wave_fixture(tmp_path, monkeypatch, plans=1, commands=[command], wave=index + 1,
                    owner=(current_supervisor, store, request), outer_command=outer_command,
                    request_key=f'ancestor-wave-{index}', launch_outer=launch_outer) as wave:
                reply = wave.consumer(wave.event)
                assert reply['results'][0]['status'] == 'complete'
                assert (wave.prepared[0].preparation.path / 'result-0.txt').read_text() == content
                if repair and index:
                    assert wave.outer.monitored
                    with store.read_transaction() as tx:
                        actions = tx.execute('SELECT a.action,a.logical_key,p.intent_id FROM authority_policy_actions a '
                            'JOIN authority_policy_action_attempts p ON p.action_id=a.id WHERE a.action=?',
                            ('repair',)).fetchall()
                    assert [tuple(row) for row in actions] == [('repair', wave.request.request_key, wave.outer.intent_id)]
                root = wave.parent / '.planning/.ffs-supervised/waves' / parent.id
                root.mkdir(parents=True, mode=0o700)
                prefix = root / f'wave-{index + 1}'
                manifest_raw = json.dumps(wave.manifest, sort_keys=True, separators=(',', ':')).encode()
                result_raw = (json.dumps(reply, indent=2) + '\n').encode()
                completion = {'schema': 'ffs.gsd-no-commit-completion/v1',
                    'manifest_sha256': hashlib.sha256(manifest_raw).hexdigest(),
                    'result_sha256': hashlib.sha256(result_raw).hexdigest(),
                    'initial_head': ready.base_commit, 'commit_mode': 'patches'}
                for suffix, raw in (('.manifest.json', manifest_raw), ('.result.json', result_raw),
                                    ('.result.json.receipt.json', (json.dumps(completion) + '\n').encode())):
                    path = Path(str(prefix) + suffix)
                    path.write_bytes(raw)
                    path.chmod(0o600)
                result = current_supervisor.finish(wave.outer, timeout=30)
                sealed = store.get_sealed_acceptance(repository_id=token.repository_id, run_id=token.run_id)
                recorded, state = bind_wave_execution_candidate(store, token, sealed=sealed, handle=wave.outer,
                    request_key=wave.request.request_key, ready=ready, process_evidence=result['evidence'])
                assert recorded.receipt.runtime_hash == wave.request.runtime_identity
                if index:
                    assert recorded.receipt.runtime_hash != sealed.material['runtime']['effective_hash']
                if repair and index:
                    assert recorded.receipt.role == 'execution'
                    budget = store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id)
                    def counts():
                        with store.read_transaction() as tx:
                            return tuple(tx.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in (
                                'authority_policy_action_attempts', 'authority_launch_intents', 'control_events'))
                    before = counts()
                    replay, rebound = bind_wave_execution_candidate(
                        store, token, sealed=sealed, handle=wave.outer, request_key=wave.request.request_key,
                        ready=ready, process_evidence=result['evidence'])
                    assert replay.reused and replay.receipt_hash == recorded.receipt_hash and rebound == state
                    assert counts() == before
                    assert store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id) == budget
                snapshot = capture_wave_snapshot(store, token, ready, wave.manifest, authority / f'ancestor-input-{index}')
                assert state.candidate_hash == snapshot.input_digest != candidates[-1]
                candidates.append(state.candidate_hash)
                assert (wave.parent / 'result-0.txt').read_text() == content
                assert git(wave.parent, 'rev-parse', 'HEAD') == ready.base_commit
                with store.read_transaction() as tx:
                    row = tx.execute('SELECT parent_candidate_hash,receipt_hash FROM authority_frontend_policy_candidates '
                        'WHERE candidate_hash=?', (state.candidate_hash,)).fetchone()
                    assert tuple(row) == (candidates[-2], recorded.receipt_hash)
                parents.append((parent.id, result['evidence']))
        with store.read_transaction() as tx:
            proof = post_repair_review_tx(store, tx, token, frozen.acceptance_hash, candidates[-1])
            assert proof is not None
            assert proof[0] == failed_review_hash
            assert proof[1].candidate_hash == candidates[0] and proof[1].completion_status == 'failed'
            assert post_repair_review_tx(store, tx, token, frozen.acceptance_hash, candidates[0]) is None
            assert post_repair_review_tx(store, tx, token, frozen.acceptance_hash, 'e' * 64) is None
        # Corrupt only fixture ancestry inside a rolled-back savepoint: cycles
        # terminate and cannot make the current candidate its own ancestor.
        with store.transaction() as tx:
            tx.execute('SAVEPOINT fixture_cycle')
            for target in (candidates[1], candidates[2]):
                tx.execute('UPDATE authority_frontend_policy_candidates SET parent_candidate_hash=? '
                           'WHERE candidate_hash=?', (candidates[2], target))
                assert post_repair_review_tx(store, tx, token, frozen.acceptance_hash, candidates[2]) is None
            tx.execute('ROLLBACK TO fixture_cycle')
            tx.execute('RELEASE fixture_cycle')
        if resolve:
            from run_state.candidate_chain import resolve_current_frontend_candidate
            # The final repair parent/worktree differs from the caller's
            # original execute context.  The durable chain chooses it.
            current = resolve_current_frontend_candidate(store, token)
            assert current is not None
            assert (current.candidate_hash, current.workspace, current.parent_activity_id) == (
                candidates[-1], str(ready.path), parent.id)
            assert current.workspace != stale_workspace and current.parent_activity_id != stale_parent_id
            # Fixture corruption only: immutable candidate provenance and the
            # selected live authority must all remain coherent.
            budget_before = store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id)
            with store.read_transaction() as tx:
                event_bytes = [tuple(row) for row in tx.execute('SELECT id,payload FROM control_events ORDER BY id')]
            mutations = [
                ('authority_child_bindings', 'runtime_identity', 'activity_id', parent.id, '0' * 64),
                ('authority_child_bindings', 'candidate_hash', 'activity_id', parent.id, '0' * 64),
                ('authority_child_bindings', 'parent_activity_id', 'activity_id', parent.id, parent.id),
                ('authority_activities', 'state', 'id', parent.id, 'succeeded'),
                ('authority_activities', 'generation', 'id', parent.id, token.generation + 1),
                ('context_workspaces', 'created_by_ffs', 'preparation_id', ready.id, 0),
                ('context_workspaces', 'base_commit', 'preparation_id', ready.id, '0' * 40),
                ('authority_frontend_policy_candidates', 'receipt_hash', 'candidate_hash', current.candidate_hash, '0' * 64),
            ]
            for table, column, identity, value, broken in mutations:
                with store.transaction() as tx:
                    original = tx.execute(f'SELECT {column} FROM {table} WHERE {identity}=?', (value,)).fetchone()[0]
                    tx.execute(f'UPDATE {table} SET {column}=? WHERE {identity}=?', (broken, value))
                try:
                    with pytest.raises(OwnershipRefused):
                        resolve_current_frontend_candidate(store, token)
                finally:
                    with store.transaction() as tx:
                        tx.execute(f'UPDATE {table} SET {column}=? WHERE {identity}=?', (original, value))
            assert resolve_current_frontend_candidate(store, token) == current
            # A mutation after the physical proof is seen by the final authority
            # transaction, before the selected context can escape to a caller.
            import run_state.candidate_chain as candidate_chain
            verify = candidate_chain.verify_candidate_chain
            for table, column, identity, value, broken in (mutations[0],
                    ('authority_frontend_policy_states', 'candidate_hash', 'run_id', token.run_id, '0' * 64)):
                with store.read_transaction() as tx:
                    original = tx.execute(f'SELECT {column} FROM {table} WHERE {identity}=?', (value,)).fetchone()[0]
                def mutate_after_proof(*args, **kwargs):
                    proof = verify(*args, **kwargs)
                    with store.transaction() as tx:
                        tx.execute(f'UPDATE {table} SET {column}=? WHERE {identity}=?', (broken, value))
                    return proof
                try:
                    with monkeypatch.context() as changed:
                        changed.setattr(candidate_chain, 'verify_candidate_chain', mutate_after_proof)
                        with pytest.raises(OwnershipRefused):
                            resolve_current_frontend_candidate(store, token)
                finally:
                    with store.transaction() as tx:
                        tx.execute(f'UPDATE {table} SET {column}=? WHERE {identity}=?', (original, value))
            assert resolve_current_frontend_candidate(store, token) == current
            assert store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id) == budget_before
            with store.read_transaction() as tx:
                assert [tuple(row) for row in tx.execute('SELECT id,payload FROM control_events ORDER BY id')] == event_bytes
            observed = []
            mapped = controller.run_mapped_checks
            def record_mapped_checks(*, workspace, supervisor, parent_activity_id):
                observed.append((workspace, parent_activity_id))
                return mapped(workspace=workspace, supervisor=supervisor, parent_activity_id=parent_activity_id)
            controller.run_mapped_checks = record_mapped_checks
            def action_counts():
                with store.read_transaction() as tx:
                    return tuple(tx.execute('SELECT count(*) FROM authority_policy_actions WHERE action=?',
                                            (action,)).fetchone()[0]
                                 for action in ('execute', 'repair', 'final_review'))
            before = action_counts()
            def settle():
                for activity_id, evidence in reversed(parents):
                    activity = store.get_activity(activity_id)
                    if activity.state == 'active':
                        store.transition_activity(token, activity_id, expected='active', new='succeeded', result=evidence)
            stage = drive_frontend_lifecycle(
                store, token, supervisor=supervisor(), controller=controller,
                workspace=stale_workspace, parent_activity_id=stale_parent_id,
                producers=LifecycleProducers(
                    execute=lambda _frozen: pytest.fail('stale lifecycle reran execute'),
                    final_review=lambda _frozen: pytest.fail('stale lifecycle reran final review'),
                    recover=lambda _packet: pytest.fail('unexpected recovery'),
                    repair=lambda *_args: pytest.fail('stale lifecycle reran repair'), settle=settle),
            )
            assert stage == 'DONE'
            assert observed and all(item == (str(ready.path), parent.id) for item in observed)
            assert before == action_counts()
            return 0
        checks = controller.run_mapped_checks(workspace=str(ready.path), supervisor=supervisor(), parent_activity_id=parent.id)
        assert checks['repaired']['status'] == 'passed'
        if repair:
            with store.read_transaction() as tx:
                checks_by_candidate = dict(tx.execute(
                    "SELECT candidate_hash,status FROM authority_frontend_policy_checks WHERE check_id='repaired'"))
                assert tx.execute("SELECT COUNT(*) FROM authority_policy_actions WHERE action='repair'").fetchone()[0] == 1
            assert checks_by_candidate == {candidates[1]: 'failed', candidates[2]: 'passed'}
        for activity_id, evidence in reversed(parents):
            store.transition_activity(token, activity_id, expected='active', new='succeeded', result=evidence)
        store.transition_frontend_policy(token, expected_stage='EXECUTE', new_stage='FINAL_REVIEW')
        assert store.transition_frontend_policy(token, expected_stage='FINAL_REVIEW', new_stage='DONE').stage == 'DONE'
        return 0

    assert prepare_managed_run(objective='produce the repaired fixture result', state_root=authority,
        selection_manifest=env['FFS_SELECTION_MANIFEST'], upstream_runtime_manifest=env['FFS_UPSTREAM_RUNTIME_MANIFEST'],
        upstream_runtime_sha256=env['FFS_UPSTREAM_RUNTIME_SHA256'], request_key='ancestor-fixture',
        command=('/gsd-execute-phase', '1'), dispatch_limit=16, token_limit=1000, worker_capacity=2,
        on_ready=execute, run_id='ancestor-fixture', activity='execute', scope='1') == 0


def test_execution_receipt_binds_ordered_multi_wave_candidate_without_replay_debit(tmp_path, monkeypatch):
    """Two scripted fixture waves share one supervised outer execution intent.

    The PIDs and resource observation below belong only to fixture Python
    children. This proves the journal/adapter binding seam, not a native host
    or model invocation.
    """
    primary, authority, _, env = _setup(tmp_path)
    monkeypatch.chdir(primary)

    def execute(store, token, context):
        store.bind_runtime(token, context.activity_id, "b" * 64)
        child, ready = _allocate_registered_child(store, token, key="multi-wave-outer")
        supervisor = Supervisor(store, token, evidence_root=authority / "multi-wave-evidence")
        request = DispatchRequest(child.id, "multi-wave", (sys.executable, "-c", "pass"),
                                  str(ready.path), ready.base_commit, "b" * 64, contract_hash="d" * 64)
        sealed = None

        def seal(qualified_request):
            nonlocal sealed
            legacy = store.get_acceptance_contract(repository_id=token.repository_id, run_id=token.run_id)
            material = build_frontend_acceptance_draft(
                objective_digest=legacy.material["objective_digest"],
                criteria=[{"id": legacy.accepted_requirement_ids[0],
                           "objective_clause": "write both sequential fixture results",
                           "checks": [{"id": f"result-{i}", "kind": "command",
                                       "locator": f"/usr/bin/grep -qx done result-{i}.txt"} for i in range(2)],
                           "evidence_rules": [{"id": "wave-results", "kind": "patch", "required": True}]}],
                exclusions=[{"id": "other-files", "reason": "fixture scope"}],
                global_invariants=[{"id": "no-commit", "reason": "fixture retains HEAD"}],
                requested_runtime_hash=qualified_request.runtime_identity,
                effective_runtime_hash=qualified_request.runtime_identity,
                candidate_hash=ready.input_digest, generation=legacy.generation,
                command_mode="feature-implement",
            )
            store.create_acceptance_draft(token, draft_id="multi-wave", revision=1,
                                          acceptance_contract_hash=legacy.contract_hash, material=material)
            sealed = store.seal_acceptance_draft(token, draft_id="multi-wave", revision=1,
                                                acceptance_contract_hash=legacy.contract_hash)
            store.initialize_frontend_policy(token, acceptance_hash=sealed.acceptance_hash)
            store.transition_frontend_policy(token, expected_stage="SEALED", new_stage="EXECUTE")

        def adapter_completion(manifest, reply):
            root = wave.parent / ".planning/.ffs-supervised/waves" / child.id
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            prefix = root / ("wave-" + str(manifest["wave"]))
            manifest_raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            result_raw = (json.dumps(reply, indent=2) + "\n").encode()
            completion = {"schema": "ffs.gsd-no-commit-completion/v1",
                          "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
                          "result_sha256": hashlib.sha256(result_raw).hexdigest(),
                          "initial_head": ready.base_commit, "commit_mode": "patches"}
            for suffix, raw in ((".manifest.json", manifest_raw), (".result.json", result_raw),
                                (".result.json.receipt.json", (json.dumps(completion) + "\n").encode())):
                path = Path(str(prefix) + suffix)
                path.write_bytes(raw)
                path.chmod(0o600)
            return Path(str(prefix) + ".result.json")

        outer_command = (sys.executable, "-c",
                         "from pathlib import Path; import time\n"
                         "while not all(Path('result-%d.txt' % i).exists() for i in range(2)): time.sleep(.02)\n")
        with wave_fixture(tmp_path, monkeypatch, plans=1, wave=1, owner=(supervisor, store, request),
                          outer_command=outer_command, before_launch=seal) as wave:
            first = wave.consumer(wave.event)
            second_manifest = json.loads(json.dumps(wave.manifest))
            second_manifest["wave"] = 2
            second_manifest["plans"][0].update(id="second-wave", files_modified=["result-1.txt"],
                                                 depends_on=["plan-0"], prompt_nonce="second-wave-fixture")
            second_event = record_wave(wave, second_manifest, "gsd-wave:multi-wave-second")
            second = wave.consumer(second_event)
            assert [first["wave"], second["wave"]] == [1, 2]
            assert all(reply["results"][0]["status"] == "complete" for reply in (first, second))
            assert (wave.parent / "result-0.txt").read_text() == "done"
            assert (wave.parent / "result-1.txt").read_text() == "done"
            assert (wave.prepared[1].preparation.path / "result-0.txt").read_text() == "done"
            assert git(wave.parent, "rev-parse", "HEAD") == ready.base_commit
            first_result = adapter_completion(wave.manifest, first)
            adapter_completion(second_manifest, second)
            result = supervisor.finish(wave.outer, timeout=15)
            snapshot = capture_wave_snapshot(store, token, ready, second_manifest, authority / "multi-wave-input")
            assert [entry["path"] for entry in snapshot.manifest["entries"]] == ["result-0.txt", "result-1.txt"]
            recorded, bound = bind_wave_execution_candidate(
                store, token, sealed=sealed, handle=wave.outer, request_key=wave.request.request_key,
                ready=ready, process_evidence=result["evidence"])
            receipt = json.loads(recorded.receipt.receipt_json)
            assert receipt["candidate_hash"] == ready.input_digest
            assert [item["id"] for item in receipt["evidence"]] == [
                f"wave-result:gsd-wave:{wave.event}", f"wave-result:gsd-wave:{second_event}", "process-result",
            ]
            assert bound.candidate_hash == snapshot.input_digest
            with store.read_transaction() as tx:
                journals = tx.execute(
                    "SELECT wave_key,contract_sha256 FROM authority_workspace_integrations "
                    "WHERE issuing_intent_id=? ORDER BY event_id", (wave.outer.intent_id,)).fetchall()
                outputs = [json.loads(tx.execute(
                    "SELECT e.payload FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id "
                    "WHERE k.activity_id=? AND k.idempotency_key=?",
                    (wave.outer.activity_id, key + ":candidate-output")).fetchone()[0])["data"]
                    for key, _contract in journals]
                integration = json.loads(tx.execute(
                    "SELECT e.payload FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id "
                    "WHERE k.idempotency_key=?", ("frontend-integration:" + recorded.receipt_hash,)).fetchone()[0])["data"]
                candidate = tx.execute(
                    "SELECT candidate_hash,parent_candidate_hash,receipt_hash FROM authority_frontend_policy_candidates "
                    "WHERE candidate_hash=?", (snapshot.input_digest,)).fetchone()
                before = (tx.execute("SELECT COUNT(*) FROM authority_policy_action_attempts").fetchone()[0],
                          tx.execute("SELECT COUNT(*) FROM authority_launch_intents").fetchone()[0],
                          tx.execute("SELECT COUNT(*) FROM control_events").fetchone()[0])
            assert [key for key, _contract in journals] == [f"gsd-wave:{wave.event}", f"gsd-wave:{second_event}"]
            assert outputs[0]["input_digest"] == ready.input_digest
            assert outputs[1]["input_digest"] == outputs[0]["output_digest"]
            assert outputs[1]["output_digest"] == snapshot.input_digest
            assert integration["candidate_chain"]["input_candidate_hash"] == ready.input_digest
            assert integration["candidate_chain"]["candidate_hash"] == snapshot.input_digest
            assert integration["candidate_chain"]["journal_hashes"] == [contract for _key, contract in journals]
            assert tuple(candidate) == (snapshot.input_digest, ready.input_digest, recorded.receipt_hash)
            before_budget = store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id)
            assert before_budget.launch_charged == 3
            replayed, rebound = bind_wave_execution_candidate(
                store, token, sealed=sealed, handle=wave.outer, request_key=wave.request.request_key,
                ready=ready, process_evidence=result["evidence"])
            assert replayed.reused and replayed.receipt_hash == recorded.receipt_hash
            assert rebound == bound
            assert store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id) == before_budget
            with store.read_transaction() as tx:
                assert (tx.execute("SELECT COUNT(*) FROM authority_policy_action_attempts").fetchone()[0],
                        tx.execute("SELECT COUNT(*) FROM authority_launch_intents").fetchone()[0],
                        tx.execute("SELECT COUNT(*) FROM control_events").fetchone()[0]) == before
            original_first_result = first_result.read_bytes()
            first_result.write_bytes(b"{}\n")
            with pytest.raises(OwnershipRefused, match="FRONTEND_NO_COMMIT_RECEIPT_INVALID"):
                bind_wave_execution_candidate(
                    store, token, sealed=sealed, handle=wave.outer, request_key=wave.request.request_key,
                    ready=ready, process_evidence=result["evidence"])
            first_result.write_bytes(original_first_result)
        return 0

    assert prepare_managed_run(
        objective="write both sequential fixture results", state_root=authority,
        selection_manifest=env["FFS_SELECTION_MANIFEST"],
        upstream_runtime_manifest=env["FFS_UPSTREAM_RUNTIME_MANIFEST"],
        upstream_runtime_sha256=env["FFS_UPSTREAM_RUNTIME_SHA256"],
        request_key="policy-production-multi-wave", command=("/gsd-execute-phase", "14"),
        dispatch_limit=12, token_limit=100, worker_capacity=2, on_ready=execute,
        run_id="policy-production-multi-wave", activity="execute", scope="14",
    ) == 0


@pytest.mark.parametrize(('terminal_stage', 'root_state'), [('CANCELLED', 'aborted'), ('CAPABILITY_FAILURE', 'failed')])
def test_frontend_terminal_routes_revoke_live_authority_without_refund(tmp_path, monkeypatch, terminal_stage, root_state):
    from run_state.supervisor import _publish
    primary, authority, _, env = _setup(tmp_path)
    monkeypatch.chdir(primary)

    def execute(store, token, context):
        store.bind_runtime(token, context.activity_id, 'b' * 64)
        child, ready = _allocate_registered_child(store, token, key='terminal-child')
        supervisor = Supervisor(store, token, evidence_root=authority / 'terminal-evidence')
        import time
        from run_state.managed_admission import ManagedAdmissionQueue
        from run_state.resource_observation import ResourceObservation
        from run_state.shared_resources import SharedResourceCoordinator, cold_start_demand
        queue = ManagedAdmissionQueue(tmp_path / 'terminal-shared-resources', observation_provider=lambda:
            ResourceObservation(time.monotonic_ns(), 64, 64 << 30, 64 << 30, 1000, 1000, {}, 'scripted-fixture'))
        supervisor.shared_resource_coordinator = SharedResourceCoordinator(store, token, queue=queue)
        supervisor.resource_demand_policy = cold_start_demand
        request = _receipt_bound_request(store, token, DispatchRequest(
            child.id, 'terminal-launch', (sys.executable, '-c', 'import time; time.sleep(60)'),
            str(ready.path), ready.base_commit, 'b' * 64, contract_hash='d' * 64,
            token_reservation=10))
        legacy = store.get_acceptance_contract(repository_id=token.repository_id, run_id=token.run_id)
        material = build_frontend_acceptance_draft(
            objective_digest=legacy.material['objective_digest'],
            criteria=[{'id':legacy.accepted_requirement_ids[0], 'objective_clause':'terminal fixture',
                'checks':[{'id':'terminal-check', 'kind':'command', 'locator':'/usr/bin/true'}],
                'evidence_rules':[{'id':'terminal-result', 'kind':'log', 'required':True}]}],
            exclusions=[{'id':'other-work', 'reason':'fixture'}],
            global_invariants=[{'id':'no-commit', 'reason':'fixture'}],
            requested_runtime_hash=request.runtime_identity, effective_runtime_hash=request.runtime_identity,
            candidate_hash=ready.input_digest, generation=legacy.generation, command_mode='feature-implement')
        store.create_acceptance_draft(token, draft_id='terminal', revision=1,
            acceptance_contract_hash=legacy.contract_hash, material=material)
        sealed = store.seal_acceptance_draft(token, draft_id='terminal', revision=1,
            acceptance_contract_hash=legacy.contract_hash)
        store.initialize_frontend_policy(token, acceptance_hash=sealed.acceptance_hash)
        store.transition_frontend_policy(token, expected_stage='SEALED', new_stage='EXECUTE')
        request = supervisor.reserve_request_action(request, action='execute')
        handle = supervisor.launch(request)
        try:
            before = store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id)
            store.transition_frontend_policy(token, expected_stage='EXECUTE', new_stage=terminal_stage)
            assert store.get_activity(context.activity_id).state == root_state
            with store.read_transaction() as tx:
                intent = tx.execute('SELECT state,permit_id,child_pid FROM authority_launch_intents WHERE id=?',
                    (handle.intent_id,)).fetchone()
                assert tuple(intent) == ('reconcile_required', None, handle.identity.pid)
            after = store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id)
            assert after.launch_charged == before.launch_charged == 1
            handle.process.terminate()
            handle.process.wait(timeout=10)
            evidence = _publish(authority / 'terminal-evidence', 'late.json', {'fixture':'late-success'})
            with pytest.raises(OwnershipRefused, match='CHILD_NOT_AUTHORIZED'):
                store.complete_launch(handle.intent_id, token, status='succeeded', evidence=evidence, token_usage=0)
        finally:
            if handle.process.poll() is None:
                handle.process.terminate()
            handle.process.wait(timeout=10)
        return 0

    assert prepare_managed_run(objective='terminal fixture', state_root=authority,
        selection_manifest=env['FFS_SELECTION_MANIFEST'], upstream_runtime_manifest=env['FFS_UPSTREAM_RUNTIME_MANIFEST'],
        upstream_runtime_sha256=env['FFS_UPSTREAM_RUNTIME_SHA256'], request_key='frontend-terminal',
        command=('/gsd-execute-phase', '1'), dispatch_limit=12, token_limit=100, worker_capacity=2,
        on_ready=execute, run_id='frontend-terminal', activity='execute', scope='1') == 0
