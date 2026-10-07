"""spec-014 E8 prerequisite 4b, end to end: a Codex outer whose spec and final reviews run natively on Claude.

The managed Codex lifecycle runs as in ``test_spec_review_lifecycle`` (fixture Codex host, real ControlStore and
Supervisor, real child workspaces with real Git worktrees, real ``frontend-start`` entry).  An explicit
``--review-host claude`` request gives the reviewers the production Claude seam over a fixture qualification and launch
adapter, and a Python stand-in for the Claude CLI that prints Claude stream-json; the native review transport, its
authority and its records are real.  Recovery and repair stay on the Codex outer.  The Claude-outer direction is covered
over scripted collaborators in ``test_cross_family_review``.  Fixture-level proof only: no Codex or Claude CLI runs.
Not native host qualification, not E8.
"""
from __future__ import annotations

import json
import sys
import time
from types import SimpleNamespace


import run_state.managed_claude_qualification as managed_claude
import run_state.managed_qualification as managed_qualification
import run_state.shared_resources as shared_resources
from recovery_fixture import (
    CHECK_ALWAYS_PASSES, SPEC_REVIEW_ACTIONS, assert_repaired_once, ledger, spec_review_records, world,
)
from run_state.claude_host import QUALIFIED_CLAUDE_RUNTIME_SCHEMA, ClaudeLaunchMaterial
from run_state.managed_admission import ManagedAdmissionQueue
from run_state.native_review_runtime import CLAUDE_CLI_VERSION
from run_state.resource_observation import ResourceObservation
from test_final_review_resume import _Killed, _assert_done_once, _crash, _held_resources
from test_managed_lifecycle_assembly import requires_local_confinement
from test_native_review_supervisor_dispatch import _json_sha, _ordinary_runtime, _sha, _write

pytestmark = requires_local_confinement

JUDGMENT = '{"kind":"tier","name":"judgment"}'

# A stand-in for the Claude CLI's artifact-only review: it answers the spec review and the final review contracts.
_CLAUDE_REVIEWER = '''import json, os, pathlib, sys
prompt = sys.argv[-1]
assert prompt.startswith("Artifact-only review request:")
data = json.loads(prompt.split("\\n", 1)[1])
contract, context = data["output_contract"], data["review_context"]
if contract["fixed_fields"]["schema"] == "ffs.spec-review/v1":
    marks = {cid: {"status": "acceptable", "reason": "fixture accept"} for cid in contract["criteria"]}
    reply = {**contract["fixed_fields"], "verdict": "accept", "criteria": marks, "notes": []}
else:
    check = next(iter(context["checks"].values()))["evidence"][0]
    criteria = {cid: {"status": "passed", "evidence": [{"id": rid, **check}
                for rid in spec["required_evidence_ids_for_pass"]]} for cid, spec in contract["criteria"].items()}
    reply = {**contract["fixed_fields"], "criteria": criteria, "findings": []}
text = json.dumps(reply, sort_keys=True, separators=(",", ":"))
session = sys.argv[sys.argv.index("--session-id") + 1]
assert (pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".credentials.json").is_file()
print(json.dumps({"type":"system","subtype":"init","session_id":session,"cwd":os.getcwd(),"model":"claude-opus-5","claude_code_version":VERSION,"tools":[],"mcp_servers":[],"slash_commands":[],"skills":[],"plugins":[]}), flush=True)
print(json.dumps({"type":"assistant","session_id":session,"parent_tool_use_id":None,"message":{"role":"assistant","model":"claude-opus-5","content":[{"type":"text","text":"fixture"}]}}), flush=True)
print(json.dumps({"type":"result","subtype":"success","is_error":False,"session_id":session,"num_turns":1,"stop_reason":"end_turn","permission_denials":[],"usage":{"input_tokens":3,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":4,"server_tool_use":{"web_search_requests":0,"web_fetch_requests":0},"output_tokens_details":{"thinking_tokens":0}},"modelUsage":{"claude-opus-5":{"canonicalModel":"claude-opus-5","webSearchRequests":0}},"result":text}), flush=True)
'''


