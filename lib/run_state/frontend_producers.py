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

``produce_spec_review`` is the same native review one step earlier, for a draft that opted in (``"spec_review":
"native"``): it reviews the UNSEALED draft under the draft hash, records its verdict, and ``seal_from_draft`` seals
only a draft whose accepted review is retained.
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
from .spec_review import (
    record_spec_review, require_spec_review_accepted, retained_spec_review, spec_review_input_context,
    spec_review_output_contract,
)
from .supervisor import DispatchRequest, SupervisorRefused, artifact_review_inputs
from .wave_execution import capture_prelaunch_snapshot
from .workspace import (
    WorkspaceRefused, begin_child_workspace_preparation, inspect_workspace, load_input_snapshot,
    prepare_workspace, revalidate_ready_fence,
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
    final_contract_hash, role, *, supervisor=None)`` returns ``QualifiedHostRuntime``
    (``supervisor`` defaults to the outer orchestrator's; the reviewer passes its own);
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


def _retained_action(store, token, logical_key: str, action: str = "final_review",
                     ambiguous_code: str = "FINAL_REVIEW_ACTION_AMBIGUOUS"):
    with store.read_transaction() as tx:
        rows = tx.execute(
            "SELECT * FROM authority_policy_actions WHERE repository_id=? AND run_id=? AND action=? "
            "AND logical_key=? AND state<>'cancelled' ORDER BY created_at,id",
            (token.repository_id, token.run_id, action, logical_key)).fetchall()
        if len(rows) > 1:
            raise SupervisorRefused(ambiguous_code)
        action = dict(rows[0]) if rows else None
        intent = None
        if action is not None and action["intent_id"] is not None:
            row = tx.execute("SELECT * FROM authority_launch_intents WHERE id=?", (action["intent_id"],)).fetchone()
            intent = None if row is None else dict(row)
    return action, intent


def _retained_reviewer(store, token, *, parent_activity_id: str, reviewer_key: str):
    with store.read_transaction() as tx:
        activity = tx.execute(
            "SELECT a.id,a.state,a.generation FROM authority_activities a JOIN authority_child_bindings b "
            "ON b.activity_id=a.id WHERE b.parent_activity_id=? AND a.request_key=? AND a.repository_id=? "
            "AND a.run_id=?", (parent_activity_id, reviewer_key, token.repository_id, token.run_id)).fetchone()
        preparation = tx.execute(
            "SELECT preparation_id,state,generation FROM context_workspaces WHERE repository_id=? AND run_id=? "
            "AND child_request_key=?", (token.repository_id, token.run_id, reviewer_key)).fetchone()
    return activity, preparation


def _current_reviewer(store, token, *, parent_activity_id: str, base_key: str, action,
                      reconciliation_code: str = "REVIEW_RECONCILIATION_REQUIRED"):
    """The reviewer (or recovery child) this owner may qualify: ``(key, retained activity id, retained preparation id)``.

    F51: a reviewer qualified under an earlier owner fence cannot be re-qualified
    here (its admission file, probe settlements and runtime home bind that
    fence), so it is abandoned: each unfinished probe must be a dead owner's
    dead child (``close_dead_qualification_intent``), the reviewer is aborted,
    and the next attempt key gets a fresh activity, workspace and private home.
    A review grant already reserved against it is never moved to another reviewer.
    F51c: a reviewer workspace an earlier owner began but never made READY (and
    so never bound to a reviewer) is not finished here either; it is left
    exactly as retained, as evidence, and the next attempt key captures afresh.
    """
    from .ownership import OwnershipRefused
    attempt = 1
    while True:
        key = base_key if attempt == 1 else f"{base_key}:{attempt}"
        activity, preparation = _retained_reviewer(store, token, parent_activity_id=parent_activity_id,
                                                   reviewer_key=key)
        if activity is not None and activity["state"] in {"failed", "aborted"}:
            attempt += 1
            continue
        if (activity is None and preparation is not None and preparation["state"] != "ready"
                and preparation["generation"] != token.generation):
            attempt += 1
            continue
        preparation_id = None if preparation is None else preparation["preparation_id"]
        if activity is None or activity["generation"] == token.generation:
            return key, None if activity is None else activity["id"], preparation_id
        if action is not None:
            raise SupervisorRefused(reconciliation_code)
        with store.read_transaction() as tx:
            unfinished = [row["id"] for row in tx.execute(
                "SELECT id FROM authority_launch_intents WHERE activity_id=? "
                "AND state NOT IN ('completed_succeeded','completed_failed','closed_dead')", (activity["id"],))]
        try:
            for intent_id in unfinished:
                store.close_dead_qualification_intent(intent_id, token)
            store.transition_activity(token, activity["id"], expected=activity["state"], new="aborted",
                                      reason="reviewer of an earlier owner fence abandoned unreviewed (F51)")
        except OwnershipRefused as error:
            raise SupervisorRefused(error.code) from error
        attempt += 1


def _current_candidate(store, token, *, parent_activity_id: str, preparation):
    """Durable current candidate, else the supplied executed-parent preparation."""
    from .candidate_chain import resolve_current_frontend_candidate
    current = resolve_current_frontend_candidate(store, token)
    if current is not None:
        return (inspect_workspace(store, current.workspace_preparation_id), current.parent_activity_id,
                current.runtime_identity)
    return _outer_parent(store, token, parent_activity_id=parent_activity_id, preparation=preparation)


def _outer_parent(store, token, *, parent_activity_id: str, preparation):
    """The supplied parent and its prepared workspace as the candidate to capture; the only one an unsealed run has."""
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
                        reviewer_key, candidate_hash, retained_preparation_id, retained_activity_id=None,
                        role: str = "reviewer"):
    """Capture the candidate once; replay reuses the retained reviewer preparation.

    Qualification only admits an ``inventory`` workspace and promotes it to the
    final role (``reviewer``, or ``recovery`` for a diagnosis or trial child), so
    the capture is prepared as inventory.  A retained preparation reads that role
    once promoted, ``inventory`` before; a ready one (and its child) is rebound
    to a resumed owner's fence (F51).
    """
    if retained_preparation_id is not None:
        ready = inspect_workspace(store, retained_preparation_id)
        if ready.ready:
            ready = rebind_retained_child(store, token, retained_activity_id, ready.id)
        else:
            ready = prepare_workspace(store, token, ready, input_snapshot=load_input_snapshot(store, ready))
    else:
        snapshot = capture_prelaunch_snapshot(store, token, preparation, activity_id=parent_activity_id,
                                              runtime_identity=runtime_identity, evidence_root=supervisor.evidence_root)
        if snapshot.input_digest != candidate_hash:
            raise SupervisorRefused("FRONTEND_REVIEW_CANDIDATE_STALE")
        pending = begin_child_workspace_preparation(
            store, token, parent_activity_id=parent_activity_id, request_key=reviewer_key, role="inventory",
            base_commit=preparation.base_commit, selected_input_manifest=snapshot.manifest,
            repository_path=preparation.repository_path)
        ready = prepare_workspace(store, token, pending, input_snapshot=snapshot)
    if ready.input_digest != candidate_hash or ready.child_role not in {"inventory", role}:
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


