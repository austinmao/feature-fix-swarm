"""CLI: python -m run_state.cli <command> ..."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import sqlite3
import stat
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from run_state.state import (
    RunStore, ControlStore, ControlStoreRefused, MigrationRawMutationRefused,
    DEFAULT_DB, VALID_STATES, UnknownRunError,
)


_FIXTURE_CODES = {
    "INVALID_REQUEST": 2, "INVALID_RUN_ID": 2, "CONFLICTING_RUN_ID": 2, "STATE_ROOT_REQUIRED": 2,
    "UNSAFE_STATE_ROOT": 2, "IDEMPOTENCY_CONFLICT": 2,
    "MANAGED_COMMAND_CONTEXT_CONFLICT": 2,
    "RUN_NOT_FOUND": 3, "AMBIGUOUS_RUN": 3, "RESUME_REQUIRED": 3,
    "OWNER_LIVE": 3, "OBJECTIVE_RESERVED": 3, "WORKSPACE_REGISTERED": 3,
    "FENCE_REVOKED": 4,
    "WORKSPACE_PREPARE_FAILED": 5, "WORKSPACE_OWNERSHIP_MISMATCH": 5,
    "WORKSPACE_MISSING": 5, "SELECTED_INPUT_UNSUPPORTED": 5,
    "INPUT_SELECTION_CHANGED": 5, "INPUT_SELECTION_REQUIRED": 2,
    "SELECTION_INPUT_MISSING": 2, "SELECTION_REPOSITORY_MISMATCH": 2,
    "SELECTION_BASE_MISMATCH": 2, "UPSTREAM_CHANGED": 3,
    "UPSTREAM_RUNTIME_REQUIRED": 2, "UPSTREAM_RUNTIME_DRIFT": 2,
    "UPSTREAM_RUNTIME_CHANGED": 3, "INVALID_UPSTREAM_SCOPE": 2,
    "UPSTREAM_INVALID": 2, "UPSTREAM_ESCAPE": 2,
    "UPSTREAM_BINDING_INCOMPLETE": 5, "SNAPSHOT_INCOMPLETE": 5,
    "OWNER_UNKNOWN": 6, "WORKSPACE_IDENTITY_UNKNOWN": 6,
}


def _fixture_refusal(
    code: str,
    *,
    run_id: str | None = None,
    candidates=None,
    exit_code: int | None = None,
    **extra,
) -> int:
    actions = {
        "AMBIGUOUS_RUN": "select_run",
        "RESUME_REQUIRED": "resume_run",
        "SELECTED_INPUT_UNSUPPORTED": "remove_selected_inputs",
        "WORKSPACE_MISSING": "recover_workspace",
        "WORKSPACE_PREPARE_FAILED": "inspect_owned_resources",
    }
    payload = {
        "schema_version": 1, "ok": False, "code": code,
        "problem": code.replace("_", " ").lower(),
        "cause": "the fixture authority could not prove the requested transition",
        "fix": "follow the typed recovery action and retry",
        "docs": "specs/014-parallel-host-parity/contracts/run-context.md",
        "recovery_action": {"action": actions.get(code, "correct_request")},
        "candidates": candidates or [],
    }
    if run_id is not None:
        payload["run_id"] = run_id
    payload.update(extra)
    print(json.dumps(payload, sort_keys=True))
    return _FIXTURE_CODES.get(code, 5) if exit_code is None else exit_code


def _managed_run_refusals() -> tuple[type[Exception], ...]:
    """Typed refusals which can escape a managed host run's callback."""
    from run_state.frontend_policy import FrontendPolicyRefused
    from run_state.run_policy import RunPolicyRefused
    from run_state.supervisor import SupervisorRefused
    return SupervisorRefused, FrontendPolicyRefused, RunPolicyRefused


_REFUSAL_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


def _refusal_detail(error: Exception) -> str | None:
    """The underlying error's type and typed code; never its free-text message."""
    cause = error.__cause__
    if cause is None:
        return None
    code = getattr(cause, "code", None)
    if isinstance(code, str) and _REFUSAL_CODE.fullmatch(code):
        return type(cause).__name__ + ": " + code
    return type(cause).__name__


# Supervisor codes whose cause is this request key's retained launch, not the host adapter.
_REQUEST_KEY_REFUSALS = {
    "RETAINED_RUNTIME_NOT_REUSABLE": (
        "the outer runtime retained for this request key was consumed by its qualification",
        "resume_with_new_request_key"),
    # A new request key would start a new outer run, repeating its completed work.
    "CHILD_RUNTIME_NOT_REUSABLE": (
        "a wave child or final reviewer runtime retained for this run was consumed by its qualification",
        "inspect_retained_child"),
    # The launch may have succeeded, so a new request key could repeat its work.
    "REQUEST_ALREADY_COMPLETED": (
        "a launch for this request key already completed; a new request key would run it again",
        "inspect_completed_launch"),
    "INTENT_RECONCILIATION_REQUIRED": (
        "a launch for this request key has not settled; only owner-fence reconciliation may settle it",
        "reconcile_intent"),
}


def _managed_run_refusal(error: Exception, *, run_id: str) -> int:
    """The managed-run JSON envelope (exit 78) for one of ``_managed_run_refusals``."""
    from run_state.supervisor import SupervisorRefused
    extra = {}
    detail = _refusal_detail(error)
    if detail is not None:
        extra["detail"] = detail
    if error.code in _REQUEST_KEY_REFUSALS:
        cause, action = _REQUEST_KEY_REFUSALS[error.code]
        extra.update(cause=cause, recovery_action={"action": action})
    elif isinstance(error, SupervisorRefused):
        extra.update(cause="the selected host backend has not demonstrated managed admission",
                     recovery_action={"action": "qualify_host_adapter"})
    else:
        extra["cause"] = "the managed run policy refused the transition"
    return _fixture_refusal(error.code, run_id=run_id, exit_code=78, **extra)


def _parse_tokens(value):
    """Parse '250K' / '1.5M' / '1B' / '2T' / '250000' to int.

    Accepts case-insensitive suffix. Returns int, or raises
    argparse.ArgumentTypeError on invalid input.
    """
    if value is None:
        return None
    s = str(value).strip().upper()
    m = re.match(r'^(\d+(?:\.\d+)?)\s*([KMBT]?)$', s)
    if not m:
        raise argparse.ArgumentTypeError(
            f"invalid token count: {value!r} (expected '250000' or '250K'/'1.5M'/'1B'/'2T')"
        )
    num, suffix = m.group(1), m.group(2)
    multipliers = {
        "": 1,
        "K": 1_000,
        "M": 1_000_000,
        "B": 1_000_000_000,
        "T": 1_000_000_000_000,
    }
    return int(float(num) * multipliers[suffix])


def _store() -> RunStore:
    db = Path(os.environ.get("RUN_STATE_DB", str(DEFAULT_DB)))
    return RunStore(db)


def cmd_start(args: argparse.Namespace) -> int:
    if getattr(args, "fixture_mode", False):
        return _cmd_fixture_start(args)
    store = _store()
    run_id = store.create_run(
        skill=args.skill,
        objective=args.objective,
        session_id=args.session_id,
        tokens_budget=args.tokens,
        worktree=args.worktree,
    )
    print(json.dumps({"run_id": run_id}))
    return 0


def cmd_register_repository(args: argparse.Namespace) -> int:
    """Create only the durable repository binding required by managed ingress."""
    from run_context import (
        ContextRefused,
        register_repository,
        resolve_repository,
        validate_state_root,
    )
    from run_state.state import ControlStoreRefused

    try:
        repository = resolve_repository(Path.cwd())
        state_root = validate_state_root(Path(args.state_root), repository)
        store = ControlStore(state_root / "control.sqlite3")
        repository_id = register_repository(store, repository, state_root)
    except (ContextRefused, ControlStoreRefused, OSError) as error:
        return _fixture_refusal(
            getattr(error, "code", "REPOSITORY_REGISTRATION_FAILED"),
            cause="the repository identity could not be bound to the requested control store",
            recovery_action={"action": "inspect_repository_registration"},
        )
    print(json.dumps({
        "schema_version": 1,
        "ok": True,
        "repository_id": repository_id,
        "state_root": str(state_root),
    }, sort_keys=True))
    return 0


def _parse_ceremony_estimate(value: str) -> dict:
    from run_state.run_policy import RunPolicyRefused, classify_ceremony
    try:
        estimate = json.loads(value)
        if estimate is None:
            raise ValueError
        classify_ceremony(estimate)
        return estimate
    except (ValueError, TypeError, RunPolicyRefused) as error:
        raise argparse.ArgumentTypeError("POLICY_ESTIMATE_INVALID: expected files, loc, protected JSON") from error


def _parse_capacity_policy(value: str) -> dict:
    from run_state.managed import validate_capacity_policy
    try:
        policy = validate_capacity_policy(json.loads(value))
        if policy is None:
            raise ValueError
        return policy
    except (ValueError, TypeError) as error:
        raise argparse.ArgumentTypeError(
            'CAPACITY_POLICY_INVALID: expected worker_capacity, expected_revision, request_key JSON') from error


def cmd_managed_start(args: argparse.Namespace) -> int:
    from run_state.managed import prepare_managed_run
    from run_state.commands import ManagedCommandRefused, parse_managed_command

    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if (
        not command or args.dispatch_limit <= 0
        or args.token_limit is None or args.token_limit <= 0
        or (args.worker_capacity is not None and args.worker_capacity <= 0)
    ):
        return _fixture_refusal("INVALID_REQUEST", exit_code=2)

    try:
        drive = parse_managed_command(command)
    except ManagedCommandRefused as error:
        return _fixture_refusal(error.code, exit_code=2)
    try:
        host_request = _host_request_from_args(args)
        review_catalog = _review_catalog_from_args(args)
        acceptance_draft = _read_json_document(getattr(args, "acceptance_draft", None), label="acceptance draft")
    except Exception as error:
        from run_state.host_request import HostRequestRefused
        if not isinstance(error, HostRequestRefused):
            raise
        return _fixture_refusal(error.code, exit_code=2)
    if (
        args.activity is not None and args.activity != drive.activity
        or args.scope is not None and args.scope != drive.scope
    ):
        return _fixture_refusal("MANAGED_COMMAND_CONTEXT_CONFLICT", exit_code=2)

    def execute(store, token, context):
        from run_state.supervisor import run_managed_command

        try:
            upstream_runtime, _descriptor_digest = _load_upstream_runtime(args)
            return run_managed_command(
                store, token, context, command=command, request_key=args.request_key,
                dispatch_limit=args.dispatch_limit, token_limit=args.token_limit,
                host_request=host_request, upstream_runtime=upstream_runtime,
                model_request=_model_request_from_args(args), review_catalog=review_catalog,
                acceptance_draft=acceptance_draft,
            )
        except _managed_run_refusals() as error:
            return _managed_run_refusal(error, run_id=context.run_id)

    return prepare_managed_run(
        objective=args.objective, state_root=args.state_root,
        selection_manifest=args.selection_manifest,
        upstream_runtime_manifest=args.upstream_runtime_manifest,
        upstream_runtime_sha256=args.upstream_runtime_sha256,
        request_key=args.request_key, on_ready=execute, run_id=args.run_id,
        activity=drive.activity, scope=drive.scope, resume=args.resume,
        revise=args.revise, dispatch_limit=args.dispatch_limit,
        token_limit=args.token_limit, worker_capacity=args.worker_capacity,
        command=tuple(command), host_request=host_request,
        ceremony_estimate=getattr(args, "ceremony_estimate", None),
        capacity_policy=getattr(args, 'capacity_policy', None),
    )