def _claude_reviewer(tmp_path, monkeypatch, *, crash_first_reviewer=False):
    """The Claude reviewer the ``--review-host claude`` request names, over fixture qualification and adapter.

    Qualification creates and promotes the child as the Codex assembly fixture does, and commits a qualified Claude
    runtime receipt; the adapter binds the ordinary Claude launch material the native transport copies its credential
    from.  ``crash_first_reviewer`` kills the run right after the first reviewer is qualified.
    """
    private = (tmp_path / "claude-reviewer").resolve()
    private.mkdir(mode=0o700)
    binary = _write(private / "claude", (f"#!{sys.executable}\nVERSION = {CLAUDE_CLI_VERSION!r}\n"
                                         + _CLAUDE_REVIEWER).encode(), 0o700)
    home = private / "ordinary"
    home.mkdir(mode=0o700)
    credential = _write(home / ".credentials.json", b'{"fixture":"dummy"}')
    calls, retained = [], {}

    def qualify(store, token, *, activity_id, activity_request_key, parent_activity_id, workspace, host_request, role,
                evidence_root, final_contract_hash, supervisor, bridge_command, project=None, workstream=None):
        del evidence_root, supervisor, bridge_command, project, workstream
        calls.append((role, activity_request_key, host_request))
        if activity_id in retained:
            qualified, receipt = retained[activity_id]
            return store.get_activity(activity_id), qualified, receipt, home, SimpleNamespace()
        qualified = _ordinary_runtime("claude", workspace.path, home, binary, host_request.model, host_request.effort)
        with store.transaction() as tx:
            tx.execute("UPDATE context_workspaces SET child_role=? WHERE preparation_id=?", (role, workspace.id))
        activity = store.create_child_activity(
            token, parent_activity_id=parent_activity_id, role=role, request_key=activity_request_key,
            candidate_hash=workspace.input_digest, contract_hash=final_contract_hash,
            runtime_identity=store.runtime_tuple_hash(qualified), workspace_binding=str(workspace.path),
            workspace_preparation_id=workspace.id, retry_budget=2, activity_id=activity_id)
        activity = store.transition_activity(token, activity.id, expected="pending", new="active",
                                             reason="fixture qualified")
        receipt = store.commit_runtime_receipt(token, activity.id, qualified)
        retained[activity_id] = (qualified, receipt)
        if crash_first_reviewer and len(calls) == 1:
            raise _Killed()
        return activity, qualified, receipt, home, SimpleNamespace()

    class Adapter:
        def __init__(self, qualified, _binary, version):
            self.qualified, self.version = qualified, version

        def build_launch_material(self, prompt, *, attempt, session_id, gsd_environment):
            del gsd_environment
            qualified, info, execution = self.qualified, credential.stat(), dict(self.qualified.execution)
            return ClaudeLaunchMaterial(
                binary=qualified.binary, version=self.version, argv=(str(binary), prompt), environment=(),
                cwd=dict(qualified.workspace)["path"], model=execution["model"], effort=execution["effort"],
                session_id=session_id, runtime=qualified, attempt=attempt, temporary_dir=str(home),
                temporary_device=home.stat().st_dev, temporary_inode=home.stat().st_ino,
                credential_path=str(credential), credential_sha256=_sha(credential.read_bytes()),
                credential_device=info.st_dev, credential_inode=info.st_ino, environment_sha256="a" * 64,
                runtime_sha256=_json_sha(qualified.to_dict()))

        @staticmethod
        def release_launch_material(_material):
            return None   # every fixture reviewer shares this one ordinary home

    monkeypatch.setattr(managed_claude, "qualify_managed_claude_runtime", qualify)
    monkeypatch.setattr(managed_claude, "ClaudeHostAdapter", Adapter)
    # The shared admission queue observes a Claude provider too (the Codex fixture host observes Codex only).
    monkeypatch.setattr(shared_resources, "ManagedAdmissionQueue", lambda *args, **kwargs: ManagedAdmissionQueue(
        tmp_path / "managed-admission", observation_provider=lambda: ResourceObservation(
            time.monotonic_ns(), 4, 4 << 30, 4 << 30, 100, 100, {"codex": 4, "claude": 4}, "fixture")))
    codex_roles = []
    codex_qualify = managed_qualification.qualify_managed_runtime

    def recorded(store, token, **kwargs):
        codex_roles.append(kwargs["role"])
        return codex_qualify(store, token, **kwargs)

    monkeypatch.setattr(managed_qualification, "qualify_managed_runtime", recorded)
    flags = ["--review-host", "claude", "--review-host-runtime-home", str(private / "template"),
             "--review-host-credential-source", str(credential), "--review-host-binary", str(binary),
             "--review-host-model-request", JUDGMENT, "--review-host-sandbox", "workspace-write",
             "--review-host-network", "disabled", "--review-host-token-reservation", "100",
             "--review-host-timeout", "30"]
    return SimpleNamespace(flags=flags, calls=calls, codex_roles=codex_roles, binary=binary, credential=credential)


