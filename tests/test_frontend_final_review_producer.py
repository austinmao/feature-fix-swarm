"""Production final-review producer over the real Supervisor authority chain.

The reviewer executable is a Python telemetry fixture: it contacts no model
and uses no real credential.  Runtime qualification is a fixture seam that
returns the same qualified-runtime/receipt/launch-material shape the managed
host adapters produce.  This proves the production producer's sequencing,
its single-grant replay discipline and its terminal outcomes; it is not
native host qualification.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

requires_local_confinement = pytest.mark.skipif(
    sys.platform != "darwin" or not os.path.isfile("/usr/bin/sandbox-exec"),
    reason="needs Darwin sandbox-exec local-check confinement (non-Darwin fails closed; covered by test_local_check_transport)",
)
pytestmark = requires_local_confinement

from run_state.claude_host import ClaudeLaunchMaterial
from run_state.codex_host import CodexLaunchMaterial
from run_state.frontend_policy import FrontendPolicyController
from run_state.frontend_producers import HostRuntimeSeam, QualifiedHostRuntime, produce_final_review
from run_state.managed import prepare_managed_run
from run_state.managed_admission import ManagedAdmissionQueue
from run_state.native_review_runtime import CLAUDE_CLI_VERSION, CODEX_CLI_VERSION
from run_state.resource_observation import ResourceObservation
from run_state.shared_resources import SharedResourceCoordinator, cold_start_demand
from run_state.state import qualified_runtime_tuple_hash
from run_state.supervisor import DispatchRequest, Supervisor, SupervisorRefused
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

    retained_runtimes = {}

    def qualify(activity_id, workspace, activity_request_key, parent_activity_id, final_contract_hash, role):
        # Production replays read the retained qualification observation; the
        # fixture keeps the same qualified tuple per reviewer activity likewise.
        qualified = retained_runtimes.setdefault(
            activity_id, _ordinary_runtime(host, workspace.path, home, binary, model, effort))
        tuple_hash = qualified_runtime_tuple_hash(qualified)
        with store.read_transaction() as tx:
            existing = tx.execute("SELECT 1 FROM authority_activities WHERE id=?", (activity_id,)).fetchone()
            receipt_row = tx.execute("SELECT receipt_sha256 FROM authority_runtime_receipts "
                                     "WHERE producer_activity_id=? ORDER BY created_at DESC", (activity_id,)).fetchone()
        if existing is not None:
            # Replay: the same reviewer activity, runtime and receipt are retained.
            return QualifiedHostRuntime(store.get_activity(activity_id), qualified,
                                        SimpleNamespace(receipt_sha256=receipt_row["receipt_sha256"]), None, None)
        child = store.create_child_activity(
            token, parent_activity_id=parent_activity_id, role=role, request_key=activity_request_key,
            candidate_hash=workspace.input_digest, contract_hash=final_contract_hash,
            runtime_identity=tuple_hash, workspace_binding=str(workspace.path),
            workspace_preparation_id=workspace.id, retry_budget=2, activity_id=activity_id,
        )
        _tuple, receipt = _commit_same_activity_runtime(store, token, child, qualified)
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


def _counts(store, parent_activity_id):
    with store.read_transaction() as tx:
        ids = [row[0] for row in tx.execute(
            "SELECT activity_id FROM authority_child_bindings WHERE parent_activity_id=? AND role='reviewer'",
            (parent_activity_id,)).fetchall()]
        marks = ",".join("?" for _ in ids) or "''"
        intents = tx.execute(f"SELECT count(*) FROM authority_launch_intents WHERE activity_id IN ({marks})", ids).fetchone()[0]
        actions = tx.execute("SELECT count(*) FROM authority_policy_actions WHERE action='final_review' AND state<>'cancelled'").fetchone()[0]
        attempts = tx.execute(f"SELECT count(*) FROM authority_policy_action_attempts p JOIN authority_launch_intents i "
                              f"ON i.id=p.intent_id WHERE i.activity_id IN ({marks})", ids).fetchone()[0]
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
                "WHERE b.role='reviewer' AND i.child_pid IS NOT NULL").fetchone()[0] == 0
    _run_case(tmp_path, monkeypatch, "codex", check)
