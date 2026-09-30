"""Production final-review producer over the real Supervisor authority chain.

The reviewer executable is a Python telemetry fixture: it contacts no model
and uses no real credential.  Runtime qualification is a fixture seam that
returns the same qualified-runtime/receipt/launch-material shape the managed
host adapters produce.  This proves the production producer's sequencing,
its single-grant replay discipline and its terminal outcomes; it is not
native host qualification.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

requires_local_confinement = pytest.mark.skipif(
    sys.platform != "darwin" or not os.path.isfile("/usr/bin/sandbox-exec"),
    reason="needs Darwin sandbox-exec local-check confinement (non-Darwin fails closed; covered by test_local_check_transport)",
)
pytestmark = requires_local_confinement

from process_identity import ProcessIdentity
from run_state.claude_host import ClaudeLaunchMaterial
from run_state.codex_host import CodexLaunchMaterial
from run_state.frontend_policy import FrontendPolicyController
from run_state.frontend_producers import (
    HostRuntimeSeam, QualifiedHostRuntime, _current_candidate, _native_request, _reviewer_workspace,
    produce_final_review,
)
from run_state.managed import prepare_managed_run
from run_state.managed_admission import ManagedAdmissionQueue
from run_state.native_review_runtime import CLAUDE_CLI_VERSION, CODEX_CLI_VERSION
from run_state.resource_observation import ResourceObservation
from run_state.shared_resources import SharedResourceCoordinator, cold_start_demand
from run_state.state import qualified_runtime_tuple_hash
from run_state.supervisor import DispatchRequest, Supervisor, SupervisorRefused, _publish
from run_state.wave_execution import capture_prelaunch_snapshot
from run_state.workspace import (
    begin_child_workspace_preparation, inspect_workspace, prepare_workspace,
)
from test_managed_production_ingress import _setup
from test_native_review_supervisor_dispatch import (
    _commit_same_activity_runtime, _json_sha, _ordinary_runtime, _seal, _sha, _write,
)
from test_runtime_receipt_authority import _qualified

_COMMON = '''import json, os, pathlib, sys, time
prompt = sys.argv[-1]
assert prompt.startswith("Artifact-only review request:")
assert "FFS_FIXTURE_PARENT_SECRET" not in os.environ
data = json.loads(prompt.split("\\n", 1)[1])
contract, context = data["output_contract"], data["review_context"]
check = next(iter(context["checks"].values()))["evidence"][0]
status = "failed" if "FAIL" in pathlib.Path("src/input.txt").read_text() else "passed"
criteria = {cid: {"status": status, "evidence": [{"id": rid, **check}
            for rid in spec["required_evidence_ids_for_pass"]]} for cid, spec in contract["criteria"].items()}
text = json.dumps({**contract["fixed_fields"], "criteria": criteria, "findings": []},
                  sort_keys=True, separators=(",", ":"))
'''


def _script(host: str) -> bytes:
    if host == "codex":
        body = _COMMON + '''credential = pathlib.Path(os.environ["CODEX_HOME"]) / "auth.json"
assert credential.is_file()
sessions = credential.parent / "sessions"
sessions.mkdir()
records = [{"type":"session_meta","payload":{"id":"fixture-thread","cwd":os.getcwd()}},
           {"type":"turn_context","payload":{"cwd":os.getcwd(),"model":"gpt-5.6-terra","effort":"high","turn_id":"fixture-turn"}},
           {"type":"response_item","payload":{"type":"message","role":"assistant"}}]
(sessions / "rollout-fixture-thread.jsonl").write_text("".join(json.dumps(r)+"\\n" for r in records))
print(json.dumps({"type":"thread.started","thread_id":"fixture-thread"}), flush=True)
for _ in range(100):
    if not credential.exists(): break
    time.sleep(.01)
else: raise SystemExit(21)
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":text}}), flush=True)
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":3,"cached_input_tokens":0,"output_tokens":4,"cache_write_input_tokens":0,"reasoning_output_tokens":1}}), flush=True)
'''
    else:
        body = _COMMON + f'''session = sys.argv[sys.argv.index("--session-id") + 1]
credential = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
assert credential.is_file()
print(json.dumps({{"type":"system","subtype":"init","session_id":session,"cwd":os.getcwd(),"model":"claude-opus-5","claude_code_version":"{CLAUDE_CLI_VERSION}","tools":[],"mcp_servers":[],"slash_commands":[],"skills":[],"plugins":[]}}), flush=True)
print(json.dumps({{"type":"assistant","session_id":session,"parent_tool_use_id":None,"message":{{"role":"assistant","model":"claude-opus-5","content":[{{"type":"text","text":"fixture"}}]}}}}), flush=True)
print(json.dumps({{"type":"result","subtype":"success","is_error":False,"session_id":session,"num_turns":1,"stop_reason":"end_turn","permission_denials":[],"usage":{{"input_tokens":3,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":4,"server_tool_use":{{"web_search_requests":0,"web_fetch_requests":0}},"output_tokens_details":{{"thinking_tokens":0}}}},"modelUsage":{{"claude-opus-5":{{"canonicalModel":"claude-opus-5","webSearchRequests":0}}}},"result":text}}), flush=True)
'''
    return (f"#!{sys.executable}\n" + body).encode()


def _version(host: str) -> str:
    return CODEX_CLI_VERSION if host == "codex" else CLAUDE_CLI_VERSION


_PROBES = ("ordinary", "native-positive", "native-negative", "native-multi-agent")


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _admit_as_qualification_does(store, token, evidence_root, *, activity_id, workspace, activity_request_key,
                                 parent_activity_id, final_contract_hash, role, qualified):
    """The authority calls ``qualify_managed_runtime`` makes on a prepared workspace.

    Qualification only ever admits an ``inventory`` child: it creates the activity
    as inventory, completes the four probes, then promotes activity, binding and
    workspace to the final role.  F48 hid because this stub created the child as
    ``reviewer`` directly, a shape production cannot produce.
    """
    cohort = "qualification:" + activity_request_key
    contracts = {name: {"probe_name": name, "command_sha256": _sha((name + "-command").encode()),
                        "environment_sha256": _sha((name + "-environment").encode()),
                        "qualification_request_id": cohort + ":" + name} for name in _PROBES}
    hashes = {name: _sha(_canonical(contract).encode()) for name, contract in contracts.items()}
    envelope = {
        "schema": "ffs.qualification-envelope/v1", "qualification_cohort_id": cohort,
        "probes": [{"probe_name": name, "probe_contract_sha256": hashes[name]} for name in _PROBES],
        "runtime_template_sha256": "3" * 64, "workspace_binding": str(workspace.path),
        "candidate_input_sha256": workspace.input_digest, "model": "fixture-model", "effort": "high",
        "sandbox": "workspace-write", "roots": [str(workspace.path)], "policy_sha256": final_contract_hash,
    }
    envelope_sha256 = _sha(_canonical(envelope).encode())
    with store.read_transaction() as tx:
        binding = tx.execute("SELECT role FROM authority_child_bindings WHERE activity_id=?", (activity_id,)).fetchone()
    # Like qualify_managed_runtime, resume an unpromoted child: the store calls below are
    # idempotent by activity id, and completed probes are skipped.  A promoted one has only
    # its receipt left to commit.
    if binding is None or binding["role"] == "inventory":
        child = store.create_child_activity(
            token, parent_activity_id=parent_activity_id, role="inventory", request_key=activity_request_key,
            candidate_hash=workspace.input_digest, contract_hash=envelope_sha256, runtime_identity=envelope_sha256,
            workspace_binding=str(workspace.path), workspace_preparation_id=workspace.id,
            retry_budget=len(_PROBES) + 1, activity_id=activity_id,
        )
        if child.state == "pending":
            store.transition_activity(token, child.id, expected="pending", new="active", reason="fixture qualification")
        for index, name in enumerate(_PROBES):
            request_key = contracts[name]["qualification_request_id"]
            with store.read_transaction() as tx:
                completed = tx.execute(
                    "SELECT 1 FROM authority_qualification_launches q JOIN authority_launch_intents i "
                    "ON i.id=q.intent_id WHERE q.activity_id=? AND q.request_key=? "
                    "AND i.state='completed_succeeded'", (activity_id, request_key)).fetchone()
            if completed is not None:
                continue
            action = store.reserve_policy_action(token, action="qualification", logical_key=request_key,
                                                 input_hash=hashes[name])
            intent = store.reserve_qualification_launch(
                child.id, token, request_key=request_key, policy_action_id=action.id,
                qualification_contract={"schema": "ffs.qualification-launch/v1", "probe_contract": contracts[name],
                                        "qualification_envelope": envelope,
                                        "qualification_envelope_sha256": envelope_sha256},
                token_reservation=1, managed_input_sha256=workspace.input_digest)
            sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                store.authorize_child(
                    store.acknowledge_child(intent.id, token, ProcessIdentity.from_pid(sleeper.pid)), token)
            finally:
                sleeper.terminate()
                sleeper.wait(timeout=10)
            # The class method, not ``store.complete_launch``: the crash-boundary tests patch the
            # instance attribute to interrupt the review launch, not these probes.
            type(store).complete_launch(
                store, intent.id, token, status="succeeded", token_usage=0,
                evidence=_publish(evidence_root, f"{activity_id}-probe-{index}.json", {"probe": name}))
        store.promote_qualified_activity(
            token, child.id, qualification_request_key=cohort, expected_contract_hashes=hashes,
            runtime_identity=qualified_runtime_tuple_hash(qualified), final_contract_hash=final_contract_hash,
            role=role, observation_evidence=_publish(evidence_root, activity_id + "-observation.json",
                                                     {"qualified": True}))
    return store.get_activity(activity_id), store.commit_runtime_receipt(token, activity_id, qualified)


def _fixture_seam(tmp_path: Path, host: str, store, token) -> HostRuntimeSeam:
    private = tmp_path / (host + "-private")
    private.mkdir(mode=0o700)
    model = "gpt-5.6-terra" if host == "codex" else "claude-opus-5"
    effort = "high" if host == "codex" else None
    binary = _write(private / "native-fixture", _script(host), 0o700)
    home = private / "ordinary"
    home.mkdir(mode=0o700)
    credential = _write(home / ("auth.json" if host == "codex" else ".credentials.json"), b'{"fixture":"dummy"}')
    catalog = None
    if host == "codex":
        catalog = _write(private / "models.json", json.dumps({"models": [{"slug": model}]}).encode())

    evidence = tmp_path / (host + "-qualification-evidence")
    evidence.mkdir(mode=0o700)
    retained_runtimes = {}

    def qualify(activity_id, workspace, activity_request_key, parent_activity_id, final_contract_hash, role):
        # Production replays read the retained qualification observation; the
        # fixture keeps the same qualified tuple per reviewer activity likewise.
        qualified = retained_runtimes.setdefault(
            activity_id, _ordinary_runtime(host, workspace.path, home, binary, model, effort))
        with store.read_transaction() as tx:
            binding = tx.execute("SELECT role FROM authority_child_bindings WHERE activity_id=?",
                                 (activity_id,)).fetchone()
            receipt_row = tx.execute("SELECT receipt_sha256 FROM authority_runtime_receipts "
                                     "WHERE producer_activity_id=? ORDER BY created_at DESC", (activity_id,)).fetchone()
        if binding is not None and binding["role"] != "inventory" and receipt_row is not None:
            # Replay: the same reviewer activity, runtime and receipt are retained.  An activity
            # still bound as inventory, or promoted without its receipt, resumes below.
            return QualifiedHostRuntime(store.get_activity(activity_id), qualified,
                                        SimpleNamespace(receipt_sha256=receipt_row["receipt_sha256"]), None, None)
        child, receipt = _admit_as_qualification_does(
            store, token, evidence, activity_id=activity_id, workspace=workspace,
            activity_request_key=activity_request_key, parent_activity_id=parent_activity_id,
            final_contract_hash=final_contract_hash, role=role, qualified=qualified)
        return QualifiedHostRuntime(store.get_activity(child.id), qualified, receipt, None, None)

    def bind(qualified, prompt, workspace, final_contract_hash, launch_request_key):
        info = credential.stat()
        common = dict(binary=qualified.qualified.binary, version=_version(host),
                      argv=(str(binary), prompt), environment=(), cwd=str(workspace.path), model=model, effort=effort,
                      runtime=qualified.qualified, attempt=1, temporary_dir=str(home),
                      temporary_device=home.stat().st_dev, temporary_inode=home.stat().st_ino,
                      runtime_sha256=_json_sha(qualified.qualified.to_dict()))
        if host == "codex":
            material = CodexLaunchMaterial(**common, config_sha256="a" * 64, auth_path=str(credential),
                auth_sha256=_sha(credential.read_bytes()), auth_device=info.st_dev, auth_inode=info.st_ino)
            fields = {"codex_material": material}
        else:
            material = ClaudeLaunchMaterial(**common, session_id="00000000-0000-4000-8000-000000000000",
                environment_sha256="a" * 64, credential_path=str(credential),
                credential_sha256=_sha(credential.read_bytes()), credential_device=info.st_dev,
                credential_inode=info.st_ino)
            fields = {"claude_material": material}
        return DispatchRequest(
            activity_id=qualified.activity.id, request_key=launch_request_key, command=common["argv"],
            workspace=str(workspace.path), expected_head=workspace.base_commit,
            runtime_identity=qualified_runtime_tuple_hash(qualified.qualified), token_reservation=20,
            contract_hash=final_contract_hash, runtime_receipt_sha256=qualified.receipt.receipt_sha256,
            managed_input_sha256=workspace.input_digest, **fields,
        ), None

    return HostRuntimeSeam(
        host=host, qualify=qualify, bind=bind, binary=str(binary), cli_version=_version(host),
        model=model, effort=effort, model_request={"kind": "exact", "id": model},
        catalog_path=None if catalog is None else str(catalog),
        catalog_sha256=None if catalog is None else _sha(catalog.read_bytes()),
    )


def _candidate(store, token, supervisor, *, text: bytes):
    """One executed worker candidate: an overlay change captured from the run root."""
    with store.read_transaction() as tx:
        parent = tx.execute("SELECT * FROM context_runs WHERE repository_id=? AND run_id=?",
                            (token.repository_id, token.run_id)).fetchone()
    root = inspect_workspace(store, parent["preparation_id"])
    (root.path / "src" / "input.txt").write_bytes(text)
    snapshot = capture_prelaunch_snapshot(store, token, root, activity_id=parent["activity_id"],
                                          runtime_identity="b" * 64, evidence_root=supervisor.evidence_root)
    pending = begin_child_workspace_preparation(
        store, token, parent_activity_id=parent["activity_id"], request_key="executed:workspace",
        role="worker", base_commit=root.base_commit, selected_input_manifest=snapshot.manifest,
        repository_path=root.repository_path,
    )
    ready = prepare_workspace(store, token, pending, input_snapshot=snapshot)
    return parent, ready


def _run_case(tmp_path, monkeypatch, host, check, *, text=b"base-input\nfixture reviewed candidate\n"):
    primary, authority, _repository_id, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    monkeypatch.setenv("FFS_FIXTURE_PARENT_SECRET", "fixture-only")
    seen = []

    def execute(store, token, context):
        try:
            return _execute(store, token, context)
        except Exception:
            # The managed ingress maps every failure to a typed refusal; keep the fixture cause visible.
            import traceback
            traceback.print_exc()
            raise

    def _execute(store, token, context):
        store.bind_runtime(token, context.activity_id, "b" * 64)
        queue = ManagedAdmissionQueue(tmp_path / "resource-registry", observation_provider=lambda:
            ResourceObservation(time.monotonic_ns(), 4, 4 << 30, 4 << 30, 100, 100, {host: 4}, "fixture"))

        def review_supervisor(fault_probe=None):
            return Supervisor(store, token, evidence_root=authority / "review-evidence", fault_probe=fault_probe,
                shared_resource_coordinator=SharedResourceCoordinator(store, token, queue=queue),
                resource_demand_policy=cold_start_demand)

        supervisor = review_supervisor()
        parent, ready = _candidate(store, token, supervisor, text=text)
        worker_runtime = _qualified(ready.path)
        tuple_hash = qualified_runtime_tuple_hash(worker_runtime)
        sealed, criterion = _seal(store, token, runtime_hash=tuple_hash, candidate_hash=ready.input_digest)
        worker = store.create_child_activity(
            token, parent_activity_id=parent["activity_id"], role="worker", request_key="executed:activity",
            candidate_hash=ready.input_digest, contract_hash=sealed.acceptance_hash, runtime_identity=tuple_hash,
            workspace_binding=str(ready.path), workspace_preparation_id=ready.id, retry_budget=2,
        )
        _commit_same_activity_runtime(store, token, worker, worker_runtime)
        controller = FrontendPolicyController(store, token, command_mode="feature-implement")
        checks = controller.run_mapped_checks(workspace=str(ready.path), supervisor=supervisor,
                                              parent_activity_id=worker.id)
        assert checks["fixture-check"]["status"] == "passed"
        seam = _fixture_seam(tmp_path, host, store, token)
        check(SimpleNamespace(store=store, token=token, supervisor=supervisor, new_supervisor=review_supervisor,
                              controller=controller, seam=seam, worker=worker, ready=ready, sealed=sealed,
                              criterion=criterion))
        seen.append(True)
        return 0

    result = prepare_managed_run(objective=env["FFS_OBJECTIVE"], state_root=authority,
        selection_manifest=env["FFS_SELECTION_MANIFEST"],
        upstream_runtime_manifest=env["FFS_UPSTREAM_RUNTIME_MANIFEST"],
        upstream_runtime_sha256=env["FFS_UPSTREAM_RUNTIME_SHA256"], request_key=env["FFS_REQUEST_KEY"],
        run_id=env["GSD_RUN_ID"], command=("/gsd-plan-phase", "1"), activity="plan", scope="1",
        dispatch_limit=8, token_limit=1000, on_ready=execute)
    assert result == 0 and seen == [True]


def _produce(case, supervisor=None):
    return produce_final_review(
        case.store, case.token, supervisor=supervisor or case.supervisor, controller=case.controller,
        seam=case.seam, parent_activity_id=case.worker.id, preparation=case.ready, timeout_seconds=15)


_NOT_A_PROBE = "NOT EXISTS (SELECT 1 FROM authority_qualification_launches q WHERE q.intent_id=i.id)"


def _counts(store, parent_activity_id):
    with store.read_transaction() as tx:
        ids = [row[0] for row in tx.execute(
            "SELECT activity_id FROM authority_child_bindings WHERE parent_activity_id=? AND role='reviewer'",
            (parent_activity_id,)).fetchall()]
        marks = ",".join("?" for _ in ids) or "''"
        # Qualification probes are their own intents; only the review launch counts here.
        intents = tx.execute(f"SELECT count(*) FROM authority_launch_intents i WHERE i.activity_id IN ({marks}) "
                             f"AND {_NOT_A_PROBE}", ids).fetchone()[0]
        actions = tx.execute("SELECT count(*) FROM authority_policy_actions WHERE action='final_review' AND state<>'cancelled'").fetchone()[0]
        attempts = tx.execute(f"SELECT count(*) FROM authority_policy_action_attempts p JOIN authority_launch_intents i "
                              f"ON i.id=p.intent_id WHERE i.activity_id IN ({marks}) "
                              f"AND {_NOT_A_PROBE}", ids).fetchone()[0]
        receipts = tx.execute("SELECT count(*) FROM authority_acceptance_receipts "
                              "WHERE json_extract(receipt_json,'$.role')='review'").fetchone()[0]
    return {"reviewers": len(ids), "intents": intents, "actions": actions, "attempts": attempts, "receipts": receipts}


_ONE = {"reviewers": 1, "intents": 1, "actions": 1, "attempts": 1, "receipts": 1}


@pytest.mark.parametrize("host", ["codex", "claude"])
def test_producer_reviews_current_candidate_natively_with_one_grant(tmp_path, monkeypatch, host):
    def check(case):
        recorded = _produce(case)
        assert recorded.receipt.role == "review" and recorded.receipt.completion_status == "succeeded"
        assert recorded.receipt.candidate_hash == case.sealed.material["candidate_hash"]
        assert _counts(case.store, case.worker.id) == _ONE
        with case.store.read_transaction() as tx:
            usage = [row[0] for row in tx.execute(
                "SELECT token_usage FROM authority_launch_intents WHERE completion_status='succeeded'").fetchall()]
        assert (8 if host == "codex" else 7) in usage
        # Replay after the receipt exists reuses the retained intent and spends nothing.
        again = _produce(case, supervisor=case.new_supervisor())
        assert again.receipt_hash == recorded.receipt_hash
        assert _counts(case.store, case.worker.id) == _ONE
    _run_case(tmp_path, monkeypatch, host, check)


def test_producer_records_a_failed_review_as_the_spent_grant(tmp_path, monkeypatch):
    def check(case):
        with pytest.raises(SupervisorRefused, match="FINAL_REVIEW_CRITERION_FAILED"):
            _produce(case)
        assert _counts(case.store, case.worker.id) == _ONE
        # The refused review is durable: a replay cannot buy a second broad review.
        with pytest.raises(SupervisorRefused, match="FINAL_REVIEW_CRITERION_FAILED"):
            _produce(case, supervisor=case.new_supervisor())
        assert _counts(case.store, case.worker.id) == _ONE
    _run_case(tmp_path, monkeypatch, "codex", check, text=b"base-input\nFAIL fixture candidate\n")


@pytest.mark.parametrize("boundary", ["capture", "preparation", "action", "material", "completion", "record"])
def test_producer_replays_through_interruption_without_a_second_review(tmp_path, monkeypatch, boundary):
    import run_state.frontend_producers as producers
    import run_state.native_review_supervision as supervision

    def check(case):
        crashed = []

        def once(original):
            def wrapped(*args, **kwargs):
                value = original(*args, **kwargs)
                if not crashed:
                    crashed.append(True)
                    raise RuntimeError("fixture crash")
                return value
            return wrapped

        if boundary == "capture":
            monkeypatch.setattr(producers, "capture_prelaunch_snapshot", once(producers.capture_prelaunch_snapshot))
        elif boundary == "preparation":
            monkeypatch.setattr(producers, "prepare_workspace", once(producers.prepare_workspace))
        elif boundary == "action":
            monkeypatch.setattr(case.supervisor, "reserve_request_action", once(case.supervisor.reserve_request_action))
        elif boundary == "material":
            monkeypatch.setattr(supervision, "publish_material", once(supervision.publish_material))
        elif boundary == "completion":
            monkeypatch.setattr(case.store, "complete_launch", once(case.store.complete_launch))
        elif boundary == "record":
            monkeypatch.setattr(case.store, "record_acceptance_receipt", once(case.store.record_acceptance_receipt))
        with pytest.raises(RuntimeError, match="fixture crash"):
            _produce(case)
        assert crashed
        before = _counts(case.store, case.worker.id)
        assert before["actions"] <= 1 and before["intents"] <= 1 and before["attempts"] <= 1
        recorded = _produce(case, supervisor=case.new_supervisor())
        assert recorded.receipt.role == "review" and recorded.receipt.completion_status == "succeeded"
        assert _counts(case.store, case.worker.id) == _ONE, (boundary, before)
    _run_case(tmp_path, monkeypatch, "codex", check)


def test_producer_replays_a_crash_after_qualification_created_the_inventory_child(tmp_path, monkeypatch):
    """F48: a crash between inventory-child creation and promotion resumes on the retained workspace."""
    key = "final-review:reviewer"

    def check(case):
        create, crashed = case.store.create_child_activity, []

        def create_then_crash(*args, **kwargs):
            activity = create(*args, **kwargs)
            if not crashed:
                crashed.append(True)
                raise RuntimeError("fixture crash")
            return activity

        monkeypatch.setattr(case.store, "create_child_activity", create_then_crash)
        with pytest.raises(RuntimeError, match="fixture crash"):
            _produce(case)
        assert crashed
        with case.store.read_transaction() as tx:
            retained = [tuple(row) for row in tx.execute(
                "SELECT preparation_id,child_role FROM context_workspaces WHERE child_request_key=?", (key,))]
        # Qualification was interrupted: the retained workspace is still the inventory one.
        assert len(retained) == 1 and retained[0][1] == "inventory"
        recorded = _produce(case, supervisor=case.new_supervisor())
        assert recorded.receipt.role == "review" and recorded.receipt.completion_status == "succeeded"
        assert _counts(case.store, case.worker.id) == _ONE
        with case.store.read_transaction() as tx:
            workspaces = [tuple(row) for row in tx.execute(
                "SELECT preparation_id,child_role FROM context_workspaces WHERE child_request_key=?", (key,))]
            activities = tx.execute("SELECT count(*) FROM authority_activities WHERE request_key=?", (key,)).fetchone()[0]
        # The same workspace was replayed and then promoted; no second activity was created.
        assert workspaces == [(retained[0][0], "reviewer")] and activities == 1
    _run_case(tmp_path, monkeypatch, "codex", check)


def test_producer_refuses_replay_of_an_unacknowledged_intent_without_a_second_launch(tmp_path, monkeypatch):
    def check(case):
        def probe(point):
            if point == "after_intent_commit":
                raise RuntimeError("fixture crash before acknowledgement")
        with pytest.raises(RuntimeError, match="before acknowledgement"):
            _produce(case, supervisor=case.new_supervisor(fault_probe=probe))
        before = _counts(case.store, case.worker.id)
        assert before == {"reviewers": 1, "intents": 1, "actions": 1, "attempts": 1, "receipts": 0}
        with pytest.raises(SupervisorRefused, match="INTENT_RECONCILIATION_REQUIRED"):
            _produce(case, supervisor=case.new_supervisor())
        assert _counts(case.store, case.worker.id) == before
        with case.store.read_transaction() as tx:
            assert tx.execute(
                "SELECT count(*) FROM authority_launch_intents i JOIN authority_child_bindings b ON b.activity_id=i.activity_id "
                "WHERE b.role='reviewer' AND i.child_pid IS NOT NULL AND " + _NOT_A_PROBE).fetchone()[0] == 0
    _run_case(tmp_path, monkeypatch, "codex", check)


def test_reviewer_workspace_qualifies_as_inventory_and_replay_reads_the_promoted_role(tmp_path, monkeypatch):
    """F48: qualification admits only an inventory workspace and promotes it to the final role."""
    def check(case):
        frozen = case.controller.sealed()
        preparation, parent_id, runtime_identity = _current_candidate(
            case.store, case.token, parent_activity_id=case.worker.id, preparation=case.ready)

        def reviewer_workspace(retained_preparation_id):
            return _reviewer_workspace(
                case.store, case.token, case.supervisor, preparation=preparation, parent_activity_id=parent_id,
                runtime_identity=runtime_identity, reviewer_key="final-review:reviewer",
                candidate_hash=frozen.candidate_hash, retained_preparation_id=retained_preparation_id)

        fresh = reviewer_workspace(None)
        activity_id = str(uuid.uuid4())
        qualified = case.seam.qualify(activity_id, fresh, "final-review:reviewer", parent_id,
                                      frozen.acceptance_hash, "reviewer")
        assert qualified.activity.id == activity_id
        with case.store.read_transaction() as tx:
            assert tx.execute("SELECT role FROM authority_child_bindings WHERE activity_id=?",
                              (activity_id,)).fetchone()[0] == "reviewer"
        assert inspect_workspace(case.store, fresh.id).child_role == "reviewer"
        # Replay after promotion reads the retained, already-promoted workspace.
        replayed = reviewer_workspace(fresh.id)
        assert replayed.id == fresh.id and replayed.child_role == "reviewer"
    _run_case(tmp_path, monkeypatch, "codex", check)


def test_native_request_carries_the_qualified_node_pin_for_codex_only(tmp_path):
    """F53: the reviewer's Node identity is the one its runtime was qualified with."""
    launcher = tmp_path / "codex.js"
    launcher.write_bytes(b"#!/usr/bin/env node\n")
    seam = SimpleNamespace(host="codex", binary=str(launcher), cli_version=CODEX_CLI_VERSION, model="gpt-5.6-terra",
                           effort="high", catalog_path=str(tmp_path / "models.json"), catalog_sha256="c" * 64)
    chain = {"launcher_sha256": "a" * 64, "node_sha256": "b" * 64}

    assert _native_request(seam, runtime_identity="r", prompt="p", qualified_binary=chain).node_sha256 == "b" * 64
    assert _native_request(seam, runtime_identity="r", prompt="p",
                           qualified_binary={"launcher_sha256": "a" * 64}).node_sha256 is None
    claude = SimpleNamespace(**{**vars(seam), "host": "claude", "catalog_path": None, "catalog_sha256": None})
    assert _native_request(claude, runtime_identity="r", prompt="p", qualified_binary=chain).node_sha256 is None


def test_native_request_carries_the_qualified_vendor_pin_for_codex_only(tmp_path):
    """F53 review: the vendor executable a `.js` launcher spawns is pinned by the qualified chain."""
    launcher = tmp_path / "codex.js"
    launcher.write_bytes(b"#!/usr/bin/env node\n")
    seam = SimpleNamespace(host="codex", binary=str(launcher), cli_version=CODEX_CLI_VERSION, model="gpt-5.6-terra",
                           effort="high", catalog_path=str(tmp_path / "models.json"), catalog_sha256="c" * 64)
    chain = {"launcher_sha256": "a" * 64, "node_sha256": "b" * 64, "native_sha256": "d" * 64}

    assert _native_request(seam, runtime_identity="r", prompt="p", qualified_binary=chain).native_sha256 == "d" * 64
    assert _native_request(seam, runtime_identity="r", prompt="p",
                           qualified_binary={"launcher_sha256": "a" * 64}).native_sha256 is None
    claude = SimpleNamespace(**{**vars(seam), "host": "claude", "catalog_path": None, "catalog_sha256": None})
    assert _native_request(claude, runtime_identity="r", prompt="p", qualified_binary=chain).native_sha256 is None