def _launch_hosts(store) -> list[tuple[str, str, str]]:
    """``(role, launch key, host)`` of every native review and ordinary host launch, oldest first."""
    with store.read_transaction() as tx:
        rows = tx.execute(
            "SELECT b.role,k.idempotency_key,e.payload FROM authority_event_keys k JOIN control_events e "
            "ON e.id=k.event_id JOIN authority_child_bindings b ON b.activity_id=k.activity_id "
            "WHERE k.idempotency_key LIKE 'dispatch-request:%' ORDER BY e.id").fetchall()
    hosts = []
    for role, key, payload in rows:
        request = json.loads(payload)["data"]["request"]
        if "native_review_material" in request:
            host = request["native_review_material"]["native"]["host"]
        elif "codex_material" in request or "claude_material" in request:
            host = "codex" if "codex_material" in request else "claude"
        else:
            continue   # a sealed local check runs no host
        hosts.append((role, key.removeprefix("dispatch-request:"), host))
    return hosts


def _reviewer_runtime_schemas(store) -> list[str]:
    with store.read_transaction() as tx:
        return [json.loads(row[0])["schema"] for row in tx.execute(
            "SELECT r.receipt_json FROM authority_runtime_receipts r JOIN authority_child_bindings b "
            "ON b.activity_id=r.producer_activity_id WHERE b.role='reviewer' ORDER BY r.created_at")]


def _reviewer_requests(reviewer) -> set:
    """The Claude review host request each reviewer qualification received, field for field."""
    return {(request.binary, request.credential_source, request.model, request.token_reservation)
            for _role, _key, request in reviewer.calls}


def test_a_codex_outer_is_spec_reviewed_and_final_reviewed_natively_on_claude(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, check=CHECK_ALWAYS_PASSES, spec_review="accept")
    reviewer = _claude_reviewer(tmp_path, monkeypatch)
    assert w.run(*reviewer.flags) == 0
    led = ledger(w)
    assert led.stage == "DONE" and led.actions == SPEC_REVIEW_ACTIONS and (led.native, led.reviews) == (2, 1)
    assert [record["verdict"] for record in spec_review_records(w)] == ["accept"]
    hosts = _launch_hosts(led.store)
    reviews = [(key, host) for role, key, host in hosts if role == "reviewer"]
    assert [host for _key, host in reviews] == ["claude", "claude"]
    assert reviews[0][0].startswith("spec-review:") and reviews[1][0] == "final-review:launch"
    # The outer orchestrator ran on Codex; nothing but the two reviews ran on Claude.
    assert [(role, host) for role, _key, host in hosts if role != "reviewer"] == [("worker", "codex")]
    assert _reviewer_runtime_schemas(led.store) == [QUALIFIED_CLAUDE_RUNTIME_SCHEMA] * 2
    assert [role for role, _key, _request in reviewer.calls] == ["reviewer", "reviewer"]
    assert _reviewer_requests(reviewer) == {(str(reviewer.binary), str(reviewer.credential), "claude-opus-5", 100)}
    assert "reviewer" not in reviewer.codex_roles
    assert _held_resources(tmp_path) == {}
    # The identical request replays to the same facts without a new launch, grant or qualification.
    calls = len(reviewer.calls)
    assert w.run(*reviewer.flags) == 0
    again = ledger(w)
    assert (again.stage, again.actions, again.launches, again.native, again.reviews) == (
        led.stage, led.actions, led.launches, led.native, led.reviews)
    assert len(reviewer.calls) == calls


