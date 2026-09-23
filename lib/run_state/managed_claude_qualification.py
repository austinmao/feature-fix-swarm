"""Managed Claude qualification using the existing four-slot authority journal."""
from __future__ import annotations

from .run_policy import productive_work

import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import uuid

from host_capabilities import GsdSupervisorEnvironment

from .claude_host import ClaudeHostAdapter, ClaudeHostRefused, SUPPORTED_CLAUDE_VERSION
from .claude_qualification import (
    QUALIFICATION_PROBES, QualificationResult, prepare_claude_qualification_plan,
    publish_claude_qualification_results,
)
from .claude_runtime_staging import stage_private_claude_runtime
from .host_request import ClaudeHostRequest
from .supervisor import (
    ClaudeQualificationLaunchMaterial, DispatchRequest, Supervisor,
    SupervisorRefused, _gsd_wave_completion_code, _managed_command_requires_wave_proof,
    _managed_wave_prompt, finish_owned_wave_client,
)
from .wave_consumer import WaveConsumer
from .worker_channel import WorkerChannelServer
from .workspace import (
    _from_row, _verify_snapshot_complete, begin_child_workspace_preparation, inspect_workspace,
    load_input_snapshot, prepare_workspace,
)


_AUTHORITY_PROBES = ("ordinary", "native-positive", "native-negative", "native-multi-agent")
_PROBE_SLOTS = dict(zip(_AUTHORITY_PROBES, QUALIFICATION_PROBES, strict=True))


