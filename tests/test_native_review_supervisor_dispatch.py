"""Real-Supervisor fixture for restricted native final review.

The executable is a Python telemetry fixture.  It never contacts a model or
uses a real credential. Resource observations and host telemetry are fixtures.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

import pytest

requires_local_confinement = pytest.mark.skipif(
    sys.platform != "darwin" or not os.path.isfile("/usr/bin/sandbox-exec"),
    reason="needs Darwin sandbox-exec local-check confinement (non-Darwin fails closed; covered by test_local_check_transport)",
)
pytestmark = requires_local_confinement

from host_capabilities import build_artifact_review_material
from run_state.claude_host import ClaudeLaunchMaterial
from run_state.codex_host import CodexLaunchMaterial
from run_state.frontend_policy import run_sealed_checks
from run_state.managed import build_frontend_acceptance_draft, prepare_managed_run
from run_state.managed_admission import ManagedAdmissionQueue
from run_state.resource_observation import ResourceObservation
from run_state.shared_resources import SharedResourceCoordinator, cold_start_demand
from run_state.native_review_runtime import (
    CLAUDE_CLI_VERSION, CODEX_CLI_VERSION, NativeReviewRequest,
    prepare_native_review_runtime,
)
from run_state.native_review_transport import prepare_native_review_launch
from run_state.ownership import OwnershipRefused
from run_state.sealed_review import (
    final_review_input_context, final_review_output_contract, record_final_review,
)
from run_state.state import qualified_runtime_tuple_hash
from run_state.supervisor import DispatchRequest, Supervisor, SupervisorRefused
from run_state.workspace import (
    begin_child_workspace_preparation, inspect_workspace,
    load_input_snapshot, parse_input_selection, prepare_workspace, snapshot_inputs,
)
from test_m4_workspace_acceptance import _copy, _selection
from test_runtime_receipt_authority import _qualified, _qualified_claude
from test_managed_production_ingress import _setup


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_sha(value: object) -> str:
    return _sha(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _write(path: Path, value: bytes, mode: int = 0o600) -> Path:
    path.write_bytes(value)
    path.chmod(mode)
    return path


def _reviewer_workspace(store, token, *, key: str):
    """Allocate a reviewer through the real preparation/binding APIs."""
    with store.read_transaction() as tx:
        parent = tx.execute("SELECT * FROM context_runs WHERE repository_id=? AND run_id=?",
                            (token.repository_id, token.run_id)).fetchone()
    root = inspect_workspace(store, parent["preparation_id"])
    source = root.repository_path / "src" / "input.txt"
    # A selected copy must be an actual overlay change for the local-check
    # candidate verifier, rather than a redundant copy of the base blob.
    contents = source.read_bytes() + b'fixture native review candidate\n'
    source.write_bytes(contents)
    selection = parse_input_selection(_selection(
        root.repository_path, token.repository_id, entries=[_copy("src/input.txt", contents)],
    ))
    capture = snapshot_inputs(root.repository_path, selection, root.repository_path / ".ffs-native-capture")
    pending = begin_child_workspace_preparation(
        store, token, parent_activity_id=parent["activity_id"], request_key=key + ":workspace",
        role="reviewer", base_commit=root.base_commit, selected_input_manifest=capture.manifest,
        repository_path=root.repository_path,
    )
    ready = prepare_workspace(store, token, pending, input_snapshot=capture)
    return parent, ready, contents


def _seal(store, token, *, runtime_hash: str, candidate_hash: str):
    legacy = store.get_acceptance_contract(repository_id=token.repository_id, run_id=token.run_id)
    criterion = legacy.accepted_requirement_ids[0]
    material = build_frontend_acceptance_draft(
        objective_digest=legacy.material["objective_digest"],
        criteria=[{
            "id": criterion, "objective_clause": "review the retained fixture input",
            "checks": [{"id": "fixture-check", "kind": "command",
                        "locator": "/bin/bash --noprofile --norc -c 'printf fixture-check-stdout; printf fixture-check-stderr >&2'"}],
            "evidence_rules": [{"id": "fixture-evidence", "kind": "log", "required": True}],
        }], exclusions=[{"id": "none", "reason": "fixture"}],
        global_invariants=[{"id": "no-commit", "reason": "fixture"}],
        requested_runtime_hash=runtime_hash, effective_runtime_hash=runtime_hash,
        candidate_hash=candidate_hash, generation=legacy.generation, command_mode="feature-implement",
    )
    store.create_acceptance_draft(token, draft_id="native-final", revision=1,
                                  acceptance_contract_hash=legacy.contract_hash, material=material)
    sealed = store.seal_acceptance_draft(token, draft_id="native-final", revision=1,
                                         acceptance_contract_hash=legacy.contract_hash)
    store.initialize_frontend_policy(token, acceptance_hash=sealed.acceptance_hash)
    store.transition_frontend_policy(token, expected_stage="SEALED", new_stage="EXECUTE")
    store.transition_frontend_policy(token, expected_stage="EXECUTE", new_stage="FINAL_REVIEW")
    return sealed, criterion


def _ordinary_runtime(host: str, workspace: Path, home: Path, binary: Path, model: str, effort):
    qualified = (_qualified if host == "codex" else _qualified_claude)(workspace)
    runtime = dict(qualified.runtime)
    runtime.update(path=str(home), device=home.stat().st_dev, inode=home.stat().st_ino)
    execution = dict(qualified.execution)
    execution.update(model=model, effort=effort)
    qualified = replace(qualified, binary=(("launcher_sha256", _sha(binary.read_bytes())),),
                        runtime=tuple(sorted(runtime.items())), execution=tuple(sorted(execution.items())))
    if host == "claude":
        observation = dict(qualified.observation)
        observation["version"] = CLAUDE_CLI_VERSION
        qualified = replace(qualified, observation=tuple(sorted(observation.items())))
    return qualified


def _commit_same_activity_runtime(store, token, child, qualified):
    tuple_hash = qualified_runtime_tuple_hash(qualified)
    store.transition_activity(token, child.id, expected="pending", new="active", reason="fixture qualification")
    # Child creation received this same tuple hash, and bind_runtime is the
    # authority API that verifies the activity-side binding.
    store.bind_runtime(token, child.id, tuple_hash)
    return tuple_hash, store.commit_runtime_receipt(token, child.id, qualified)


def _script(host: str, *, session: str | None) -> bytes:
    if host == "codex":
        return f'''#!{sys.executable}
import json, os, pathlib, sys, time
assert "ffs.sealed-final-review/v1" in sys.argv[-1]
assert "FFS_FIXTURE_PARENT_SECRET" not in os.environ
credential = pathlib.Path(os.environ["CODEX_HOME"]) / "auth.json"
assert credential.is_file()
sessions = credential.parent / "sessions"
sessions.mkdir()
records = [{{"type":"session_meta","payload":{{"id":"fixture-thread","cwd":os.getcwd()}}}},
           {{"type":"turn_context","payload":{{"cwd":os.getcwd(),"model":"gpt-5.6-terra","effort":"high","turn_id":"fixture-turn"}}}},
           {{"type":"response_item","payload":{{"type":"message","role":"assistant"}}}}]
(sessions / "rollout-fixture-thread.jsonl").write_text("".join(json.dumps(r)+"\\n" for r in records))
print(json.dumps({{"type":"thread.started","thread_id":"fixture-thread"}}), flush=True)
for _ in range(100):
    if not credential.exists(): break
    time.sleep(.01)
else: raise SystemExit(21)
text = pathlib.Path("review-output.json").read_text()
print(json.dumps({{"type":"item.completed","item":{{"type":"agent_message","text":text}}}}), flush=True)
print(json.dumps({{"type":"turn.completed","usage":{{"input_tokens":3,"cached_input_tokens":0,"output_tokens":4,"cache_write_input_tokens":0,"reasoning_output_tokens":1}}}}), flush=True)
'''.encode()
    return (f"#!{sys.executable}\n" + f'''import json, os, pathlib, sys
assert "ffs.sealed-final-review/v1" in sys.argv[-1]
assert "FFS_FIXTURE_PARENT_SECRET" not in os.environ
session = {session!r}
credential = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
assert credential.is_file()
print(json.dumps({{"type":"system","subtype":"init","session_id":session,"cwd":os.getcwd(),"model":"claude-opus-5","claude_code_version":"{CLAUDE_CLI_VERSION}","tools":[],"mcp_servers":[],"slash_commands":[],"skills":[],"plugins":[]}}), flush=True)
print(json.dumps({{"type":"assistant","session_id":session,"parent_tool_use_id":None,"message":{{"role":"assistant","model":"claude-opus-5","content":[{{"type":"text","text":"fixture"}}]}}}}), flush=True)
text = pathlib.Path("review-output.json").read_text()
print(json.dumps({{"type":"result","subtype":"success","is_error":False,"session_id":session,"num_turns":1,"stop_reason":"end_turn","permission_denials":[],"usage":{{"input_tokens":3,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":4,"server_tool_use":{{"web_search_requests":0,"web_fetch_requests":0}},"output_tokens_details":{{"thinking_tokens":0}}}},"modelUsage":{{"claude-opus-5":{{"canonicalModel":"claude-opus-5","webSearchRequests":0}}}},"result":text}}), flush=True)
''').encode()


def _native_final_review_fixture(tmp_path: Path, host: str, supervisor, store):
    parent, ready, captured = _reviewer_workspace(store, supervisor.token, key="native-review")
    private = tmp_path / (host + "-private")
    private.mkdir(mode=0o700)
    model = "gpt-5.6-terra" if host == "codex" else "claude-opus-5"
    effort = "high" if host == "codex" else None
    session = None if host == "codex" else str(uuid.uuid4())
    binary = private / "native-fixture"
    home = private / "ordinary"
    home.mkdir(mode=0o700)
    _write(binary, _script(host, session=session), 0o700)
    qualified = _ordinary_runtime(host, ready.path, home, binary, model, effort)
    tuple_hash = qualified_runtime_tuple_hash(qualified)
    # Freeze before binding the reviewer, so no binding field needs mutation.
    sealed, criterion = _seal(store, supervisor.token, runtime_hash=tuple_hash, candidate_hash=ready.input_digest)
    child = store.create_child_activity(
        supervisor.token, parent_activity_id=parent["activity_id"], role="reviewer",
        request_key="native-review:activity", candidate_hash=ready.input_digest,
        contract_hash=sealed.acceptance_hash, runtime_identity=tuple_hash,
        workspace_binding=str(ready.path), workspace_preparation_id=ready.id, retry_budget=2,
    )
    tuple_hash, receipt = _commit_same_activity_runtime(store, supervisor.token, child, qualified)
    # This is a real sealed local-check launch and persisted policy result.  The
    # native Python executable below remains an explicit telemetry fixture.
    snapshot = load_input_snapshot(store, ready)
    assert snapshot.input_digest == ready.input_digest
    results, checks = run_sealed_checks(store, supervisor.token, supervisor,
        acceptance_hash=sealed.acceptance_hash, candidate_hash=ready.input_digest,
        checks={item['id']: item for criterion in sealed.material['criteria'] for item in criterion['checks']},
        parent_activity_id=child.id, runtime_identity=tuple_hash, ready=ready, snapshot=snapshot,
        key_prefix='native-fixture-check:')
    store.record_frontend_check_results(supervisor.token, acceptance_hash=sealed.acceptance_hash,
        candidate_hash=ready.input_digest, results=results)
    review_context = final_review_input_context(
        store, supervisor.token, acceptance_hash=sealed.acceptance_hash,
        candidate_hash=ready.input_digest, reviewer_activity_id=child.id,
        selected_artifacts={"src/input.txt": _sha(captured)},
    )
    assert review_context["selected_sources"] == {"src/input.txt": {
        "locator": str(snapshot.staging / "files" / "src/input.txt"), "sha256": _sha(captured)}}
    assert review_context["source_scope"] == "exact-selected-review-inputs"
    check = review_context["checks"]["fixture-check"]
    assert check["status"] == "passed"
    assert check["result"]["returncode"] == 0
    assert check["result"]["stdout"]["contents"] == "fixture-check-stdout"
    assert check["result"]["stderr"]["contents"] == "fixture-check-stderr"
    # Alias a verifier-owned terminal descriptor into the criterion rule. No
    # caller-selected review-evidence file participates in this response.
    evidence = {"id": "fixture-evidence", **checks["fixture-check"]["evidence"][0]}
    response = {"schema": "ffs.sealed-final-review/v1", "acceptance_hash": sealed.acceptance_hash,
                "candidate_hash": ready.input_digest,
                "review_dimensions": sealed.material["required_review_dimensions"],
                "criteria": {criterion: {"status": "passed", "evidence": [{
                    **evidence}]}}, "findings": []}
    _write(ready.path / "review-output.json", json.dumps(response, sort_keys=True, separators=(",", ":")).encode())
    ordinary_sha = _json_sha(qualified.to_dict())
    source = _write(home / ("auth.json" if host == "codex" else ".credentials.json"), b'{"fixture":"dummy"}')
    info = source.stat()
    common = dict(binary=qualified.binary, version=CODEX_CLI_VERSION if host == "codex" else CLAUDE_CLI_VERSION,
                  argv=(str(binary),), environment=(), cwd=str(ready.path), model=model, effort=effort,
                  runtime=qualified, attempt=0, temporary_dir=str(home), temporary_device=home.stat().st_dev,
                  temporary_inode=home.stat().st_ino, runtime_sha256=ordinary_sha)
    if host == "codex":
        ordinary = CodexLaunchMaterial(**common, config_sha256="a" * 64, auth_path=str(source),
            auth_sha256=_sha(source.read_bytes()), auth_device=info.st_dev, auth_inode=info.st_ino)
        catalog = _write(private / "models.json", json.dumps({"models": [{"slug": model}]}).encode())
        extra = {"catalog_path": str(catalog), "catalog_sha256": _sha(catalog.read_bytes())}
    else:
        ordinary = ClaudeLaunchMaterial(**common, session_id=session, environment_sha256="a" * 64,
            credential_path=str(source), credential_sha256=_sha(source.read_bytes()), credential_device=info.st_dev,
            credential_inode=info.st_ino)
        extra = {}
    artifact = build_artifact_review_material(
        host=host, model_request={"kind": "exact", "id": model}, config_sha256="a" * 64,
        policy_sha256="b" * 64, environment={"HOME": str(private), "PATH": "/usr/bin:/bin", "TMPDIR": str(private)},
        selected_artifacts={"src/input.txt": _sha(captured)}, selected_contents={"src/input.txt": captured.decode()},
        provenance={"activity_id": child.id, "preparation_id": ready.id, "input_digest": ready.input_digest,
                    "repository_id": ready.repository_id, "run_id": ready.run_id,
                    "selection_manifest_hash": ready.selected_manifest_hash},
        output_contract=final_review_output_contract(sealed, candidate_hash=ready.input_digest),
        review_context=review_context,
    )
    native = prepare_native_review_runtime(NativeReviewRequest(
        host=host, requested_model=model, cli_version=common["version"], binary=str(binary),
        binary_sha256=_sha(binary.read_bytes()), runtime_identity=tuple_hash, prompt=artifact.prompt,
        effort=effort, session_id=session, **extra), runtime_root=private / "native", workspace=ready.path)
    material = prepare_native_review_launch(native=native, artifact=artifact, ordinary=ordinary,
                                            runtime_receipt_sha256=receipt.receipt_sha256)
    request = DispatchRequest(child.id, "native-review:launch", native.argv, str(ready.path), ready.base_commit,
                              tuple_hash, token_reservation=20, contract_hash=sealed.acceptance_hash, monitor_result=True,
                              runtime_receipt_sha256=receipt.receipt_sha256, managed_input_sha256=ready.input_digest,
                              native_review_material=material)
    return supervisor, store, request, sealed, source, material


def _run_case(tmp_path, monkeypatch, host, check):
    primary, authority, _repository_id, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    monkeypatch.setenv("FFS_FIXTURE_PARENT_SECRET", "fixture-only")
    seen = []

    def execute(store, token, context):
        store.bind_runtime(token, context.activity_id, "b" * 64)
        queue = ManagedAdmissionQueue(tmp_path / "resource-registry", observation_provider=lambda:
            ResourceObservation(time.monotonic_ns(), 4, 4 << 30, 4 << 30, 100, 100, {}, "fixture"))
        supervisor = Supervisor(store, token, evidence_root=authority / "native-review-evidence",
            shared_resource_coordinator=SharedResourceCoordinator(store, token, queue=queue),
            resource_demand_policy=cold_start_demand)
        check(*_native_final_review_fixture(tmp_path, host, supervisor, store))
        seen.append(True)
        return 0

    result = prepare_managed_run(objective=env["FFS_OBJECTIVE"], state_root=authority,
        selection_manifest=env["FFS_SELECTION_MANIFEST"],
        upstream_runtime_manifest=env["FFS_UPSTREAM_RUNTIME_MANIFEST"],
        upstream_runtime_sha256=env["FFS_UPSTREAM_RUNTIME_SHA256"], request_key=env["FFS_REQUEST_KEY"],
        run_id=env["GSD_RUN_ID"], command=("/gsd-plan-phase", "1"), activity="plan", scope="1",
        dispatch_limit=3, token_limit=1000, on_ready=execute)
    assert result == 0 and seen == [True]


@pytest.mark.parametrize("host", ["codex", "claude"])
def test_native_final_review_uses_real_supervisor_authority_chain(tmp_path, monkeypatch, host):
    def check(supervisor, store, request, sealed, source, material):
        handle = supervisor.launch_native_review(request)
        result = supervisor.finish(handle, timeout=15)
        assert result["returncode"] == 0 and result["host_receipt"]["status"] == "complete"
        assert not Path(material.credential_path).exists() and source.read_bytes() == b'{"fixture":"dummy"}'
        accepted = record_final_review(supervisor, handle, acceptance_hash=sealed.acceptance_hash)
        assert accepted.receipt.role == "review"
        prompt = json.loads(material.artifact.prompt.split("\n", 1)[1])
        assert prompt["review_context"] == final_review_input_context(
            store, supervisor.token, acceptance_hash=sealed.acceptance_hash,
            candidate_hash=request.managed_input_sha256, reviewer_activity_id=request.activity_id,
            selected_artifacts=material.artifact.selected_artifacts,
        )
        with store.read_transaction() as tx:
            assert tx.execute("SELECT count(*) FROM authority_policy_action_attempts p JOIN authority_launch_intents i ON i.id=p.intent_id WHERE i.activity_id=?", (request.activity_id,)).fetchone()[0] == 1
            assert tx.execute("SELECT token_usage FROM authority_launch_intents WHERE id=?", (handle.intent_id,)).fetchone()[0] == (8 if host == "codex" else 7)
        assert supervisor.shared_resource_coordinator.queue.snapshot()[0]["status"] == "released"
    _run_case(tmp_path, monkeypatch, host, check)


@pytest.mark.parametrize("boundary", ["monitor-result", "sidecar", "completion", "feedback"])
def test_native_review_resume_releases_same_owner_resource_lease(tmp_path, monkeypatch, boundary):
    def check(supervisor, store, request, sealed, source, material):
        handle = supervisor.launch_native_review(request)
        reservation = supervisor._shared_reservations[handle.intent_id]
        coordinator = supervisor.shared_resource_coordinator
        assert coordinator.queue.status(reservation[0])["status"] == "active"
        handle.process.wait(timeout=15)
        if boundary != "monitor-result":
            with monkeypatch.context() as crash:
                def interrupted(*args, **kwargs):
                    raise RuntimeError("fixture crash")
                crash.setattr(store if boundary == "sidecar" else coordinator,
                              {"sidecar": "complete_launch", "completion": "release",
                               "feedback": "record_feedback"}[boundary], interrupted)
                with pytest.raises(RuntimeError, match="fixture crash"):
                    supervisor.finish(handle, timeout=15)
        resumed = Supervisor(store, supervisor.token, evidence_root=supervisor.evidence_root,
            shared_resource_coordinator=coordinator, resource_demand_policy=cold_start_demand)
        recovered = resumed.resume_monitored(handle.intent_id)
        result = resumed.finish(recovered, timeout=15)
        assert result["host_receipt"]["status"] == "complete"
        assert coordinator.queue.status(reservation[0])["status"] == "released"
        assert record_final_review(resumed, recovered, acceptance_hash=sealed.acceptance_hash).receipt.role == "review"
        replay = Supervisor(store, supervisor.token, evidence_root=supervisor.evidence_root,
            shared_resource_coordinator=coordinator, resource_demand_policy=cold_start_demand)
        assert replay.finish(replay.resume_monitored(handle.intent_id), timeout=15) == result
        # Same-supervisor replays after the release settle idempotently, whatever each still caches;
        # feedback that crashed after the release is retried rather than dropped.
        retried, feedback = [], coordinator.record_feedback
        with monkeypatch.context() as spy:
            spy.setattr(coordinator, "record_feedback",
                        lambda reservation, *, outcome: (retried.append(outcome), feedback(reservation, outcome=outcome)))
            for owner in (resumed, supervisor):
                assert owner.finish(owner.resume_monitored(handle.intent_id), timeout=15) == result
                assert handle.intent_id not in owner._shared_reservations
        if boundary == "feedback":
            assert retried
        assert coordinator.queue.status(reservation[0])["status"] == "released"
        with store.read_transaction() as tx:
            assert tx.execute("SELECT count(*) FROM authority_policy_action_attempts p JOIN authority_launch_intents i ON i.id=p.intent_id WHERE i.activity_id=?", (request.activity_id,)).fetchone()[0] == 1
            assert tx.execute("SELECT count(*) FROM authority_launch_intents WHERE activity_id=?", (request.activity_id,)).fetchone()[0] == 1
    _run_case(tmp_path, monkeypatch, "codex", check)


@pytest.mark.parametrize("mutation", ["missing", "stale"])
def test_native_review_refuses_missing_or_stale_mapped_check_context_without_native_debit(
        tmp_path, monkeypatch, mutation):
    def check(supervisor, store, request, sealed, source, material):
        if mutation == "missing":
            with store.transaction() as tx:
                tx.execute(
                    "DELETE FROM authority_frontend_policy_checks WHERE repository_id=? AND run_id=? "
                    "AND acceptance_hash=? AND candidate_hash=? AND check_id=?",
                    (supervisor.token.repository_id, supervisor.token.run_id,
                     sealed.acceptance_hash, request.managed_input_sha256, "fixture-check"),
                )
        else:
            context = final_review_input_context(
                store, supervisor.token, acceptance_hash=sealed.acceptance_hash,
                candidate_hash=request.managed_input_sha256,
            )
            terminal = Path(context["checks"]["fixture-check"]["evidence"][0]["locator"])
            stdout = Path(json.loads(terminal.read_text())["streams"]["stdout"]["locator"])
            stdout.write_text("stale check stdout")
        with pytest.raises((SupervisorRefused, OwnershipRefused, ValueError)):
            supervisor.launch_native_review(request)
        with store.read_transaction() as tx:
            assert tx.execute(
                "SELECT count(*) FROM authority_launch_intents WHERE activity_id=?", (request.activity_id,),
            ).fetchone()[0] == 0
            assert tx.execute(
                "SELECT count(*) FROM authority_policy_action_attempts p JOIN authority_launch_intents i "
                "ON i.id=p.intent_id WHERE i.activity_id=?", (request.activity_id,),
            ).fetchone()[0] == 0
        assert source.exists() and Path(material.credential_path).exists()
    _run_case(tmp_path, monkeypatch, "codex", check)


@pytest.mark.parametrize("mutation", ["generic", "managed", "cohort", "qualification", "check", "unmonitored",
                                    "receipt", "runtime", "workspace", "command", "candidate", "contract", "ipc",
                                    "output-contract", "missing-context", "forged-context", "forged-source"])
def test_native_review_invalid_requests_do_not_debit(tmp_path, monkeypatch, mutation):
    if mutation == "output-contract":
        monkeypatch.setattr(sys.modules[__name__], "final_review_output_contract",
                            lambda *args, **kwargs: {"schema": "fixture-wrong-output"})
    if mutation in {'missing-context', 'forged-context', 'forged-source'}:
        build = build_artifact_review_material
        def changed(**kwargs):
            kwargs['review_context'] = (None if mutation == 'missing-context'
                else {**kwargs['review_context'], **(
                    {'selected_sources': {'src/input.txt': {'locator': '/fixture/foreign', 'sha256': '0' * 64}}}
                    if mutation == 'forged-source' else {'objective_digest': '0' * 64})})
            return build(**kwargs)
        monkeypatch.setattr(sys.modules[__name__], 'build_artifact_review_material', changed)
    def check(supervisor, store, request, sealed, source, material):
        changes = {"unmonitored": {"monitor_result": False}, "receipt": {"runtime_receipt_sha256": "0" * 64},
                   "runtime": {"runtime_identity": "0" * 64}, "workspace": {"workspace": str(tmp_path)},
                   "command": {"command": ("/usr/bin/true",)}, "candidate": {"managed_input_sha256": "0" * 64},
                   "contract": {"contract_hash": "0" * 64}}
        if mutation == "ipc":
            supervisor.worker_channel = object()
        def launch(value):
            if mutation == "cohort":
                return supervisor.launch_cohort((value,), request_key="fixture-cohort")
            if mutation == "qualification":
                return supervisor.launch_qualification(value, qualification_contract={})
            if mutation == "check":
                return supervisor.launch_sealed_check(value, acceptance_hash=sealed.acceptance_hash,
                                                       check_id="fixture-check")
            entrypoint = (supervisor.launch if mutation == "generic" else supervisor.launch_managed_outer
                          if mutation == "managed" else supervisor.launch_native_review)
            return entrypoint(value)
        with pytest.raises((SupervisorRefused, OwnershipRefused)):
            launch(replace(request, **changes.get(mutation, {})))
        with store.read_transaction() as tx:
            assert tx.execute("SELECT count(*) FROM authority_launch_intents WHERE activity_id=?", (request.activity_id,)).fetchone()[0] == 0
            assert tx.execute("SELECT count(*) FROM authority_policy_action_attempts p JOIN authority_launch_intents i ON i.id=p.intent_id WHERE i.activity_id=?", (request.activity_id,)).fetchone()[0] == 0
        assert source.exists() and Path(material.credential_path).exists()
    _run_case(tmp_path, monkeypatch, "codex", check)


@pytest.mark.parametrize("boundary", ["before-finish", "before-record"])
def test_native_review_stage_drift_cannot_supply_acceptance(tmp_path, monkeypatch, boundary):
    def check(supervisor, store, request, sealed, source, material):
        handle = supervisor.launch_native_review(request)
        handle.process.wait(timeout=15)
        if boundary == "before-record":
            assert supervisor.finish(handle, timeout=15)["host_receipt"]["status"] == "complete"
        store.transition_frontend_policy(supervisor.token, expected_stage="FINAL_REVIEW", new_stage="RECOVER")
        if boundary == "before-finish":
            assert supervisor.finish(handle, timeout=15)["host_receipt"]["status"] == "uncertain"
        with pytest.raises(SupervisorRefused):
            record_final_review(supervisor, handle, acceptance_hash=sealed.acceptance_hash)
    _run_case(tmp_path, monkeypatch, "codex", check)


@pytest.mark.parametrize("host", ["codex", "claude"])
def test_native_review_malformed_usage_retains_token_reservation(tmp_path, monkeypatch, host):
    script = _script
    monkeypatch.setattr(sys.modules[__name__], "_script", lambda host, **kwargs:
                        script(host, **kwargs).replace(b'"output_tokens":4', b'"output_tokens":"unknown"'))
    def check(supervisor, store, request, sealed, source, material):
        handle = supervisor.launch_native_review(request)
        result = supervisor.finish(handle, timeout=15)
        assert result["host_receipt"]["status"] == "uncertain"
        with store.read_transaction() as tx:
            row = tx.execute("SELECT completion_status,token_usage FROM authority_launch_intents WHERE id=?",
                             (handle.intent_id,)).fetchone()
            assert tuple(row) == ("uncertain", None)
            assert tx.execute("SELECT token_committed FROM authority_run_limits").fetchone()[0] == 20
        with pytest.raises(SupervisorRefused):
            record_final_review(supervisor, handle, acceptance_hash=sealed.acceptance_hash)
    _run_case(tmp_path, monkeypatch, host, check)


@pytest.mark.parametrize("mutation", ["material", "generation", "permit", "request", "caller-usage"])
def test_native_review_completion_rechecks_retained_authority(tmp_path, monkeypatch, mutation):
    def check(supervisor, store, request, sealed, source, material):
        handle = supervisor.launch_native_review(request)
        handle.process.wait(timeout=15)
        if mutation == "material":
            path = Path(handle.replay_material["native_review_material"]["material_locator"])
            path.write_bytes(path.read_bytes() + b" ")
        elif mutation == "request":
            handle.replay_material = {**handle.replay_material, "command_sha256": "0" * 64}
        elif mutation == "caller-usage":
            with pytest.raises(SupervisorRefused, match="NATIVE_REVIEW_USAGE_CALLER_FORBIDDEN"):
                supervisor.finish(handle, timeout=15, token_usage=0)
            assert supervisor.finish(handle, timeout=15)["host_receipt"]["status"] == "complete"
            return
        else:
            with store.transaction() as tx:
                if mutation == "generation":
                    tx.execute("UPDATE authority_launch_intents SET generation=generation+1 WHERE id=?", (handle.intent_id,))
                else:
                    tx.execute("UPDATE authority_launch_intents SET permit_id=NULL WHERE id=?", (handle.intent_id,))
        result = supervisor.finish(handle, timeout=15)
        assert result["host_receipt"]["status"] == "uncertain"
        with pytest.raises(SupervisorRefused):
            record_final_review(supervisor, handle, acceptance_hash=sealed.acceptance_hash)
    _run_case(tmp_path, monkeypatch, "codex", check)


@pytest.mark.parametrize('mutation', ['row-after-read', 'stream-after-read'])
def test_review_context_refuses_changes_during_evidence_capture(tmp_path, monkeypatch, mutation):
    import run_state.final_review_context as module
    def check(supervisor, store, request, sealed, source, material):
        original = module._read_checked
        changed = False
        def interleaved(path, *args, **kwargs):
            nonlocal changed
            value = original(path, *args, **kwargs)
            if not changed and path.name == 'stdout.log':
                changed = True
                if mutation == 'stream-after-read':
                    path.write_bytes(b'changed after read')
                else:
                    with store.transaction() as tx:
                        tx.execute("UPDATE authority_frontend_policy_checks SET status='failed'")
            return value
        monkeypatch.setattr(module, '_read_checked', interleaved)
        with pytest.raises(SupervisorRefused, match='FINAL_REVIEW_CONTEXT_INVALID'):
            final_review_input_context(store, supervisor.token, acceptance_hash=sealed.acceptance_hash,
                                       candidate_hash=request.managed_input_sha256)
        assert changed
    _run_case(tmp_path, monkeypatch, 'codex', check)


@pytest.mark.parametrize('host', ['codex', 'claude'])
@pytest.mark.parametrize('scope', ['criterion', 'invariant'])
def test_selected_source_findings_are_retained_as_blocking_review(tmp_path, monkeypatch, host, scope):
    def check(supervisor, store, request, sealed, source, material):
        output_path = Path(request.workspace) / 'review-output.json'
        output = json.loads(output_path.read_text())
        prompt = json.loads(material.artifact.prompt.split('\n', 1)[1])
        context = prompt['review_context']
        assert 'never evidence IDs' in context['source_evidence_policy']['names']
        assert 'at least one mapped check' in context['source_evidence_policy']['criterion_finding']
        criterion = context['criteria'][0]
        # The finding names a mapped check as its control scope, but cites
        # retained source bytes through a rule ID, not through a check ID.
        identifier = (criterion['evidence_rules'][0]['id'] if scope == 'criterion'
                      else context['global_invariants'][0]['id'])
        reference = context['selected_sources']['src/input.txt']
        assert reference['locator'] != str(Path(request.workspace) / 'src/input.txt')
        assert Path(reference['locator']).read_text() == dict(material.artifact.selected_contents)['src/input.txt']
        output['findings'] = [{
            'acceptance_hash': sealed.acceptance_hash, 'candidate_hash': request.managed_input_sha256,
            'runtime_hash': sealed.material['runtime']['effective_hash'],
            'criterion_ids': [criterion['id']] if scope == 'criterion' else [],
            'check_ids': [criterion['checks'][0]['id']] if scope == 'criterion' else [],
            'invariant_ids': [identifier] if scope == 'invariant' else [],
            'evidence': [{'id': identifier, **reference}],
        }]
        output_path.write_text(json.dumps(output))
        handle = supervisor.launch_native_review(request)
        assert supervisor.finish(handle, timeout=15)['host_receipt']['status'] == 'complete'
        with pytest.raises(SupervisorRefused, match='FINAL_REVIEW_BLOCKING_FINDING'):
            record_final_review(supervisor, handle, acceptance_hash=sealed.acceptance_hash)
        with store.read_transaction() as tx:
            findings = tx.execute('SELECT classification,evidence_json FROM authority_frontend_policy_findings').fetchall()
        assert len(findings) == 1
        assert findings[0]['classification'] == ('CONTRACT_FAILURE' if scope == 'criterion' else 'INVARIANT_VIOLATION')
        assert json.loads(findings[0]['evidence_json']) == output['findings'][0]['evidence']
    _run_case(tmp_path, monkeypatch, host, check)


@pytest.mark.parametrize('boundary', ['before-launch', 'before-finish', 'before-record'])
def test_native_review_rechecks_selected_capture_at_each_boundary(tmp_path, monkeypatch, boundary):
    def check(supervisor, store, request, sealed, source, material):
        reference = json.loads(material.artifact.review_context_json)['selected_sources']['src/input.txt']
        if boundary != 'before-launch':
            handle = supervisor.launch_native_review(request)
            handle.process.wait(timeout=15)
            if boundary == 'before-record':
                assert supervisor.finish(handle, timeout=15)['host_receipt']['status'] == 'complete'
        Path(reference['locator']).write_text('fixture changed captured source')
        if boundary == 'before-launch':
            with pytest.raises(SupervisorRefused, match='FINAL_REVIEW_CONTEXT_INVALID'):
                supervisor.launch_native_review(request)
            with store.read_transaction() as tx:
                assert tx.execute('SELECT count(*) FROM authority_launch_intents WHERE activity_id=?',
                                  (request.activity_id,)).fetchone()[0] == 0
            assert Path(material.credential_path).exists()
        else:
            if boundary == 'before-finish':
                assert supervisor.finish(handle, timeout=15)['host_receipt']['status'] == 'uncertain'
            with pytest.raises(SupervisorRefused):
                record_final_review(supervisor, handle, acceptance_hash=sealed.acceptance_hash)
    _run_case(tmp_path, monkeypatch, 'codex', check)


@pytest.mark.parametrize('mutation', ['source-after-read', 'reviewer-after-read'])
def test_selected_capture_context_rechecks_files_and_reviewer_authority(tmp_path, monkeypatch, mutation):
    import run_state.final_review_context as module
    def check(supervisor, store, request, sealed, source, material):
        reference = json.loads(material.artifact.review_context_json)['selected_sources']['src/input.txt']
        original = module._read_checked
        changed = False
        def interleaved(path, *args, **kwargs):
            nonlocal changed
            value = original(path, *args, **kwargs)
            if not changed and str(path) == reference['locator']:
                changed = True
                if mutation == 'source-after-read':
                    path.write_bytes(b'fixture changed after read')
                else:
                    with store.transaction() as tx:
                        tx.execute("UPDATE authority_child_bindings SET role='worker' WHERE activity_id=?",
                                   (request.activity_id,))
            return value
        monkeypatch.setattr(module, '_read_checked', interleaved)
        with pytest.raises(SupervisorRefused, match='FINAL_REVIEW_CONTEXT_INVALID'):
            final_review_input_context(store, supervisor.token, acceptance_hash=sealed.acceptance_hash,
                candidate_hash=request.managed_input_sha256, reviewer_activity_id=request.activity_id,
                selected_artifacts=material.artifact.selected_artifacts)
        assert changed
    _run_case(tmp_path, monkeypatch, 'codex', check)


@pytest.mark.parametrize('boundary', ['before-finish', 'before-record'])
def test_native_review_rechecks_check_context_after_launch(tmp_path, monkeypatch, boundary):
    def check(supervisor, store, request, sealed, source, material):
        handle = supervisor.launch_native_review(request)
        handle.process.wait(timeout=15)
        if boundary == 'before-record':
            assert supervisor.finish(handle, timeout=15)['host_receipt']['status'] == 'complete'
        with store.transaction() as tx:
            tx.execute("UPDATE authority_frontend_policy_checks SET status='failed'")
        if boundary == 'before-finish':
            assert supervisor.finish(handle, timeout=15)['host_receipt']['status'] == 'uncertain'
        with pytest.raises(SupervisorRefused):
            record_final_review(supervisor, handle, acceptance_hash=sealed.acceptance_hash)
    _run_case(tmp_path, monkeypatch, 'codex', check)


@pytest.mark.parametrize('host', ['codex', 'claude'])
def test_native_review_transports_large_escaped_sealed_prompt(tmp_path, monkeypatch, host):
    import run_state.supervisor as supervisor_module
    from run_state.supervisor import _canonical
    spawn = supervisor_module.subprocess.Popen
    monitor_modes = []
    def observed_spawn(args, *positional, **kwargs):
        if isinstance(args, list) and len(args) >= 4 and args[2] == 'run_state.supervisor':
            monitor_modes.append(args[3])
        return spawn(args, *positional, **kwargs)
    monkeypatch.setattr(supervisor_module.subprocess, 'Popen', observed_spawn)
    build = build_frontend_acceptance_draft
    def enlarged(**kwargs):
        kwargs['exclusions'] = [{'id': f'fixture-exclusion-{index}', 'reason': '\\"' * 110}
                                for index in range(100)]
        return build(**kwargs)
    monkeypatch.setattr(sys.modules[__name__], 'build_frontend_acceptance_draft', enlarged)
    def check(supervisor, store, request, sealed, source, material):
        assert len(material.artifact.prompt.encode()) <= 65536
        preview = supervisor._monitor_request(request, intent_id='0' * 36,
            workspace_identity=[1, 2], streams={'stdout': [1, 2], 'stderr': [3, 4]})
        assert 65536 < len(_canonical(preview)) <= 147456
        # The generic entrypoint retains its original transport bound.
        with pytest.raises(SupervisorRefused, match='MESSAGE_TOO_LARGE'):
            supervisor._validate_child_message_size(replace(request, native_review_material=None), [1, 2])
        handle = supervisor.launch_native_review(request)
        assert '_monitor_native' in handle.process.args
        result = supervisor.finish(handle, timeout=15)
        assert result['returncode'] == 0 and result['host_receipt']['status'] == 'complete'
        assert record_final_review(supervisor, handle, acceptance_hash=sealed.acceptance_hash).receipt.role == 'review'
    _run_case(tmp_path, monkeypatch, host, check)
    assert '_child' in monitor_modes  # ordinary local check retains generic routing
    assert monitor_modes.count('_monitor_native') == 1


def test_native_review_frame_overflow_refuses_before_material_or_action(tmp_path, monkeypatch):
    import run_state.native_review_supervision as native_supervision
    def check(supervisor, store, request, sealed, source, material):
        original = supervisor._monitor_request
        def oversized(*args, **kwargs):
            return {**original(*args, **kwargs), 'fixture_padding': 'x' * 147456}
        def premature_publish(*args, **kwargs):
            pytest.fail('oversized frame reached material publication')
        monkeypatch.setattr(supervisor, '_monitor_request', oversized)
        monkeypatch.setattr(native_supervision, 'publish_material', premature_publish)
        with pytest.raises(SupervisorRefused, match='MESSAGE_TOO_LARGE'):
            supervisor.launch_native_review(request)
        with store.read_transaction() as tx:
            assert tx.execute('SELECT count(*) FROM authority_launch_intents WHERE activity_id=?',
                              (request.activity_id,)).fetchone()[0] == 0
            assert tx.execute("SELECT count(*) FROM authority_policy_actions WHERE action='final_review'").fetchone()[0] == 0
        assert not native_supervision.material_locator(supervisor, material).exists()
        assert source.exists() and Path(material.credential_path).exists()
    _run_case(tmp_path, monkeypatch, 'codex', check)


@pytest.mark.parametrize('contents', ['non-utf8', 'oversized'])
def test_review_context_refuses_unrepresentable_real_check_output(tmp_path, monkeypatch, contents):
    build = build_frontend_acceptance_draft
    def changed(**kwargs):
        kwargs['criteria'][0]['checks'][0]['locator'] = (
            r"""/bin/bash --noprofile --norc -c 'printf "\377"'""" if contents == 'non-utf8'
            else "/bin/bash --noprofile --norc -c 'printf %070000d 0'")
        return build(**kwargs)
    monkeypatch.setattr(sys.modules[__name__], 'build_frontend_acceptance_draft', changed)
    with pytest.raises(SupervisorRefused, match='FINAL_REVIEW_CONTEXT_INVALID'):
        _run_case(tmp_path, monkeypatch, 'codex', lambda *_: pytest.fail('invalid context admitted'))


def test_native_review_cannot_relabel_an_unrelated_valid_file_as_criterion_proof(tmp_path, monkeypatch):
    def check(supervisor, store, request, sealed, source, material):
        response = Path(request.workspace) / 'review-output.json'
        output = json.loads(response.read_text())
        unrelated = _write(tmp_path / 'unrelated-proof', b'not a mapped check result')
        next(iter(output['criteria'].values()))['evidence'][0].update(
            locator=str(unrelated), sha256=_sha(unrelated.read_bytes()))
        response.write_text(json.dumps(output))
        handle = supervisor.launch_native_review(request)
        assert supervisor.finish(handle, timeout=15)['host_receipt']['status'] == 'complete'
        with pytest.raises(SupervisorRefused, match='FINAL_REVIEW_EVIDENCE_SCOPE_INVALID'):
            record_final_review(supervisor, handle, acceptance_hash=sealed.acceptance_hash)
    _run_case(tmp_path, monkeypatch, 'codex', check)
