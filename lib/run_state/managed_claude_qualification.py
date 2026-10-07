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
from .claude_runtime_staging import (
    STAGE_MANIFEST_NAME, RetainedClaudeRuntimeNotReusable, stage_or_reuse_private_claude_runtime,
)
from .host_request import ClaudeHostRequest
from .managed_qualification import (
    ManagedQualificationRefused, _completed_probe, _preparation_event, _read_result_stream,
)
from .ownership import OwnershipRefused
from .supervisor import (
    ClaudeQualificationLaunchMaterial, DispatchRequest, Supervisor,
    SupervisorRefused, _WAVE_FAILURE_REASONS, _gsd_wave_completion_code, _managed_command_requires_wave_proof,
    _managed_wave_prompt, _replayed_launch_refusal, _retained_runtime_refusal, finish_owned_wave_client,
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


def claude_runtime_home(evidence_root, activity_id: str) -> Path:
    """The one staged-runtime path for a Claude activity: qualification stages
    here, and the outer prompt's dispatch doc/script sit underneath it."""
    return Path(evidence_root) / "runtimes" / activity_id


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
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise ManagedClaudeQualificationRefused("ADMISSION_CONFLICT")
        current = path.read_bytes()
        if current == admitted_bytes:
            return  # a replay after the crash that followed the promotion: never rewrite it
        if current != expected_bytes:
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


def _publish_placeholder(path: Path, placeholder: dict[str, object], binding) -> None:
    """Create the qualification admission placeholder once; a replay accepts only what the journal allows.

    Before the promotion (no child binding, or still the inventory role) only the exact placeholder
    bytes are acceptable.  After it, the placeholder or the exact admitted descriptor carrying the
    binding's runtime_tuple_hash.  It is never rewritten here.
    """
    encoded = _canonical(placeholder) + b"\n"
    # ponytail: a kill between this open and the write leaves an empty file the replay refuses
    # (ADMISSION_CONFLICT, fail-closed); write-to-temp + link if that window ever bites.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        info = path.lstat()
        if (path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise ManagedClaudeQualificationRefused("ADMISSION_CONFLICT") from None
        accepted = {encoded}
        if binding is not None and binding["role"] != "inventory":
            accepted.add(_canonical({**placeholder, "runtime_identity": binding["runtime_tuple_hash"]}) + b"\n")
        if path.read_bytes() not in accepted:
            raise ManagedClaudeQualificationRefused("ADMISSION_CONFLICT") from None
        return
    with os.fdopen(fd, "wb") as output:
        output.write(encoded)


def _child_binding(store, activity_id: str):
    with store.read_transaction() as tx:
        return tx.execute(
            "SELECT a.request_key,a.runtime_tuple_hash,b.* FROM authority_activities a "
            "JOIN authority_child_bindings b ON b.activity_id=a.id WHERE a.id=?", (activity_id,),
        ).fetchone()


def _replayed_completion(store, activity_id: str, request_key: str):
    """A probe an earlier attempt already ran to completion, else None."""
    try:
        return _completed_probe(store, activity_id, request_key)
    except ManagedQualificationRefused as error:
        raise ManagedClaudeQualificationRefused(error.code) from error


def _stream_text(completion: dict, name: str, *, verified: bool) -> str:
    if not verified:
        return Path(completion["streams"][name]["locator"]).read_text()
    try:
        return _read_result_stream(completion, name)
    except ManagedQualificationRefused as error:
        raise ManagedClaudeQualificationRefused(error.code) from error


def qualify_managed_claude_runtime(
    store, token, *, activity_id: str, activity_request_key: str,
    parent_activity_id: str, workspace, host_request: ClaudeHostRequest,
    role: str, evidence_root: Path, final_contract_hash: str, supervisor,
    bridge_command: str, project: str | None = None, workstream: str | None = None,
):
    """Stage, fence four Claude probes, promote, then commit one exact receipt."""
    if (
        type(host_request) is not ClaudeHostRequest or role not in {"worker", "reviewer", "recovery"}
        or not isinstance(activity_id, str) or not activity_id
        or workspace.parent_activity_id != parent_activity_id
        or workspace.child_request_key != activity_request_key or not workspace.ready
    ):
        raise ManagedClaudeQualificationRefused("QUALIFICATION_INPUT_INVALID")
    with productive_work(store, token, kind="qualification"):
        # Replay-safe: the plan (session ids, sentinel suffix, probe credential identities; no
        # credential bytes) is persisted once, under qualification-preparation:<activity_id>, before the
        # activity exists.  A resume restages nothing, re-runs no completed probe and rewrites no
        # hash-bound byte.  A retained activity with no such record predates replay support: fail closed.
        runtime = claude_runtime_home(evidence_root, activity_id)
        preparation_key = "qualification-preparation:" + activity_id
        qualification_key = activity_request_key + ":qualification:" + activity_id
        retained_runtime = os.path.lexists(runtime)
        try:
            runtime.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            runtime.parent.chmod(0o700)
            stage_or_reuse_private_claude_runtime(
                Path(host_request.runtime_home), Path(host_request.credential_source), runtime, workspace.path,
            )
            additions = GsdSupervisorEnvironment(
                "ffs-supervised-process", "patches", str(runtime / "supervisor-admission.json"), bridge_command,
                project=project, workstream=workstream,
            )
            admission = Path(additions.admission_file)
            placeholder = {"schema": "ffs.supervisor-admission/v1", "available": True,
                           "repository_id": token.repository_id, "run_id": token.run_id,
                           "activity_id": activity_id, "generation": token.generation,
                           "workspace": str(workspace.path), "runtime_identity": "qualification"}
            retained = _preparation_event(store, token, parent_activity_id, preparation_key)
            existing = _child_binding(store, activity_id)
            if retained is None and existing is not None:
                raise ManagedClaudeQualificationRefused("QUALIFICATION_PREPARATION_INVALID")
            binding = {
                "schema": "ffs.claude-qualification-preparation/v1", "activity_id": activity_id,
                "activity_request_key": activity_request_key, "qualification_request_key": qualification_key,
                "workspace_preparation_id": workspace.id, "workspace": str(workspace.path),
                "candidate_input_sha256": workspace.input_digest,
                "runtime_stage_sha256": hashlib.sha256((runtime / STAGE_MANIFEST_NAME).read_bytes()).hexdigest(),
                "gsd_environment_sha256": _digest(additions.as_dict()),
                "host_request_sha256": _digest(host_request.material()),
                "role": role, "final_contract_hash": final_contract_hash,
            }
            if retained is not None and (
                    not retained_runtime or {key: retained.get(key) for key in binding} != binding):
                raise ManagedClaudeQualificationRefused("QUALIFICATION_PREPARATION_CONFLICT")
            _publish_placeholder(admission, placeholder, existing)
            plan = prepare_claude_qualification_plan(
                runtime, Path(host_request.binary), workspace.path, runtime / "qualification.json",
                version=SUPPORTED_CLAUDE_VERSION, model=host_request.model, effort=host_request.effort,
                gsd_environment=additions, seed=None if retained is None else retained.get("plan_seed"),
                resume=retained_runtime,
            )
            record = {**binding, "plan_envelope_sha256": plan.envelope_sha256, "plan_seed": plan.seed}
            if retained is None:
                store.record_event_once(token, parent_activity_id, preparation_key, record)
            elif {key: retained.get(key) for key in record} != record:
                raise ManagedClaudeQualificationRefused("QUALIFICATION_PREPARATION_CONFLICT")
        except (RetainedClaudeRuntimeNotReusable, ManagedClaudeQualificationRefused):
            raise
        except OwnershipRefused as error:
            raise ManagedClaudeQualificationRefused(
                "QUALIFICATION_PREPARATION_CONFLICT" if error.code == "IDEMPOTENCY_CONFLICT"
                else "QUALIFICATION_PREPARATION_INVALID") from error
        except (OSError, ValueError, ClaudeHostRefused, ManagedQualificationRefused) as error:
            raise ManagedClaudeQualificationRefused("QUALIFICATION_PREPARATION_INVALID") from error
        template = hashlib.sha256(plan.envelope_sha256.encode()).hexdigest()
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
            if existing is None or existing["role"] == "inventory":
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
            elif (existing["role"] == role and existing["request_key"] == activity_request_key
                    and existing["candidate_hash"] == workspace.input_digest
                    and existing["contract_hash"] == final_contract_hash
                    and existing["workspace_preparation_id"] == workspace.id):
                # Promoted by an earlier attempt: its retained evidence must still be there.
                if not os.path.lexists(plan.output):
                    raise ManagedClaudeQualificationRefused("QUALIFICATION_RESULT_INVALID")
                activity = store.get_activity(activity_id)
            else:
                raise ManagedClaudeQualificationRefused("QUALIFICATION_PREPARATION_CONFLICT")
        except ManagedClaudeQualificationRefused:
            raise
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
            completion = _replayed_completion(store, activity.id, request_key)
            replayed = completion is not None
            if completion is None:
                handle = supervisor.launch_qualification(request, qualification_contract=contract)
                completion = supervisor.finish(handle, timeout=probe.timeout_seconds)
            with productive_work(store, token, kind="qualification"):
                receipt = completion.get("host_receipt", {})
                if receipt.get("status") == "uncertain" or receipt.get("passed") is not True:
                    raise ManagedClaudeQualificationRefused("QUALIFICATION_UNCERTAIN")
                stdout = _stream_text(completion, "stdout", verified=replayed)
                stderr = _stream_text(completion, "stderr", verified=replayed)
                if replayed and tuple(receipt.get(key) for key in (
                        "schema", "probe_name", "contract_sha256", "envelope_sha256",
                        "runtime_template_sha256", "exit_code", "telemetry_sha256")) != (
                        "ffs.claude-qualification-invocation/v1", probe.name, material_contract, envelope_sha,
                        template, completion["returncode"], completion["streams"]["stdout"]["sha256"]):
                    raise ManagedClaudeQualificationRefused("QUALIFICATION_UNCERTAIN")
                results.append(QualificationResult(probe.name, stdout, stderr, completion["returncode"]))
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
                               model_request=None, acceptance_draft=None, review_host_request=None,
                               review_model_request=None):
    """Production Claude callback; every probe and the final native run is supervised."""
    from .frontend_producers import drive_managed_session
    session = prepare_managed_claude_session(store, token, context, command, request_key, host_request,
                                             upstream_runtime=upstream_runtime, model_request=model_request,
                                             review_host_request=review_host_request,
                                             review_model_request=review_model_request)
    return drive_managed_session(store, token, context, session, acceptance_draft=acceptance_draft)


def build_claude_runtime_seam(store, token, host_request, *, model_request, host_evidence: Path, upstream: dict,
                              child_key: str, outer_supervisor):
    """The Claude ``HostRuntimeSeam`` and the release of what it bound.

    Workspace-bound qualification and launch binding, unchanged for a Claude outer's orchestrator and wave workers.
    A Codex outer builds its opted-in Claude reviewer (D31) with the same pieces, from the reviewer's own request.
    Claude launch material is released by its consumer (the outer's session close, the native review's own
    release), so ``release()`` has nothing left to free.
    """
    from .frontend_producers import HostRuntimeSeam, QualifiedHostRuntime, retained_launch
    bridge = Path(__file__).with_name("gsd_wave_bridge.py").resolve()
    if bridge.is_symlink() or not bridge.is_file():
        raise SupervisorRefused("HOST_CAPABILITY_UNQUALIFIED")
    bridge_command = json.dumps([str(Path(sys.executable).resolve()), str(bridge)], separators=(",", ":"))

    def qualify_runtime(activity_id: str, preparation, activity_request_key: str,
                        parent_activity_id: str, final_contract_hash: str, child_role: str, *,
                        supervisor=None):
        """Qualify one workspace-bound Claude runtime (F50: the final reviewer passes its own supervisor)."""
        try:
            activity, qualified, receipt, _runtime, additions = qualify_managed_claude_runtime(
                store, token, activity_id=activity_id, activity_request_key=activity_request_key,
                parent_activity_id=parent_activity_id, workspace=preparation,
                host_request=host_request, role=child_role, evidence_root=host_evidence,
                final_contract_hash=final_contract_hash,
                supervisor=outer_supervisor if supervisor is None else supervisor,
                bridge_command=bridge_command,
                project=upstream.get("project"), workstream=upstream.get("workstream"),
            )
            observed_version = dict(qualified.observation).get("version")
            if observed_version != SUPPORTED_CLAUDE_VERSION:
                raise ClaudeHostRefused("VERSION_INVALID")
            adapter = ClaudeHostAdapter(qualified, host_request.binary, observed_version)
        except RetainedClaudeRuntimeNotReusable as error:
            raise SupervisorRefused(_retained_runtime_refusal(
                retained_launch(store, activity_id), outer=activity_request_key == child_key)) from error
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

    seam = HostRuntimeSeam(
        host="claude", qualify=qualify_runtime, bind=bind_launch, binary=host_request.binary,
        cli_version=SUPPORTED_CLAUDE_VERSION, model=host_request.model, effort=host_request.effort,
        model_request=dict(model_request) if model_request is not None else {"kind": "exact", "id": host_request.model},
    )
    return seam, lambda: None


def prepare_managed_claude_session(store, token, context, command, request_key, host_request, *,
                                   upstream_runtime=None, model_request=None, review_host_request=None,
                                   review_model_request=None):
    """Qualification seams, worker channel and outer contract for one Claude host run.

    An opted-in Codex reviewer (D31) gets its own seam from the Codex host's admission, staging and qualification.
    """
    import tempfile
    from .frontend_producers import (
        ManagedHostSession, rebind_retained_child, resumable_outer_completion, retained_launch,
        retained_outer_activity,
    )
    from .prelaunch_inventory import PrelaunchInventoryRefused, rebase_planning_root
    from .supervisor import _managed_inventory_workspace, _managed_prompt

    root, operation, child_key, ready = _managed_inventory_workspace(
        store, token, context, request_key, workspace_api={
            "_from_row": _from_row, "load_input_snapshot": load_input_snapshot,
            "_verify_snapshot_complete": _verify_snapshot_complete,
            "begin_child_workspace_preparation": begin_child_workspace_preparation,
            "prepare_workspace": prepare_workspace, "inspect_workspace": inspect_workspace,
        })
    host_evidence = Path(context.evidence_root) / "host"
    host_evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    outer_activity_id = (retained_outer_activity(store, token, parent_activity_id=context.activity_id,
                                                 child_key=child_key) or str(uuid.uuid4()))
    upstream = context.upstream or {}
    try:
        planning_root = str(rebase_planning_root(
            upstream, root_workspace=context.workspace, preparation_path=ready.path,
        ))
    except PrelaunchInventoryRefused as error:
        raise SupervisorRefused(str(error)) from error
    invocation, prompt, role = _managed_prompt(
        root, operation, command, staged_runtime_home=claude_runtime_home(host_evidence, outer_activity_id),
        planning_root=planning_root, project=upstream.get("project"),
        workstream=upstream.get("workstream"), planning_scope=token.planning_scope,
    )
    launch = retained_launch(store, outer_activity_id)
    # F51: a settled succeeded outer launch under a lifecycle at FINAL_REVIEW is resumed, never relaunched.
    resume = resumable_outer_completion(store, token, outer_activity_id, launch)
    if launch is not None and not resume:
        # A real outer launch holds Claude state in its home and may have done work: never re-stage,
        # re-qualify, relaunch or replay it as a success.  Refused before any resource is allocated.
        raise SupervisorRefused(_replayed_launch_refusal(launch))
    if resume:
        # Checks, recovery children and the final reviewer capture from, and parent under, the retained outer.
        ready = rebind_retained_child(store, token, outer_activity_id, ready.id)
    socket_root = Path(tempfile.mkdtemp(prefix="ffs-worker-", dir="/tmp")).resolve()
    channel = WorkerChannelServer(store, token, socket_root / "worker.sock")
    supervisor = Supervisor(store, token, evidence_root=host_evidence, worker_channel=channel)
    try:
        seam, _release = build_claude_runtime_seam(
            store, token, host_request, model_request=model_request, host_evidence=host_evidence,
            upstream=upstream, child_key=child_key, outer_supervisor=supervisor,
        )
        review_seam, review_release = None, None
        if review_host_request is not None:
            from .supervisor import build_codex_runtime_seam
            review_seam, review_release = build_codex_runtime_seam(
                store, token, review_host_request, model_request=review_model_request, host_evidence=host_evidence,
                upstream=upstream, child_key=child_key, outer_supervisor=supervisor,
            )
    except BaseException:
        # A refused seam (an opted-in Codex reviewer's admission) has bound nothing yet: free the channel.
        from .supervisor import _close_unused_worker_channel
        _close_unused_worker_channel(channel, socket_root)
        raise

    def prepare_runtime(activity_id: str, preparation, activity_request_key: str,
                        parent_activity_id: str, final_contract_hash: str,
                        child_role: str, child_prompt: str, launch_request_key: str):
        qualified = seam.qualify(activity_id, preparation, activity_request_key, parent_activity_id,
                                 final_contract_hash, child_role)
        return seam.bind(qualified, child_prompt, preparation, final_contract_hash, launch_request_key)

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
    stage_identity = hashlib.sha256((str(ready.path) + host_request.model).encode()).hexdigest()
    contract = _digest({"schema": "ffs.managed-claude-contract/v1", "command": list(invocation),
                        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                        "host_request": host_request.material(), "input_digest": ready.input_digest,
                        "workspace": str(ready.path), "stage_identity": stage_identity})

    def prepare_outer():
        if resume:
            # The resumed lifecycle binds the retained outer completion; it is never re-qualified.
            raise SupervisorRefused(_replayed_launch_refusal(launch))
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
            if wave_refusal is not None:
                code = wave_refusal if wave_refusal in _WAVE_FAILURE_REASONS else "WAVE_EXECUTION_UNPROVEN"
                store.transition_activity(
                    token, activity.id, expected="active", new="failed",
                    result=result["evidence"], reason=_WAVE_FAILURE_REASONS[code],
                )
                raise SupervisorRefused(code)
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
        if review_release is not None:
            # The Codex reviewer's bound material, through its own liveness proof (F43).
            review_release()

    return ManagedHostSession(
        host="claude", supervisor=supervisor, evidence_root=host_evidence, ready=ready, child_key=child_key,
        outer_activity_id=outer_activity_id, invocation=invocation, timeout_seconds=host_request.timeout_seconds, seam=seam,
        prepare_outer=prepare_outer, execute=execute, close=close, review_seam=review_seam,
        review_timeout_seconds=None if review_host_request is None else review_host_request.timeout_seconds,
    )