def _native_request(seam: HostRuntimeSeam, *, runtime_identity: str, prompt: str,
                    qualified_binary=()) -> NativeReviewRequest:
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
        # A `.js` launcher runs under the Node, and spawns the vendor executable, that its runtime
        # was qualified with (chain pins); native review binds and re-verifies both.
        node_sha256=dict(qualified_binary).get("node_sha256") if seam.host == "codex" else None,
        native_sha256=dict(qualified_binary).get("native_sha256") if seam.host == "codex" else None,
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


@dataclass(frozen=True)
class _Review:
    """What differs between the sealed final review and the unsealed spec review; every other step is shared.

    A final review runs on the CURRENT candidate under the acceptance hash, and re-enters a grant it reserved but
    never launched.  A spec review runs on the draft's own candidate under the draft hash, and releases such a grant
    unspent (its launch material is rebuilt from a fresh private runtime root), after ``precheck`` has refused what
    its allowance and the launch budget cannot cover.
    """

    action: str             # the policy action the one native review is granted under
    ambiguous: str          # typed refusal when two live grants carry the launch key
    reconcile: str          # typed refusal for a review or reviewer issued under an earlier owner fence
    reviewer_key: str       # the reviewer child's base logical key
    launch_key: str
    contract_hash: str      # what the reviewer child is bound to: the acceptance hash, or the draft hash
    candidate_hash: str
    candidate: Callable     # () -> (preparation, parent_activity_id, runtime_identity) of the candidate to capture
    context: Callable       # (reviewer_activity_id, selected_artifacts) -> the review context
    contract: Callable      # () -> the output contract
    settle: Callable        # (supervisor, handle) -> the recorded review
    release_stale_grant: bool = False
    precheck: Callable | None = None   # () -> None; refuses before any workspace, probe or grant is touched


