"""F51b: a resume never rewrites the workspace row a sealed execution receipt hashed.

Live M5 (``e2e-m5c-phase02``): the outer orchestrator ran a supervised GSD wave,
so ``bind_wave_execution_candidate`` recorded its execution receipt, which hashes
the outer workspace's ``context_workspaces`` row.  ``frontend-start`` died at
FINAL_REVIEW; on the same-key resume ``rebind_retained_child`` re-fenced that
row to the new generation and the lifecycle's first candidate resolution
re-verified the receipt: ``ACCEPTANCE_RECEIPT_BINDING_INVALID``.

The first owner here records the receipt through the real wave path, then
dies at FINAL_REVIEW (an uncaught ``BaseException``; its fence is released, so
the next owner holds a new generation).  The resumed owner runs the F51 resume
seam on the retained outer child and drives the real lifecycle to DONE.
Scripted Python children and fixture runtime receipts, as in
``test_policy_production_slice``; no native host qualification.
"""
import hashlib
import json
from pathlib import Path
import sys

import pytest

from run_state.frontend_lifecycle import LifecycleProducers, drive_frontend_lifecycle
from run_state.frontend_policy import FrontendPolicyController
from run_state.frontend_producers import rebind_retained_child
from run_state.managed import build_frontend_acceptance_draft, prepare_managed_run
from run_state.sealed_review import record_final_review
from run_state.supervisor import DispatchRequest, Supervisor
from run_state.wave_candidate import bind_wave_execution_candidate
from run_state.wave_execution import capture_prelaunch_snapshot
from run_state.workspace import begin_child_workspace_preparation, inspect_workspace, prepare_workspace
from test_managed_production_ingress import _setup
from test_policy_production_slice import requires_local_confinement
from test_supervised_process import _allocate_registered_child, _receipt_bound_request
from test_wave_consumer import wave_fixture

_REVIEW = """from pathlib import Path
import hashlib,json,sys
path=Path('result-0.txt').resolve()
assert path.read_bytes()==b'done'
print(json.dumps({'schema':'ffs.sealed-final-review/v1','acceptance_hash':sys.argv[1],
 'candidate_hash':sys.argv[2],'review_dimensions':['correctness','security','regression'],
 'criteria':{sys.argv[3]:{'status':'passed','evidence':[{'id':'wave-result','locator':str(path),
 'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}]}},'findings':[]}))
"""


class _Killed(BaseException):
    """The first owner dies at FINAL_REVIEW without a typed refusal or settle path."""


def _row(store, preparation_id):
    with store.read_transaction() as tx:
        return dict(tx.execute("SELECT * FROM context_workspaces WHERE preparation_id=?",
                               (preparation_id,)).fetchone())


def _record_wave_execution(tmp_path, monkeypatch, store, token, supervisor, child, ready):
    """Seal, run one supervised wave, and record the execution receipt and candidate (real record path)."""
    request = DispatchRequest(child.id, "resume-outer", (sys.executable, "-c", "pass"),
                              str(ready.path), ready.base_commit, "b" * 64, contract_hash="d" * 64)
    sealed = {}

    def seal(qualified_request):
        legacy = store.get_acceptance_contract(repository_id=token.repository_id, run_id=token.run_id)
        material = build_frontend_acceptance_draft(
            objective_digest=legacy.material["objective_digest"],
            criteria=[{"id": legacy.accepted_requirement_ids[0], "objective_clause": "write result-0.txt containing done",
                       "checks": [{"id": "result-content", "kind": "command",
                                   "locator": "/usr/bin/grep -qx done result-0.txt"}],
                       "evidence_rules": [{"id": "wave-result", "kind": "patch", "required": True}]}],
            exclusions=[{"id": "other-files", "reason": "only the declared result file may change"}],
            global_invariants=[{"id": "no-commit", "reason": "retain the original HEAD"}],
            requested_runtime_hash=qualified_request.runtime_identity,
            effective_runtime_hash=qualified_request.runtime_identity,
            candidate_hash=ready.input_digest, generation=legacy.generation, command_mode="feature-implement")
        store.create_acceptance_draft(token, draft_id="resume", revision=1,
                                      acceptance_contract_hash=legacy.contract_hash, material=material)
        sealed["value"] = store.seal_acceptance_draft(token, draft_id="resume", revision=1,
                                                      acceptance_contract_hash=legacy.contract_hash)
        store.initialize_frontend_policy(token, acceptance_hash=sealed["value"].acceptance_hash)
        store.transition_frontend_policy(token, expected_stage="SEALED", new_stage="EXECUTE")

    outer_command = (sys.executable, "-c", "from pathlib import Path; import time\n"
                     "while not Path('result-0.txt').exists(): time.sleep(.02)\n")
    with wave_fixture(tmp_path, monkeypatch, plans=1, owner=(supervisor, store, request),
                      outer_command=outer_command, before_launch=seal) as wave:
        reply = wave.consumer(wave.event)
        assert reply["results"][0]["status"] == "complete"
        result = supervisor.finish(wave.outer, timeout=15)
        # The real adapter's exact no-commit completion bytes (fixture-written).
        prefix = wave.parent / ".planning/.ffs-supervised/waves" / child.id / ("wave-" + str(wave.manifest["wave"]))
        prefix.parent.mkdir(parents=True, mode=0o700)
        manifest_raw = json.dumps(wave.manifest, sort_keys=True, separators=(",", ":")).encode()
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
        recorded, state = bind_wave_execution_candidate(
            store, token, sealed=sealed["value"], handle=wave.outer, request_key=wave.request.request_key,
            ready=ready, process_evidence=result["evidence"])
    assert recorded.receipt.role == "execution" and state.candidate_hash != ready.input_digest
    return sealed["value"], result["evidence"]