def _host_request_from_args(args: argparse.Namespace):
    """Build one all-or-none host request before managed preparation effects."""
    from run_state.host_request import (
        HostRequestRefused, parse_claude_host_request, parse_codex_host_request,
    )
    host = getattr(args, "host", None)
    fields = (
        getattr(args, "host_runtime_home", None), getattr(args, "host_binary", None),
        getattr(args, "host_model_request", None), getattr(args, "host_sandbox", None),
        getattr(args, "host_network", None), getattr(args, "host_token_reservation", None),
        getattr(args, "host_timeout", None),
    )
    credential_source = getattr(args, "host_credential_source", None)
    if host is None and credential_source is None and all(value is None for value in fields):
        return None
    if host not in {"codex", "claude"} or any(value is None for value in fields):
        raise HostRequestRefused("HOST_REQUEST_INCOMPLETE")
    if host == "claude":
        if credential_source is None:
            raise HostRequestRefused("HOST_REQUEST_INCOMPLETE")
        return parse_claude_host_request(
            runtime_home=fields[0], credential_source=credential_source,
            binary=fields[1], model_request_json=fields[2], sandbox=fields[3],
            network_enabled=fields[4] == "enabled", token_reservation=fields[5],
            timeout_seconds=fields[6],
        )
    if credential_source is not None:
        raise HostRequestRefused("HOST_REQUEST_INCOMPLETE")
    return parse_codex_host_request(
        runtime_home=fields[0], binary=fields[1], model_request_json=fields[2],
        sandbox=fields[3], network_enabled=fields[4] == "enabled",
        token_reservation=fields[5], timeout_seconds=fields[6],
    )


def cmd_frontend_start(args: argparse.Namespace) -> int:
    """Build explicit selected input and enter the existing managed writer."""
    from run_context import ContextRefused, select_run_id
    from run_state.frontend_selection import build_frontend_selection, resume_frontend_selection
    from run_state.state import ControlStoreRefused
    from run_state.managed import prepare_frontend_run
    from run_state.upstream import UpstreamRefused
    from run_state.workspace import WorkspaceRefused

    if (
        not args.objective.strip() or not args.state_root or not args.request_key
        or "\0" in args.invocation_text or len(args.invocation_text.encode("utf-8")) > 16384
        or any(isinstance(value, bool) or not isinstance(value, int)
               or not 0 < value <= 9_223_372_036_854_775_807
               for value in (args.dispatch_limit, args.token_limit))
        or (args.worker_capacity is not None and (
            isinstance(args.worker_capacity, bool) or not isinstance(args.worker_capacity, int)
            or not 0 < args.worker_capacity <= 9_223_372_036_854_775_807))
    ):
        return _fixture_refusal("INVALID_REQUEST", exit_code=2)
    try:
        host_request = _host_request_from_args(args)
        review_catalog = _review_catalog_from_args(args)
        acceptance_draft = _read_json_document(getattr(args, "acceptance_draft", None), label="acceptance draft")
        # Verify the controller's pin before even reading selected source bytes.
        runtime, digest = _load_upstream_runtime(args)
        selection_arguments = {
            "selected_files": tuple(args.select_file), "deleted_files": tuple(args.delete_file),
            "required_context": tuple(args.required_context),
            "upstream": {"project": args.project, "workstream": args.workstream,
                         "session_key": args.session_key},
        }
        # Same contract as gsd-run.sh: only GSD_RESUME=1 resumes; 0 is an explicit fresh start.
        if args.resume or os.environ.get("GSD_RESUME") == "1":
            if not (args.run_id or os.environ.get("GSD_RUN_ID") or os.environ.get("FFS_RUN_ID")):
                return _fixture_refusal(
                    "RUN_NOT_FOUND", cause="resume requires the retained run identity",
                )
            run_id = select_run_id(args.run_id, os.environ)
            args.run_id = run_id
            selection = resume_frontend_selection(
                Path.cwd(), state_root=Path(args.state_root), run_id=run_id,
                runtime=runtime, runtime_manifest_sha256=digest,
                request_key=args.request_key, **selection_arguments,
            )
        else:
            selection = build_frontend_selection(Path.cwd(), **selection_arguments)
    except (ContextRefused, WorkspaceRefused, UpstreamRefused, ControlStoreRefused) as error:
        return _fixture_refusal(error.code, exit_code=_FIXTURE_CODES.get(error.code, 2))
    except Exception as error:
        from run_state.host_request import HostRequestRefused
        if not isinstance(error, HostRequestRefused):
            raise
        return _fixture_refusal(error.code, exit_code=2)
    return prepare_frontend_run(
        frontend=args.frontend, objective=args.objective, state_root=args.state_root,
        selection=selection, upstream_runtime_manifest=args.upstream_runtime_manifest,
        upstream_runtime_sha256=args.upstream_runtime_sha256,
        request_key=args.request_key, run_id=args.run_id, resume=args.resume,
        revise=args.revise, dispatch_limit=args.dispatch_limit,
        token_limit=args.token_limit, worker_capacity=args.worker_capacity,
        invocation_text=args.invocation_text, host_request=host_request,
        ceremony_estimate=getattr(args, "ceremony_estimate", None),
        capacity_policy=getattr(args, 'capacity_policy', None),
        scope=args.scope or "", model_request=_model_request_from_args(args),
        review_catalog=review_catalog, acceptance_draft=acceptance_draft,
    )


def _fixture_request(args: argparse.Namespace):
    from run_context import ContextRefused, ContextRequest, select_run_id
    from dataclasses import replace

    inherited = dict(os.environ)
    request = ContextRequest(
        cwd=Path.cwd(), operation="start", objective=args.objective,
        explicit_run_id=args.run_id, activity=args.activity,
        planning_scope=args.scope or "", resume=bool(args.resume or inherited.get("GSD_RESUME") == "1"),
        revise=bool(args.revise), request_key=args.request_key,
        selected_inputs=tuple(args.selected_input or ()), inherited=inherited,
    )
    minted = (
        request.explicit_run_id is None
        and not inherited.get("GSD_RUN_ID") and not inherited.get("FFS_RUN_ID")
    )
    run_id = select_run_id(request.explicit_run_id, inherited)
    if request.request_key is not None and (
        not request.request_key or len(request.request_key.encode("utf-8")) > 256
    ):
        raise ContextRefused("INVALID_REQUEST")
    # Freeze an anonymous selection once. Downstream resolution must never
    # mint a second ID from the same request.
    return replace(
        request, explicit_run_id=run_id, inherited={}, minted_run_id=minted,
    ), run_id


def _release_fixture_owner(store, token, *, preserve_unresolved_launches=False) -> None:
    from run_state.ownership import OwnershipRefused, assert_owner, release_owner

    try:
        with store.transaction() as tx:
            if preserve_unresolved_launches:
                assert_owner(tx, token)
                unresolved = tx.execute(
                    "SELECT 1 FROM authority_launch_intents i "
                    "JOIN authority_activities a ON a.id=i.activity_id "
                    "WHERE a.repository_id=? AND a.run_id=? AND i.state IN "
                    "('reserved','acknowledged','released_to_execute','reconcile_required','uncertain') "
                    "LIMIT 1", (token.repository_id, token.run_id),
                ).fetchone()
                if unresolved is not None:
                    # A callback exception is not proof its children stopped.
                    # Retain the reservation; eventual owner death requires
                    # native identity reconciliation before another dispatch.
                    return
            release_owner(tx, token)
    except OwnershipRefused as error:
        if error.code != "FENCE_REVOKED":
            raise


def _load_upstream_runtime(args: argparse.Namespace):
    """Validate a controller-supplied runtime descriptor before any effects."""
    from run_state.upstream import UpstreamRefused, UpstreamRuntime, _anchored_regular

    manifest = args.upstream_runtime_manifest
    digest = args.upstream_runtime_sha256
    if bool(manifest) != bool(digest):
        raise UpstreamRefused("UPSTREAM_RUNTIME_REQUIRED")
    if manifest is None:
        raise UpstreamRefused("UPSTREAM_RUNTIME_REQUIRED")
    if not isinstance(digest, str) or len(digest) != 64:
        raise UpstreamRefused("UPSTREAM_RUNTIME_DRIFT")
    try:
        raw, _ = _anchored_regular(Path(manifest))
        if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != digest:
            raise UpstreamRefused("UPSTREAM_RUNTIME_DRIFT")
        runtime = UpstreamRuntime.from_manifest(json.loads(raw.decode("utf-8")))
        # The descriptor hash alone does not bind the closed Node/module tree.
        runtime.verify()
        return runtime, digest
    except UpstreamRefused as error:
        if error.code == "UPSTREAM_RUNTIME_DRIFT":
            raise
        raise UpstreamRefused("UPSTREAM_RUNTIME_DRIFT") from error
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as error:
        raise UpstreamRefused("UPSTREAM_RUNTIME_DRIFT") from error


def _normalized_upstream_request(selection, run_id: str, runtime_manifest_sha256: str) -> dict:
    """Return the one partial durable binding used before resolver completion."""
    return {
        "project": selection.upstream.project,
        "workstream": selection.upstream.workstream,
        "session_key": selection.upstream.session_key or run_id,
        "runtime_manifest_sha256": runtime_manifest_sha256,
    }