def _native_review(store, token, supervisor, seam: HostRuntimeSeam, review: _Review):
    """Run or resume the one native review described by ``review`` on a channel-less supervisor."""
    from host_capabilities import CapabilityError, build_artifact_review_material
    from .ownership import OwnershipRefused
    contract_hash, candidate_hash, launch_key = review.contract_hash, review.candidate_hash, review.launch_key

    action, intent = _retained_action(store, token, launch_key, review.action, review.ambiguous)
    if intent is not None:
        if not intent["permit_id"] or intent["child_pid"] is None:
            # Reserved but never acknowledged: only owner-fence reconciliation may settle it.
            raise SupervisorRefused("INTENT_RECONCILIATION_REQUIRED")
        if intent["generation"] != token.generation:
            # F51: a native review's completion proof binds the owner fence that issued it, so a
            # resumed owner can neither re-verify nor record it, and never launches a second one.
            raise SupervisorRefused(review.reconcile)
        return review.settle(supervisor, supervisor.resume_monitored(intent["id"]))

    if review.release_stale_grant and action is not None:
        try:
            store.cancel_unlaunched_policy_review(token, action_id=action["id"])
        except OwnershipRefused as error:
            raise SupervisorRefused(error.code) from error
        action = None
    if review.precheck is not None:
        review.precheck()
    try:
        preparation, parent_activity_id, runtime_identity = review.candidate()
        reviewer_key, retained_activity_id, retained_preparation_id = _current_reviewer(
            store, token, parent_activity_id=parent_activity_id, base_key=review.reviewer_key, action=action,
            reconciliation_code=review.reconcile)
        ready = _reviewer_workspace(
            store, token, supervisor, preparation=preparation, parent_activity_id=parent_activity_id,
            runtime_identity=runtime_identity, reviewer_key=reviewer_key, candidate_hash=candidate_hash,
            retained_preparation_id=retained_preparation_id, retained_activity_id=retained_activity_id)
    except WorkspaceRefused as error:
        raise SupervisorRefused(error.code) from error
    # The outer orchestrator's prepaid group has ended by now: qualify on this channel-less supervisor.
    qualified = seam.qualify(retained_activity_id or str(uuid.uuid4()), ready, reviewer_key,
                             parent_activity_id, contract_hash, "reviewer", supervisor=supervisor)
    activity = qualified.activity
    tuple_hash = store.runtime_tuple_hash(qualified.qualified)
    selected = _selected_artifacts(store, ready)
    context = review.context(activity.id, selected)
    contract = review.contract()
    private = _private_root(supervisor, activity.id)
    try:
        inputs = artifact_review_inputs(store, ready, selected)
        artifact = build_artifact_review_material(
            host=seam.host, model_request=dict(seam.model_request),
            config_sha256=hashlib.sha256(_canonical(qualified.qualified.to_dict())).hexdigest(),
            policy_sha256=contract_hash,
            environment={"HOME": str(private), "PATH": "/usr/bin:/bin", "TMPDIR": str(private)},
            selected_artifacts=selected, selected_contents=inputs["contents"],
            provenance={"activity_id": activity.id, **inputs["provenance"]},
            output_contract=contract, review_context=context)
    except (WorkspaceRefused, CapabilityError, OSError, UnicodeError) as error:
        raise SupervisorRefused("HOST_MATERIAL_INVALID") from error
    # The ordinary launch material is the adapter's own post-qualification closure:
    # its credential is the only source the native transport may copy.
    ordinary_request, adapter = seam.bind(qualified, artifact.prompt, ready, contract_hash, launch_key)
    ordinary = ordinary_request.codex_material if seam.host == "codex" else ordinary_request.claude_material
    if ordinary is None:
        _release(adapter, ordinary_request.codex_material or ordinary_request.claude_material)
        raise SupervisorRefused("HOST_CAPABILITY_UNQUALIFIED")
    try:
        if action is not None:
            request = _published_request(
                supervisor, activity_id=activity.id, launch_key=launch_key, acceptance_hash=contract_hash,
                candidate_hash=candidate_hash, expected_head=ready.base_commit,
                token_reservation=ordinary_request.token_reservation, input_hash=action["input_hash"])
            if request is None:
                raise SupervisorRefused("FINAL_REVIEW_ACTION_UNRECOVERABLE")
            request = replace(request, policy_action_id=action["id"])
        else:
            try:
                native = prepare_native_review_runtime(
                    _native_request(seam, runtime_identity=tuple_hash, prompt=artifact.prompt,
                                    qualified_binary=qualified.qualified.binary),
                    runtime_root=private / uuid.uuid4().hex, workspace=ready.path)
                material = prepare_native_review_launch(native=native, artifact=artifact, ordinary=ordinary,
                                                        runtime_receipt_sha256=qualified.receipt.receipt_sha256)
            except (NativeReviewRuntimeRefused, NativeReviewTransportRefused) as error:
                raise SupervisorRefused("NATIVE_REVIEW_MATERIAL_INVALID") from error
            request = DispatchRequest(
                activity_id=activity.id, request_key=launch_key, command=native.argv, workspace=str(ready.path),
                expected_head=ready.base_commit, runtime_identity=tuple_hash,
                token_reservation=ordinary_request.token_reservation, contract_hash=contract_hash,
                monitor_result=True, runtime_receipt_sha256=qualified.receipt.receipt_sha256,
                managed_input_sha256=ready.input_digest, native_review_material=material)
        handle = supervisor.launch_native_review(request)
        return review.settle(supervisor, handle)
    finally:
        _release(adapter, ordinary)