@requires_local_confinement
def test_resume_keeps_the_receipt_hashed_outer_row_and_reverifies_it_to_done(tmp_path, monkeypatch, capsys):
    primary, authority, _, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    retained = {}

    def first(store, token, context):
        store.bind_runtime(token, context.activity_id, "b" * 64)
        child, ready = _allocate_registered_child(store, token, key="resume-outer")
        supervisor = Supervisor(store, token, evidence_root=authority / "first-evidence")
        sealed, process_evidence = _record_wave_execution(tmp_path, monkeypatch, store, token, supervisor,
                                                          child, ready)
        # Sealed checks cross a channel-less supervisor; the worker channel stays with the outer.
        checks = FrontendPolicyController(store, token, command_mode="feature-implement").run_mapped_checks(
            workspace=str(ready.path), parent_activity_id=child.id, supervisor=Supervisor(
                store, token, evidence_root=authority / "first-checks",
                shared_resource_coordinator=supervisor.shared_resource_coordinator,
                resource_demand_policy=supervisor.resource_demand_policy))
        assert checks["result-content"]["status"] == "passed"
        store.transition_frontend_policy(token, expected_stage="EXECUTE", new_stage="FINAL_REVIEW")
        retained.update(child=child.id, ready=ready.id, generation=token.generation, sealed=sealed,
                        process_evidence=process_evidence, row=_row(store, ready.id))
        raise _Killed()

    arguments = dict(
        objective="write result-0.txt containing done", state_root=authority,
        selection_manifest=env["FFS_SELECTION_MANIFEST"],
        upstream_runtime_manifest=env["FFS_UPSTREAM_RUNTIME_MANIFEST"],
        upstream_runtime_sha256=env["FFS_UPSTREAM_RUNTIME_SHA256"],
        request_key="resume-receipt", command=("/gsd-execute-phase", "1"),
        dispatch_limit=12, token_limit=100, worker_capacity=2,
        run_id="resume-receipt", activity="execute", scope="1",
    )
    with pytest.raises(_Killed):
        prepare_managed_run(**arguments, on_ready=first)

    def resumed(store, token, context):
        assert token.generation > retained["generation"]
        sealed, child_id = retained["sealed"], retained["child"]
        # The F51 resume seam re-fences the retained outer child for checks and the reviewer.
        rebind_retained_child(store, token, child_id, retained["ready"])
        supervisor = Supervisor(store, token, evidence_root=authority / "resume-evidence")
        controller = FrontendPolicyController(store, token, command_mode="feature-implement")
        reviews = []

        def final_review(frozen):
            outer = inspect_workspace(store, retained["ready"])
            snapshot = capture_prelaunch_snapshot(store, token, outer, activity_id=child_id,
                                                  runtime_identity="b" * 64, evidence_root=authority / "resume-capture")
            assert snapshot.input_digest == frozen.candidate_hash
            pending = begin_child_workspace_preparation(
                store, token, parent_activity_id=child_id, request_key="resume-final-review", role="reviewer",
                base_commit=outer.base_commit, selected_input_manifest=snapshot.manifest,
                repository_path=outer.repository_path)
            review_ready = prepare_workspace(store, token, pending, input_snapshot=snapshot)
            reviewer = store.create_child_activity(
                token, parent_activity_id=child_id, role="reviewer", request_key="resume-final-review-activity",
                candidate_hash=review_ready.input_digest, contract_hash=sealed.acceptance_hash,
                runtime_identity="b" * 64, workspace_binding=str(review_ready.path),
                workspace_preparation_id=review_ready.id, retry_budget=2)
            request = _receipt_bound_request(store, token, DispatchRequest(
                reviewer.id, "resume-final-review-launch",
                (sys.executable, "-c", _REVIEW, sealed.acceptance_hash, review_ready.input_digest,
                 sealed.material["criteria"][0]["id"]),
                str(review_ready.path), review_ready.base_commit, "b" * 64, contract_hash=sealed.acceptance_hash))
            handle = supervisor.launch(supervisor.reserve_request_action(request, action="final_review"))
            supervisor.finish(handle, timeout=15)
            record_final_review(supervisor, handle, acceptance_hash=sealed.acceptance_hash)
            reviews.append((reviewer.id, handle.result["evidence"]))

        def settle():
            for reviewer_id, evidence in reviews:
                store.transition_activity(token, reviewer_id, expected="active", new="succeeded", result=evidence)
            store.transition_activity(token, child_id, expected="active", new="succeeded",
                                      result=retained["process_evidence"])

        def unexpected(_value):
            raise AssertionError("the resumed lifecycle neither re-executes nor recovers")

        stage = drive_frontend_lifecycle(
            store, token, supervisor=supervisor, controller=controller, workspace=str(inspect_workspace(
                store, retained["ready"]).path), parent_activity_id=child_id,
            producers=LifecycleProducers(execute=unexpected, final_review=final_review, recover=unexpected,
                                         settle=settle))
        assert stage == "DONE" and len(reviews) == 1
        # The row the pre-crash execution receipt hashed is exactly as it was.
        assert _row(store, retained["ready"]) == retained["row"]
        return 0

    returncode = prepare_managed_run(**arguments, on_ready=resumed)
    assert returncode == 0, capsys.readouterr().out[-1500:]