def _read_captured_material(
    store,
    *,
    repository_id: str,
    run_id: str,
    selection,
    runtime,
    runtime_manifest_sha256: str,
    context_input_digest: str,
    preparation_id: str | None,
):
    """Load the immutable pre-preparation capture for a versioned resume."""
    from run_state.upstream import UpstreamRefused
    from run_state.workspace import InputSnapshot, WorkspaceRefused, parse_input_selection

    with store.read_transaction() as tx:
        try:
            row = tx.execute(
                "SELECT * FROM context_run_material WHERE repository_id=? AND run_id=?",
                (repository_id, run_id),
            ).fetchone()
            workspace_row = None if preparation_id is None else tx.execute(
                "SELECT selected_manifest_json,selected_manifest_hash "
                "FROM context_workspaces WHERE preparation_id=? AND repository_id=? "
                "AND run_id=?",
                (preparation_id, repository_id, run_id),
            ).fetchone()
        except sqlite3.Error as error:
            raise WorkspaceRefused("UPSTREAM_BINDING_INCOMPLETE") from error
    if row is None:
        raise WorkspaceRefused("UPSTREAM_BINDING_INCOMPLETE")
    if (
        row["runtime_manifest_sha256"] != runtime_manifest_sha256
        or row["runtime_digest"] != runtime.runtime_digest
    ):
        raise UpstreamRefused("UPSTREAM_RUNTIME_CHANGED")
    if (
        row["selection_manifest_sha256"] != selection.manifest_sha256
        or row["input_digest"] != selection.input_digest
        or row["input_digest"] != context_input_digest
    ):
        raise UpstreamRefused("UPSTREAM_CHANGED")
    try:
        manifest = json.loads(row["snapshot_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise WorkspaceRefused("SNAPSHOT_INCOMPLETE") from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != "ffs.input-snapshot/v1"
        or manifest.get("selection_manifest_hash") != row["selection_manifest_sha256"]
        or manifest.get("input_digest") != row["input_digest"]
        or not isinstance(manifest.get("capture"), dict)
        or not isinstance(manifest["capture"].get("locator"), str)
    ):
        raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
    if preparation_id is not None:
        if workspace_row is None:
            raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
        try:
            workspace_manifest = json.loads(workspace_row["selected_manifest_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise WorkspaceRefused("SNAPSHOT_INCOMPLETE") from error
        if (
            workspace_manifest != manifest
            or workspace_row["selected_manifest_hash"]
            != row["selection_manifest_sha256"]
            or workspace_manifest.get("input_digest") != row["input_digest"]
        ):
            raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
    selected_value = {
        "schema": "ffs.input-selection/v1",
        "base_oid": manifest.get("base_oid"),
        "repository_id": manifest.get("repository_id"),
        "entries": manifest.get("entries"),
        "required_context": manifest.get("required_context", []),
        "upstream": manifest.get("upstream"),
    }
    try:
        recovered_selection = parse_input_selection(selected_value)
    except WorkspaceRefused as error:
        raise WorkspaceRefused("SNAPSHOT_INCOMPLETE") from error
    if (
        recovered_selection.manifest_sha256 != selection.manifest_sha256
        or recovered_selection.input_digest != selection.input_digest
    ):
        raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
    return InputSnapshot(
        selection=recovered_selection,
        staging=Path(manifest["capture"]["locator"]),
        selection_manifest_hash=row["selection_manifest_sha256"],
        input_digest=row["input_digest"],
        _manifest_json=json.dumps(manifest, sort_keys=True, separators=(",", ":")),
    )
def _refresh_parent_activity_generation(store, token, activity_id: str) -> None:
    """Rebind only the durable top-level activity after parent revalidation."""
    from run_state.ownership import OwnershipRefused, assert_owner

    with store.fenced_operation(token):
        with store.transaction() as tx:
            assert_owner(tx, token)
            changed = tx.execute(
                "UPDATE authority_activities SET generation=?,updated_at=? WHERE id=? "
                "AND repository_id=? AND run_id=? AND NOT EXISTS "
                "(SELECT 1 FROM authority_child_bindings b WHERE b.activity_id=authority_activities.id) "
                "AND EXISTS (SELECT 1 FROM context_runs r WHERE r.repository_id=? AND r.run_id=? "
                "AND r.activity_id=authority_activities.id AND r.generation=? AND r.state='ready')",
                (token.generation, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 activity_id, token.repository_id, token.run_id,
                 token.repository_id, token.run_id, token.generation),
            ).rowcount
            if changed != 1:
                raise OwnershipRefused("FENCE_REVOKED")


def _cmd_fixture_start(args: argparse.Namespace, *, on_ready=None) -> int:
    """Prepare through the journal; optionally retain ownership for a consumer.

    The callback is an in-process supervisor seam, never a worker capability.
    Its return code becomes the command result. Ownership is held throughout
    the callback and retained afterward if launches remain unresolved.
    Existing fixture callers keep their JSON projection and release behavior.
    """
    from run_context import (
        ContextRefused, objective_digest, register_repository, request_digest,
        resolve_context, resolve_evidence, resolve_repository, validate_state_root,
        registered_repository_identity,
    )
    from run_state.ownership import (
        ControlStore, OwnershipRefused, ProcessIdentity, StartRequest, reserve_resources,
    )
    from run_state.state import ControlStoreRefused
    from run_state.migration import MigrationRefused, assert_managed_epoch
    from run_state.upstream import UpstreamRefused, _validate_segment, resolve_upstream_binding
    from run_state.managed_admission import ManagedAdmissionRefused
    from run_state.workspace import (
        WorkspaceRefused, adopt_unstarted_workspace_preparation_fence,
        adopt_workspace_preparation_fence,
        begin_workspace_preparation, finalize_ready_unlock,
        prepare_workspace, recover_workspace_preparation,
        revalidate_ready_fence, parse_input_selection, read_operator_selection_manifest,
        snapshot_inputs, load_input_snapshot, validate_selected_inputs,
    )

    preparation_interval = None
    preparation_token = None
    capacity_wait_ns = 0

    def finish_preparation_accounting():
        nonlocal preparation_interval
        if preparation_interval is not None:
            interval, preparation_interval = preparation_interval, None
            store.end_policy_work(preparation_token, interval)

    def begin_preparation_accounting(token):
        nonlocal preparation_interval, preparation_token
        if managed_material is None:
            return
        store.configure_run_limits(
            token, dispatch_limit=managed_material["dispatch_limit"],
            token_limit=managed_material["token_limit"],
            worker_capacity=managed_material["worker_capacity"],
        )
        budget = store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id)
        if budget is None:
            store.configure_run_policy_budget(
                token, tier=managed_material.get("policy_tier", "medium"), clock_boot_id=ProcessIdentity.current().boot_id,
                clock_monotonic_ns=time.monotonic_ns(),
            )
        store.record_policy_wait(
            token, kind="capacity", elapsed_ns=capacity_wait_ns,
        )
        preparation_token = token
        preparation_interval = store.begin_policy_work(token, kind="preparation")

    import time

    try:
        request, run_id = _fixture_request(args)
        # Legacy raw selected-input flags never enter the manifest authority
        # flow.  Refuse before resolving Git or creating any control state.
        if args.selected_input:
            return _fixture_refusal("SELECTED_INPUT_UNSUPPORTED", run_id=run_id)
        selection = None
        selection_object = getattr(args, "selection_object", None)
        if args.selection_manifest is not None or selection_object is not None:
            try:
                if args.selection_manifest is not None and selection_object is not None:
                    raise WorkspaceRefused("INVALID_SELECTION")
                selection = parse_input_selection(
                    selection_object if selection_object is not None else
                    read_operator_selection_manifest(Path(args.selection_manifest)),
                )
            except WorkspaceRefused as error:
                # This ingress boundary occurs before Git, marker, or control
                # state access.  Its operator-input refusals are request errors.
                return _fixture_refusal(error.code, run_id=run_id, exit_code=2)
            try:
                runtime, runtime_manifest_sha256 = _load_upstream_runtime(args)
            except UpstreamRefused as error:
                return _fixture_refusal(error.code, run_id=run_id, exit_code=2)
            try:
                # Validate explicit scope before registration and Git effects.
                _validate_segment(selection.upstream.project)
                _validate_segment(selection.upstream.workstream)
                _validate_segment(
                    selection.upstream.session_key, allow_none=True,
                )
            except UpstreamRefused:
                return _fixture_refusal("INVALID_UPSTREAM_SCOPE", run_id=run_id, exit_code=2)
        else:
            runtime = None
            runtime_manifest_sha256 = None
        if args.state_root is None:
            return _fixture_refusal("STATE_ROOT_REQUIRED", run_id=run_id)
        repository = resolve_repository(Path.cwd())
        state_root = validate_state_root(Path(args.state_root), repository)
        managed_material = getattr(args, "managed_request_material", None)
        store = None
        if managed_material is not None:
            # Idle controllers reserve no shared execution ticket. Supervisor
            # dispatch binds each actual consumer to the machine-wide queue.
            # New managed input must be valid before creating any authority,
            # ownership or evidence. Resumes consume their captured snapshot,
            # since the invoking checkout may have changed in the meantime.
            registered_id = registered_repository_identity(repository)
            retained = None
            if (state_root / "control.sqlite3").exists():
                store = ControlStore(state_root / "control.sqlite3")
                with store.read_transaction() as tx:
                    retained = tx.execute(
                        "SELECT run_id FROM context_runs WHERE repository_id=? AND run_id=? "
                        "UNION SELECT run_id FROM context_requests "
                        "WHERE repository_id=? AND request_key=?",
                        (registered_id, run_id, registered_id, request.request_key),
                    ).fetchone()
            if retained is None:
                validate_selected_inputs(repository.checkout, selection)
            else:
                from run_state.managed import MANAGED_WRITER_VERSION
                store.assert_writer_version_before_ownership(
                    repository_id=registered_id, run_id=retained["run_id"],
                    writer_version=MANAGED_WRITER_VERSION,
                )
        if store is None:
            store = ControlStore(state_root / "control.sqlite3")
        if managed_material is not None:
            # This occurs before repository registration, reservation, input
            # capture, or workspace preparation. A migrated legacy run cannot
            # enter managed/frontend execution until its own ``new`` epoch is
            # selected and observed accounting is present.
            migration_epoch = assert_managed_epoch(store, run_id)
            if migration_epoch is not None:
                store.bind_migration_epoch(run_id, migration_epoch)
        repository_id = register_repository(store, repository, state_root)
        store.ensure_authority_schema()
        digest = objective_digest(request.objective)
        req_digest = request_digest(request, run_id)
        if managed_material is not None:
            req_digest = hashlib.sha256(json.dumps(
                {"context_request_digest": req_digest, "managed": managed_material},
                sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest()
        if selection is not None:
            req_digest = hashlib.sha256(
                (req_digest + selection.manifest_sha256 + selection.input_digest).encode("utf-8")
            ).hexdigest()

        def requested_material(run_row):
            """Validate every versioned byte binding before reservations/effects."""
            with store.read_transaction() as tx:
                child_pointer = tx.execute(
                    "SELECT 1 FROM authority_child_bindings WHERE activity_id=?",
                    (run_row["activity_id"],),
                ).fetchone()
            if child_pointer is not None:
                raise OwnershipRefused("PARENT_ACTIVITY_INVALID")
            if managed_material is not None:
                from run_state.managed import MANAGED_WRITER_VERSION
                store.assert_writer_version_before_ownership(
                    repository_id=repository_id, run_id=run_row["run_id"],
                    writer_version=MANAGED_WRITER_VERSION,
                )
            elif run_row["writer_version"] is not None:
                raise OwnershipRefused("WRITER_VERSION_MISMATCH")
            try:
                persisted = json.loads(run_row["upstream_json"])
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise WorkspaceRefused("UPSTREAM_BINDING_INCOMPLETE") from error
            if not isinstance(persisted, dict):
                raise WorkspaceRefused("UPSTREAM_BINDING_INCOMPLETE")
            versioned = "runtime_manifest_sha256" in persisted
            if not versioned:
                if (
                    selection is not None
                    or runtime is not None
                    or runtime_manifest_sha256 is not None
                ):
                    raise WorkspaceRefused("UPSTREAM_BINDING_INCOMPLETE")
                return None
            if selection is None or runtime is None or runtime_manifest_sha256 is None:
                raise UpstreamRefused("UPSTREAM_RUNTIME_REQUIRED")
            expected = _normalized_upstream_request(
                selection, run_row["run_id"], runtime_manifest_sha256,
            )
            if persisted.get("runtime_manifest_sha256") != runtime_manifest_sha256:
                raise UpstreamRefused("UPSTREAM_RUNTIME_CHANGED")
            if {key: persisted.get(key) for key in ("project", "workstream", "session_key")} != {
                key: expected[key] for key in ("project", "workstream", "session_key")
            }:
                raise UpstreamRefused("UPSTREAM_CHANGED")
            complete_keys = {
                "project", "workstream", "session_key", "effective_session_key",
                "planning_root", "resolver_version", "runtime_digest",
                "runtime_manifest_sha256",
            }
            persisted_keys = frozenset(persisted)
            if persisted_keys not in {frozenset(expected), frozenset(complete_keys)}:
                raise WorkspaceRefused("UPSTREAM_BINDING_INCOMPLETE")
            if run_row["state"] == "ready" and persisted_keys != frozenset(complete_keys):
                raise WorkspaceRefused("UPSTREAM_BINDING_INCOMPLETE")
            if persisted_keys == frozenset(complete_keys) and persisted["runtime_digest"] != runtime.runtime_digest:
                raise UpstreamRefused("UPSTREAM_RUNTIME_CHANGED")
            return _read_captured_material(
                store, repository_id=repository_id, run_id=run_row["run_id"],
                selection=selection, runtime=runtime,
                runtime_manifest_sha256=runtime_manifest_sha256,
                context_input_digest=run_row["input_digest"],
                preparation_id=run_row["preparation_id"],
            )

        def bind_upstream(token, workspace_path: Path) -> None:
            """Resolve and publish the complete upstream binding under the owner fence."""
            if selection is None:
                return
            expected = _normalized_upstream_request(
                selection, token.run_id, runtime_manifest_sha256,
            )
            try:
                binding = resolve_upstream_binding(
                    Path(workspace_path), runtime=runtime,
                    project=expected["project"], workstream=expected["workstream"],
                    session_key=expected["session_key"],
                    stored_workstream=expected["workstream"],
                )
                complete = {
                    **binding.as_payload(),
                    "runtime_manifest_sha256": runtime_manifest_sha256,
                }
                from run_state.ownership import assert_owner
                with store.transaction() as tx:
                    assert_owner(tx, token)
                    current = tx.execute(
                        "SELECT upstream_json FROM context_runs "
                        "WHERE repository_id=? AND run_id=? AND generation=?",
                        (token.repository_id, token.run_id, token.generation),
                    ).fetchone()
                    if current is None:
                        raise OwnershipRefused("FENCE_REVOKED")
                    try:
                        current_value = json.loads(current["upstream_json"])
                    except (TypeError, ValueError, json.JSONDecodeError) as error:
                        raise WorkspaceRefused("UPSTREAM_BINDING_INCOMPLETE") from error
                    if current_value not in (expected, complete):
                        raise UpstreamRefused("UPSTREAM_CHANGED")
                    changed = tx.execute(
                        "UPDATE context_runs SET upstream_json=?,updated_at=? "
                        "WHERE repository_id=? AND run_id=? AND generation=?",
                        (json.dumps(complete, sort_keys=True, separators=(",", ":")),
                         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         token.repository_id, token.run_id, token.generation),
                    ).rowcount
                    if changed != 1:
                        raise OwnershipRefused("FENCE_REVOKED")
            except UpstreamRefused as error:
                from run_state.ownership import assert_owner
                with store.transaction() as tx:
                    assert_owner(tx, token)
                    changed = tx.execute(
                        "UPDATE context_runs SET state='blocked',updated_at=? "
                        "WHERE repository_id=? AND run_id=? AND generation=? "
                        "AND state IN ('preparing','blocked')",
                        (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         token.repository_id, token.run_id, token.generation),
                    ).rowcount
                    if changed != 1:
                        raise OwnershipRefused("FENCE_REVOKED")
                # Normalize the resolver refusal at the workspace boundary so
                # prepare_workspace records the owned preparation as blocked
                # before the outer CLI releases this owner.  The typed upstream
                # code remains the public refusal.
                raise WorkspaceRefused(error.code) from error

        if request.request_key:
            with store.read_transaction() as tx:
                replay = tx.execute(
                    "SELECT request_digest, run_id, result_json FROM context_requests "
                    "WHERE repository_id = ? AND request_key = ?",
                    (repository_id, request.request_key),
                ).fetchone()
            if replay is not None:
                run_id = replay["run_id"]
                from dataclasses import replace
                request = replace(request, explicit_run_id=run_id)
                with store.read_transaction() as tx:
                    replay_run = tx.execute(
                        "SELECT * FROM context_runs WHERE repository_id=? AND run_id=?",
                        (repository_id, run_id),
                    ).fetchone()
                    unbound = None if replay_run is None else tx.execute(
                        "SELECT a.id FROM authority_activities a WHERE a.repository_id=? AND a.run_id=? "
                        "AND a.state IN ('pending','active') AND a.id != ? AND NOT EXISTS ("
                        "SELECT 1 FROM authority_child_bindings b JOIN context_workspaces w "
                        "ON w.preparation_id=b.workspace_preparation_id "
                        "JOIN authority_activities p ON p.id=b.parent_activity_id "
                        "JOIN context_runs r ON r.repository_id=a.repository_id AND r.run_id=a.run_id "
                        "WHERE b.activity_id=a.id AND w.repository_id=a.repository_id "
                        "AND w.run_id=a.run_id AND w.parent_preparation_id IS NOT NULL "
                        "AND w.parent_activity_id=b.parent_activity_id AND w.child_role=b.role "
                        "AND w.path=b.workspace_binding AND p.repository_id=a.repository_id "
                        "AND p.run_id=a.run_id AND w.parent_preparation_id=r.preparation_id "
                        "AND b.candidate_hash=a.input_digest AND b.runtime_identity=a.runtime_tuple_hash "
                        "AND b.role IN ('worker','reviewer','recovery','inventory'))",
                        (repository_id, run_id, replay_run["activity_id"]),
                    ).fetchone()
                if replay_run is None:
                    return _fixture_refusal("FENCE_REVOKED", run_id=run_id)
                if replay["request_digest"] != req_digest:
                    return _fixture_refusal("IDEMPOTENCY_CONFLICT", run_id=run_id)
                requested_material(replay_run)
                if unbound is not None:
                    if not request.resume:
                        return _fixture_refusal("RESUME_REQUIRED", run_id=run_id)
                    _fixture_refusal("FENCE_REVOKED", run_id=run_id)
                    return 3
                if replay["result_json"]:
                    context = resolve_context(request, store, repository_id)
                    if not context.ready:
                        return _fixture_refusal(
                            "WORKSPACE_MISSING", run_id=context.run_id,
                            workspace=context.workspace, workspace_state=context.workspace_state,
                            ready=False,
                        )
                    if on_ready is not None:
                        owned = reserve_resources(store, StartRequest(
                            run_id, replay_run["workspace"], replay_run["objective_digest"],
                            ProcessIdentity.current(), repository_id=repository_id,
                            planning_scope=replay_run["planning_scope"],
                        ))
                        begin_preparation_accounting(owned.token)
                        revalidate_ready_fence(store, owned.token, replay_run["preparation_id"])
                        _refresh_parent_activity_generation(store, owned.token, replay_run["activity_id"])
                        context = resolve_context(request, store, repository_id)
                        finish_preparation_accounting()
                        return on_ready(store, owned.token, context)
                    print(replay["result_json"])
                    return 0
                with store.read_transaction() as tx:
                    interrupted_ready = tx.execute(
                        "SELECT * FROM context_runs WHERE repository_id = ? AND run_id = ?",
                        (repository_id, replay["run_id"]),
                    ).fetchone()
                if interrupted_ready is not None and interrupted_ready["state"] == "ready":
                    owned = reserve_resources(store, StartRequest(
                        replay["run_id"], interrupted_ready["workspace"],
                        interrupted_ready["objective_digest"], ProcessIdentity.current(),
                        repository_id=repository_id,
                        planning_scope=interrupted_ready["planning_scope"],
                    ))
                    begin_preparation_accounting(owned.token)
                    revalidate_ready_fence(
                        store, owned.token, interrupted_ready["preparation_id"],
                    )
                    _refresh_parent_activity_generation(store, owned.token, interrupted_ready["activity_id"])
                    replay_request = request.__class__(
                        **{**request.__dict__, "explicit_run_id": replay["run_id"], "inherited": {}}
                    )
                    context = resolve_context(replay_request, store, repository_id)
                    payload = context.as_payload()
                    result_json = json.dumps(payload, sort_keys=True)
                    from run_state.ownership import assert_owner, release_owner
                    with store.transaction() as tx:
                        assert_owner(tx, owned.token)
                        tx.execute(
                            "UPDATE context_runs SET result_json = ?, updated_at = ? "
                            "WHERE repository_id = ? AND run_id = ?",
                            (result_json, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                             repository_id, replay["run_id"]),
                        )
                        tx.execute(
                            "UPDATE context_requests SET result_json = ? "
                            "WHERE repository_id = ? AND request_key = ?",
                            (result_json, repository_id, request.request_key),
                        )
                        if on_ready is None:
                            release_owner(tx, owned.token)
                    if on_ready is not None:
                        finish_preparation_accounting()
                        return on_ready(store, owned.token, context)
                    print(result_json)
                    return 0

        with store.read_transaction() as tx:
            existing = tx.execute(
                "SELECT * FROM context_runs WHERE repository_id = ? AND run_id = ?",
                (repository_id, run_id),
            ).fetchone()
        if existing is not None:
            captured_material = requested_material(existing)
            if (
                not request.revise
                and (existing["objective_digest"] != digest
                     or existing["planning_scope"] != request.planning_scope)
            ):
                return _fixture_refusal("CONFLICTING_RUN_ID", run_id=run_id)
            current_activity = store.get_activity(existing["activity_id"])
            if (
                current_activity.state not in {"succeeded", "failed", "aborted"}
                and not request.resume
            ):
                return _fixture_refusal("RESUME_REQUIRED", run_id=run_id)
            # An activity selected before its context-run pointer was durably
            # updated is an ambiguous crash boundary.  It must never reclaim
            # the ready workspace or overwrite the retained request result.
            with store.read_transaction() as tx:
                unbound = tx.execute(
                    "SELECT a.id FROM authority_activities a WHERE a.repository_id=? AND a.run_id=? "
                    "AND a.state IN ('pending','active') AND a.id != ? AND NOT EXISTS ("
                        "SELECT 1 FROM authority_child_bindings b JOIN context_workspaces w "
                        "ON w.preparation_id=b.workspace_preparation_id "
                        "JOIN authority_activities p ON p.id=b.parent_activity_id "
                        "JOIN context_runs r ON r.repository_id=a.repository_id AND r.run_id=a.run_id "
                        "WHERE b.activity_id=a.id AND w.repository_id=a.repository_id "
                        "AND w.run_id=a.run_id AND w.parent_preparation_id IS NOT NULL "
                        "AND w.parent_activity_id=b.parent_activity_id AND w.child_role=b.role "
                        "AND w.path=b.workspace_binding AND p.repository_id=a.repository_id "
                        "AND p.run_id=a.run_id AND w.parent_preparation_id=r.preparation_id "
                        "AND b.candidate_hash=a.input_digest AND b.runtime_identity=a.runtime_tuple_hash "
                        "AND b.role IN ('worker','reviewer','recovery','inventory'))",
                    (repository_id, run_id, existing["activity_id"]),
                ).fetchone()
            if unbound is not None:
                if not request.resume:
                    return _fixture_refusal("RESUME_REQUIRED", run_id=run_id)
                _fixture_refusal("FENCE_REVOKED", run_id=run_id)
                return 3
            owned = reserve_resources(store, StartRequest(
                run_id, existing["workspace"],
                digest if request.revise else existing["objective_digest"], ProcessIdentity.current(),
                repository_id=repository_id, planning_scope=request.planning_scope,
            ))
            begin_preparation_accounting(owned.token)
            if existing["state"] in ("preparing", "blocked"):
                if existing["preparation_id"]:
                    # The unstarted helper inspects path/ref/registration while
                    # holding Git administration.  Do not branch on those
                    # mutable facts in the CLI before that guard is acquired.
                    try:
                        recovered = adopt_unstarted_workspace_preparation_fence(
                            store, owned.token, repository_path=repository.checkout,
                            preparation_id=existing["preparation_id"],
                        )
                    except WorkspaceRefused as error:
                        if error.code != "WORKSPACE_OWNERSHIP_MISMATCH":
                            raise
                        adopt_workspace_preparation_fence(
                            store, owned.token, existing["preparation_id"],
                        )
                        ready = recover_workspace_preparation(
                            store, owned.token, existing["preparation_id"],
                            before_ready=lambda workspace: bind_upstream(owned.token, workspace),
                        )
                        finalize_ready_unlock(store, owned.token, ready.id)
                    else:
                        if recovered is None:
                            raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")
                        ready = prepare_workspace(
                            store, owned.token, recovered,
                            input_snapshot=load_input_snapshot(store, recovered),
                            before_ready=lambda workspace: bind_upstream(owned.token, workspace),
                        )
                else:
                    adopt_unstarted_workspace_preparation_fence(
                        store, owned.token, repository_path=repository.checkout,
                    )
                    selected_manifest = (
                        captured_material.manifest
                        if captured_material is not None else {"entries": []}
                    )
                    retained_base = (
                        captured_material.selection.base_oid
                        if captured_material is not None else repository.head
                    )
                    preparation = begin_workspace_preparation(
                        store, owned.token, run_id=run_id,
                        workspace=Path(existing["workspace"]),
                        branch=f"ffs/runs/{run_id}", base_commit=retained_base,
                        selected_input_manifest=selected_manifest,
                        repository_path=repository.checkout,
                    )
                    ready = prepare_workspace(
                        store, owned.token, preparation,
                        input_snapshot=captured_material,
                        before_ready=lambda workspace: bind_upstream(owned.token, workspace),
                    )
            elif existing["state"] == "ready" and existing["preparation_id"]:
                revalidate_ready_fence(store, owned.token, existing["preparation_id"])
            selected = store.select_activity(
                owned.token, kind=request.activity or existing["activity_kind"],
                input_digest=existing["input_digest"], resume=request.resume,
                revise=request.revise,
            )
            if selected.state == "pending":
                selected = store.transition_activity(
                    owned.token, selected.id, expected="pending", new="active",
                )
            from run_state.ownership import assert_owner
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            with store.transaction() as tx:
                assert_owner(tx, owned.token)
                changed = tx.execute(
                    "UPDATE authority_activities SET generation=?,updated_at=? WHERE id=? "
                    "AND repository_id=? AND run_id=?",
                    (owned.generation, now, selected.id, repository_id, run_id),
                ).rowcount
                changed_run = tx.execute(
                    "UPDATE context_runs SET objective_digest=?,objective_text=?,activity_id=?,"
                    "activity_kind=?,generation=?,updated_at=? WHERE repository_id=? AND run_id=?",
                    (digest if request.revise else existing["objective_digest"],
                     request.objective if request.revise else existing["objective_text"],
                     selected.id, selected.kind, owned.generation, now, repository_id, run_id),
                ).rowcount
                if changed != 1 or changed_run != 1:
                    raise OwnershipRefused("FENCE_REVOKED")
            context = resolve_context(request, store, repository_id)
            if selected.reused_result:
                from dataclasses import replace
                context = replace(context, reused_result=True)
            payload = context.as_payload(code="RUN_READY" if context.ready else "WORKSPACE_MISSING")
            result_json = json.dumps(payload, sort_keys=True)
            from run_state.ownership import assert_owner, release_owner
            with store.transaction() as tx:
                assert_owner(tx, owned.token)
                tx.execute(
                    "UPDATE context_runs SET result_json = ?, updated_at = ? "
                    "WHERE repository_id = ? AND run_id = ?",
                    (result_json, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     repository_id, run_id),
                )
                if request.request_key:
                    recorded = tx.execute(
                        "INSERT INTO context_requests "
                        "(repository_id,request_key,request_digest,run_id,result_json,created_at) "
                        "VALUES (?,?,?,?,?,?) "
                        "ON CONFLICT(repository_id,request_key) DO UPDATE "
                        "SET result_json=excluded.result_json "
                        "WHERE context_requests.request_digest=excluded.request_digest "
                        "AND context_requests.run_id=excluded.run_id",
                        (repository_id, request.request_key, req_digest, run_id, result_json, now),
                    ).rowcount
                    if recorded != 1:
                        raise OwnershipRefused("IDEMPOTENCY_CONFLICT")
                if on_ready is None:
                    release_owner(tx, owned.token)
            if on_ready is not None and context.ready:
                finish_preparation_accounting()
                return on_ready(store, owned.token, context)
            print(result_json)
            return 0 if context.ready else 5

        workspace_root = repository.primary_root.parent / ".ffs-workspaces" / repository_id
        workspace = workspace_root / run_id
        evidence_root = resolve_evidence(state_root, run_id, repository_id)
        owned = reserve_resources(store, StartRequest(
            run_id, os.fspath(workspace), digest, ProcessIdentity.current(),
            repository_id=repository_id, planning_scope=request.planning_scope,
        ))
        begin_preparation_accounting(owned.token)
        for directory in (state_root / "runs", evidence_root.parent, evidence_root):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        activity_id = str(uuid.uuid4())
        snapshot = None
        if selection is not None:
            store.assert_integration_settled(owned.token, repository.checkout)
            snapshot = snapshot_inputs(
                repository.checkout, selection,
                evidence_root / "input-snapshots",
            )
            input_digest = snapshot.input_digest
            upstream = _normalized_upstream_request(
                selection, run_id, runtime_manifest_sha256,
            )
            selected_manifest = snapshot.manifest
        else:
            input_digest = hashlib.sha256(b'{"entries":[]}').hexdigest()
            upstream = {
                "project": os.environ.get("GSD_PROJECT"),
                "workstream": os.environ.get("GSD_WORKSTREAM"),
                "session_key": os.environ.get("GSD_SESSION_KEY"),
            }
            selected_manifest = {"entries": []}
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        from run_state.ownership import assert_owner, durable_workspace_key
        with store.transaction() as tx:
            assert_owner(tx, owned.token)
            context_values = {
                "repository_id": repository_id, "run_id": run_id,
                "objective_digest": digest, "objective_text": request.objective,
                "planning_scope": request.planning_scope, "workspace": os.fspath(workspace),
                "workspace_key": durable_workspace_key(os.fspath(workspace)),
                "evidence_root": os.fspath(evidence_root), "state": "preparing",
                "generation": owned.generation, "activity_id": activity_id,
                "activity_kind": request.activity or "plan", "input_digest": input_digest,
                "request_key": request.request_key, "request_digest": req_digest,
                "upstream_json": json.dumps(upstream, sort_keys=True, separators=(",", ":")),
                "created_at": now, "updated_at": now,
            }
            tx.execute(
                "INSERT INTO authority_activities "
                "(id,repository_id,run_id,kind,input_digest,revision,state,retry_budget,"
                "remaining_retry_budget,runtime_tuple_hash,request_key,generation,created_at,updated_at) "
                "VALUES(?,?,?,?,?,1,'active',1,1,?,?,?,?,?)",
                (activity_id, repository_id, run_id, request.activity or "plan", input_digest,
                 None, request.request_key or f"context:{activity_id}",
                 owned.generation, now, now),
            )
            if managed_material is not None:
                from run_state.managed import MANAGED_WRITER_VERSION
                store.insert_context_run_with_writer(
                    tx, owned.token, context_values=context_values,
                    writer_version=MANAGED_WRITER_VERSION,
                )
            else:
                tx.execute(
                    f"INSERT INTO context_runs ({','.join(context_values)}) "
                    f"VALUES ({','.join('?' for _ in context_values)})",
                    tuple(context_values.values()),
                )
            if snapshot is not None:
                tx.execute(
                    "INSERT INTO context_run_material "
                    "(repository_id,run_id,selection_manifest_sha256,input_digest,"
                    "snapshot_json,runtime_manifest_sha256,runtime_digest,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (repository_id, run_id, selection.manifest_sha256,
                     snapshot.input_digest,
                     json.dumps(snapshot.manifest, sort_keys=True, separators=(",", ":")),
                     runtime_manifest_sha256, runtime.runtime_digest, now),
                )
            if request.request_key:
                tx.execute(
                    "INSERT INTO context_requests (repository_id, request_key, request_digest, run_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (repository_id, request.request_key, req_digest, run_id, now),
                )

        if managed_material is not None:
            accepted_requirement_ids = managed_material.get("accepted_requirement_ids")
            if not isinstance(accepted_requirement_ids, list):
                raise OwnershipRefused("ACCEPTANCE_REQUIREMENTS_INVALID")
            store.create_initial_acceptance_contract(
                owned.token, accepted_requirement_ids=accepted_requirement_ids,
            )

        preparation = begin_workspace_preparation(
            store, owned.token, run_id=run_id, workspace=workspace,
            branch=f"ffs/runs/{run_id}", base_commit=repository.head,
            selected_input_manifest=selected_manifest, repository_path=repository.checkout,
        )
        ready = prepare_workspace(
            store, owned.token, preparation, input_snapshot=snapshot,
            before_ready=lambda workspace: bind_upstream(owned.token, workspace),
        )
        context = resolve_context(request, store, repository_id)
        payload = context.as_payload()
        result_json = json.dumps(payload, sort_keys=True)
        with store.transaction() as tx:
            assert_owner(tx, owned.token)
            tx.execute(
                "UPDATE context_runs SET result_json = ?, updated_at = ? "
                "WHERE repository_id = ? AND run_id = ?",
                (result_json, now, repository_id, run_id),
            )
            if request.request_key:
                tx.execute(
                    "UPDATE context_requests SET result_json = ? WHERE repository_id = ? AND request_key = ?",
                    (result_json, repository_id, request.request_key),
                )
            from run_state.ownership import release_owner
            if on_ready is None:
                release_owner(tx, owned.token)
        if on_ready is not None:
            finish_preparation_accounting()
            return on_ready(store, owned.token, context)
        print(result_json)
        return 0
    except ContextRefused as error:
        return _fixture_refusal(error.code, candidates=error.candidates)
    except OwnershipRefused as error:
        return _fixture_refusal(error.code, run_id=locals().get("run_id"))
    except ControlStoreRefused as error:
        return _fixture_refusal(error.code, run_id=locals().get("run_id"))
    except MigrationRefused as error:
        return _fixture_refusal(error.code, run_id=locals().get("run_id"))
    except ManagedAdmissionRefused as error:
        return _fixture_refusal(
            error.code, run_id=locals().get("run_id"), exit_code=6,
            cause="global managed-run admission could not be proved",
            recovery_action={"action": "inspect_managed_admission"},
        )
    except UpstreamRefused as error:
        if "owned" in locals():
            finish_preparation_accounting()
            _release_fixture_owner(
                store, owned.token, preserve_unresolved_launches=on_ready is not None,
            )
        return _fixture_refusal(error.code, run_id=locals().get("run_id"))
    except WorkspaceRefused as error:
        if "owned" in locals():
            finish_preparation_accounting()
            _release_fixture_owner(
                store, owned.token, preserve_unresolved_launches=on_ready is not None,
            )
        return _fixture_refusal(
            error.code, run_id=locals().get("run_id"), workspace_state=error.state,
            owned_resource_manifest=error.owned_resource_manifest,
        )
    finally:
        try:
            finish_preparation_accounting()
            if on_ready is not None and "owned" in locals():
                _release_fixture_owner(store, owned.token, preserve_unresolved_launches=True)
        finally:
            if "store" in locals() and "run_id" in locals():
                store.clear_migration_epoch(run_id)


def cmd_context(args: argparse.Namespace) -> int:
    from run_context import (
        ContextRefused, ContextRequest, objective_digest, repository_identity,
        resolve_context, resolve_repository, select_run_id, validate_state_root,
    )
    from run_state.state import ControlStore, ControlStoreRefused

    try:
        if args.state_root is None:
            return _fixture_refusal("STATE_ROOT_REQUIRED")
        inherited = dict(os.environ)
        explicit = args.run_id
        if explicit is not None or inherited.get("GSD_RUN_ID") or inherited.get("FFS_RUN_ID"):
            run_id = select_run_id(explicit, inherited)
        else:
            run_id = None
        repository = resolve_repository(Path.cwd())
        state_root = validate_state_root(Path(args.state_root), repository)
        db = state_root / "control.sqlite3"
        if not db.exists():
            return _fixture_refusal("RUN_NOT_FOUND", run_id=run_id)
        repository_id = repository_identity(repository)
        store = ControlStore.open_read_only(db)
        store.validate_context_schema()
        if run_id is None:
            if not args.objective or not args.resume:
                return _fixture_refusal("RUN_NOT_FOUND")
            digest = objective_digest(args.objective)
            with store.read_transaction() as tx:
                rows = tx.execute(
                    "SELECT run_id, planning_scope FROM context_runs WHERE repository_id = ? "
                    "AND objective_digest = ? AND state NOT IN ('complete', 'failed', 'aborted') "
                    "ORDER BY run_id",
                    (repository_id, digest),
                ).fetchall()
            candidates = [{"run_id": row["run_id"], "scope": row["planning_scope"]} for row in rows]
            if len(candidates) != 1:
                return _fixture_refusal("AMBIGUOUS_RUN", candidates=candidates)
            run_id = candidates[0]["run_id"]
        request = ContextRequest(
            cwd=Path.cwd(), operation="context", objective=args.objective or "",
            explicit_run_id=run_id, resume=args.resume, inherited={},
        )
        context = resolve_context(request, store, repository_id)
        print(json.dumps(context.as_payload(code="RUN_CONTEXT"), sort_keys=True))
        return 0
    except (ContextRefused, ControlStoreRefused) as error:
        return _fixture_refusal(error.code, run_id=locals().get("run_id"), candidates=getattr(error, "candidates", []))


def cmd_status(args: argparse.Namespace) -> int:
    run = _store().get_run(args.run_id)
    if run is None:
        print(json.dumps({"error": "not_found", "run_id": args.run_id}), file=sys.stderr)
        return 1
    print(json.dumps({
        "run_id": run.id,
        "skill": run.skill,
        "state": run.state,
        "phase": run.current_phase,
        "objective": run.objective,
        "tokens_used": run.tokens_used,
        "tokens_budget": run.tokens_budget,
        "audit_attempts": run.audit_attempts,
        "last_audit_verdict": run.last_audit_verdict,
    }))
    return 0


def _migration_sources(values, repository_id: str):
    """Parse explicit ``KIND:ABSOLUTE_PATH`` source bindings."""
    from run_state.migration import LegacySource, MigrationRefused

    sources = []
    for value in values or ():
        if not isinstance(value, str) or ":" not in value:
            raise MigrationRefused("INVALID_MIGRATION_SOURCES")
        kind, raw_path = value.split(":", 1)
        if kind not in {"run-store", "context"} or not raw_path:
            raise MigrationRefused("INVALID_MIGRATION_SOURCES")
        sources.append(LegacySource(Path(raw_path), kind, repository_id))
    if not sources:
        raise MigrationRefused("INVALID_MIGRATION_SOURCES")
    return tuple(sources)


def _migration_capability_digest(value: str) -> str:
    """Read a private capability file and retain only its SHA-256 digest."""
    from run_state.migration import MigrationRefused

    path = Path(value)
    if not path.is_absolute():
        raise MigrationRefused("MIGRATION_CAPABILITY_FILE_REQUIRED")
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != 0o600
        ):
            raise MigrationRefused("MIGRATION_CAPABILITY_FILE_UNSAFE")
        content = bytearray()
        while len(content) <= 4096:
            chunk = os.read(descriptor, 4097 - len(content))
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        if (
            len(content) == 0
            or len(content) > 4096
            or (before.st_dev, before.st_ino, before.st_size, stat.S_IMODE(before.st_mode))
            != (after.st_dev, after.st_ino, after.st_size, stat.S_IMODE(after.st_mode))
        ):
            raise MigrationRefused("MIGRATION_CAPABILITY_FILE_UNSAFE")
        return hashlib.sha256(bytes(content)).hexdigest()
    except FileNotFoundError as error:
        raise MigrationRefused("MIGRATION_CAPABILITY_FILE_REQUIRED") from error
    except OSError as error:
        raise MigrationRefused("MIGRATION_CAPABILITY_FILE_UNSAFE") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _migration_coordinator(args):
    from run_state.migration import MigrationCoordinator

    return MigrationCoordinator(
        ControlStore(Path(args.state_root) / "control.sqlite3"),
        registration_id=args.registration_id,
        capability_sha256=_migration_capability_digest(args.capability_file),
    )


def _migration_refusal(error, *, run_id: str | None = None) -> int:
    return _fixture_refusal(
        getattr(error, "code", "MIGRATION_FAILED"), run_id=run_id,
        exit_code=5,
        cause="the migration authority could not prove a single safe writer",
        recovery_action={"action": "inspect_migration_status"},
    )


def cmd_migration_enroll(args: argparse.Namespace) -> int:
    from run_state.migration import MigrationRefused, enroll_authority

    try:
        sources = _migration_sources(args.source, args.repository_id)
        migration = enroll_authority(
            Path(args.state_root) / "control.sqlite3",
            registration_id=args.registration_id,
            capability_sha256=_migration_capability_digest(args.capability_file),
            sources=sources,
        )
        print(json.dumps({
            "schema_version": 1, "ok": True, "authority": str(migration.store.db_path),
            "repository_id": args.repository_id,
            "sources": [source.manifest_entry() for source in sources],
        }, sort_keys=True))
        return 0
    except (MigrationRefused, ControlStoreRefused, OSError) as error:
        return _migration_refusal(error)


def cmd_migration_import(args: argparse.Namespace) -> int:
    from run_state.migration import MigrationRefused

    try:
        sources = _migration_sources(args.source, args.repository_id)
        reports = _migration_coordinator(args).import_sources(sources)
        print(json.dumps({
            "schema_version": 1, "ok": True,
            "reports": [{
                "source_id": report.source_id, "source_sha256": report.source_sha256,
                "imported": report.imported, "quarantined": report.quarantined,
                "replayed": report.replayed,
            } for report in reports],
        }, sort_keys=True))
        return 0
    except (MigrationRefused, ControlStoreRefused, OSError) as error:
        return _migration_refusal(error)


def cmd_migration_status(args: argparse.Namespace) -> int:
    from run_state.migration import MigrationRefused

    try:
        migration = _migration_coordinator(args)
        journal = migration.journal(args.run_id)
        epochs = []
        run_ids = sorted({row["run_id"] for row in journal if row["run_id"]})
        for run_id in run_ids:
            try:
                epoch = migration.writer_for(run_id)
                epochs.append({
                    "run_id": epoch.run_id, "epoch": epoch.epoch,
                    "writer": epoch.writer, "checkpoint": epoch.checkpoint,
                })
            except MigrationRefused as error:
                if error.code != "MIGRATION_WRITER_PAUSED":
                    raise
                with migration.store.read_transaction() as tx:
                    row = tx.execute(
                        "SELECT run_id,epoch,writer,checkpoint FROM migration_epochs WHERE run_id=?",
                        (run_id,),
                    ).fetchone()
                epochs.append(dict(row))
        print(json.dumps({
            "schema_version": 1, "ok": True, "journal": journal, "epochs": epochs,
        }, sort_keys=True))
        return 0
    except (MigrationRefused, ControlStoreRefused, OSError) as error:
        return _migration_refusal(error, run_id=args.run_id)


def cmd_migration_handoff(args: argparse.Namespace) -> int:
    from run_state.migration import MigrationRefused

    try:
        epoch = _migration_coordinator(args).handoff_trusted(args.run_id)
        print(json.dumps({
            "schema_version": 1, "ok": True, "run_id": epoch.run_id,
            "epoch": epoch.epoch, "writer": epoch.writer,
            "checkpoint": epoch.checkpoint,
        }, sort_keys=True))
        return 0
    except (MigrationRefused, ControlStoreRefused, OSError) as error:
        return _migration_refusal(error, run_id=args.run_id)


def cmd_migration_rollback(args: argparse.Namespace) -> int:
    from run_state.migration import MigrationRefused

    try:
        # Public rollback never accepts caller-provided compatibility JSON.
        # Until an installed reverse-compatibility adapter exists, preserving
        # the new evidence and selecting the durable paused state is the only
        # safe outcome.
        epoch = _migration_coordinator(args).rollback(
            args.run_id, legacy_writer_compatible=False,
            proof={"schema": "ffs.rollback-adapter/v1", "reason": "reverse_adapter_unavailable"},
        )
        if epoch.writer == "none":
            return _fixture_refusal(
                "MIGRATION_ROLLBACK_PAUSED", run_id=epoch.run_id, exit_code=5,
                cause="no installed reverse-compatibility adapter proved legacy restoration",
                recovery_action={"action": "install_reverse_compatibility_adapter"},
                epoch=epoch.epoch, checkpoint=epoch.checkpoint,
            )
        print(json.dumps({
            "schema_version": 1, "ok": epoch.writer != "none", "run_id": epoch.run_id,
            "epoch": epoch.epoch, "writer": epoch.writer,
            "checkpoint": epoch.checkpoint,
        }, sort_keys=True))
        return 0
    except (MigrationRefused, ControlStoreRefused, OSError) as error:
        return _migration_refusal(error, run_id=args.run_id)


def cmd_update(args: argparse.Namespace) -> int:
    store = _store()
    if args.phase:
        store.update_phase(args.run_id, args.phase)
    if args.tokens is not None:
        breach = store.inc_tokens(args.run_id, args.tokens)
        if breach is not None:
            limit, spent = breach
            print(f"BUDGET-BREACH: {args.run_id} {limit} {spent}")
    if args.state:
        store.update_state(args.run_id, args.state)
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    if getattr(args, "fixture_mode", False):
        return _cmd_fixture_complete(args)
    _store().update_state(args.run_id, "complete")
    return 0


def _cmd_fixture_complete(args: argparse.Namespace) -> int:
    from run_context import (
        ContextRefused, repository_identity, resolve_repository, validate_state_root,
    )
    from run_state.ownership import (
        ControlStore, OwnershipRefused, ProcessIdentity, StartRequest,
        release_owner, reserve_resources,
    )
    from run_state.state import ControlStoreRefused
    try:
        if args.state_root is None:
            return _fixture_refusal("STATE_ROOT_REQUIRED", run_id=args.run_id)
        if (
            not isinstance(args.result_locator, str) or not args.result_locator
            or not isinstance(args.result_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", args.result_sha256) is None
        ):
            return _fixture_refusal("INVALID_REQUEST", run_id=args.run_id)
        repository = resolve_repository(Path.cwd())
        state_root = validate_state_root(Path(args.state_root), repository)
        store = ControlStore(state_root / "control.sqlite3")
        repository_id = repository_identity(repository)
        with store.read_transaction() as tx:
            run = tx.execute(
                "SELECT * FROM context_runs WHERE repository_id=? AND run_id=?",
                (repository_id, args.run_id),
            ).fetchone()
            activity = None if run is None else tx.execute(
                "SELECT * FROM authority_activities WHERE id=?", (run["activity_id"],),
            ).fetchone()
        if run is None or activity is None:
            return _fixture_refusal("RUN_NOT_FOUND", run_id=args.run_id)
        if "writer_version" in run.keys() and run["writer_version"] is not None:
            return _fixture_refusal("WRITER_VERSION_MISMATCH", run_id=args.run_id)
        owned = reserve_resources(store, StartRequest(
            args.run_id, run["workspace"], run["objective_digest"], ProcessIdentity.current(),
            repository_id=repository_id, planning_scope=run["planning_scope"],
        ))
        evidence = {"locator": args.result_locator, "sha256": args.result_sha256}
        completed = store.transition_activity(
            owned.token, activity["id"], expected=activity["state"],
            new="succeeded", result=evidence,
        )
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = {
            "run_id": args.run_id, "activity_id": completed.id,
            "activity_state": "succeeded", "run_state": "idle",
            "result": evidence,
        }
        with store.transaction() as tx:
            tx.execute(
                "UPDATE context_runs SET result_json=?,updated_at=? WHERE repository_id=? AND run_id=?",
                (json.dumps(result, sort_keys=True), now, repository_id, args.run_id),
            )
            release_owner(tx, owned.token)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ContextRefused, OwnershipRefused, ControlStoreRefused) as error:
        return _fixture_refusal(error.code, run_id=args.run_id)


def cmd_abort(args: argparse.Namespace) -> int:
    _store().update_state(args.run_id, "aborted")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    runs = _store().list_runs(state=args.state)
    payload = [
        {"run_id": r.id, "skill": r.skill, "state": r.state,
         "objective": r.objective[:80], "created_at": r.created_at}
        for r in runs
    ]
    print(json.dumps(payload, indent=2))
    return 0


def cmd_finalize_preview(args: argparse.Namespace) -> int:
    """Read one explicitly named managed run without claiming its ownership."""
    from run_state.supervisor import Supervisor
    from run_context import (
        ContextRefused, resolve_repository, registered_repository_identity, validate_state_root,
        validate_run_id,
    )
    from run_state.state import ControlStoreRefused
    from run_state.workspace import WorkspaceRefused

    try:
        run_id = validate_run_id(args.run_id)
        repository = resolve_repository(Path.cwd())
        repository_id = registered_repository_identity(repository)
        if args.repository_id is not None and args.repository_id != repository_id:
            return _fixture_refusal("RUN_NOT_FOUND", run_id=run_id)
        state_root = validate_state_root(Path(args.state_root), repository)
        db = state_root / "control.sqlite3"
        if not db.exists():
            return _fixture_refusal("RUN_NOT_FOUND", run_id=run_id)
        store = ControlStore.open_read_only(db)
        store.validate_context_schema()

        def verify_repository_binding():
            current = resolve_repository(Path.cwd())
            if (registered_repository_identity(current) != repository_id
                    or current.common_dir != repository.common_dir
                    or current.filesystem_id != repository.filesystem_id):
                raise ContextRefused("REPOSITORY_IDENTITY_INVALID")
            with store.read_transaction() as tx:
                recorded = tx.execute(
                    "SELECT marker_id, common_dir, filesystem_id, primary_root "
                    "FROM context_repositories WHERE repository_id=?", (repository_id,),
                ).fetchone()
            expected = (repository_id, str(current.common_dir), current.filesystem_id,
                        str(current.primary_root))
            if recorded is None or tuple(recorded) != expected:
                raise ContextRefused("REPOSITORY_IDENTITY_INVALID")

        verify_repository_binding()
        value = Supervisor.observe_finalization(store, repository_id, run_id, args.preparation_id)
        verify_repository_binding()
    except (ContextRefused, ControlStoreRefused, WorkspaceRefused) as error:
        return _fixture_refusal(
            error.code, run_id=args.run_id,
            cause="the read-only authority could not verify the requested finalization observation",
            recovery_action={"action": "inspect_retained_evidence"},
        )
    print(json.dumps(value, sort_keys=True))
    return 0


def cmd_finalize_apply(args: argparse.Namespace) -> int:
    """Apply one exact preview fence without reviving the completed run owner."""
    from run_state.supervisor import Supervisor
    from run_context import (
        ContextRefused, resolve_repository, registered_repository_identity, validate_state_root,
        validate_run_id,
    )
    from run_state.state import ControlStoreRefused
    from run_state.workspace import WorkspaceRefused

    try:
        run_id = validate_run_id(args.run_id)
        if (type(args.expected_generation) is not int or args.expected_generation < 1
                or re.fullmatch(r"[0-9a-f]{64}", args.expected_manifest_sha256 or "") is None):
            return _fixture_refusal("INVALID_REQUEST", run_id=run_id, exit_code=2)
        repository = resolve_repository(Path.cwd())
        repository_id = registered_repository_identity(repository)
        if args.repository_id is not None and args.repository_id != repository_id:
            return _fixture_refusal("RUN_NOT_FOUND", run_id=run_id)
        state_root = validate_state_root(Path(args.state_root), repository)
        db = state_root / "control.sqlite3"
        if not db.exists():
            return _fixture_refusal("RUN_NOT_FOUND", run_id=run_id)
        store = ControlStore(db)
        store.validate_context_schema()

        def verify_repository_binding():
            current = resolve_repository(Path.cwd())
            if (registered_repository_identity(current) != repository_id
                    or current.common_dir != repository.common_dir
                    or current.filesystem_id != repository.filesystem_id):
                raise ContextRefused("REPOSITORY_IDENTITY_INVALID")
            with store.read_transaction() as tx:
                recorded = tx.execute(
                    "SELECT marker_id, common_dir, filesystem_id, primary_root "
                    "FROM context_repositories WHERE repository_id=?", (repository_id,),
                ).fetchone()
            expected = (repository_id, str(current.common_dir), current.filesystem_id,
                        str(current.primary_root))
            if recorded is None or tuple(recorded) != expected:
                raise ContextRefused("REPOSITORY_IDENTITY_INVALID")

        verify_repository_binding()
        value = Supervisor.apply_finalization(
            store, repository_id, run_id, args.preparation_id,
            expected_generation=args.expected_generation,
            expected_manifest_sha256=args.expected_manifest_sha256,
        )
        verify_repository_binding()
    except (ContextRefused, ControlStoreRefused, WorkspaceRefused) as error:
        return _fixture_refusal(
            error.code, run_id=args.run_id,
            cause="the managed finalization fence or retained resource identity was not proven",
            recovery_action={"action": "inspect_retained_evidence"},
        )
    print(json.dumps(value, sort_keys=True))
    return 0


def _read_json_document(path: str | None, *, label: str):
    """Read a small explicit JSON document; refusals surface as HOST_REQUEST_INCOMPLETE."""
    from run_state.host_request import HostRequestRefused
    if path is None:
        return None
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 1024 * 1024:
            raise ValueError(label)
        return json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise HostRequestRefused("HOST_REQUEST_INCOMPLETE") from error


def _review_catalog_from_args(args: argparse.Namespace):
    from run_state.host_request import HostRequestRefused
    path = getattr(args, "review_model_catalog", None)
    if path is None:
        return None
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise HostRequestRefused("HOST_REQUEST_INCOMPLETE") from error
    return str(Path(path).resolve()), hashlib.sha256(raw).hexdigest()


def _model_request_from_args(args: argparse.Namespace):
    value = getattr(args, "host_model_request", None)
    if value is None:
        return None
    try:
        request = json.loads(value)
    except ValueError:
        return None
    return request if isinstance(request, dict) else None


def cmd_describe_upstream_runtime(args: argparse.Namespace) -> int:
    """Write the private runtime descriptor the controller registers for opt-in runs."""
    from run_state.upstream import UpstreamRefused, describe_runtime
    try:
        descriptor = describe_runtime(Path(args.module_root), Path(args.node))
    except UpstreamRefused as error:
        return _fixture_refusal(error.code, exit_code=2)
    raw = json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    output = Path(args.output)
    try:
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
    except OSError:
        return _fixture_refusal("UPSTREAM_RUNTIME_DESCRIPTOR_EXISTS", exit_code=2)
    print(json.dumps({"ok": True, "manifest": str(output), "sha256": hashlib.sha256(raw).hexdigest(),
                      "version": descriptor["version"]}, sort_keys=True))
    return 0


def _add_host_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--review-model-catalog", default=None,
                        help="Codex model catalog JSON for the native final review (caller-resolved)")
    parser.add_argument("--acceptance-draft", default=None,
                        help="explicit acceptance draft JSON to seal after outer qualification")
    parser.add_argument("--host", choices=("codex", "claude"))
    parser.add_argument("--host-runtime-home")
    parser.add_argument("--host-credential-source")
    parser.add_argument("--host-binary")
    parser.add_argument("--host-model-request")
    parser.add_argument("--host-sandbox", choices=("read-only", "workspace-write", "danger-full-access"))
    parser.add_argument("--host-network", choices=("disabled", "enabled"))
    parser.add_argument("--host-token-reservation", type=_parse_tokens)
    parser.add_argument("--host-timeout", type=int)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="run-state")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("register-repository")
    s.add_argument("--state-root", required=True)
    s.set_defaults(func=cmd_register_repository)

    migration = sub.add_parser("migration")
    migration_sub = migration.add_subparsers(dest="migration_command", required=True)

    def migration_arguments(command, *, sources: bool = False, run_id: bool = False):
        command.add_argument("--state-root", required=True)
        command.add_argument("--registration-id", required=True)
        command.add_argument("--capability-file", required=True)
        if sources:
            command.add_argument("--repository-id", required=True)
            command.add_argument(
                "--source", action="append", required=True,
                help="KIND:ABSOLUTE_PATH; KIND is run-store or context; repeat as needed",
            )
        if run_id:
            command.add_argument("--run-id", required=True)

    s = migration_sub.add_parser("enroll")
    migration_arguments(s, sources=True)
    s.set_defaults(func=cmd_migration_enroll)

    s = migration_sub.add_parser("import")
    migration_arguments(s, sources=True)
    s.set_defaults(func=cmd_migration_import)

    s = migration_sub.add_parser("status")
    migration_arguments(s)
    s.add_argument("--run-id")
    s.set_defaults(func=cmd_migration_status)

    s = migration_sub.add_parser("handoff")
    migration_arguments(s, run_id=True)
    s.set_defaults(func=cmd_migration_handoff)

    s = migration_sub.add_parser("rollback")
    migration_arguments(s, run_id=True)
    s.set_defaults(func=cmd_migration_rollback)

    s = sub.add_parser("describe-upstream-runtime")
    s.add_argument("--module-root", required=True)
    s.add_argument("--node", required=True)
    s.add_argument("--output", required=True)
    s.set_defaults(func=cmd_describe_upstream_runtime)

    s = sub.add_parser("frontend-start")
    s.add_argument("--ceremony-estimate", type=_parse_ceremony_estimate, default=None)
    s.add_argument("--frontend", choices=("feature-spec", "fix", "code-uplift", "feature-implement", "task-swarm"),
                   required=True)
    s.add_argument("--scope", default="", help="GSD phase scope for execute-family frontends")
    s.add_argument("--invocation-text", default="")
    s.add_argument("--objective", required=True)
    s.add_argument("--state-root", required=True)
    s.add_argument("--upstream-runtime-manifest", required=True)
    s.add_argument("--upstream-runtime-sha256", required=True)
    s.add_argument("--request-key", required=True)
    s.add_argument("--run-id", default=None)
    s.add_argument("--resume", action="store_true")
    s.add_argument("--revise", action="store_true")
    s.add_argument("--select-file", action="append", default=[])
    s.add_argument("--delete-file", action="append", default=[])
    s.add_argument("--required-context", action="append", default=[])
    s.add_argument("--project", default=os.environ.get("GSD_PROJECT"))
    s.add_argument("--workstream", default=os.environ.get("GSD_WORKSTREAM"))
    s.add_argument("--session-key", default=os.environ.get("GSD_SESSION_KEY"))
    s.add_argument("--dispatch-limit", type=int, required=True)
    s.add_argument("--token-limit", type=_parse_tokens, required=True)
    s.add_argument("--worker-capacity", type=int, default=None)
    s.add_argument('--capacity-policy', type=_parse_capacity_policy,
                   help='Versioned operator ceiling revision JSON; does not change cumulative allowances')
    _add_host_arguments(s)
    s.set_defaults(func=cmd_frontend_start)

    s = sub.add_parser("managed-start")
    s.add_argument("--ceremony-estimate", type=_parse_ceremony_estimate, default=None)
    s.add_argument("--objective", required=True)
    s.add_argument("--state-root", required=True)
    s.add_argument("--selection-manifest", required=True)
    s.add_argument("--upstream-runtime-manifest", required=True)
    s.add_argument("--upstream-runtime-sha256", required=True)
    s.add_argument("--request-key", required=True)
    s.add_argument("--run-id", default=None)
    s.add_argument("--activity", choices=("plan", "execute", "review"), default=None)
    s.add_argument("--scope", default=None)
    s.add_argument("--resume", action="store_true")
    s.add_argument("--revise", action="store_true")
    s.add_argument("--dispatch-limit", type=int, required=True)
    s.add_argument("--token-limit", type=_parse_tokens, required=True)
    s.add_argument("--worker-capacity", type=int, default=None)
    s.add_argument('--capacity-policy', type=_parse_capacity_policy,
                   help='Versioned operator ceiling revision JSON; does not change cumulative allowances')
    _add_host_arguments(s)
    s.add_argument("command", nargs=argparse.REMAINDER)
    s.set_defaults(func=cmd_managed_start)

    s = sub.add_parser("start")
    s.add_argument("--skill", required=True, choices=["feature", "fix"])
    s.add_argument("--objective", required=True)
    s.add_argument("--tokens", type=_parse_tokens, default=None)
    s.add_argument("--worktree", default=None)
    s.add_argument("--session-id", default=None)
    s.add_argument("--state-root", default=None)
    s.add_argument("--run-id", default=None)
    s.add_argument("--activity", choices=("plan", "execute", "review"), default=None)
    s.add_argument("--scope", default="")
    s.add_argument("--resume", action="store_true")
    s.add_argument("--revise", action="store_true")
    s.add_argument("--request-key", default=None)
    s.add_argument("--selected-input", action="append", default=[])
    s.add_argument("--selection-manifest", default=None)
    s.add_argument("--upstream-runtime-manifest", default=None)
    s.add_argument("--upstream-runtime-sha256", default=None)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("context")
    s.add_argument("--state-root", default=None)
    s.add_argument("--run-id", default=None)
    s.add_argument("--objective", default=None)
    s.add_argument("--resume", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_context, fixture_mode=True)

    s = sub.add_parser("status")
    s.add_argument("run_id")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("finalize-preview")
    s.add_argument("--state-root", required=True)
    s.add_argument("--repository-id")
    s.add_argument("--run-id", required=True)
    s.add_argument("--preparation-id", required=True)
    s.set_defaults(func=cmd_finalize_preview)

    s = sub.add_parser("finalize-apply")
    s.add_argument("--state-root", required=True)
    s.add_argument("--repository-id")
    s.add_argument("--run-id", required=True)
    s.add_argument("--preparation-id", required=True)
    s.add_argument("--expected-generation", type=int, required=True)
    s.add_argument("--expected-manifest-sha256", required=True)
    s.set_defaults(func=cmd_finalize_apply)

    s = sub.add_parser("update")
    s.add_argument("run_id")
    s.add_argument("--phase", default=None)
    s.add_argument("--tokens", type=_parse_tokens, default=None, help="delta to add to tokens_used (accepts K/M/B/T suffix)")
    s.add_argument("--state", default=None, choices=list(VALID_STATES))
    s.set_defaults(func=cmd_update)

    for name, fn in (("complete", cmd_complete), ("abort", cmd_abort)):
        s = sub.add_parser(name)
        s.add_argument("run_id")
        if name == "complete":
            s.add_argument("--state-root", default=None)
            s.add_argument("--json", action="store_true")
            s.add_argument("--result-locator", default=None)
            s.add_argument("--result-sha256", default=None)
        s.set_defaults(func=fn)

    s = sub.add_parser("list")
    s.add_argument("--state", default=None, choices=list(VALID_STATES))
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("audit")
    s.add_argument("run_id")
    s.add_argument("--kind", required=True, choices=["fix", "feature", "phase"])
    s.add_argument("--context", action="append", help="KEY=VALUE for prompt substitution; repeat as needed")
    s.add_argument("--cwd", default=None)
    s.set_defaults(func=cmd_audit)

    args = p.parse_args(argv)
    if args.cmd == "start":
        args.fixture_mode = any((
            args.state_root is not None, args.run_id is not None, args.activity is not None,
            bool(args.scope), args.resume, args.revise, args.request_key is not None,
            bool(args.selected_input), args.selection_manifest is not None, args.json,
            args.upstream_runtime_manifest is not None,
            args.upstream_runtime_sha256 is not None,
        ))
    elif args.cmd == "complete":
        args.fixture_mode = args.state_root is not None or args.json
    try:
        return args.func(args)
    except MigrationRawMutationRefused as error:
        return _fixture_refusal(error.code, run_id=getattr(args, "run_id", None))
    except UnknownRunError:
        # Same shape cmd_status already emits for a missing run.
        print(json.dumps({"error": "not_found", "run_id": args.run_id}), file=sys.stderr)
        return 1