def produce_final_review(store, token, *, supervisor, controller, seam: HostRuntimeSeam, parent_activity_id: str,
                         preparation, request_key: str = "final-review", timeout_seconds=None):
    """Run or resume the one native final review of the current sealed candidate."""
    if supervisor.worker_channel is not None:
        raise SupervisorRefused("NATIVE_REVIEW_ENTRYPOINT_REQUIRED")
    frozen = controller.sealed()
    sealed = store.get_sealed_acceptance(repository_id=token.repository_id, run_id=token.run_id)
    if sealed is None or sealed.acceptance_hash != frozen.acceptance_hash:
        raise SupervisorRefused("ACCEPTANCE_SEAL_REQUIRED")
    acceptance_hash, candidate_hash = frozen.acceptance_hash, frozen.candidate_hash
    return _native_review(store, token, supervisor, seam, _Review(
        action="final_review", ambiguous="FINAL_REVIEW_ACTION_AMBIGUOUS", reconcile="REVIEW_RECONCILIATION_REQUIRED",
        reviewer_key=request_key + ":reviewer", launch_key=request_key + ":launch", contract_hash=acceptance_hash,
        candidate_hash=candidate_hash,
        candidate=lambda: _current_candidate(store, token, parent_activity_id=parent_activity_id,
                                             preparation=preparation),
        context=lambda activity_id, selected: final_review_input_context(
            store, token, acceptance_hash=acceptance_hash, candidate_hash=candidate_hash,
            reviewer_activity_id=activity_id, selected_artifacts=selected),
        contract=lambda: final_review_output_contract(sealed, candidate_hash=candidate_hash),
        settle=lambda reviewing, handle: _settle(reviewing, handle, acceptance_hash=acceptance_hash,
                                                 timeout_seconds=timeout_seconds)))


def _spec_precheck(store, token) -> None:
    """Refuse, before any workspace is captured or probe charged, what the tier and the launch budget cannot cover.

    The store would refuse the same allowance at the reservation, but only after four qualification probes were
    charged; the budget must cover those probes and the review launch itself.
    """
    from .run_policy import action_limit
    from .state import _QUALIFICATION_PROBE_ORDER
    budget = store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id)
    if budget is None:
        raise SupervisorRefused("RUN_POLICY_REQUIRED")
    with store.read_transaction() as tx:
        used = tx.execute("SELECT count(*) FROM authority_policy_actions WHERE repository_id=? AND run_id=? "
                          "AND action='spec_review' AND state<>'cancelled'",
                          (token.repository_id, token.run_id)).fetchone()[0]
    if used >= action_limit("spec_review", budget.tier):
        raise SupervisorRefused("POLICY_ACTION_LIMIT_EXHAUSTED")
    if budget.launch_limit - budget.launch_charged < len(_QUALIFICATION_PROBE_ORDER) + 1:
        raise SupervisorRefused("SPEC_REVIEW_BUDGET_INFEASIBLE")