class ManagedClaudeQualificationRefused(RuntimeError):
    pass


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _replace_qualification_admission(path: Path, expected: dict[str, object],
                                     admitted: dict[str, object]) -> None:
    """Atomically replace the qualification placeholder with its fenced identity."""
    path = Path(path)
    expected_bytes = _canonical(expected) + b"\n"
    admitted_bytes = _canonical(admitted) + b"\n"
    temporary = path.parent / ("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        info = path.lstat()
        if (path.is_symlink() or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or path.read_bytes() != expected_bytes):
            raise ManagedClaudeQualificationRefused("ADMISSION_CONFLICT")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(admitted_bytes)
            output.flush()
            os.fsync(output.fileno())
        # The staging root is private and current-user owned. os.replace keeps
        # readers on either complete descriptor across the promotion boundary.
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except ManagedClaudeQualificationRefused:
        raise
    except OSError as error:
        raise ManagedClaudeQualificationRefused("ADMISSION_CONFLICT") from error
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def qualify_managed_claude_runtime(
    store, token, *, activity_id: str, activity_request_key: str,
    parent_activity_id: str, workspace, host_request: ClaudeHostRequest,
    role: str, evidence_root: Path, final_contract_hash: str, supervisor,
    bridge_command: str,
):
    """Stage, fence four Claude probes, promote, then commit one exact receipt."""
    if (
        type(host_request) is not ClaudeHostRequest or role not in {"worker", "reviewer"}
        or not isinstance(activity_id, str) or not activity_id
        or workspace.parent_activity_id != parent_activity_id
        or workspace.child_request_key != activity_request_key or not workspace.ready
    ):
        raise ManagedClaudeQualificationRefused("QUALIFICATION_INPUT_INVALID")
    with productive_work(store, token, kind="qualification"):
        # Known limit (fail-closed): Claude qualification is not replay-safe.  Probe
        # material carries fresh session ids and staging requires a new target, so
        # an activity retained after a crash between create_child_activity and the
        # outer launch cannot be re-qualified; resume refuses HOST_CAPABILITY_UNQUALIFIED.
        # Deferred until before native Claude qualification: persist the plan for
        # replay, as the Codex path's stage_or_reuse + retained observation does.
        runtime = Path(evidence_root) / "runtimes" / activity_id
        try:
            runtime.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            runtime.parent.chmod(0o700)
            stage_private_claude_runtime(
                Path(host_request.runtime_home), Path(host_request.credential_source), runtime, workspace.path,
            )
            additions = GsdSupervisorEnvironment(
                "ffs-supervised-process", "patches", str(runtime / "supervisor-admission.json"), bridge_command,
            )
            admission = Path(additions.admission_file)
            placeholder = {"schema": "ffs.supervisor-admission/v1", "available": True,
                           "repository_id": token.repository_id, "run_id": token.run_id,
                           "activity_id": activity_id, "generation": token.generation,
                           "workspace": str(workspace.path), "runtime_identity": "qualification"}
            fd = os.open(admission, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(placeholder, output, sort_keys=True, separators=(",", ":"))
                output.write("\n")
            plan = prepare_claude_qualification_plan(
                runtime, Path(host_request.binary), workspace.path, runtime / "qualification.json",
                version=SUPPORTED_CLAUDE_VERSION, model=host_request.model, effort=host_request.effort,
                gsd_environment=additions,
            )
        except (OSError, ValueError, ClaudeHostRefused) as error:
            raise ManagedClaudeQualificationRefused("QUALIFICATION_PREPARATION_INVALID") from error
        template = hashlib.sha256(plan.envelope_sha256.encode()).hexdigest()
        qualification_key = activity_request_key + ":qualification:" + activity_id
        contracts = {}
        for authority_name, probe in zip(_AUTHORITY_PROBES, plan.probes, strict=True):
            contracts[authority_name] = {
                "probe_name": authority_name,
                "command_sha256": _digest(probe.argv), "environment_sha256": _digest(probe.environment),
                "qualification_request_id": qualification_key + ":" + authority_name,
            }
        hashes = {name: _digest(contract) for name, contract in contracts.items()}
        envelope = {
            "schema": "ffs.qualification-envelope/v1", "qualification_cohort_id": qualification_key,
            "probes": [{"probe_name": name, "probe_contract_sha256": hashes[name]} for name in _AUTHORITY_PROBES],
            "runtime_template_sha256": template, "workspace_binding": str(workspace.path),
            "candidate_input_sha256": workspace.input_digest, "model": host_request.model,
            "effort": host_request.effort or "default", "sandbox": host_request.sandbox,
            "roots": [str(workspace.path)], "policy_sha256": final_contract_hash,
        }
        envelope_sha = _digest(envelope)
        try:
            activity = store.create_child_activity(
                token, parent_activity_id=parent_activity_id, role="inventory",
                request_key=activity_request_key, candidate_hash=workspace.input_digest,
                contract_hash=envelope_sha, runtime_identity=envelope_sha,
                workspace_binding=str(workspace.path), workspace_preparation_id=workspace.id,
                retry_budget=5, activity_id=activity_id,
            )
            if activity.state == "pending":
                activity = store.transition_activity(token, activity.id, expected="pending", new="active",
                                                     reason="Claude qualification inventory")
        except Exception as error:
            raise ManagedClaudeQualificationRefused("QUALIFICATION_BINDING_INVALID") from error
    results = []
    for authority_name, probe in zip(_AUTHORITY_PROBES, plan.probes, strict=True):
        request_key = qualification_key + ":" + authority_name
        contract = {
            "schema": "ffs.qualification-launch/v1", "probe_contract": contracts[authority_name],
            "qualification_envelope": envelope, "qualification_envelope_sha256": envelope_sha,
        }
        material_contract = _digest({
            "schema": "ffs.claude-qualification-probe/v1", "probe_name": probe.name,
            "argv_sha256": _digest(probe.argv), "environment_sha256": _digest(probe.environment),
            "runtime_home": str(runtime), "runtime_template_sha256": template,
        })
        material = ClaudeQualificationLaunchMaterial(
            probe.name, probe.argv, probe.environment, str(workspace.path), material_contract,
            # Qualification probes each have a separate, credential-scoped
            # Claude config directory.  The supervisor binds launch metadata
            # to that exact directory, rather than the shared staging root.
            envelope_sha, dict(probe.environment)["CLAUDE_CONFIG_DIR"], template,
            plan.model, plan.version, probe.session_id,
            probe.credential_path, probe.credential_sha256, probe.credential_device, probe.credential_inode,
        )
        request = DispatchRequest(
            activity_id=activity.id, request_key=request_key, command=probe.argv,
            workspace=str(workspace.path), expected_head=workspace.base_commit,
            runtime_identity=envelope_sha, token_reservation=host_request.token_reservation,
            contract_hash=envelope_sha, claude_qualification_material=material,
            managed_input_sha256=workspace.input_digest,
        )
        try:
            handle = supervisor.launch_qualification(request, qualification_contract=contract)
            completion = supervisor.finish(handle, timeout=probe.timeout_seconds)
            with productive_work(store, token, kind="qualification"):
                receipt = completion.get("host_receipt", {})
                if receipt.get("status") == "uncertain" or receipt.get("passed") is not True:
                    raise ManagedClaudeQualificationRefused("QUALIFICATION_UNCERTAIN")
                results.append(QualificationResult(
                    probe.name, Path(completion["streams"]["stdout"]["locator"]).read_text(),
                    Path(completion["streams"]["stderr"]["locator"]).read_text(), completion["returncode"],
                ))
        except (SupervisorRefused, OSError, KeyError, TypeError, ValueError) as error:
            raise ManagedClaudeQualificationRefused("QUALIFICATION_UNCERTAIN") from error
    with productive_work(store, token, kind="qualification"):
        try:
            qualified = publish_claude_qualification_results(plan, tuple(results))
            promoted = store.promote_qualified_activity(
                token, activity.id, qualification_request_key=qualification_key,
                expected_contract_hashes=hashes, runtime_identity=store.runtime_tuple_hash(qualified),
                final_contract_hash=final_contract_hash, role=role,
                observation_evidence={"locator": str(plan.output),
                                      "sha256": hashlib.sha256(plan.output.read_bytes()).hexdigest()},
            )
            if promoted.state == "pending":
                promoted = store.transition_activity(token, promoted.id, expected="pending", new="active",
                                                     reason="qualified Claude runtime admitted")
            receipt = store.commit_runtime_receipt(token, promoted.id, qualified)
            runtime_identity = store.runtime_tuple_hash(qualified)
            admitted = {**placeholder, "runtime_identity": runtime_identity}
            _replace_qualification_admission(admission, placeholder, admitted)
        except Exception as error:
            raise ManagedClaudeQualificationRefused("QUALIFICATION_PROMOTION_INVALID") from error
        return promoted, qualified, receipt, runtime, additions


def run_managed_claude_command(store, token, context, command, request_key, host_request, *, upstream_runtime=None,
                               model_request=None, acceptance_draft=None):
    """Production Claude callback; every probe and the final native run is supervised."""
    from .frontend_producers import drive_managed_session
    session = prepare_managed_claude_session(store, token, context, command, request_key, host_request,
                                             upstream_runtime=upstream_runtime, model_request=model_request)
    return drive_managed_session(store, token, context, session, acceptance_draft=acceptance_draft)


def prepare_managed_claude_session(store, token, context, command, request_key, host_request, *,
                                   upstream_runtime=None, model_request=None):
    """Qualification seams, worker channel and outer contract for one Claude host run."""
    import tempfile
    from .frontend_producers import HostRuntimeSeam, ManagedHostSession, QualifiedHostRuntime, retained_outer_activity
    from .supervisor import _managed_inventory_workspace, _managed_prompt

    root, operation, child_key, ready = _managed_inventory_workspace(
        store, token, context, request_key, workspace_api={
            "_from_row": _from_row, "load_input_snapshot": load_input_snapshot,
            "_verify_snapshot_complete": _verify_snapshot_complete,
            "begin_child_workspace_preparation": begin_child_workspace_preparation,
            "prepare_workspace": prepare_workspace, "inspect_workspace": inspect_workspace,
        })
    invocation, prompt, role = _managed_prompt(root, operation, command)
    host_evidence = Path(context.evidence_root) / "host"
    host_evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    bridge = Path(__file__).with_name("gsd_wave_bridge.py").resolve()
    if bridge.is_symlink() or not bridge.is_file():
        raise SupervisorRefused("HOST_CAPABILITY_UNQUALIFIED")
    bridge_command = json.dumps([str(Path(sys.executable).resolve()), str(bridge)], separators=(",", ":"))
    socket_root = Path(tempfile.mkdtemp(prefix="ffs-worker-", dir="/tmp")).resolve()
    channel = WorkerChannelServer(store, token, socket_root / "worker.sock")
    supervisor = Supervisor(store, token, evidence_root=host_evidence, worker_channel=channel)

    def qualify_runtime(activity_id: str, preparation, activity_request_key: str,
                        parent_activity_id: str, final_contract_hash: str, child_role: str):
        """Qualify one workspace-bound Claude runtime."""
        try:
            activity, qualified, receipt, _runtime, additions = qualify_managed_claude_runtime(
                store, token, activity_id=activity_id, activity_request_key=activity_request_key,
                parent_activity_id=parent_activity_id, workspace=preparation,
                host_request=host_request, role=child_role, evidence_root=host_evidence,
                final_contract_hash=final_contract_hash, supervisor=supervisor,
                bridge_command=bridge_command,
            )
            observed_version = dict(qualified.observation).get("version")
            if observed_version != SUPPORTED_CLAUDE_VERSION:
                raise ClaudeHostRefused("VERSION_INVALID")
            adapter = ClaudeHostAdapter(qualified, host_request.binary, observed_version)
        except (ManagedClaudeQualificationRefused, ClaudeHostRefused, OSError, ValueError) as error:
            raise SupervisorRefused("HOST_CAPABILITY_UNQUALIFIED") from error
        return QualifiedHostRuntime(activity, qualified, receipt, adapter, additions)

    def bind_launch(qualified, child_prompt: str, preparation, final_contract_hash: str, launch_request_key: str):
        try:
            launch_material = qualified.adapter.build_launch_material(
                child_prompt, attempt=1, session_id=str(uuid.uuid4()),
                gsd_environment=qualified.additions,
            )
        except (ClaudeHostRefused, OSError, ValueError) as error:
            raise SupervisorRefused("HOST_CAPABILITY_UNQUALIFIED") from error
        return DispatchRequest(
            activity_id=qualified.activity.id, request_key=launch_request_key,
            command=launch_material.argv, workspace=str(preparation.path),
            expected_head=preparation.base_commit,
            runtime_identity=store.runtime_tuple_hash(qualified.qualified),
            token_reservation=host_request.token_reservation,
            contract_hash=final_contract_hash, claude_material=launch_material,
            runtime_receipt_sha256=qualified.receipt.receipt_sha256,
            managed_input_sha256=preparation.input_digest,
        ), qualified.adapter

    def prepare_runtime(activity_id: str, preparation, activity_request_key: str,
                        parent_activity_id: str, final_contract_hash: str,
                        child_role: str, child_prompt: str, launch_request_key: str):
        qualified = qualify_runtime(activity_id, preparation, activity_request_key, parent_activity_id,
                                    final_contract_hash, child_role)
        return bind_launch(qualified, child_prompt, preparation, final_contract_hash, launch_request_key)

    def prepare_wave_child(wave_context):
        request, _adapter = prepare_runtime(
            wave_context.activity_id, wave_context.preparation,
            wave_context.request_key, wave_context.parent_activity_id,
            wave_context.contract_hash, "worker", _managed_wave_prompt(wave_context.plan["prompt"]),
            wave_context.request_key,
        )
        return request

    channel.start()
    wave_consumer = WaveConsumer(
        supervisor, prepare_wave_child,
        finish_timeout=host_request.timeout_seconds,
    )
    channel.attach_wave_consumer(wave_consumer)
    outer_activity_id = (retained_outer_activity(store, token, parent_activity_id=context.activity_id,
                                                 child_key=child_key) or str(uuid.uuid4()))
    stage_identity = hashlib.sha256((str(ready.path) + host_request.model).encode()).hexdigest()
    contract = _digest({"schema": "ffs.managed-claude-contract/v1", "command": list(invocation),
                        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                        "host_request": host_request.material(), "input_digest": ready.input_digest,
                        "workspace": str(ready.path), "stage_identity": stage_identity})

    def prepare_outer():
        return prepare_runtime(outer_activity_id, ready, child_key, context.activity_id, contract,
                               role, prompt, child_key + ":launch")

    def execute(request, _adapter, *, settle_success=True):
        activity = store.get_activity(request.activity_id)
        if upstream_runtime is not None:
            # Same as the Codex path: promotion rewrote child_role, so the
            # prelaunch capture must see the live preparation row.
            admitted = inspect_workspace(store, ready.id)
            supervisor.configure_managed_parent_resources(request, context, admitted, upstream_runtime)
        handle = supervisor.launch_managed_outer(request)
        result = finish_owned_wave_client(
            supervisor, handle, wave_consumer, timeout=host_request.timeout_seconds,
        )
        if result.get("host_receipt", {}).get("status") == "uncertain":
            store.transition_activity(token, activity.id, expected="active", new="paused",
                                      reason="qualified Claude telemetry is uncertain")
            raise SupervisorRefused("MALFORMED_TELEMETRY")
        wave_required = _managed_command_requires_wave_proof(invocation)
        if result["returncode"] == 0 and wave_required is not None:
            wave_refusal = _gsd_wave_completion_code(
                store, activity.id, handle.intent_id, require_wave=wave_required,
            )
            if wave_refusal == "WAVE_EXECUTION_REFUSED":
                store.transition_activity(
                    token, activity.id, expected="active", new="failed",
                    result=result["evidence"], reason="GSD wave execution was refused",
                )
                raise SupervisorRefused("WAVE_EXECUTION_REFUSED")
            if wave_refusal is not None:
                store.transition_activity(
                    token, activity.id, expected="active", new="failed",
                    result=result["evidence"], reason="GSD execution returned without supervised wave evidence",
                )
                raise SupervisorRefused("WAVE_EXECUTION_UNPROVEN")
        if result["returncode"] != 0 or settle_success:
            store.transition_activity(
                token, activity.id, expected="active", new="succeeded" if result["returncode"] == 0 else "failed",
                result=result["evidence"], reason="qualified Claude process settled",
            )
        return result["returncode"], handle, result

    def close(handle, adapter, material):
        channel.close()
        supervisor.contain_revoked()
        try:
            socket_root.rmdir()
        except OSError:
            pass
        if adapter is not None and material is not None and handle is not None and handle.process is not None and handle.process.poll() is not None:
            try:
                adapter.release_launch_material(material)
            except ClaudeHostRefused:
                pass

    seam = HostRuntimeSeam(
        host="claude", qualify=qualify_runtime, bind=bind_launch, binary=host_request.binary,
        cli_version=SUPPORTED_CLAUDE_VERSION, model=host_request.model, effort=host_request.effort,
        model_request=dict(model_request) if model_request is not None else {"kind": "exact", "id": host_request.model},
    )
    return ManagedHostSession(
        host="claude", supervisor=supervisor, evidence_root=host_evidence, ready=ready, child_key=child_key,
        outer_activity_id=outer_activity_id, invocation=invocation, timeout_seconds=host_request.timeout_seconds, seam=seam,
        prepare_outer=prepare_outer, execute=execute, close=close,
    )
