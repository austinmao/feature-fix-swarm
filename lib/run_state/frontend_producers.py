"""Production lifecycle producers assembled from the existing authority.

No new controller or launcher lives here.  ``produce_final_review`` is the
``LifecycleProducers.final_review`` callable: it resolves the durable current
candidate, captures it once into a registered reviewer workspace, qualifies
one host runtime through the managed adapters' own seam, binds the sealed
criteria and verified check evidence into the artifact envelope, and drives
``Supervisor.launch_native_review`` -> ``finish`` -> ``record_final_review``
on one request key.  Re-entry after a crash reconstructs the retained
reviewer, action, published material and intent before creating anything;
the single ``final_review`` grant is never spent twice.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
import uuid

from .native_review_transport import (
    NativeReviewTransportRefused, prepare_native_review_launch, read_native_review_launch,
)
from .native_review_runtime import NativeReviewRequest, NativeReviewRuntimeRefused, prepare_native_review_runtime
from .sealed_review import final_review_input_context, final_review_output_contract, record_final_review
from .supervisor import DispatchRequest, SupervisorRefused, artifact_review_inputs
from .wave_execution import capture_prelaunch_snapshot
from .workspace import (
    WorkspaceRefused, begin_child_workspace_preparation, inspect_workspace, load_input_snapshot,
    prepare_workspace,
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


@dataclass(frozen=True)
class QualifiedHostRuntime:
    """One promoted child activity with its qualified runtime and receipt."""

    activity: object
    qualified: object          # QualifiedCodexRuntime | QualifiedClaudeRuntime
    receipt: object            # has ``receipt_sha256``
    adapter: object | None     # CodexHostAdapter | ClaudeHostAdapter; None for fixtures
    additions: object | None   # GsdSupervisorEnvironment; None for fixtures


@dataclass(frozen=True)
class HostRuntimeSeam:
    """The managed host adapters' reusable runtime preparation plus host-resolved identities.

    ``qualify(activity_id, workspace, activity_request_key, parent_activity_id,
    final_contract_hash, role)`` returns ``QualifiedHostRuntime``;
    ``bind(qualified, prompt, workspace, final_contract_hash, launch_request_key)``
    returns ``(DispatchRequest with codex_material/claude_material, adapter)``.
    """

    host: str
    qualify: Callable
    bind: Callable
    binary: str
    cli_version: str
    model: str
    effort: str | None
    model_request: dict
    catalog_path: str | None = None
    catalog_sha256: str | None = None


def _retained_action(store, token, logical_key: str):
    with store.read_transaction() as tx:
        rows = tx.execute(
            "SELECT * FROM authority_policy_actions WHERE repository_id=? AND run_id=? AND action='final_review' "
            "AND logical_key=? AND state<>'cancelled' ORDER BY created_at,id",
            (token.repository_id, token.run_id, logical_key)).fetchall()
        if len(rows) > 1:
            raise SupervisorRefused("FINAL_REVIEW_ACTION_AMBIGUOUS")
        action = dict(rows[0]) if rows else None
        intent = None
        if action is not None and action["intent_id"] is not None:
            row = tx.execute("SELECT * FROM authority_launch_intents WHERE id=?", (action["intent_id"],)).fetchone()
            intent = None if row is None else dict(row)
    return action, intent


def _retained_reviewer(store, token, *, parent_activity_id: str, reviewer_key: str):
    with store.read_transaction() as tx:
        activity = tx.execute(
            "SELECT a.id FROM authority_activities a JOIN authority_child_bindings b ON b.activity_id=a.id "
            "WHERE b.parent_activity_id=? AND a.request_key=? AND a.repository_id=? AND a.run_id=?",
            (parent_activity_id, reviewer_key, token.repository_id, token.run_id)).fetchone()
        preparation = tx.execute(
            "SELECT preparation_id FROM context_workspaces WHERE repository_id=? AND run_id=? AND child_request_key=?",
            (token.repository_id, token.run_id, reviewer_key)).fetchone()
    return (None if activity is None else activity["id"],
            None if preparation is None else preparation["preparation_id"])


def _current_candidate(store, token, *, parent_activity_id: str, preparation):
    """Durable current candidate, else the supplied executed-parent preparation."""
    from .candidate_chain import resolve_current_frontend_candidate
    current = resolve_current_frontend_candidate(store, token)
    if current is not None:
        return (inspect_workspace(store, current.workspace_preparation_id), current.parent_activity_id,
                current.runtime_identity)
    with store.read_transaction() as tx:
        parent = tx.execute(
            "SELECT a.runtime_tuple_hash,b.workspace_binding FROM authority_activities a "
            "JOIN authority_child_bindings b ON b.activity_id=a.id WHERE a.id=? AND a.repository_id=? AND a.run_id=?",
            (parent_activity_id, token.repository_id, token.run_id)).fetchone()
    if parent is None or not parent["runtime_tuple_hash"] or parent["workspace_binding"] != str(preparation.path):
        raise SupervisorRefused("FRONTEND_REVIEW_PARENT_INVALID")
    # Promotion rewrote the preparation's child_role; capture compares the live row.
    return inspect_workspace(store, preparation.id), parent_activity_id, parent["runtime_tuple_hash"]


def _reviewer_workspace(store, token, supervisor, *, preparation, parent_activity_id, runtime_identity,
                        reviewer_key, candidate_hash, retained_preparation_id):
    """Capture the candidate once; replay reuses the retained reviewer preparation."""
    if retained_preparation_id is not None:
        ready = inspect_workspace(store, retained_preparation_id)
        if not ready.ready:
            ready = prepare_workspace(store, token, ready, input_snapshot=load_input_snapshot(store, ready))
    else:
        snapshot = capture_prelaunch_snapshot(store, token, preparation, activity_id=parent_activity_id,
                                              runtime_identity=runtime_identity, evidence_root=supervisor.evidence_root)
        if snapshot.input_digest != candidate_hash:
            raise SupervisorRefused("FRONTEND_REVIEW_CANDIDATE_STALE")
        pending = begin_child_workspace_preparation(
            store, token, parent_activity_id=parent_activity_id, request_key=reviewer_key, role="reviewer",
            base_commit=preparation.base_commit, selected_input_manifest=snapshot.manifest,
            repository_path=preparation.repository_path)
        ready = prepare_workspace(store, token, pending, input_snapshot=snapshot)
    if ready.input_digest != candidate_hash or ready.child_role != "reviewer":
        raise SupervisorRefused("FRONTEND_REVIEW_CANDIDATE_STALE")
    return ready


def _selected_artifacts(store, ready) -> dict[str, str]:
    snapshot = load_input_snapshot(store, ready)
    if snapshot is None:
        raise SupervisorRefused("FINAL_REVIEW_CONTEXT_INVALID")
    return {entry.path: entry.sha256 for entry in snapshot.selection.entries if entry.operation == "copy"}


def _published_request(supervisor, *, activity_id, launch_key, acceptance_hash, candidate_hash, expected_head,
                       token_reservation, input_hash):
    """Rebuild the exact request whose published material the retained action bound."""
    directory = supervisor.evidence_root / "native-review-material"
    if not directory.is_dir():
        return None
    for path in sorted(directory.iterdir()):
        if path.suffix != ".json" or len(path.stem) != 64:
            continue
        try:
            # A retained pre-launch closure still holds its guarded credential copy.
            material = read_native_review_launch(path, expected_material_sha256=path.stem, credential_required=True)
        except (NativeReviewTransportRefused, OSError, ValueError):
            continue
        if dict(material.artifact.provenance).get("activity_id") != activity_id:
            continue
        request = DispatchRequest(
            activity_id=activity_id, request_key=launch_key, command=material.native.argv,
            workspace=material.native.workspace, expected_head=expected_head,
            runtime_identity=material.runtime_tuple_hash, token_reservation=token_reservation,
            contract_hash=acceptance_hash, monitor_result=True,
            runtime_receipt_sha256=material.runtime_receipt_sha256, managed_input_sha256=candidate_hash,
            native_review_material=material)
        digest = hashlib.sha256(_canonical(supervisor._dispatch_material(request))).hexdigest()
        if digest == input_hash:
            return request
    return None


def _native_request(seam: HostRuntimeSeam, *, runtime_identity: str, prompt: str) -> NativeReviewRequest:
    binary = Path(seam.binary)
    try:
        binary_sha256 = hashlib.sha256(binary.read_bytes()).hexdigest()
    except OSError as error:
        raise SupervisorRefused("HOST_CAPABILITY_UNQUALIFIED") from error
    if seam.host == "codex" and (seam.catalog_path is None or seam.catalog_sha256 is None):
        raise SupervisorRefused("NATIVE_REVIEW_CATALOG_REQUIRED")
    return NativeReviewRequest(
        host=seam.host, requested_model=seam.model, cli_version=seam.cli_version, binary=str(binary),
        binary_sha256=binary_sha256, runtime_identity=runtime_identity, prompt=prompt, effort=seam.effort,
        catalog_path=seam.catalog_path if seam.host == "codex" else None,
        catalog_sha256=seam.catalog_sha256 if seam.host == "codex" else None,
        session_id=str(uuid.uuid4()) if seam.host == "claude" else None,
    )


def _private_root(supervisor, activity_id: str) -> Path:
    root = supervisor.evidence_root / "native-review-runtimes" / activity_id
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


def _settle(supervisor, handle, *, acceptance_hash: str, timeout_seconds):
    supervisor.finish(handle, timeout=timeout_seconds)
    return record_final_review(supervisor, handle, acceptance_hash=acceptance_hash)


def _release(adapter, material) -> None:
    if adapter is None or material is None:
        return
    try:
        adapter.release_launch_material(material)
    except Exception:
        # The verified directory is retained for finalization review.
        pass


def produce_final_review(store, token, *, supervisor, controller, seam: HostRuntimeSeam, parent_activity_id: str,
                         preparation, request_key: str = "final-review", timeout_seconds=None):
    """Run or resume the one native final review of the current sealed candidate."""
    from host_capabilities import CapabilityError, build_artifact_review_material
    if supervisor.worker_channel is not None:
        raise SupervisorRefused("NATIVE_REVIEW_ENTRYPOINT_REQUIRED")
    frozen = controller.sealed()
    sealed = store.get_sealed_acceptance(repository_id=token.repository_id, run_id=token.run_id)
    if sealed is None or sealed.acceptance_hash != frozen.acceptance_hash:
        raise SupervisorRefused("ACCEPTANCE_SEAL_REQUIRED")
    acceptance_hash, candidate_hash = frozen.acceptance_hash, frozen.candidate_hash
    launch_key, reviewer_key = request_key + ":launch", request_key + ":reviewer"

    action, intent = _retained_action(store, token, launch_key)
    if intent is not None:
        if not intent["permit_id"] or intent["child_pid"] is None:
            # Reserved but never acknowledged: only owner-fence reconciliation may settle it.
            raise SupervisorRefused("INTENT_RECONCILIATION_REQUIRED")
        return _settle(supervisor, supervisor.resume_monitored(intent["id"]),
                       acceptance_hash=acceptance_hash, timeout_seconds=timeout_seconds)

    try:
        preparation, parent_activity_id, runtime_identity = _current_candidate(
            store, token, parent_activity_id=parent_activity_id, preparation=preparation)
        retained_activity_id, retained_preparation_id = _retained_reviewer(
            store, token, parent_activity_id=parent_activity_id, reviewer_key=reviewer_key)
        ready = _reviewer_workspace(
            store, token, supervisor, preparation=preparation, parent_activity_id=parent_activity_id,
            runtime_identity=runtime_identity, reviewer_key=reviewer_key, candidate_hash=candidate_hash,
            retained_preparation_id=retained_preparation_id)
    except WorkspaceRefused as error:
        raise SupervisorRefused(error.code) from error
    qualified = seam.qualify(retained_activity_id or str(uuid.uuid4()), ready, reviewer_key,
                             parent_activity_id, acceptance_hash, "reviewer")
    activity = qualified.activity
    tuple_hash = store.runtime_tuple_hash(qualified.qualified)
    selected = _selected_artifacts(store, ready)
    context = final_review_input_context(store, token, acceptance_hash=acceptance_hash, candidate_hash=candidate_hash,
                                        reviewer_activity_id=activity.id, selected_artifacts=selected)
    contract = final_review_output_contract(sealed, candidate_hash=candidate_hash)
    private = _private_root(supervisor, activity.id)
    try:
        inputs = artifact_review_inputs(store, ready, selected)
        artifact = build_artifact_review_material(
            host=seam.host, model_request=dict(seam.model_request),
            config_sha256=hashlib.sha256(_canonical(qualified.qualified.to_dict())).hexdigest(),
            policy_sha256=acceptance_hash,
            environment={"HOME": str(private), "PATH": "/usr/bin:/bin", "TMPDIR": str(private)},
            selected_artifacts=selected, selected_contents=inputs["contents"],
            provenance={"activity_id": activity.id, **inputs["provenance"]},
            output_contract=contract, review_context=context)
    except (WorkspaceRefused, CapabilityError, OSError, UnicodeError) as error:
        raise SupervisorRefused("HOST_MATERIAL_INVALID") from error
    # The ordinary launch material is the adapter's own post-qualification closure:
    # its credential is the only source the native transport may copy.
    ordinary_request, adapter = seam.bind(qualified, artifact.prompt, ready, acceptance_hash, launch_key)
    ordinary = ordinary_request.codex_material if seam.host == "codex" else ordinary_request.claude_material
    if ordinary is None:
        _release(adapter, ordinary_request.codex_material or ordinary_request.claude_material)
        raise SupervisorRefused("HOST_CAPABILITY_UNQUALIFIED")
    try:
        if action is not None:
            request = _published_request(
                supervisor, activity_id=activity.id, launch_key=launch_key, acceptance_hash=acceptance_hash,
                candidate_hash=candidate_hash, expected_head=ready.base_commit,
                token_reservation=ordinary_request.token_reservation, input_hash=action["input_hash"])
            if request is None:
                raise SupervisorRefused("FINAL_REVIEW_ACTION_UNRECOVERABLE")
            request = replace(request, policy_action_id=action["id"])
        else:
            try:
                native = prepare_native_review_runtime(
                    _native_request(seam, runtime_identity=tuple_hash, prompt=artifact.prompt),
                    runtime_root=private / uuid.uuid4().hex, workspace=ready.path)
                material = prepare_native_review_launch(native=native, artifact=artifact, ordinary=ordinary,
                                                        runtime_receipt_sha256=qualified.receipt.receipt_sha256)
            except (NativeReviewRuntimeRefused, NativeReviewTransportRefused) as error:
                raise SupervisorRefused("NATIVE_REVIEW_MATERIAL_INVALID") from error
            request = DispatchRequest(
                activity_id=activity.id, request_key=launch_key, command=native.argv, workspace=str(ready.path),
                expected_head=ready.base_commit, runtime_identity=tuple_hash,
                token_reservation=ordinary_request.token_reservation, contract_hash=acceptance_hash,
                monitor_result=True, runtime_receipt_sha256=qualified.receipt.receipt_sha256,
                managed_input_sha256=ready.input_digest, native_review_material=material)
        handle = supervisor.launch_native_review(request)
        return _settle(supervisor, handle, acceptance_hash=acceptance_hash, timeout_seconds=timeout_seconds)
    finally:
        _release(adapter, ordinary)


@dataclass(frozen=True)
class ManagedHostSession:
    """One prepared managed host run: outer qualification, execution and cleanup seams.

    Built by the host-specific managed commands before any stateful launch.
    ``prepare_outer()`` returns ``(DispatchRequest, adapter)`` for the outer
    orchestrator (replay-safe through retained qualification); ``execute``
    launches it and settles waves; ``close`` releases channel and material.
    """

    host: str
    supervisor: object
    evidence_root: Path
    ready: object
    child_key: str
    outer_activity_id: str
    invocation: tuple
    timeout_seconds: int
    seam: HostRuntimeSeam
    prepare_outer: Callable
    execute: Callable
    close: Callable


def retained_outer_activity(store, token, *, parent_activity_id: str, child_key: str) -> str | None:
    """The retained outer orchestrator activity for this request key, if qualification already created it."""
    with store.read_transaction() as tx:
        row = tx.execute(
            "SELECT a.id FROM authority_activities a JOIN authority_child_bindings b ON b.activity_id=a.id "
            "WHERE b.parent_activity_id=? AND a.request_key=? AND a.repository_id=? AND a.run_id=? ORDER BY a.created_at",
            (parent_activity_id, child_key, token.repository_id, token.run_id)).fetchone()
    return None if row is None else row["id"]


def retained_launch(store, activity_id: str):
    """The newest real (non-qualification) launch intent on a retained activity, if any."""
    with store.read_transaction() as tx:
        return tx.execute(
            "SELECT state,completion_status FROM authority_launch_intents WHERE activity_id=? "
            "AND id NOT IN (SELECT intent_id FROM authority_qualification_launches) "
            "ORDER BY attempt_ordinal DESC", (activity_id,)).fetchone()


def _retained_outer_completion(store, activity_id: str):
    """A succeeded managed outer launch (capacity-exempt, not a qualification probe)."""
    from process_identity import ProcessIdentity
    with store.read_transaction() as tx:
        # Qualification probes run on this same activity and are capacity-exempt too.
        row = tx.execute(
            "SELECT * FROM authority_launch_intents WHERE activity_id=? AND capacity_exempt=1 "
            "AND completion_status='succeeded' "
            "AND id NOT IN (SELECT intent_id FROM authority_qualification_launches) "
            "ORDER BY attempt_ordinal DESC", (activity_id,)).fetchone()
    if row is None:
        return None
    identity = ProcessIdentity(row["child_host_id"], row["child_boot_id"], row["child_pid"], row["child_start_token"])
    return (SimpleNamespace(intent_id=row["id"], activity_id=activity_id, identity=identity),
            json.loads(row["completion_evidence_json"]))


def _bind_executed_candidate(store, token, *, sealed, handle, request_key, ready, process_evidence) -> None:
    from .wave_candidate import bind_wave_execution_candidate
    with store.read_transaction() as tx:
        journaled = tx.execute(
            "SELECT 1 FROM authority_workspace_integrations WHERE repository_id=? AND run_id=? AND issuing_intent_id=?",
            (token.repository_id, token.run_id, handle.intent_id)).fetchone()
    if journaled is None:
        # Planning-only execution leaves the sealed input as the current candidate.
        return
    bind_wave_execution_candidate(store, token, sealed=sealed, handle=handle, request_key=request_key,
                                  ready=ready, process_evidence=process_evidence)


def _settle_reviewers(store, token, *, parent_activity_id: str) -> None:
    with store.read_transaction() as tx:
        rows = tx.execute(
            "SELECT b.activity_id FROM authority_child_bindings b JOIN authority_activities a ON a.id=b.activity_id "
            "WHERE b.parent_activity_id=? AND b.role='reviewer' AND a.state='active'", (parent_activity_id,)).fetchall()
        receipts = tx.execute(
            "SELECT receipt_json FROM authority_acceptance_receipts WHERE repository_id=? AND run_id=? "
            "AND json_extract(receipt_json,'$.role')='review'", (token.repository_id, token.run_id)).fetchall()
    evidence = {}
    for row in receipts:
        receipt = json.loads(row["receipt_json"])
        evidence[receipt["activity_id"]] = {key: receipt["evidence"][0][key] for key in ("locator", "sha256")}
    for row in rows:
        result = evidence.get(row["activity_id"])
        if result is None:
            continue
        store.transition_activity(token, row["activity_id"], expected="active", new="succeeded",
                                  result=result, reason="final review recorded")


def seal_from_draft(store, token, *, command_mode: str, draft: object, runtime_hash: str, candidate_hash: str):
    """Seal an explicit operator-authored acceptance draft after outer qualification."""
    from .frontend_policy import FrontendPolicyRefused
    from .managed import build_frontend_acceptance_draft, seal_frontend_policy
    from .run_policy import RunPolicyRefused
    allowed = {"draft_id", "revision", "command_mode", "criteria", "exclusions", "global_invariants"}
    if (not isinstance(draft, dict) or set(draft) - allowed
            or not {"criteria", "exclusions", "global_invariants"} <= set(draft)):
        raise SupervisorRefused("ACCEPTANCE_DRAFT_INVALID")
    mode = draft.get("command_mode", command_mode)
    legacy = store.get_acceptance_contract(repository_id=token.repository_id, run_id=token.run_id)
    if legacy is None:
        raise SupervisorRefused("ACCEPTANCE_CONTRACT_REQUIRED")
    try:
        material = build_frontend_acceptance_draft(
            objective_digest=legacy.material["objective_digest"], criteria=draft["criteria"],
            exclusions=draft["exclusions"], global_invariants=draft["global_invariants"],
            requested_runtime_hash=runtime_hash, effective_runtime_hash=runtime_hash,
            candidate_hash=candidate_hash, generation=legacy.generation, command_mode=mode)
        return seal_frontend_policy(store, token, frontend=mode, draft_id=str(draft.get("draft_id", "acceptance")),
                                    revision=int(draft.get("revision", 1)), material=material)
    except (FrontendPolicyRefused, RunPolicyRefused, KeyError, TypeError, ValueError) as error:
        raise SupervisorRefused(getattr(error, "code", "ACCEPTANCE_DRAFT_INVALID")) from error


def drive_managed_session(store, token, context, session: ManagedHostSession, *, acceptance_draft=None) -> int:
    """Run the managed host: legacy single execution, or the sealed frontend lifecycle."""
    from .frontend_lifecycle import LifecycleProducers, drive_frontend_lifecycle
    from .frontend_policy import FrontendPolicyController
    from .supervisor import Supervisor, SupervisorRefused
    outcome = {"handle": None, "adapter": None, "material": None}
    # A frontend run is only the sealed lifecycle; the unsealed single launch
    # below is managed-start only.  Refuse before any staging or launch.
    with store.read_transaction() as tx:
        frontend = tx.execute("SELECT 1 FROM authority_event_keys "
                              "WHERE activity_id=? AND idempotency_key='frontend-operation'",
                              (context.activity_id,)).fetchone() is not None
    if frontend and acceptance_draft is None and store.get_sealed_acceptance(
            repository_id=token.repository_id, run_id=token.run_id) is None:
        raise SupervisorRefused("ACCEPTANCE_DRAFT_REQUIRED")
    try:
        retained_outer = _retained_outer_completion(store, session.outer_activity_id)
        if retained_outer is not None:
            # A launched private runtime's credential is consumed; it is never
            # re-staged or re-qualified.  Replay binds the retained outer activity.
            with store.read_transaction() as tx:
                binding = tx.execute("SELECT runtime_identity FROM authority_child_bindings WHERE activity_id=?",
                                     (session.outer_activity_id,)).fetchone()
            request = SimpleNamespace(activity_id=session.outer_activity_id, request_key=session.child_key + ":launch",
                                      runtime_identity=binding["runtime_identity"], codex_material=None,
                                      claude_material=None)
            adapter = None
        else:
            request, adapter = session.prepare_outer()
        outcome["adapter"], outcome["material"] = adapter, request.codex_material or request.claude_material
        sealed = store.get_sealed_acceptance(repository_id=token.repository_id, run_id=token.run_id)
        if sealed is None and acceptance_draft is not None:
            seal_from_draft(store, token, command_mode=str(session.invocation[0]), draft=acceptance_draft,
                            runtime_hash=request.runtime_identity, candidate_hash=session.ready.input_digest)
            sealed = store.get_sealed_acceptance(repository_id=token.repository_id, run_id=token.run_id)
        state = None if sealed is None else store.get_frontend_policy_state(
            repository_id=token.repository_id, run_id=token.run_id)
        if sealed is None or state is None:
            if retained_outer is not None:
                # The one legacy execution already completed; replay reports it without a second launch.
                return 0
            returncode, outcome["handle"], _result = session.execute(request, adapter)
            return returncode
        controller = FrontendPolicyController(store, token, command_mode=sealed.material["command_mode"])
        # Native review and sealed checks cross a channel-less supervisor; the
        # worker channel stays with the outer orchestrator only.
        review_supervisor = Supervisor(store, token, evidence_root=session.evidence_root)

        def execute(_frozen):
            retained = _retained_outer_completion(store, request.activity_id)
            if retained is None:
                returncode, outcome["handle"], result = session.execute(request, adapter, settle_success=False)
                if returncode != 0:
                    raise SupervisorRefused("FRONTEND_EXECUTION_FAILED")
                retained = outcome["handle"], result["evidence"]
            completion, evidence = retained
            _bind_executed_candidate(store, token, sealed=sealed, handle=completion, request_key=request.request_key,
                                     ready=session.ready, process_evidence=evidence)

        def final_review(_frozen):
            produce_final_review(store, token, supervisor=review_supervisor, controller=controller, seam=session.seam,
                                 parent_activity_id=request.activity_id, preparation=session.ready,
                                 timeout_seconds=session.timeout_seconds)

        def recover(_packet):
            # No production diagnosis/trial producer is assembled yet; the retained
            # handback stays durable and no recovery cycle is spent.
            raise SupervisorRefused("RECOVERY_PRODUCER_UNAVAILABLE")

        def settle():
            _settle_reviewers(store, token, parent_activity_id=request.activity_id)
            retained = _retained_outer_completion(store, request.activity_id)
            outer = store.get_activity(request.activity_id)
            if retained is not None and outer.state == "active":
                store.transition_activity(token, outer.id, expected="active", new="succeeded",
                                          result=retained[1], reason="qualified host process completed")

        stage = drive_frontend_lifecycle(
            store, token, supervisor=review_supervisor, controller=controller, workspace=str(session.ready.path),
            parent_activity_id=request.activity_id,
            producers=LifecycleProducers(execute=execute, final_review=final_review, recover=recover, settle=settle))
        if stage != "DONE":
            raise SupervisorRefused("FRONTEND_LIFECYCLE_" + stage)
        return 0
    finally:
        session.close(outcome["handle"], outcome["adapter"], outcome["material"])


def terminal_lifecycle_outcome(store, token) -> int | None:
    """Exit status for an already-terminal sealed lifecycle, else None (not terminal / unsealed)."""
    from .frontend_lifecycle import TERMINAL_STAGES
    state = store.get_frontend_policy_state(repository_id=token.repository_id, run_id=token.run_id)
    if state is None or state.stage not in TERMINAL_STAGES:
        return None
    if state.stage == "DONE":
        return 0
    raise SupervisorRefused("FRONTEND_LIFECYCLE_" + state.stage)