def _settle_spec(store, token, supervisor, handle, *, draft, timeout_seconds):
    """Finish the review and record it; a review that leaves no record ends its child ``failed``, the grant spent."""
    result = supervisor.finish(handle, timeout=timeout_seconds)
    try:
        if result["returncode"] != 0 or result.get("host_receipt", {}).get("status") != "complete":
            raise SupervisorRefused("SPEC_REVIEW_RESULT_REQUIRED")
        return record_spec_review(supervisor, handle, draft=draft)
    except SupervisorRefused:
        if store.get_activity(handle.activity_id).state == "active":
            store.transition_activity(token, handle.activity_id, expected="active", new="failed",
                                      result=result["evidence"], reason="spec review left no record")
        raise


def produce_spec_review(store, token, *, supervisor, seam: HostRuntimeSeam, parent_activity_id: str, preparation,
                        draft, timeout_seconds=None) -> dict:
    """Run or resume the one native review of an UNSEALED acceptance draft; returns its accepted record.

    Nothing here needs a seal: the reviewer child is bound to the draft hash and the draft's own candidate, the
    grant is a ``spec_review`` action (tier-bounded), and the verdict is one keyed record (``spec-review:<hash>``).  A
    ``revise`` verdict is retained and refuses ``SPEC_REVIEW_REJECTED``, on replay too, without a second launch.
    The grant, the reviewer and the budget follow the final review's rules, except that a grant reserved and
    never launched (by any owner) is released and reserved afresh, and the tier allowance and launch budget are
    checked before any probe is charged.
    """
    if supervisor.worker_channel is not None:
        raise SupervisorRefused("NATIVE_REVIEW_ENTRYPOINT_REQUIRED")
    record = retained_spec_review(store, token, draft)
    if record is None:
        tag = draft.draft_hash[:16]
        record = _native_review(store, token, supervisor, seam, _Review(
            action="spec_review", ambiguous="SPEC_REVIEW_ACTION_AMBIGUOUS",
            reconcile="SPEC_REVIEW_RECONCILIATION_REQUIRED", reviewer_key=f"spec-review:{tag}:reviewer",
            launch_key=f"spec-review:{tag}:launch", contract_hash=draft.draft_hash,
            candidate_hash=draft.material["candidate_hash"],
            candidate=lambda: _outer_parent(store, token, parent_activity_id=parent_activity_id,
                                            preparation=preparation),
            context=lambda activity_id, selected: spec_review_input_context(
                store, token, draft=draft, reviewer_activity_id=activity_id, selected_artifacts=selected),
            contract=lambda: spec_review_output_contract(draft),
            settle=lambda reviewing, handle: _settle_spec(store, token, reviewing, handle, draft=draft,
                                                          timeout_seconds=timeout_seconds),
            release_stale_grant=True, precheck=lambda: _spec_precheck(store, token)))
    if record["verdict"] != "accept":
        raise SupervisorRefused("SPEC_REVIEW_REJECTED")
    return record


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
            "AND NOT EXISTS (SELECT 1 FROM authority_qualification_launches q "
            "WHERE q.intent_id=authority_launch_intents.id) "
            "ORDER BY attempt_ordinal DESC", (activity_id,)).fetchone()


def _retained_outer_completion(store, activity_id: str):
    """A succeeded managed outer launch (capacity-exempt, not a qualification probe)."""
    from process_identity import ProcessIdentity
    with store.read_transaction() as tx:
        # Qualification probes run on this same activity and are capacity-exempt too.
        row = tx.execute(
            "SELECT * FROM authority_launch_intents WHERE activity_id=? AND capacity_exempt=1 "
            "AND completion_status='succeeded' "
            "AND NOT EXISTS (SELECT 1 FROM authority_qualification_launches q "
            "WHERE q.intent_id=authority_launch_intents.id) "
            "ORDER BY attempt_ordinal DESC", (activity_id,)).fetchone()
    if row is None:
        return None
    identity = ProcessIdentity(row["child_host_id"], row["child_boot_id"], row["child_pid"], row["child_start_token"])
    return (SimpleNamespace(intent_id=row["id"], activity_id=activity_id, identity=identity),
            json.loads(row["completion_evidence_json"]))


