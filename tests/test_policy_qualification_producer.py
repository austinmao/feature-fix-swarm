"""Real configured qualification ingress; scripted probes, not native qualification."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import time

import pytest

from run_state.managed import prepare_managed_run
from run_state.supervisor import (
    Supervisor, SupervisorRefused, DispatchRequest,
    QualificationLaunchMaterial, ClaudeQualificationLaunchMaterial,
)
from run_state.workspace import begin_child_workspace_preparation, prepare_workspace
from test_managed_production_ingress import _setup
from test_qualification_launch_authority import PROBES
from test_wave_execution import git


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.mark.parametrize("host", ["codex", "claude"])
def test_configured_supervisor_qualification_binds_authority_and_native_material(tmp_path, monkeypatch, host):
    primary, authority, _, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    observed = []

    def execute(store, token, context):
        store.bind_runtime(token, context.activity_id, "b" * 64)
        pending = begin_child_workspace_preparation(
            store, token, parent_activity_id=context.activity_id, request_key="probe-workspace",
            role="inventory", base_commit=git(primary, "rev-parse", "HEAD"),
            selected_input_manifest={"entries": []}, repository_path=primary,
        )
        ready = prepare_workspace(store, token, pending)
        runtime = authority / "probe-runtime"
        runtime.mkdir(mode=0o700)
        scratch = ready.path / ".ffs-observer-tmp"
        scratch.mkdir()
        admission = runtime / "admission.json"
        admission.write_text("{}")
        admission.chmod(0o600)
        if host == "codex":
            usage = {key: 0 for key in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                                       "output_tokens", "reasoning_output_tokens")}
            stream = "\n".join(json.dumps(row) for row in (
                {"type": "thread.started", "thread_id": "scripted-probe"},
                {"type": "turn.started"}, {"type": "turn.completed", "usage": usage},
            ))
            environment = {
                "HOME": str(runtime), "CODEX_HOME": str(runtime), "TMPDIR": str(scratch),
                "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "NO_COLOR": "1",
                "GSD_DISPATCH_MODE": "ffs-supervised-process", "FFS_SUPERVISED_COMMIT_MODE": "patches",
                "FFS_SUPERVISED_ADMISSION_FILE": str(admission),
                "FFS_SUPERVISED_DISPATCH_COMMAND_JSON": json.dumps([
                    sys.executable, str(Path(__file__).resolve().parents[1] / "lib/run_state/gsd_wave_bridge.py")],
                    separators=(",", ":")),
                "FFS_HOOK_OBSERVATION": str(runtime / "hooks.jsonl"), "FFS_HOOK_NONCE": "probe-nonce",
            }
            probe_name = "ordinary"
        else:
            stream = json.dumps({"loggedIn": False})
            environment = {"CLAUDE_CONFIG_DIR": str(runtime), "PATH": "/usr/bin:/bin"}
            probe_name = "auth-negative"
        command = (sys.executable, "-c", "print(" + repr(stream) + ")")
        pairs = tuple(sorted(environment.items()))
        probes = {name: {"probe_name": name, "command_sha256": digest(command),
                         "environment_sha256": digest(pairs), "qualification_request_id": "probe:" + name}
                  for name in PROBES}
        envelope = {
            "schema": "ffs.qualification-envelope/v1", "qualification_cohort_id": "probe",
            "probes": [{"probe_name": name, "probe_contract_sha256": digest(probes[name])} for name in PROBES],
            "runtime_template_sha256": "3" * 64, "workspace_binding": str(ready.path),
            "candidate_input_sha256": ready.input_digest, "model": "fixture-model", "effort": "high",
            "sandbox": "workspace-write", "roots": [str(ready.path)], "policy_sha256": "9" * 64,
        }
        envelope_hash = digest(envelope)
        contract = {"schema": "ffs.qualification-launch/v1", "probe_contract": probes["ordinary"],
                    "qualification_envelope": envelope, "qualification_envelope_sha256": envelope_hash}
        native_hash = digest({"schema": "ffs.codex-qualification-probe/v1", "probe_name": probe_name,
                              "argv_sha256": digest(command), "environment_sha256": digest(pairs),
                              "cwd": str(ready.path), "runtime_home": str(runtime),
                              "runtime_template_sha256": "3" * 64})
        if host == "claude":
            native_hash = digest({"schema": "ffs.claude-qualification-probe/v1", "probe_name": probe_name,
                                  "argv_sha256": digest(command), "environment_sha256": digest(pairs),
                                  "runtime_home": str(runtime), "runtime_template_sha256": "3" * 64})
        values = dict(probe_name=probe_name, argv=command, environment=pairs, cwd=str(ready.path),
                      contract_sha256=native_hash, envelope_sha256=envelope_hash,
                      runtime_home=str(runtime), runtime_template_sha256="3" * 64)
        if host == "codex":
            material = QualificationLaunchMaterial(**values)
            field = "qualification_material"
        else:
            material = ClaudeQualificationLaunchMaterial(**values, model="fixture-model", version="fixture-1",
                        session_id=None, credential_path=None, credential_sha256=None,
                        credential_device=None, credential_inode=None)
            field = "claude_qualification_material"
        child = store.create_child_activity(
            token, parent_activity_id=context.activity_id, role="inventory", request_key="probe-child",
            candidate_hash=ready.input_digest, contract_hash=envelope_hash, runtime_identity=envelope_hash,
            workspace_binding=str(ready.path), workspace_preparation_id=ready.id, retry_budget=4,
        )
        store.transition_activity(token, child.id, expected="pending", new="active")
        request = DispatchRequest(child.id, "probe:ordinary", command, str(ready.path), ready.base_commit,
                                  envelope_hash, token_reservation=7, contract_hash=envelope_hash,
                                  managed_input_sha256=ready.input_digest, **{field: material})
        from run_state.managed_admission import ManagedAdmissionQueue
        from run_state.shared_resources import SharedResourceCoordinator, cold_start_demand
        from run_state.resource_observation import ResourceObservation
        queue = ManagedAdmissionQueue(authority / "shared-admission", observation_provider=lambda:
            ResourceObservation(time.monotonic_ns(), 8, 4 << 30, 4 << 30, 100, 100,
                                {"codex": 4, "claude": 4}, "fixture-measured-envelope"))
        coordinator = SharedResourceCoordinator(store, token, queue=queue)
        supervisor = Supervisor(store, token, evidence_root=authority / "probe-evidence",
                                shared_resource_coordinator=coordinator, resource_demand_policy=cold_start_demand)
        wrong = store.reserve_policy_action(token, action="qualification", logical_key=request.request_key,
                                             input_hash=native_hash)
        with pytest.raises(SupervisorRefused, match="POLICY_ACTION_BINDING_CONFLICT"):
            supervisor.launch_qualification(replace(request, policy_action_id=wrong.id), qualification_contract=contract)
        assert store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id).launch_charged == 0
        handle = supervisor.launch_qualification(request, qualification_contract=contract)
        result = supervisor.finish(handle, timeout=15)
        assert result["returncode"] == 0 and result["host_receipt"].get("status") != "uncertain"
        assert result["host_receipt"]["contract_sha256"] == native_hash
        with store.read_transaction() as tx:
            row = tx.execute("SELECT a.input_hash FROM authority_policy_actions a JOIN authority_policy_action_attempts p "
                             "ON p.action_id=a.id WHERE p.intent_id=?", (handle.intent_id,)).fetchone()
            assert row["input_hash"] == digest(probes["ordinary"]) != native_hash
        budget = store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id)
        assert budget.launch_charged == 1
        leases = queue.snapshot()
        assert len(leases) == 1 and leases[0]["launch_intent_id"] == handle.intent_id
        assert leases[0]["status"] == "released" and leases[0]["child_pid"] == handle.identity.pid
        observed.append(handle.intent_id)
        store.transition_activity(token, child.id, expected="active", new="succeeded", result=result["evidence"])
        return 0

    assert prepare_managed_run(
        objective="scripted qualification binding", state_root=authority,
        selection_manifest=env["FFS_SELECTION_MANIFEST"],
        upstream_runtime_manifest=env["FFS_UPSTREAM_RUNTIME_MANIFEST"],
        upstream_runtime_sha256=env["FFS_UPSTREAM_RUNTIME_SHA256"],
        request_key="qualification-binding", command=("/gsd-plan-phase", "1"),
        dispatch_limit=4, token_limit=100, worker_capacity=2, on_ready=execute,
        run_id="qualification-binding", activity="plan", scope="1",
    ) == 0
    assert len(observed) == 1
