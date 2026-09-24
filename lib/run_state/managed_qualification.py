"""One production path from an unqualified private Codex home to a receipt.

The observer describes probes but never starts them.  This module binds that
description to the durable inventory activity and sends every process through
``Supervisor.launch_qualification``.
"""
from __future__ import annotations

from .run_policy import productive_work

from dataclasses import dataclass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import types
import uuid

from host_capabilities import (
    GsdSupervisorEnvironment,
    QualifiedCodexRuntime,
    codex_environment_policy_hash,
    verify_runtime,
)

from .host_request import CodexHostRequest
from .ownership import OwnershipRefused
from .runtime_staging import (
    STAGE_MANIFEST_NAME,
    RuntimeStagingError,
    validate_staged_private_codex_runtime,
)
from .supervisor import DispatchRequest, QualificationLaunchMaterial
from .workspace import WorkspacePreparation


_PROBES = ("ordinary", "native-positive", "native-negative", "native-multi-agent")
_SHA256 = frozenset("0123456789abcdef")


class ManagedQualificationRefused(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ManagedQualificationBundle:
    activity: object
    runtime_receipt: object
    qualified_runtime: object
    qualification_request_key: str
    qualification_envelope_sha256: str
    probe_contract_sha256: tuple[tuple[str, str], ...]
    admission: dict[str, object]
    observation: dict[str, object]
    results: tuple[object, ...]


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _SHA256


def _qualified(value: object) -> QualifiedCodexRuntime:
    try:
        payload = value.to_dict()
    except (AttributeError, TypeError, ValueError) as exc:
        raise ManagedQualificationRefused("OBSERVER_INVALID") from exc
    fields = ("binary", "runtime", "workspace", "supervisor", "execution", "observation")
    if (not isinstance(payload, dict) or payload.get("status") != "admitted"
            or any(not isinstance(payload.get(field), dict) for field in fields)):
        raise ManagedQualificationRefused("OBSERVER_INVALID")
    return QualifiedCodexRuntime(**{
        field: tuple(sorted(payload[field].items())) for field in fields
    })


def _observer_module() -> types.ModuleType:
    here = Path(__file__).resolve()
    candidates = (
        here.parents[2] / "scripts" / "gsd" / "codex-runtime-observer.py",
        here.parents[1] / "scripts" / "gsd" / "codex-runtime-observer.py",
    )
    for candidate in candidates:
        try:
            info = candidate.lstat()
        except OSError:
            continue
        if candidate.is_symlink() or not stat.S_ISREG(info.st_mode):
            continue
        spec = importlib.util.spec_from_file_location("ffs_managed_runtime_observer", candidate)
        if spec is None or spec.loader is None:
            break
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    raise ManagedQualificationRefused("OBSERVER_UNAVAILABLE")


def _validate_stage(runtime: Path, workspace: Path) -> None:
    try:
        info = runtime.lstat()
        manifest_path = runtime / STAGE_MANIFEST_NAME
        manifest_info = manifest_path.lstat()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ManagedQualificationRefused("RUNTIME_STAGE_INVALID") from exc
    target = manifest.get("target") if isinstance(manifest, dict) else None
    home = target.get("home") if isinstance(target, dict) else None
    bound_workspace = target.get("workspace") if isinstance(target, dict) else None
    if (
        runtime.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
        or manifest_path.is_symlink() or not stat.S_ISREG(manifest_info.st_mode)
        or manifest_info.st_uid != os.getuid() or stat.S_IMODE(manifest_info.st_mode) != 0o600
        or not isinstance(home, dict) or home.get("path") != str(runtime)
        or not isinstance(bound_workspace, dict) or bound_workspace.get("path") != str(workspace)
    ):
        raise ManagedQualificationRefused("RUNTIME_STAGE_INVALID")
    try:
        # Completed qualification replays retain supervisor-owned transcripts,
        # invocation receipts, and the observation beside the immutable staged
        # closure. The validator still rehashes every stage-owned byte.
        validate_staged_private_codex_runtime(
            runtime, workspace, allow_additional_evidence=True,
        )
    except RuntimeStagingError as exc:
        raise ManagedQualificationRefused("RUNTIME_STAGE_INVALID") from exc


def _publish_admission(path: Path, descriptor: dict[str, object]) -> None:
    encoded = _canonical(descriptor) + b"\n"
    path = Path(path)
    if not path.is_absolute() or path.parent.is_symlink() or not path.parent.is_dir():
        raise ManagedQualificationRefused("ADMISSION_PATH_INVALID")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        try:
            info = path.lstat()
            current = path.read_bytes()
        except OSError as exc:
            raise ManagedQualificationRefused("ADMISSION_CONFLICT") from exc
        if (path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or current != encoded):
            raise ManagedQualificationRefused("ADMISSION_CONFLICT")
        return
    except OSError as exc:
        raise ManagedQualificationRefused("ADMISSION_PATH_INVALID") from exc
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _read_result_stream(result: dict, name: str) -> str:
    stream = result.get("streams", {}).get(name)
    if not isinstance(stream, dict) or set(stream) != {"locator", "sha256", "bytes"}:
        raise ManagedQualificationRefused("QUALIFICATION_RESULT_INVALID")
    path = Path(stream["locator"])
    try:
        info = path.lstat()
        raw = path.read_bytes()
    except (OSError, TypeError) as exc:
        raise ManagedQualificationRefused("QUALIFICATION_RESULT_INVALID") from exc
    if (not path.is_absolute() or path.is_symlink() or not stat.S_ISREG(info.st_mode)
            or len(raw) != stream["bytes"]
            or hashlib.sha256(raw).hexdigest() != stream["sha256"]):
        raise ManagedQualificationRefused("QUALIFICATION_RESULT_INVALID")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManagedQualificationRefused("QUALIFICATION_RESULT_INVALID") from exc


def _preparation_event(store, token, parent_activity_id: str, key: str):
    with store.read_transaction() as tx:
        row = tx.execute(
            "SELECT e.payload FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id "
            "WHERE k.activity_id=? AND k.idempotency_key=?",
            (parent_activity_id, key),
        ).fetchone()
    if row is None:
        return None
    try:
        wrapped = json.loads(row["payload"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ManagedQualificationRefused("QUALIFICATION_PREPARATION_INVALID") from exc
    if (not isinstance(wrapped, dict) or wrapped.get("run_id") != token.run_id
            or wrapped.get("activity_id") != parent_activity_id
            or not isinstance(wrapped.get("data"), dict)):
        raise ManagedQualificationRefused("QUALIFICATION_PREPARATION_INVALID")
    return wrapped["data"]


def _completed_probe(store, activity_id: str, request_key: str):
    with store.read_transaction() as tx:
        row = tx.execute(
            "SELECT i.state,i.completion_evidence_json FROM authority_qualification_launches q "
            "JOIN authority_launch_intents i ON i.id=q.intent_id "
            "WHERE q.activity_id=? AND q.request_key=?",
            (activity_id, request_key),
        ).fetchone()
    if row is None:
        return None
    if row["state"] != "completed_succeeded":
        if row["state"] == "completed_failed":
            raise ManagedQualificationRefused("QUALIFICATION_FAILED")
        raise ManagedQualificationRefused("INTENT_RECONCILIATION_REQUIRED")
    try:
        evidence = json.loads(row["completion_evidence_json"])
        path = Path(evidence["locator"])
        raw = path.read_bytes()
        result = json.loads(raw)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ManagedQualificationRefused("QUALIFICATION_RESULT_INVALID") from exc
    if (path.is_symlink() or not path.is_absolute()
            or hashlib.sha256(raw).hexdigest() != evidence.get("sha256")
            or not isinstance(result, dict)):
        raise ManagedQualificationRefused("QUALIFICATION_RESULT_INVALID")
    return result


def _remove_created_scratch(scratch: Path, workspace: Path, identity: tuple[int, int] | None) -> None:
    """Remove the probe TMPDIR only if it is still the directory the plan created; never raise."""
    try:
        info = scratch.lstat()
        if (identity is None or scratch.parent != workspace or not stat.S_ISDIR(info.st_mode)
                or (info.st_dev, info.st_ino) != identity):
            return
        shutil.rmtree(scratch)
    except OSError:
        return


def qualify_managed_runtime(
    store, token, *, activity_id: str, activity_request_key: str,
    parent_activity_id: str, workspace: WorkspacePreparation,
    runtime_home: Path, binary: Path, gsd_environment: GsdSupervisorEnvironment,
    host_request: CodexHostRequest, role: str, evidence_root: Path,
    final_contract_hash: str, supervisor, observer_module=None,
) -> ManagedQualificationBundle:
    """Qualify and promote one already-registered inventory workspace."""
    with productive_work(store, token, kind="qualification"):
        runtime, executable, evidence = (
            Path(runtime_home), Path(binary), Path(evidence_root),
        )
        try:
            canonical_activity_id = str(uuid.UUID(activity_id)) == activity_id
        except (AttributeError, TypeError, ValueError):
            canonical_activity_id = False
        if (
            not canonical_activity_id
            or not isinstance(activity_request_key, str) or not activity_request_key
            or not isinstance(role, str) or role not in {"worker", "reviewer"}
            or type(workspace) is not WorkspacePreparation or not workspace.ready
            or workspace.state != "ready" or workspace.child_role not in {"inventory", role}
            or workspace.parent_activity_id != parent_activity_id
            or workspace.child_request_key != activity_request_key
            or workspace.repository_id != token.repository_id or workspace.run_id != token.run_id
            or workspace.generation != token.generation
            or workspace.path.resolve() != workspace.path
            or type(host_request) is not CodexHostRequest
            or host_request.runtime_home != str(runtime) or host_request.binary != str(executable)
            or not _sha256(final_contract_hash)
            or type(gsd_environment) is not GsdSupervisorEnvironment
            or not evidence.is_absolute() or evidence.resolve() != evidence
            or getattr(supervisor, "store", store) is not store
            or getattr(supervisor, "token", token) != token
            or Path(getattr(supervisor, "evidence_root", evidence)) != evidence
        ):
            raise ManagedQualificationRefused("QUALIFICATION_INPUT_INVALID")
        if (not executable.is_absolute() or executable.is_symlink() or not executable.is_file()
                or not os.access(executable, os.X_OK)):
            raise ManagedQualificationRefused("QUALIFICATION_INPUT_INVALID")
        _validate_stage(runtime, workspace.path)
        admission_path = Path(gsd_environment.admission_file)
        module = observer_module or _observer_module()
        required = {
            "QualificationSeed", "prepare_observer_skill", "prepare_qualification_seed",
            "preview_qualification_runtime",
            "prepare_qualification_plan", "preview_qualified_runtime",
            "publish_qualification_results", "QualificationResult",
        }
        if any(not hasattr(module, name) for name in required):
            raise ManagedQualificationRefused("OBSERVER_INVALID")

        qualification_key = activity_request_key + ":qualification:" + activity_id
        preparation_key = "qualification-preparation:" + activity_id
        observation_path = runtime / "runtime-observation.json"
        resume_evidence = os.path.lexists(observation_path)
        preparation_binding = {
            "schema": "ffs.qualification-preparation/v1", "activity_id": activity_id,
            "activity_request_key": activity_request_key, "qualification_request_key": qualification_key,
            "workspace_preparation_id": workspace.id, "workspace": str(workspace.path),
            "candidate_input_sha256": workspace.input_digest,
            "runtime_stage_sha256": hashlib.sha256(
                (runtime / STAGE_MANIFEST_NAME).read_bytes(),
            ).hexdigest(),
            "binary_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "gsd_environment_sha256": _digest(gsd_environment.as_dict()),
            "host_request_sha256": _digest(host_request.material()),
            "role": role, "final_contract_hash": final_contract_hash,
        }
        retained = _preparation_event(store, token, parent_activity_id, preparation_key)
        created_preparation = False
        if retained is None:
            seed = module.prepare_qualification_seed(runtime)
            candidate = {
                **preparation_binding, "nonce": seed.nonce,
                "observation_created_at_unix": seed.observation_created_at_unix,
                "skill_token_sha256": hashlib.sha256(seed.skill_token.encode()).hexdigest(),
            }
            try:
                store.record_event_once(token, parent_activity_id, preparation_key, candidate)
                retained = candidate
                created_preparation = True
            except OwnershipRefused as exc:
                if exc.code != "IDEMPOTENCY_CONFLICT":
                    raise
                retained = _preparation_event(store, token, parent_activity_id, preparation_key)
                if retained is None:
                    raise ManagedQualificationRefused("QUALIFICATION_PREPARATION_INVALID") from exc
        if not created_preparation:
            stable = {key: retained.get(key) for key in preparation_binding}
            if stable != preparation_binding:
                raise ManagedQualificationRefused("QUALIFICATION_PREPARATION_CONFLICT")
            skill_token = module.prepare_observer_skill(runtime)
            if (not isinstance(retained.get("nonce"), str)
                    or not isinstance(retained.get("observation_created_at_unix"), (int, float))
                    or retained.get("skill_token_sha256") != hashlib.sha256(skill_token.encode()).hexdigest()):
                raise ManagedQualificationRefused("QUALIFICATION_PREPARATION_INVALID")
            seed = module.QualificationSeed(
                retained["nonce"], skill_token, retained["observation_created_at_unix"],
            )
        preview_plan = module.prepare_qualification_plan(
            runtime, executable, workspace.path, observation_path,
            host_request.timeout_seconds, model=host_request.model, effort=host_request.effort,
            sandbox=host_request.sandbox, network_enabled=host_request.network_enabled,
            roots=[str(workspace.path)], gsd_environment=gsd_environment, seed=seed, preview=True,
            allow_existing_evidence=resume_evidence,
        )
        if tuple(probe.name for probe in preview_plan.probes) != _PROBES:
            raise ManagedQualificationRefused("OBSERVER_INVALID")
        predicted = _qualified(module.preview_qualification_runtime(
            seed, runtime, executable, workspace.path,
            model=host_request.model, effort=host_request.effort,
            sandbox=host_request.sandbox, network_enabled=host_request.network_enabled,
            roots=[str(workspace.path)], gsd_environment=gsd_environment,
        ))
        runtime_identity = store.runtime_tuple_hash(predicted)
        plan = preview_plan
        runtime_template_sha256 = hashlib.sha256(preview_plan.runtime_identity.encode("utf-8")).hexdigest()

        probe_contracts: dict[str, dict[str, object]] = {}
        probe_hashes: dict[str, str] = {}
        supervisor_contract_hashes: dict[str, str] = {}
        for probe in plan.probes:
            request_key = qualification_key + ":" + probe.name
            probe_contract = {
                "probe_name": probe.name,
                "command_sha256": _digest(probe.argv),
                "environment_sha256": _digest(probe.environment),
                "qualification_request_id": request_key,
            }
            probe_contracts[probe.name] = probe_contract
            probe_hashes[probe.name] = _digest(probe_contract)
            supervisor_contract_hashes[probe.name] = _digest({
                "schema": "ffs.codex-qualification-probe/v1",
                "probe_name": probe.name,
                "argv_sha256": _digest(probe.argv),
                "environment_sha256": _digest(probe.environment),
                "cwd": str(workspace.path), "runtime_home": str(runtime),
                "runtime_template_sha256": runtime_template_sha256,
            })
        envelope = {
            "schema": "ffs.qualification-envelope/v1",
            "qualification_cohort_id": qualification_key,
            "probes": [
                {"probe_name": name, "probe_contract_sha256": probe_hashes[name]}
                for name in _PROBES
            ],
            "runtime_template_sha256": runtime_template_sha256,
            "workspace_binding": str(workspace.path),
            "candidate_input_sha256": workspace.input_digest,
            "model": host_request.model, "effort": host_request.effort,
            "sandbox": host_request.sandbox, "roots": [str(workspace.path)],
            "policy_sha256": final_contract_hash,
        }
        envelope_sha256 = _digest(envelope)
        with store.read_transaction() as tx:
            existing_binding = tx.execute(
                "SELECT a.request_key,a.runtime_tuple_hash,b.* FROM authority_activities a "
                "JOIN authority_child_bindings b ON b.activity_id=a.id WHERE a.id=?",
                (activity_id,),
            ).fetchone()
        if existing_binding is None or existing_binding["role"] == "inventory":
            activity = store.create_child_activity(
                token, parent_activity_id=parent_activity_id, role="inventory",
                request_key=activity_request_key, candidate_hash=workspace.input_digest,
                contract_hash=envelope_sha256, runtime_identity=envelope_sha256,
                workspace_binding=str(workspace.path), workspace_preparation_id=workspace.id,
                # Four qualification attempts plus the promoted production launch.
                retry_budget=len(_PROBES) + 1, activity_id=activity_id,
            )
        elif (
            existing_binding["role"] == role
            and existing_binding["request_key"] == activity_request_key
            and existing_binding["candidate_hash"] == workspace.input_digest
            and existing_binding["contract_hash"] == final_contract_hash
            and existing_binding["runtime_identity"] == runtime_identity
            and existing_binding["workspace_preparation_id"] == workspace.id
        ):
            if not resume_evidence:
                raise ManagedQualificationRefused("QUALIFICATION_RESULT_INVALID")
            activity = store.get_activity(activity_id)
        else:
            raise ManagedQualificationRefused("QUALIFICATION_PREPARATION_CONFLICT")
        admission = {
            "schema": "ffs.supervisor-admission/v1", "available": True,
            "repository_id": token.repository_id, "run_id": token.run_id,
            "activity_id": activity_id, "generation": token.generation,
            "workspace": str(workspace.path), "runtime_identity": runtime_identity,
        }
        _publish_admission(admission_path, admission)
        exact_plan = module.prepare_qualification_plan(
            runtime, executable, workspace.path, observation_path,
            host_request.timeout_seconds, model=host_request.model, effort=host_request.effort,
            sandbox=host_request.sandbox, network_enabled=host_request.network_enabled,
            roots=[str(workspace.path)], gsd_environment=gsd_environment, seed=seed,
            allow_existing_evidence=resume_evidence,
        )
        preview_binding = _digest({
            "runtime": plan.runtime_identity, "binary": plan.binary_identity,
            "workspace": plan.workspace_identity, "policy": plan.policy_environment,
            "probes": [(item.name, item.argv, item.environment) for item in plan.probes],
        })
        exact_binding = _digest({
            "runtime": exact_plan.runtime_identity, "binary": exact_plan.binary_identity,
            "workspace": exact_plan.workspace_identity, "policy": exact_plan.policy_environment,
            "probes": [(item.name, item.argv, item.environment) for item in exact_plan.probes],
        })
        if preview_binding != exact_binding:
            raise ManagedQualificationRefused("QUALIFICATION_PREPARATION_CONFLICT")
        plan = exact_plan
        owned_scratch = (getattr(preview_plan, "scratch_identity", None)
                         or getattr(exact_plan, "scratch_identity", None))
        planned_policy = codex_environment_policy_hash(dict(plan.policy_environment))
        if (store.runtime_tuple_hash(_qualified(module.preview_qualified_runtime(plan))) != runtime_identity
                or dict(predicted.observation).get("environment_sha256") != planned_policy):
            raise ManagedQualificationRefused("ENVIRONMENT_POLICY_CHANGED")

    results = []
    for probe in plan.probes:
        name = probe.name
        request_key = qualification_key + ":" + name
        contract = {
            "schema": "ffs.qualification-launch/v1",
            "probe_contract": probe_contracts[name],
            "qualification_envelope": envelope,
            "qualification_envelope_sha256": envelope_sha256,
        }
        material = QualificationLaunchMaterial(
            probe_name=name, argv=probe.argv, environment=probe.environment,
            cwd=str(workspace.path), contract_sha256=supervisor_contract_hashes[name],
            envelope_sha256=envelope_sha256, runtime_home=str(runtime),
            runtime_template_sha256=runtime_template_sha256,
        )
        request = DispatchRequest(
            activity_id=activity.id, request_key=request_key, command=probe.argv,
            workspace=str(workspace.path), expected_head=workspace.base_commit,
            runtime_identity=envelope_sha256,
            token_reservation=host_request.token_reservation,
            contract_hash=envelope_sha256, qualification_material=material,
            managed_input_sha256=workspace.input_digest,
        )
        completion = _completed_probe(store, activity.id, request_key)
        if completion is None:
            handle = supervisor.launch_qualification(request, qualification_contract=contract)
            completion = supervisor.finish(handle, timeout=probe.timeout_seconds)
        with productive_work(store, token, kind="qualification"):
            receipt = completion.get("host_receipt")
            if (completion.get("returncode") != 0 or not isinstance(receipt, dict)
                    or receipt.get("schema") != "ffs.codex-qualification-invocation/v1"
                    or receipt.get("status") == "uncertain" or receipt.get("probe_name") != name
                    or receipt.get("contract_sha256") != supervisor_contract_hashes[name]
                    or receipt.get("envelope_sha256") != envelope_sha256
                    or receipt.get("runtime_template_sha256") != runtime_template_sha256):
                raise ManagedQualificationRefused("QUALIFICATION_UNCERTAIN")
            results.append(module.QualificationResult(
                name, _read_result_stream(completion, "stdout"),
                _read_result_stream(completion, "stderr"), completion["returncode"],
            ))

    with productive_work(store, token, kind="qualification"):
        if os.path.lexists(observation_path):
            try:
                if observation_path.is_symlink() or not observation_path.is_file():
                    raise OSError
                observation = json.loads(observation_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
                raise ManagedQualificationRefused("QUALIFICATION_RESULT_INVALID") from exc
        else:
            observation = module.publish_qualification_results(plan, tuple(results))
        # The probes' TMPDIR must sit in the worktree; left behind, it changes the
        # later mapped-check snapshot's input digest (FRONTEND_CHECK_CANDIDATE_STALE).
        # Remove only the exact directory one of this call's plans created.
        _remove_created_scratch(Path(dict(plan.probes[0].environment)["TMPDIR"]), workspace.path, owned_scratch)
        qualified = verify_runtime(
            runtime, workspace.path, sandbox_mode=host_request.sandbox,
            network_enabled=host_request.network_enabled, roots=[str(workspace.path)],
            binary=str(executable), model=host_request.model, effort=host_request.effort,
        )
        if store.runtime_tuple_hash(qualified) != runtime_identity:
            raise ManagedQualificationRefused("RUNTIME_IDENTITY_CHANGED")
        promoted = store.promote_qualified_activity(
            token, activity.id, qualification_request_key=qualification_key,
            expected_contract_hashes=probe_hashes, runtime_identity=runtime_identity,
            final_contract_hash=final_contract_hash, role=role,
            observation_evidence={
                "locator": str(observation_path),
                "sha256": hashlib.sha256(observation_path.read_bytes()).hexdigest(),
            },
        )
        if promoted.state == "pending":
            promoted = store.transition_activity(
                token, promoted.id, expected="pending", new="active",
                reason="qualified runtime admitted for production launch",
            )
        if promoted.state != "active":
            raise ManagedQualificationRefused("INTENT_RECONCILIATION_REQUIRED")
        receipt = store.commit_runtime_receipt(token, promoted.id, qualified)
        return ManagedQualificationBundle(
            promoted, receipt, qualified, qualification_key, envelope_sha256,
            tuple((name, probe_hashes[name]) for name in _PROBES), admission,
            observation, tuple(results),
        )