def _repair_proof(store, token, state) -> bool:
    """An issued repair, or a failed frozen check on the current candidate, proves execution happened and was checked.

    Both exist only after the execute producer bound the candidate and ``run_mapped_checks`` ran on it, so an outer
    that settled before its candidate was bound still has neither.  A grant that was only reserved is not an issued
    repair, but the failed check that led to it is retained, so a crash before the repair's intent stays resumable.
    """
    with store.read_transaction() as tx:
        return tx.execute(
            "SELECT 1 FROM authority_policy_actions WHERE repository_id=? AND run_id=? AND action='repair' "
            "AND state<>'cancelled' AND intent_id IS NOT NULL UNION ALL "
            "SELECT 1 FROM authority_frontend_policy_checks WHERE repository_id=? AND run_id=? "
            "AND acceptance_hash=? AND candidate_hash=? AND status='failed'",
            (token.repository_id, token.run_id, token.repository_id, token.run_id, state.acceptance_hash,
             state.candidate_hash)).fetchone() is not None


def resumable_outer_completion(store, token, activity_id: str, launch) -> bool:
    """F51: the settled outer launch a same-key resume continues past instead of refusing.

    Only a succeeded launch whose completion evidence still verifies, under a
    sealed lifecycle that provably passed execution, qualifies.  FINAL_REVIEW is
    entered only after the execute producer bound the candidate (and its wave
    proof); RECOVER only from a handback of that point; EXECUTE only with a
    retained recovery continuation (``CONTINUATION_SCHEMA``, bound to the state's
    current candidate) or a repair proof (``_repair_proof``), because a bare
    EXECUTE stage, or any other decision, proves nothing about the execution.
    The lifecycle then needs the retained activity, never the outer runtime again.
    """
    from .ownership import OwnershipRefused
    if launch is None or launch["state"] != "completed_succeeded":
        return False
    from .recovery_integration import CONTINUATION_SCHEMA
    state = store.get_frontend_policy_state(repository_id=token.repository_id, run_id=token.run_id)
    retained = _retained_outer_completion(store, activity_id)
    if state is None or retained is None:
        return False
    decision = state.decision_json
    continued = (state.stage == "EXECUTE" and isinstance(decision, dict)
                 and decision.get("schema") == CONTINUATION_SCHEMA and decision.get("candidate_hash") == state.candidate_hash)
    if (state.stage not in {"FINAL_REVIEW", "RECOVER"} and not continued
            and not (state.stage == "EXECUTE" and _repair_proof(store, token, state))):
        return False
    try:
        store._verified_evidence(retained[1])
    except OwnershipRefused:
        return False
    return True


def refence_unlaunched_outer(store, token, activity_id: str, preparation_id: str) -> None:
    """Put the retained, qualified and never launched outer (its workspace and its activity) on this owner's fence.

    A review, the seal's checks and the outer's own launch all capture from, and parent under, the outer, and each
    needs it on the current fence.  No receipt hashes the workspace row of an outer that never ran, so (unlike a
    settled outer, see ``rebind_retained_child``) the row itself moves too.  An outer already on this fence (the
    run's first owner) is left exactly as it is.
    """
    with store.read_transaction() as tx:
        generation = tx.execute("SELECT generation FROM authority_activities WHERE id=?", (activity_id,)).fetchone()
    if generation is not None and generation["generation"] == token.generation:
        return
    rebind_retained_child(store, token, None, preparation_id)
    rebind_retained_child(store, token, activity_id, preparation_id)


