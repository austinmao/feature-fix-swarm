"""Production managed callback reaches Supervisor with a qualified Codex tuple."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

import host_capabilities
from host_capabilities import QualifiedCodexRuntime, TELEMETRY_SCHEMA, _binary_chain
from process_identity import ProcessIdentity
from run_state.host_request import parse_codex_host_request
from run_state.managed import prepare_managed_run
from run_state.state import ControlStore
from run_state.supervisor import run_managed_command
import run_state.managed_qualification as managed_qualification
import run_state.runtime_staging as runtime_staging
from test_m4_upstream_context_acceptance import (
    _env, _git, _manifest, _register, _registered_runtime, _repository, _write_manifest,
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _setup(tmp_path):
    """Production shape: an anchored planning root with one frozen active-phase plan.

    ``run_managed_command`` launches the outer orchestrator only inside a parent
    resource group, and that group freezes the admitted phase's plan bytes from
    the registered upstream runtime.  Both are part of the real ingress contract
    (``cmd_managed_start``/``prepare_frontend_run`` always supply the runtime).
    """
    primary = _repository(tmp_path)
    phase = primary / ".planning" / "phases" / "01-fixture"
    phase.mkdir(parents=True, exist_ok=True)
    (phase / "01-01-PLAN.md").write_text("---\nphase: 01\nplan: 01\n---\nPlan\n")
    _git("add", ".planning/phases", cwd=primary)
    _git("commit", "-qm", "fixture frozen plan", cwd=primary)
    authority = tmp_path / "authority"
    repository_id = _register(primary, authority)
    selection = _write_manifest(tmp_path, _manifest(
        primary, repository_id,
        upstream={"project": None, "workstream": None, "session_key": "ingress-session"},
    ))
    runtime, digest = _registered_runtime()
    env = _env(tmp_path)
    env.update(
        PATH=str(Path(sys.executable).parent) + os.pathsep + env["PATH"],
        FFS_SELECTION_MANIFEST=str(selection), FFS_STATE_ROOT=str(authority),
        FFS_UPSTREAM_RUNTIME_MANIFEST=str(runtime), FFS_UPSTREAM_RUNTIME_SHA256=digest,
    )
    return primary, authority, repository_id, env


def test_managed_codex_dispatch_commits_receipt_before_supervised_spawn(tmp_path, monkeypatch):
    primary, authority, _repository_id, env = _setup(tmp_path)
    # The supervisor builds its shared coordinator on the machine-global
    # admission queue with the live observer.  Keep that production path but
    # give it an isolated root and a fixture observation, as the resource-group
    # suites do; otherwise the real host load decides whether the group admits.
    import run_state.shared_resources as shared_resources
    from run_state.managed_admission import ManagedAdmissionQueue
    from run_state.resource_observation import ResourceObservation
    monkeypatch.setattr(shared_resources, "ManagedAdmissionQueue", lambda *args, **kwargs: ManagedAdmissionQueue(
        tmp_path / "managed-admission", observation_provider=lambda:
        ResourceObservation(time.monotonic_ns(), 4, 4 << 30, 4 << 30, 100, 100, {"codex": 4}, "fixture")))
    runtime = tmp_path / "private-runtime"
    runtime.mkdir(mode=0o700)
    for directory in ("skills", "agents"):
        (runtime / directory).mkdir(mode=0o700)
        (runtime / directory / "fixture").write_text(directory)
    (runtime / "config.toml").write_text("qualified fixture\n")
    (runtime / "hooks.json").write_text("{}\n")
    (runtime / "auth.json").write_text("{}\n")
    (runtime / "auth.json").chmod(0o600)
    fake = tmp_path / "qualified-codex"
    fake.write_text(
        "#!/usr/bin/python3\n"
        "import json\n"
        "print(json.dumps({'type':'thread.started','thread_id':'managed-thread'}), flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':__import__('sys').argv[-1]}}), flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{"
        "'input_tokens':7,'cached_input_tokens':2,'cache_write_input_tokens':1,"
        "'output_tokens':3,'reasoning_output_tokens':2}}), flush=True)\n"
    )
    fake.chmod(0o700)

    def stage(_template, home, worktree):
        home = Path(home)
        if not home.exists():
            home.mkdir(mode=0o700)
            (home / "config.toml").write_text("qualified fixture\n")
            (home / "hooks.json").write_text("{}\n")
            (home / "auth.json").write_text("{}\n")
            (home / "auth.json").chmod(0o600)
            (home / runtime_staging.STAGE_MANIFEST_NAME).write_text(json.dumps({
                "target": {"home": {"path": str(home)}, "workspace": {"path": str(worktree)}},
            }) + "\n")
        return json.loads((home / runtime_staging.STAGE_MANIFEST_NAME).read_text())

    def qualify(store, token, *, activity_id, activity_request_key, parent_activity_id,
                workspace, runtime_home, binary, gsd_environment, host_request, role,
                evidence_root, final_contract_hash, supervisor, observer_module=None):
        del evidence_root, supervisor, observer_module
        home, worktree = Path(runtime_home), workspace.path
        admission = {
            "schema": "ffs.supervisor-admission/v1", "available": True,
            "repository_id": token.repository_id, "run_id": token.run_id,
            "activity_id": activity_id, "generation": token.generation,
            "workspace": str(worktree), "runtime_identity": "pending",
        }
        Path(gsd_environment.admission_file).write_text(json.dumps(admission) + "\n")
        Path(gsd_environment.admission_file).chmod(0o600)
        rinfo, winfo = home.stat(), worktree.stat()
        principal = ProcessIdentity.current()
        runtime_value = {
            "path": str(home), "device": rinfo.st_dev, "inode": rinfo.st_ino,
            "config_sha256": _digest(home / "config.toml"),
            "hooks_sha256": _digest(home / "hooks.json"),
            "skills_sha256": hashlib.sha256(b"skills").hexdigest(),
            "agents_sha256": hashlib.sha256(b"agents").hexdigest(),
            "gsd_core_sha256": hashlib.sha256(b"core").hexdigest(),
            "scripts_sha256": hashlib.sha256(b"scripts").hexdigest(),
            "gsd_manifest_sha256": hashlib.sha256(b"manifest").hexdigest(),
        }
        policy = host_capabilities.codex_closed_environment(
            home, home / "ffs-codex-policy-tmp", fake, _binary_chain(fake), gsd_environment,
        )
        qualified = QualifiedCodexRuntime(
            binary=tuple(sorted(_binary_chain(fake).items())),
            runtime=tuple(sorted(runtime_value.items())),
            workspace=tuple(sorted({"path": str(worktree), "device": winfo.st_dev, "inode": winfo.st_ino}.items())),
            supervisor=tuple(sorted({"host_id": principal.host_id, "boot_id": principal.boot_id,
                                     "pid": principal.pid, "start_token": principal.start_token}.items())),
            execution=tuple(sorted({"model": host_request.model, "effort": host_request.effort,
                                    "sandbox": host_request.sandbox,
                                    "network_enabled": host_request.network_enabled,
                                    "roots": [str(worktree)],
                                    "disabled_features": list(host_capabilities.DISABLED_NATIVE_FEATURES)}.items())),
            observation=tuple(sorted({"id": "a" * 32, "created_at_unix": time.time(),
                                      "environment_sha256": host_capabilities.codex_environment_policy_hash(policy),
                                      "telemetry_schema": TELEMETRY_SCHEMA}.items())),
        )
        runtime_identity = store.runtime_tuple_hash(qualified)
        admission["runtime_identity"] = runtime_identity
        Path(gsd_environment.admission_file).write_text(json.dumps(admission) + "\n")
        Path(gsd_environment.admission_file).chmod(0o600)
        with store.transaction() as tx:
            tx.execute(
                "UPDATE context_workspaces SET child_role=? WHERE preparation_id=?",
                (role, workspace.id),
            )
        activity = store.create_child_activity(
            token, parent_activity_id=parent_activity_id, role=role,
            request_key=activity_request_key, candidate_hash=workspace.input_digest,
            contract_hash=final_contract_hash, runtime_identity=runtime_identity,
            workspace_binding=str(worktree), workspace_preparation_id=workspace.id,
            retry_budget=1, activity_id=activity_id,
        )
        activity = store.transition_activity(
            token, activity.id, expected="pending", new="active", reason="fixture qualified",
        )
        receipt = store.commit_runtime_receipt(token, activity.id, qualified)
        return SimpleNamespace(
            activity=activity, qualified_runtime=qualified, runtime_receipt=receipt,
        )

    monkeypatch.setattr(runtime_staging, "stage_or_reuse_private_codex_runtime", stage)
    monkeypatch.setattr(managed_qualification, "qualify_managed_runtime", qualify)
    monkeypatch.setattr(host_capabilities, "admit_cli", lambda _binary: {"version": "0.154.0"})
    monkeypatch.chdir(primary)
    request = parse_codex_host_request(
        runtime_home=str(runtime), binary=str(fake),
        model_request_json='{"kind":"tier","name":"execution"}',
        sandbox="workspace-write", network_enabled=False,
        token_reservation=100, timeout_seconds=30,
    )

    def execute(store, token, context):
        from run_state.cli import _load_upstream_runtime
        # Same verified descriptor the CLI binds before its first mutation; the
        # parent resource group is not optional for a managed outer launch.
        upstream_runtime, _digest = _load_upstream_runtime(SimpleNamespace(
            upstream_runtime_manifest=env["FFS_UPSTREAM_RUNTIME_MANIFEST"],
            upstream_runtime_sha256=env["FFS_UPSTREAM_RUNTIME_SHA256"],
        ))
        return run_managed_command(
            store, token, context, command=("/gsd-plan-phase", "1"),
            request_key="qualified-dispatch", dispatch_limit=3, token_limit=1000,
            host_request=request, upstream_runtime=upstream_runtime,
        )

    assert prepare_managed_run(
        objective="qualified dispatch", state_root=authority,
        selection_manifest=env["FFS_SELECTION_MANIFEST"],
        upstream_runtime_manifest=env["FFS_UPSTREAM_RUNTIME_MANIFEST"],
        upstream_runtime_sha256=env["FFS_UPSTREAM_RUNTIME_SHA256"],
        request_key="qualified-dispatch", command=("/gsd-plan-phase", "1"),
        dispatch_limit=3, token_limit=1000, on_ready=execute,
        run_id="qualified-dispatch", activity="plan", scope="1",
        host_request=request,
    ) == 0
    store = ControlStore(authority / "control.sqlite3")
    with store.read_transaction() as tx:
        assert tx.execute("SELECT COUNT(*) FROM authority_runtime_receipts").fetchone()[0] == 1
        intent = tx.execute("SELECT * FROM authority_launch_intents").fetchone()
        activities = tx.execute(
            "SELECT state,result_json FROM authority_activities ORDER BY created_at,id"
        ).fetchall()
        limits = tx.execute("SELECT * FROM authority_run_limits").fetchone()
        event = tx.execute("SELECT payload FROM control_events WHERE event_type='launch_reserved'").fetchone()
    assert intent["completion_status"] == "succeeded"
    assert activities[0]["state"] == "succeeded"
    assert activities[-1]["state"] == "succeeded"
    assert activities[-1]["result_json"] is not None
    assert all(row["state"] != "active" for row in activities)
    assert limits["dispatch_used"] == 1 and limits["token_used"] == 13
    assert "runtime_receipt_sha256" in event["payload"]
    retained = Path(json.loads(intent["completion_evidence_json"])["locator"]).with_name("stdout.log").read_text()
    assert "FFS-supervised-process compatibility path is mandatory" in retained
    assert "outer orchestrator must never edit a plan's declared target files" in retained


def _retain_outer(store, token, context, *, launch):
    """Journal a retained outer activity for ``managed-host:retained-outer`` whose stage was consumed.

    ``launch`` is None (qualification only) or the (state, completion_status) of
    its real, non-qualification outer launch.
    """
    identity = ProcessIdentity.current()
    evidence = Path(context.evidence_root) / "outer-result.json"
    evidence.write_text("{}")
    completion = json.dumps({"locator": str(evidence), "sha256": _digest(evidence)})
    outer = "77777777-7777-4777-8777-777777777777"
    home = Path(context.evidence_root) / "host" / "runtimes" / outer
    home.mkdir(mode=0o700, parents=True)
    (home / runtime_staging.STAGE_MANIFEST_NAME).write_text("{}\n")
    with store.transaction() as tx:
        tx.execute(
            "INSERT INTO authority_activities (id,repository_id,run_id,kind,input_digest,revision,state,"
            "retry_budget,remaining_retry_budget,runtime_tuple_hash,request_key,generation,created_at,updated_at) "
            "VALUES(?,?,?,'execute',?,1000,?,5,0,?,'managed-host:retained-outer',?,'now','now')",
            (outer, token.repository_id, token.run_id, "c" * 64,
             "succeeded" if launch == ("completed_succeeded", "succeeded") else "active", "d" * 64,
             token.generation))
        tx.execute(
            "INSERT INTO authority_child_bindings (activity_id,parent_activity_id,role,candidate_hash,"
            "contract_hash,runtime_identity,workspace_binding,workspace_preparation_id,created_at) "
            "VALUES(?,?,'worker',?,?,?,'/unused','unused','now')",
            (outer, context.activity_id, "c" * 64, "e" * 64, "d" * 64))
        for ordinal, (state, status) in enumerate(
                [("completed_succeeded", "succeeded")] * 4 + ([] if launch is None else [launch]), start=1):
            intent = f"outer-intent-{ordinal}"
            tx.execute(
                "INSERT INTO authority_launch_intents (id,activity_id,attempt_ordinal,state,generation,"
                "capacity_exempt,child_host_id,child_boot_id,child_pid,child_start_token,created_at,updated_at,"
                "completion_status,completion_evidence_json) VALUES(?,?,?,?,?,1,?,?,?,?,'now','now',?,?)",
                (intent, outer, ordinal, state, token.generation, identity.host_id, identity.boot_id,
                 identity.pid, identity.start_token, status, None if status is None else completion))
            if ordinal <= 4:
                tx.execute(
                    "INSERT INTO authority_qualification_launches (intent_id,activity_id,request_key,probe_name,"
                    "contract_sha256,contract_json,qualification_envelope_sha256,managed_input_sha256,"
                    "qualification_cohort_id,qualification_request_id,created_at) "
                    "VALUES(?,?,?,'probe',?,'{}',?,?,'cohort',?,'now')",
                    (intent, outer, f"probe-{ordinal}", "1" * 64, "2" * 64, "c" * 64, f"probe-{ordinal}"))
    return home


@pytest.mark.parametrize(("launch", "variant", "code", "action"), [
    # Qualification alone consumed the stage: only then is a new request key the recovery.
    (None, None, "RETAINED_RUNTIME_NOT_REUSABLE", "resume_with_new_request_key"),
    # ... as when the outer child died before its permit: no work ran under the key.
    (("closed_dead", None), None, "RETAINED_RUNTIME_NOT_REUSABLE", "resume_with_new_request_key"),
    # A completed outer launch replays through its retained completion; nothing is re-staged.
    (("completed_succeeded", "succeeded"), None, None, None),
    # ... and if its home was pruned it is reported, never re-staged or relaunched.
    (("completed_succeeded", "succeeded"), "pruned", "REQUEST_ALREADY_COMPLETED", "inspect_completed_launch"),
    # ... and if its activity then failed (wave proof) it never replays as a success.
    (("completed_succeeded", "succeeded"), "failed", "REQUEST_ALREADY_COMPLETED", "inspect_completed_launch"),
    # An in-flight, uncertain or failed outer launch reaches the supervisor's replay refusals.
    (("released_to_execute", None), None, "INTENT_RECONCILIATION_REQUIRED", "reconcile_intent"),
    (("reconcile_required", None), None, "INTENT_RECONCILIATION_REQUIRED", "reconcile_intent"),
    (("completed_failed", "failed"), None, "REQUEST_ALREADY_COMPLETED", "inspect_completed_launch"),
])
def test_retained_outer_replay_never_re_stages_a_launched_runtime(tmp_path, monkeypatch, capsys,
                                                                   launch, variant, code, action):
    from run_state.cli import _managed_run_refusal, _managed_run_refusals

    primary, authority, _repository_id, env = _setup(tmp_path)
    runtime = tmp_path / "private-runtime"
    runtime.mkdir(mode=0o700)
    fake = tmp_path / "qualified-codex"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o700)
    staged = []

    def stage(_template, home, _worktree):
        # The retained outer home holds consumed auth and probe or Codex state.
        staged.append(Path(home))
        raise runtime_staging.RetainedRuntimeNotReusable("retained stage contains an unowned or missing file")

    monkeypatch.setattr(runtime_staging, "stage_or_reuse_private_codex_runtime", stage)
    monkeypatch.setattr(host_capabilities, "admit_cli", lambda _binary: {"version": "0.154.0"})
    monkeypatch.chdir(primary)
    request = parse_codex_host_request(
        runtime_home=str(runtime), binary=str(fake),
        model_request_json='{"kind":"tier","name":"execution"}',
        sandbox="workspace-write", network_enabled=False,
        token_reservation=100, timeout_seconds=30,
    )

    def execute(store, token, context):
        from run_state.cli import _load_upstream_runtime
        upstream_runtime, _digest = _load_upstream_runtime(SimpleNamespace(
            upstream_runtime_manifest=env["FFS_UPSTREAM_RUNTIME_MANIFEST"],
            upstream_runtime_sha256=env["FFS_UPSTREAM_RUNTIME_SHA256"],
        ))
        home = _retain_outer(store, token, context, launch=launch)
        if variant == "pruned":
            (home / runtime_staging.STAGE_MANIFEST_NAME).unlink()
        if variant == "failed":
            with store.transaction() as tx:
                tx.execute("UPDATE authority_activities SET state='failed' WHERE id=?", (home.name,))
        try:
            return run_managed_command(
                store, token, context, command=("/gsd-plan-phase", "1"),
                request_key="retained-outer", dispatch_limit=3, token_limit=1000,
                host_request=request, upstream_runtime=upstream_runtime,
            )
        except _managed_run_refusals() as error:
            return _managed_run_refusal(error, run_id=context.run_id)

    returncode = prepare_managed_run(
        objective="retained outer", state_root=authority,
        selection_manifest=env["FFS_SELECTION_MANIFEST"],
        upstream_runtime_manifest=env["FFS_UPSTREAM_RUNTIME_MANIFEST"],
        upstream_runtime_sha256=env["FFS_UPSTREAM_RUNTIME_SHA256"],
        request_key="retained-outer", command=("/gsd-plan-phase", "1"),
        dispatch_limit=3, token_limit=1000, on_ready=execute,
        run_id="retained-outer", activity="plan", scope="1", host_request=request,
    )
    if code is None:
        assert returncode == 0 and staged == []
        return
    body = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert returncode == 78 and (body["code"], body["recovery_action"]["action"]) == (code, action)
    assert len(staged) == (1 if launch is None else 0)


@pytest.mark.parametrize(("launch", "outer", "code"), [
    (None, True, "RETAINED_RUNTIME_NOT_REUSABLE"),
    # A new request key starts a new outer run: never the recovery for a wave child or reviewer.
    (None, False, "CHILD_RUNTIME_NOT_REUSABLE"),
    # A child that died before its permit ran nothing: treated as no launch.
    ({"state": "closed_dead"}, True, "RETAINED_RUNTIME_NOT_REUSABLE"),
    ({"state": "closed_dead"}, False, "CHILD_RUNTIME_NOT_REUSABLE"),
    ({"state": "completed_succeeded"}, False, "REQUEST_ALREADY_COMPLETED"),
    ({"state": "released_to_execute"}, True, "INTENT_RECONCILIATION_REQUIRED"),
])
def test_consumed_runtime_refusal_names_a_new_request_key_only_for_the_outer_child(launch, outer, code):
    from run_state.supervisor import _retained_runtime_refusal

    assert _retained_runtime_refusal(launch, outer=outer) == code
