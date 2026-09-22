"""Production preparation using the existing ControlStore workspace journal.

Only an in-process supervisor receives the owner capability. The callback
must finish supervising its children before returning; it must not hand the
capability to a worker or start an unobserved background supervisor.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from run_context import RunContext
from run_state.ownership import OwnerToken
from run_state.state import ControlStore

MANAGED_WRITER_VERSION = "ffs-supervisor/1"


def validate_capacity_policy(value: object) -> dict | None:
    """An explicit operator revision is separate from the sealed run request."""
    if value is None:
        return None
    if (not isinstance(value, dict)
            or set(value) != {'worker_capacity', 'expected_revision', 'request_key'}
            or type(value['worker_capacity']) is not int
            or not 1 <= value['worker_capacity'] <= 9_223_372_036_854_775_807
            or type(value['expected_revision']) is not int
            or not 0 <= value['expected_revision'] < 9_223_372_036_854_775_807
            or not isinstance(value['request_key'], str) or not value['request_key']
            or len(value['request_key']) > 256 or not value['request_key'].isprintable()
            or value['request_key'] != value['request_key'].strip()):
        raise ValueError('CAPACITY_POLICY_INVALID')
    return dict(value)


def build_frontend_acceptance_draft(
    *, objective_digest: str, criteria: list[dict], exclusions: list[dict],
    global_invariants: list[dict], requested_runtime_hash: str,
    effective_runtime_hash: str, candidate_hash: str, generation: int,
    command_mode: str,
) -> dict:
    """Build explicit E3 acceptance material without inventing a dispatch hash.

    This is deliberately an ingress helper only.  It neither writes the
    ControlStore nor seals a contract: the existing initial-contract and
    operator-amendment adapters remain authoritative, and E6 will choose the
    frontend lifecycle point at which a draft is sealed.
    """
    from run_state.run_policy import build_draft_material

    return build_draft_material(
        objective_digest=objective_digest, criteria=criteria, exclusions=exclusions,
        global_invariants=global_invariants, requested_runtime_hash=requested_runtime_hash,
        effective_runtime_hash=effective_runtime_hash, candidate_hash=candidate_hash,
        generation=generation, command_mode=command_mode,
    )


def seal_frontend_policy(
    store: ControlStore, token: OwnerToken, *, frontend: str, draft_id: str,
    revision: int, material: dict,
):
    """E6 ingress seam for CLI/supervisor after runtime qualification.

    The caller must supply the qualified requested/effective runtime and the
    current prepared candidate in ``material``.  This helper does not launch a
    model or accept a caller's approval; it only routes the frozen draft
    through the existing legacy acceptance binding and ControlStore seal.
    """
    from run_state.frontend_policy import FrontendPolicyController

    return FrontendPolicyController(store, token, command_mode=frontend).freeze(
        draft_id=draft_id, revision=revision, material=material,
    )


def _publish_callback_result(context: RunContext, returncode: int) -> dict[str, str]:
    root = Path(context.evidence_root) / "managed"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    value = {
        "schema": "ffs.managed-callback-result/v1",
        "repository_id": context.repository_id,
        "run_id": context.run_id,
        "activity_id": context.activity_id,
        "generation": context.generation,
        "workspace": context.workspace,
        "input_digest": context.input_digest,
        "returncode": returncode,
    }
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    name = "callback-" + context.activity_id + "-" + uuid.uuid4().hex + ".json"
    fd = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        try:
            (root / name).unlink()
        except OSError:
            pass
        raise
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return {"locator": str(root / name), "sha256": hashlib.sha256(raw).hexdigest()}


def _settle_managed_activity(
    store: ControlStore, token: OwnerToken, context: RunContext, returncode: int,
) -> None:
    from run_state.ownership import OwnershipRefused

    try:
        current = store.get_activity(context.activity_id)
        if current.state != "active" or returncode != 0:
            return
        store.transition_activity(
            token, context.activity_id, expected="active", new="succeeded",
            result=_publish_callback_result(context, returncode),
            reason="managed callback completed",
        )
    except (OSError, OwnershipRefused):
        if returncode == 0:
            raise


def _configure_managed_callback(
    on_ready: Callable[[ControlStore, OwnerToken, RunContext], int],
    *,
    dispatch_limit: int,
    token_limit: int,
    worker_capacity: int,
    policy_tier: str = "medium",
    capacity_policy: dict | None = None,
) -> Callable[[ControlStore, OwnerToken, RunContext], int]:
    """Configure immutable run authority before giving the live fence to a consumer.

    The selected upstream digest is already durable context material.  It is
    not an executable-closure or host-capability receipt, so this ingress must
    leave ``runtime_tuple_hash`` unset until a qualified adapter binds one.
    """
    def configured(store: ControlStore, token: OwnerToken, context: RunContext) -> int:
        # The ingress preflight binds an observed epoch to this store. Recheck
        # immediately around the first managed mutation so a rollback/handoff
        # race cannot spend quota or invoke a consumer under a stale writer.
        from run_state.migration import assert_managed_epoch

        run_id = getattr(context, "run_id", None)
        if isinstance(store, ControlStore) and isinstance(run_id, str):
            assert_managed_epoch(store, run_id)
        store.configure_run_limits(
            token,
            dispatch_limit=dispatch_limit,
            token_limit=token_limit,
            worker_capacity=worker_capacity,
        )
        if capacity_policy is not None:
            store.revise_capacity_policy(token, **validate_capacity_policy(capacity_policy))
        if isinstance(store, ControlStore):
            from process_identity import ProcessIdentity
            # Resume reads the existing cumulative ledger. A fresh clock sample
            # must never be mistaken for immutable configuration replay.
            budget = store.get_run_policy_budget(
                repository_id=token.repository_id, run_id=token.run_id,
            )
            if budget is None:
                store.configure_run_policy_budget(
                    token, tier=policy_tier, clock_boot_id=ProcessIdentity.current().boot_id,
                    clock_monotonic_ns=time.monotonic_ns(),
                )
        if isinstance(store, ControlStore) and isinstance(run_id, str):
            assert_managed_epoch(store, run_id)
        try:
            returncode = on_ready(store, token, context)
        except BaseException:
            raise
        _settle_managed_activity(store, token, context, returncode)
        return returncode

    return configured


def prepare_managed_run(
    *,
    objective: str,
    state_root: str | Path,
    selection_manifest: str | Path,
    upstream_runtime_manifest: str | Path,
    upstream_runtime_sha256: str,
    request_key: str,
    command: tuple[str, ...],
    dispatch_limit: int,
    token_limit: int,
    on_ready: Callable[[ControlStore, OwnerToken, RunContext], int],
    worker_capacity: int | None = None,
    run_id: str | None = None,
    activity: str = "plan",
    scope: str = "",
    resume: bool = False,
    revise: bool = False,
    host_request=None,
    ceremony_estimate: dict | None = None,
    capacity_policy: dict | None = None,
) -> int:
    """Prepare an explicit snapshot and run ``on_ready`` under its live fence.

    Resolve the repository from the invoking working directory. The existing
    context resolver preserves GSD_RUN_ID/GSD_RESUME and legacy ID mappings;
    the selected runtime's upstream functions resolve planning identifiers.
    Refusals use the existing structured CLI contract and return nonzero.
    Required descriptors are validated before any repository/state writes.
    """
    from run_state.cli import _cmd_fixture_start, _fixture_refusal
    from run_state.commands import ManagedCommandRefused, parse_managed_command
    from run_state.run_policy import classify_ceremony, RunPolicyRefused

    try:
        tier = classify_ceremony(ceremony_estimate)
    except RunPolicyRefused as error:
        return _fixture_refusal(error.code, exit_code=2)
    try:
        capacity_policy = validate_capacity_policy(capacity_policy)
    except ValueError:
        return _fixture_refusal('CAPACITY_POLICY_INVALID', exit_code=2)

    # Without an operator ceiling, only the cumulative launch allowance can
    # bound this compatibility counter. Actual concurrency is shared-resource
    # derived before each launch; there is no implicit three-worker policy.
    if worker_capacity is None:
        worker_capacity = dispatch_limit
    if not callable(on_ready) or not isinstance(objective, str) or not objective.strip():
        return _fixture_refusal("INVALID_REQUEST", exit_code=2)
    if not state_root:
        return _fixture_refusal("STATE_ROOT_REQUIRED", exit_code=2)
    if not selection_manifest:
        return _fixture_refusal("INPUT_SELECTION_REQUIRED", exit_code=2)
    if not upstream_runtime_manifest or not upstream_runtime_sha256:
        return _fixture_refusal("UPSTREAM_RUNTIME_REQUIRED", exit_code=2)
    try:
        drive = parse_managed_command(command)
    except ManagedCommandRefused as error:
        return _fixture_refusal(error.code, exit_code=2)
    if (activity, scope) != (drive.activity, drive.scope):
        return _fixture_refusal("MANAGED_COMMAND_CONTEXT_CONFLICT", exit_code=2)
    if (
        not isinstance(request_key, str) or not request_key
        or activity not in {"plan", "execute", "review"}
        or any(isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 9_223_372_036_854_775_807
               for value in (dispatch_limit, token_limit, worker_capacity))
    ):
        return _fixture_refusal("INVALID_REQUEST", exit_code=2)

    args = argparse.Namespace(
        objective=objective, state_root=str(state_root),
        selection_manifest=str(selection_manifest),
        upstream_runtime_manifest=str(upstream_runtime_manifest),
        upstream_runtime_sha256=upstream_runtime_sha256, request_key=request_key,
        run_id=run_id, activity=activity, scope=scope, resume=resume, revise=revise,
        selected_input=[], managed_request_material={
            "writer_version": MANAGED_WRITER_VERSION,
            "skill": drive.skill, "arguments": list(drive.arguments),
            "accepted_requirement_ids": [
                "objective:" + hashlib.sha256(objective.strip().encode("utf-8")).hexdigest()
            ],
            "dispatch_limit": dispatch_limit, "token_limit": token_limit,
            "worker_capacity": worker_capacity,
            "policy_tier": tier, "ceremony_estimate": ceremony_estimate,
            "upstream_runtime_sha256": upstream_runtime_sha256,
            "host_request": None if host_request is None else host_request.material(),
        },
    )
    return _cmd_fixture_start(
        args,
        on_ready=_configure_managed_callback(
            on_ready,
            dispatch_limit=dispatch_limit,
            token_limit=token_limit,
            worker_capacity=worker_capacity,
            policy_tier=tier,
            capacity_policy=capacity_policy,
        ),
    )


_FRONTEND_ACTIVITIES = {
    "feature-spec": "plan", "fix": "plan", "code-uplift": "review",
    "feature-implement": "execute", "task-swarm": "execute",
}
_FRONTEND_OPERATIONS = {
    "feature-spec": "author", "fix": "investigate", "code-uplift": "review",
    "feature-implement": "implement", "task-swarm": "orchestrate",
}


def prepare_frontend_run(
    *, frontend: str, objective: str, state_root: str | Path, selection,
    upstream_runtime_manifest: str | Path, upstream_runtime_sha256: str,
    request_key: str, dispatch_limit: int, token_limit: int,
    worker_capacity: int | None = None, run_id: str | None = None,
    resume: bool = False, revise: bool = False, invocation_text: str = "",
    host_request=None, accepted_requirement_ids: tuple[str, ...] | None = None,
    ceremony_estimate: dict | None = None,
    capacity_policy: dict | None = None,
    scope: str = "", model_request: dict | None = None,
    review_catalog: tuple[str, str] | None = None, acceptance_draft: dict | None = None,
) -> int:
    """Admit authoring/investigation under the same writer and live owner fence.

    No ambient caller receives write authority when this callback returns. Host
    execution, including the original frontend skill body, remains unqualified.
    """
    from run_state.cli import _cmd_fixture_start, _fixture_refusal
    from run_state.selection import InputSelection
    from run_state.run_policy import classify_ceremony, RunPolicyRefused

    try:
        tier = classify_ceremony(ceremony_estimate)
    except RunPolicyRefused as error:
        return _fixture_refusal(error.code, exit_code=2)
    try:
        capacity_policy = validate_capacity_policy(capacity_policy)
    except ValueError:
        return _fixture_refusal('CAPACITY_POLICY_INVALID', exit_code=2)

    if worker_capacity is None:
        worker_capacity = dispatch_limit
    if accepted_requirement_ids is None:
        objective_id = "objective:" + hashlib.sha256(objective.strip().encode("utf-8")).hexdigest()
        accepted_requirement_ids = (objective_id,)
    if (
        frontend not in _FRONTEND_ACTIVITIES or not isinstance(selection, InputSelection)
        or not isinstance(invocation_text, str) or "\0" in invocation_text
        or len(invocation_text.encode("utf-8")) > 16384
        or not isinstance(objective, str) or not objective.strip()
        or not state_root or not upstream_runtime_manifest or not upstream_runtime_sha256
        or not isinstance(request_key, str) or not request_key
        or not isinstance(scope, str)
        or not isinstance(accepted_requirement_ids, tuple)
        or not accepted_requirement_ids
        or any(not isinstance(item, str) or not item.strip() or item != item.strip()
               or len(item.encode("utf-8")) > 256 for item in accepted_requirement_ids)
        or len(set(accepted_requirement_ids)) != len(accepted_requirement_ids)
        or any(isinstance(value, bool) or not isinstance(value, int)
               or not 0 < value <= 9_223_372_036_854_775_807
               for value in (dispatch_limit, token_limit, worker_capacity))
    ):
        return _fixture_refusal("INVALID_REQUEST", exit_code=2)

    def execute(store, token, context):
        from run_state.cli import _load_upstream_runtime
        from run_state.ownership import OwnershipRefused
        from run_state.supervisor import SupervisorRefused, run_managed_command
        # Resource admission must use the same verified runtime descriptor as
        # workspace preparation, including on resume. Revalidate before this
        # callback's first mutation, as managed-start does at its launch seam.
        upstream_runtime, _descriptor_digest = _load_upstream_runtime(args)
        with store.transaction() as tx:
            activity = store._assert_activity_binding(tx, token, context.activity_id)
            if activity["kind"] != _FRONTEND_ACTIVITIES[frontend]:
                raise OwnershipRefused("MANAGED_COMMAND_CONTEXT_CONFLICT")
            binding = tx.execute(
                "SELECT 1 FROM authority_event_keys WHERE activity_id=? AND idempotency_key=?",
                (context.activity_id, "frontend-operation"),
            ).fetchone()
            if binding is None and (
                activity["runtime_tuple_hash"] is not None
                or activity["state"] != "active"
                or tx.execute(
                    "SELECT 1 FROM authority_launch_intents WHERE activity_id=? UNION "
                    "SELECT 1 FROM authority_child_bindings WHERE parent_activity_id=?",
                    (context.activity_id, context.activity_id),
                ).fetchone() is not None
            ):
                raise OwnershipRefused("MANAGED_COMMAND_CONTEXT_CONFLICT")
            store._record_event_once_tx(
                tx, token, context.activity_id, "frontend-operation",
                {"frontend": frontend, "operation": _FRONTEND_OPERATIONS[frontend],
                 "invocation_text": invocation_text},
            )
        try:
            return run_managed_command(
                store, token, context, command=(frontend,), request_key=request_key,
                dispatch_limit=dispatch_limit, token_limit=token_limit,
                host_request=host_request, upstream_runtime=upstream_runtime,
                model_request=model_request, review_catalog=review_catalog,
                acceptance_draft=acceptance_draft,
            )
        except SupervisorRefused as error:
            return _fixture_refusal(
                error.code, run_id=context.run_id, exit_code=78,
                cause="the selected host backend has not demonstrated managed admission",
                recovery_action={"action": "qualify_host_adapter"},
            )

    args = argparse.Namespace(
        objective=objective, state_root=str(state_root), selection_manifest=None,
        selection_object=selection.canonical_manifest,
        upstream_runtime_manifest=str(upstream_runtime_manifest),
        upstream_runtime_sha256=upstream_runtime_sha256, request_key=request_key,
        run_id=run_id, activity=_FRONTEND_ACTIVITIES[frontend], scope=scope,
        resume=resume, revise=revise, selected_input=[], managed_request_material={
            "writer_version": MANAGED_WRITER_VERSION, "frontend": frontend,
            "invocation_text": invocation_text,
            "operation": _FRONTEND_OPERATIONS[frontend],
            "dispatch_limit": dispatch_limit, "token_limit": token_limit,
            "worker_capacity": worker_capacity,
            "policy_tier": tier, "ceremony_estimate": ceremony_estimate,
            "accepted_requirement_ids": sorted(accepted_requirement_ids),
            "upstream_runtime_sha256": upstream_runtime_sha256,
            "host_request": None if host_request is None else host_request.material(),
        },
    )
    return _cmd_fixture_start(
        args, on_ready=_configure_managed_callback(
            execute, dispatch_limit=dispatch_limit, token_limit=token_limit,
            worker_capacity=worker_capacity,
            policy_tier=tier,
            capacity_policy=capacity_policy,
        ),
    )