def rebind_retained_child(store, token, activity_id: str | None, preparation_id: str):
    """F51: put one retained child on the resumed owner's fence.

    The workspace takes the existing READY revalidation, which refuses while any
    launch on it is unsettled.  F51b: a child with an activity may have a sealed
    receipt that hashes its workspace row (the outer's execution receipt), so
    that row is revalidated without a write and only the pending or active
    activity bound to it moves to this generation; capture and candidate
    resolution accept the retained row through that activity.  A preparation
    with no activity yet (no receipt can bind it) is rebound as before.
    Nothing is launched, debited or re-qualified.
    """
    from .ownership import OwnershipRefused, assert_owner
    try:
        ready = revalidate_ready_fence(store, token, preparation_id, rebind=activity_id is None)
    except WorkspaceRefused as error:
        raise SupervisorRefused(error.code) from error
    if activity_id is None:
        return ready
    bound = ("FROM authority_activities a WHERE a.id=? AND a.repository_id=? AND a.run_id=? AND a.state IN ({}) "
             "AND EXISTS (SELECT 1 FROM authority_child_bindings b JOIN context_workspaces w "
             "ON w.preparation_id=b.workspace_preparation_id WHERE b.activity_id=a.id AND w.preparation_id=? "
             "AND w.generation<=? AND w.state='ready')")
    scope = (activity_id, token.repository_id, token.run_id, preparation_id, token.generation)
    with store.fenced_operation(token):
        with store.transaction() as tx:
            assert_owner(tx, token)
            changed = tx.execute(
                "UPDATE authority_activities SET generation=?,updated_at=? WHERE id IN (SELECT a.id "
                + bound.format("'pending','active'") + ")", (token.generation, store._now(), *scope)).rowcount
            # F51c: this child's own activity, already settled succeeded by this run (the outer between
            # settle and DONE), is never re-fenced; nothing captures from it again.  Same predicates,
            # same transaction (R1-1): another run's or workspace's activity never qualifies.
            settled = changed == 0 and tx.execute("SELECT 1 " + bound.format("'succeeded'"), scope).fetchone()
    if changed != 1 and not settled:
        raise OwnershipRefused("FENCE_REVOKED")
    return ready


def _refuse_undelivered_retained_wave(store, invocation, activity_id: str, intent_id: str) -> None:
    from .supervisor import _WAVE_FAILURE_REASONS, _gsd_wave_completion_code, _managed_command_requires_wave_proof
    required = _managed_command_requires_wave_proof(invocation)
    code = None if required is None else _gsd_wave_completion_code(
        store, activity_id, intent_id, require_wave=required)
    if code is not None:
        raise SupervisorRefused(code if code in _WAVE_FAILURE_REASONS else "WAVE_EXECUTION_UNPROVEN")


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


def seal_from_draft(store, token, *, command_mode: str, draft: object, runtime_hash: str, candidate_hash: str,
                    review: Callable | None = None):
    """Seal an explicit operator-authored acceptance draft after outer qualification.

    A draft that carries ``"spec_review": "native"`` is sealed only after ``review(draft_row)`` has produced an
    accepted native review of that exact persisted draft: the gate is ``require_spec_review_accepted``, and a native
    draft with no ``review`` is refused, never sealed unreviewed.  Without the key nothing changes.
    """
    from .frontend_policy import FrontendPolicyRefused
    from .managed import build_frontend_acceptance_draft, seal_frontend_policy
    from .run_policy import RunPolicyRefused
    allowed = {"draft_id", "revision", "command_mode", "criteria", "exclusions", "global_invariants", "spec_review"}
    if (not isinstance(draft, dict) or set(draft) - allowed
            or not {"criteria", "exclusions", "global_invariants"} <= set(draft)
            or draft.get("spec_review", "native") != "native"):
        raise SupervisorRefused("ACCEPTANCE_DRAFT_INVALID")
    if draft.get("spec_review") == "native" and review is None:
        raise SupervisorRefused("SPEC_REVIEW_REQUIRED")
    mode = draft.get("command_mode", command_mode)
    legacy = store.get_acceptance_contract(repository_id=token.repository_id, run_id=token.run_id)
    if legacy is None:
        raise SupervisorRefused("ACCEPTANCE_CONTRACT_REQUIRED")
    invalid = (FrontendPolicyRefused, RunPolicyRefused, KeyError, TypeError, ValueError)
    try:
        material = build_frontend_acceptance_draft(
            objective_digest=legacy.material["objective_digest"], criteria=draft["criteria"],
            exclusions=draft["exclusions"], global_invariants=draft["global_invariants"],
            requested_runtime_hash=runtime_hash, effective_runtime_hash=runtime_hash,
            candidate_hash=candidate_hash, generation=legacy.generation, command_mode=mode)
        draft_id, revision = str(draft.get("draft_id", "acceptance")), int(draft.get("revision", 1))
        # The draft is persisted first (idempotently: `freeze` replays this create), so it can be reviewed unsealed.
        row = None if review is None else store.create_acceptance_draft(
            token, draft_id=draft_id, revision=revision, acceptance_contract_hash=legacy.contract_hash,
            material=material)
    except invalid as error:
        raise SupervisorRefused(getattr(error, "code", "ACCEPTANCE_DRAFT_INVALID")) from error
    if review is not None:
        # The seal below re-creates and seals exactly this row; a revised draft is another row with no record.
        review(row)
        require_spec_review_accepted(store, token, row)
    try:
        return seal_frontend_policy(store, token, frontend=mode, draft_id=draft_id, revision=revision,
                                    material=material)
    except invalid as error:
        raise SupervisorRefused(getattr(error, "code", "ACCEPTANCE_DRAFT_INVALID")) from error