def test_repair_stays_on_the_codex_outer_while_the_final_review_runs_on_claude(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, repair=True)
    reviewer = _claude_reviewer(tmp_path, monkeypatch)
    assert w.run(*reviewer.flags) == 0
    led = assert_repaired_once(w)
    hosts = _launch_hosts(led.store)
    assert [(key, host) for role, key, host in hosts if role == "reviewer"] == [("final-review:launch", "claude")]
    repairs = [host for _role, key, host in hosts if key.startswith("repair:")]
    assert repairs == ["codex"]
    assert {host for role, _key, host in hosts if role != "reviewer"} == {"codex"}
    assert [role for role, _key, _request in reviewer.calls] == ["reviewer"]
    assert "reviewer" not in reviewer.codex_roles and "worker" in reviewer.codex_roles


def test_a_crash_after_the_claude_reviewer_qualified_resumes_on_claude(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, check=CHECK_ALWAYS_PASSES)
    reviewer = _claude_reviewer(tmp_path, monkeypatch, crash_first_reviewer=True)
    _crash(lambda: w.run(*reviewer.flags), w.authority)
    crashed = ledger(w)
    assert (crashed.stage, crashed.native, crashed.reviews) == ("FINAL_REVIEW", 0, 0)
    assert "final_review" not in crashed.actions
    # The same-key resume rebuilds the Claude reviewer seam: the earlier owner's reviewer is abandoned (F51) and
    # the next attempt qualifies and reviews on Claude again, once.
    assert w.run(*reviewer.flags) == 0
    facts = _assert_done_once(w.authority, w.repository_id)
    assert [(role, key) for role, key, _request in reviewer.calls] == [
        ("reviewer", "final-review:reviewer"), ("reviewer", "final-review:reviewer:2")]
    assert _reviewer_requests(reviewer) == {(str(reviewer.binary), str(reviewer.credential), "claude-opus-5", 100)}
    assert "reviewer" not in reviewer.codex_roles
    with facts.store.read_transaction() as tx:
        states = [tuple(row) for row in tx.execute(
            "SELECT a.request_key,a.state FROM authority_activities a JOIN authority_child_bindings b "
            "ON b.activity_id=a.id WHERE b.role='reviewer' ORDER BY a.created_at")]
    assert states == [("final-review:reviewer", "aborted"), ("final-review:reviewer:2", "succeeded")]
    assert [(key, host) for role, key, host in _launch_hosts(facts.store) if role == "reviewer"] == [
        ("final-review:launch", "claude")]
    assert _reviewer_runtime_schemas(facts.store) == [QUALIFIED_CLAUDE_RUNTIME_SCHEMA] * 2
    assert _held_resources(tmp_path) == {}


def test_without_a_review_host_request_the_reviews_stay_on_codex(tmp_path, monkeypatch):
    """Control: no ``--review-host*`` request, no Claude seam; the existing lifecycle as before."""
    w = world(tmp_path, monkeypatch, check=CHECK_ALWAYS_PASSES)
    reviewer = _claude_reviewer(tmp_path, monkeypatch)
    assert w.run() == 0
    led = ledger(w)
    assert led.stage == "DONE" and (led.native, led.reviews) == (1, 1)
    assert {host for _role, _key, host in _launch_hosts(led.store)} == {"codex"}
    assert reviewer.calls == [] and "reviewer" in reviewer.codex_roles