def cmd_audit(args: argparse.Namespace) -> int:
    """Run adversarial audit. Updates run state based on verdict."""
    from run_state.audit import run_audit
    import sqlite3

    prompt_dir = Path(__file__).resolve().parent / "prompts"
    template_path = prompt_dir / f"{args.kind}_audit.txt"
    if not template_path.exists():
        print(json.dumps({"error": "unknown_kind", "kind": args.kind}), file=sys.stderr)
        return 1
    prompt = template_path.read_text(encoding="utf-8")
    for kv in args.context or []:
        if "=" not in kv:
            print(json.dumps({"error": "bad_context", "value": kv}), file=sys.stderr)
            return 1
        k, v = kv.split("=", 1)
        prompt = prompt.replace("{{" + k + "}}", v)

    cwd = Path(args.cwd or os.getcwd())
    store = _store()
    store.update_state(args.run_id, "pending_audit")

    # GH-3: an exception, SIGTERM, or SIGINT during the audit subprocess must
    # not strand the run in pending_audit forever. Convert both signals to a
    # catchable KeyboardInterrupt (keeping the previous handlers so they can
    # be restored), and restore state=active in `finally` unless a verdict
    # already landed.
    def _raise_keyboard_interrupt(signum, frame):
        raise KeyboardInterrupt(f"interrupted by signal {signum}")

    prev_sigterm = signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    prev_sigint = signal.signal(signal.SIGINT, _raise_keyboard_interrupt)
    settled = False
    try:
        result = run_audit(prompt=prompt, cwd=cwd)

        conn = sqlite3.connect(store.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            store.assert_raw_mutation_allowed(conn, args.run_id)
            conn.execute(
                "UPDATE runs SET audit_attempts = audit_attempts + 1, last_audit_verdict = ? WHERE id = ?",
                (result.verdict, args.run_id),
            )
            conn.execute(
                "INSERT INTO events (run_id, event_type, payload_json, created_at) VALUES (?, 'audit', ?, datetime('now'))",
                (args.run_id, json.dumps({"verdict": result.verdict, "reasoning": result.reasoning, "missing": result.missing})),
            )
            conn.commit()
        finally:
            conn.close()

        # v3.0 codex-gate Pass 1 P2 fix: also append to ~/.claude/state/audits.jsonl
        # so native `/goal` condition checker can grep audit history without
        # needing to open SQLite. One line per audit; append-only; never rewritten.
        audits_log = Path.home() / ".claude" / "state" / "audits.jsonl"
        try:
            audits_log.parent.mkdir(parents=True, exist_ok=True)
            from datetime import datetime, timezone
            record = {
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "run_id": args.run_id,
                "kind": args.kind,
                "verdict": result.verdict,
                "reasoning": result.reasoning[:500],
                "missing": result.missing,
            }
            with audits_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            # Best-effort log; SQLite remains source of truth.
            pass

        # v3.0: native /goal handles continuation; no marker to manage.
        # kind=fix pass → terminal complete. kind=phase/kind=feature pass,
        # and any fail, stay active so the caller (the skill) advances to
        # the next wedge, retries, or reaches the final canary stage.
        if result.verdict == "pass" and args.kind == "fix":
            target_state = "complete"
        else:
            target_state = "active"

        # review-gate round 2 HIGH: CAS, not an unconditional write. A
        # concurrent abort/complete landing while run_audit was in flight
        # must not be clobbered by the verdict this call just computed —
        # False means someone else moved the state first; leave it alone
        # and say so honestly in the output instead of pretending the
        # verdict took effect.
        transitioned = store.recover_state(args.run_id, "pending_audit", target_state)
        settled = True

        print(json.dumps({
            "run_id": args.run_id,
            "verdict": result.verdict,
            "reasoning": result.reasoning,
            "missing": result.missing,
            "state_transition": "applied" if transitioned else "superseded",
        }))
        return 0 if result.verdict == "pass" else 1
    except KeyboardInterrupt:
        print(json.dumps({"error": "interrupted", "run_id": args.run_id}), file=sys.stderr)
        return 130
    finally:
        signal.signal(signal.SIGTERM, prev_sigterm)
        signal.signal(signal.SIGINT, prev_sigint)
        if not settled:
            # CAS, not an unconditional write: only resurrect pending_audit
            # -> active. A concurrent abort/complete, or a verdict that
            # already landed in this same call before the interrupt hit,
            # must not be overwritten. False means someone else moved the
            # state first — leave it alone.
            store.recover_state(args.run_id, "pending_audit", "active")


if __name__ == "__main__":
    raise SystemExit(main())