def drive_managed_session(store, token, context, session: ManagedHostSession, *, acceptance_draft=None) -> int:
    """Run the managed host: legacy single execution, or the sealed frontend lifecycle."""
    from .frontend_lifecycle import LifecycleProducers, drive_frontend_lifecycle
    from .frontend_policy import FrontendPolicyController
    from .recovery_producer import produce_recovery, produce_repair, repair_unfinished
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
        native = isinstance(acceptance_draft, dict) and acceptance_draft.get("spec_review") == "native"
        if retained_outer is None and native:
            # An opted-in run's outer waits qualified through the spec review, so a crash there leaves it for a
            # resumed owner, whose capture, seal checks and launch need it on the new fence.
            refence_unlaunched_outer(store, token, request.activity_id, session.ready.id)
        sealed = store.get_sealed_acceptance(repository_id=token.repository_id, run_id=token.run_id)
        # Native review and sealed checks cross a channel-less supervisor; the
        # worker channel stays with the outer orchestrator only.
        review_supervisor = Supervisor(store, token, evidence_root=session.evidence_root)
        # An opted-in draft sealed by an owner that died before the lifecycle state existed replays its seal here
        # (idempotent: draft, record and seal are all retained); it never falls through to the unsealed single launch.
        reseal = native and sealed is not None and store.get_frontend_policy_state(
            repository_id=token.repository_id, run_id=token.run_id) is None
        if (sealed is None or reseal) and acceptance_draft is not None:
            review = None
            if native:
                # The review runs between the outer's qualification and the seal, with nothing in between: the
                # outer's runtime receipt is fresh for only a few minutes.
                def review(draft_row):
                    return produce_spec_review(
                        store, token, supervisor=review_supervisor, seam=session.seam,
                        parent_activity_id=request.activity_id, preparation=session.ready, draft=draft_row,
                        timeout_seconds=session.timeout_seconds)
            seal_from_draft(store, token, command_mode=str(session.invocation[0]), draft=acceptance_draft,
                            runtime_hash=request.runtime_identity, candidate_hash=session.ready.input_digest,
                            review=review)
            sealed = store.get_sealed_acceptance(repository_id=token.repository_id, run_id=token.run_id)
        state = None if sealed is None else store.get_frontend_policy_state(
            repository_id=token.repository_id, run_id=token.run_id)
        if sealed is None or state is None:
            if retained_outer is not None:
                # The one legacy execution already completed; replay reports it without a second launch,
                # after the wave-delivery check a crash may have skipped (F37a).
                _refuse_undelivered_retained_wave(store, session.invocation, request.activity_id,
                                                  retained_outer[0].intent_id)
                return 0
            returncode, outcome["handle"], _result = session.execute(request, adapter)
            return returncode
        controller = FrontendPolicyController(store, token, command_mode=sealed.material["command_mode"])

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

        def recover(packet):
            return produce_recovery(store, token, supervisor=review_supervisor, controller=controller,
                                    seam=session.seam, parent_activity_id=request.activity_id,
                                    preparation=session.ready, packet=packet, timeout_seconds=session.timeout_seconds)

        def repair(_frozen, failed):
            produce_repair(store, token, supervisor=review_supervisor, controller=controller, seam=session.seam,
                           parent_activity_id=request.activity_id, preparation=session.ready,
                           failed_criteria=failed, timeout_seconds=session.timeout_seconds)

        def repair_open():
            return repair_unfinished(store, token, supervisor=review_supervisor, controller=controller,
                                     seam=session.seam, parent_activity_id=request.activity_id,
                                     preparation=session.ready)

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
            producers=LifecycleProducers(execute=execute, final_review=final_review, recover=recover, settle=settle,
                                         repair=repair, repair_unfinished=repair_open))
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
