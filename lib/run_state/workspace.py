"""Fenced real-Git workspace preparation for isolated fixture authorities."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import shutil
import sqlite3
import stat
import subprocess
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from run_context import (
    ContextRefused, git_admin_lock, registered_repository_identity, resolve_repository,
    sanitized_git_environment,
)
from run_state.ownership import OwnerToken, OwnershipRefused, assert_owner, durable_workspace_key
from run_state.selection import InputSelection, SelectionRefused, parse_input_selection as _parse_selection


_GIT_TIMEOUT = 30.0


class WorkspaceRefused(RuntimeError):
    def __init__(
        self, code: str, *, state: str = "blocked", owned_resource_manifest: str | None = None, candidates: list[str] | None = None,
    ) -> None:
        self.code = code
        self.state = state
        self.owned_resource_manifest = owned_resource_manifest
        self.candidates = candidates or []
        super().__init__(code)


@dataclass(frozen=True)
class WorkspacePreparation:
    id: str
    run_id: str
    repository_id: str
    path: Path
    branch: str
    base_commit: str
    repository_path: Path
    state: str
    ready: bool
    generation: int
    selected_manifest_hash: str
    input_digest: str
    selected_manifest_json: str
    path_existed_before: bool
    branch_existed_before: bool
    registered_before: bool
    created_by_ffs: bool = False
    owned_resource_manifest: str | None = None
    parent_preparation_id: str | None = None
    parent_activity_id: str | None = None
    child_role: str | None = None
    child_request_key: str | None = None
    native_identity: tuple[int, int] | None = None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _portable_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def child_workspace_identity(parent_workspace: Path, run_id: str, request_key: str) -> tuple[Path, str]:
    """Derive a replay-stable child path/ref without using the parent branch."""
    if not isinstance(run_id, str) or not run_id or not isinstance(request_key, str) or not request_key:
        raise WorkspaceRefused("INVALID_CHILD_ALLOCATION")
    key = hashlib.sha256(request_key.encode("utf-8")).hexdigest()[:24]
    parent = Path(parent_workspace)
    if not parent.is_absolute() or parent.resolve() != parent:
        raise WorkspaceRefused("WORKSPACE_PREPARE_FAILED")
    return parent / ".ffs-children" / key, f"ffs/children/{run_id}/{key}"


def begin_child_workspace_preparation(
    store, token: OwnerToken, *, parent_activity_id: str, request_key: str,
    role: str, base_commit: str, selected_input_manifest: dict, repository_path: Path,
    admission_guard: Callable | None = None,
) -> WorkspacePreparation:
    """Journal a derived child worktree without changing the run's parent pointer."""
    if role not in {"worker", "reviewer", "recovery", "inventory"} or not base_commit:
        raise WorkspaceRefused("INVALID_CHILD_ALLOCATION")
    workspace, branch = child_workspace_identity(Path(token.workspace), token.run_id, request_key)
    repository_path = Path(repository_path).resolve(strict=True)
    descriptor = resolve_repository(repository_path)
    observed_base = _git(repository_path, "rev-parse", "--verify", f"{base_commit}^{{commit}}").stdout.strip()
    if observed_base != base_commit or len(base_commit) != 40:
        raise WorkspaceRefused("FORK_BASE_MISMATCH")
    encoded, manifest_hash = _manifest(selected_input_manifest)
    store.ensure_context_schema()
    with git_admin_lock(descriptor.common_dir):
        path_existed = workspace.exists() or workspace.is_symlink()
        branch_existed = _branch_exists(repository_path, branch)
        registered_before = _registered(repository_path, workspace)
    with store.transaction() as tx:
        assert_owner(tx, token)
        if admission_guard is not None:
            admission_guard(tx)
        parent = tx.execute(
            "SELECT w.preparation_id FROM context_workspaces w JOIN context_runs r "
            "ON r.repository_id=w.repository_id AND r.run_id=w.run_id "
            "AND r.preparation_id=w.preparation_id WHERE w.repository_id=? AND w.run_id=? "
            "AND w.path=? AND w.generation=? AND w.state='ready' AND w.created_by_ffs=1",
            (token.repository_id, token.run_id, token.workspace, token.generation),
        ).fetchone()
        activity = tx.execute(
            "SELECT 1 FROM authority_activities WHERE id=? AND repository_id=? AND run_id=? "
            "AND generation=? AND state='active'",
            (parent_activity_id, token.repository_id, token.run_id, token.generation),
        ).fetchone()
        if parent is None or activity is None:
            raise OwnershipRefused("PARENT_ACTIVITY_INVALID")
        from .integration_journal import assert_settled_tx
        source = tx.execute(
            "SELECT workspace_preparation_id FROM authority_child_bindings WHERE activity_id=?",
            (parent_activity_id,),
        ).fetchone()
        assert_settled_tx(tx, source[0] if source is not None else parent["preparation_id"])
        parent_repo = tx.execute(
            "SELECT repository_path,common_dir,base_commit FROM context_workspaces WHERE preparation_id=?",
            (parent["preparation_id"],),
        ).fetchone()
        if (parent_repo is None or parent_repo["common_dir"] != str(descriptor.common_dir)
                or parent_repo["base_commit"] != base_commit):
            raise WorkspaceRefused("FORK_BASE_MISMATCH")
        existing = tx.execute(
            "SELECT * FROM context_workspaces WHERE repository_id=? AND run_id=? AND child_request_key=?",
            (token.repository_id, token.run_id, request_key),
        ).fetchone()
        if existing is not None:
            if (existing["parent_preparation_id"], existing["parent_activity_id"], existing["child_role"],
                existing["path"], existing["branch"], existing["base_commit"], existing["selected_manifest_hash"],
                existing["selected_manifest_json"], existing["repository_path"]) != (
                parent["preparation_id"], parent_activity_id, role, str(workspace), branch, base_commit, manifest_hash,
                encoded, str(repository_path)):
                raise WorkspaceRefused("INPUT_SELECTION_CHANGED")
            if existing["generation"] != token.generation:
                raise WorkspaceRefused("WORKSPACE_RECONCILIATION_REQUIRED")
            return _from_row(existing)
        preparation_id, now = str(uuid.uuid4()), _now()
        tx.execute(
            "INSERT INTO context_workspaces (preparation_id,repository_id,run_id,path,path_key,branch,branch_key,"
            "base_commit,repository_path,common_dir,selected_manifest_json,selected_manifest_hash,path_existed_before,"
            "branch_existed_before,registered_before,generation,state,parent_preparation_id,parent_activity_id,child_role,"
            "child_request_key,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'preparing',?,?,?,?,?,?)",
            (preparation_id, token.repository_id, token.run_id, str(workspace), durable_workspace_key(str(workspace)),
             branch, _portable_key(branch), base_commit, str(repository_path), str(descriptor.common_dir), encoded,
             manifest_hash, int(path_existed), int(branch_existed),
             int(registered_before), token.generation, parent["preparation_id"], parent_activity_id,
             role, request_key, now, now),
        )
        capture = selected_input_manifest.get("capture", {})
        tx.execute(
            "INSERT INTO context_input_snapshots "
            "(preparation_id,repository_id,run_id,base_commit,input_digest,full_manifest_hash,"
            "capture_locator,capture_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (preparation_id, token.repository_id, token.run_id, base_commit,
             selected_input_manifest.get("input_digest", manifest_hash),
             hashlib.sha256(encoded.encode()).hexdigest(),
             capture.get("locator", "") if isinstance(capture, dict) else "",
             capture.get("files_hash", "") if isinstance(capture, dict) else "", now),
        )
        tx.execute("INSERT INTO control_events(event_type,payload) VALUES('CHILD_PREPARING',?)", (
            json.dumps({"preparation_id": preparation_id, "parent_activity_id": parent_activity_id,
                        "request_key": request_key}, sort_keys=True, separators=(",", ":")),))
    return inspect_workspace(store, preparation_id)


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, env=sanitized_git_environment(),
            capture_output=True, text=True,
            timeout=_GIT_TIMEOUT, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkspaceRefused("WORKSPACE_PREPARE_FAILED") from error
    if check and result.returncode:
        raise WorkspaceRefused("WORKSPACE_PREPARE_FAILED")
    return result


def _branch_names(repository_path: Path) -> list[str]:
    return _git(
        repository_path, "for-each-ref", "--format=%(refname)", "refs/heads",
    ).stdout.splitlines()


def _branch_exists(repository_path: Path, branch: str) -> bool:
    target = f"refs/heads/{branch}"
    return target in _branch_names(repository_path)


def _registered(repository_path: Path, workspace: Path) -> bool:
    prefix = f"worktree {workspace}\n"
    text = _git(repository_path, "worktree", "list", "--porcelain").stdout
    return any((record + "\n").startswith(prefix) for record in text.strip().split("\n\n"))


def _manifest(selected_input_manifest: dict) -> tuple[str, str]:
    encoded = json.dumps(selected_input_manifest, sort_keys=True, separators=(",", ":"))
    if selected_input_manifest.get("schema") == "ffs.input-snapshot/v1":
        manifest_hash = selected_input_manifest.get("selection_manifest_hash")
        if isinstance(manifest_hash, str) and len(manifest_hash) == 64:
            return encoded, manifest_hash
    return encoded, hashlib.sha256(encoded.encode()).hexdigest()


def _assert_preparation_binding(row, token: OwnerToken, *, require_generation: bool, tx) -> None:
    if (
        row is None
        or row["repository_id"] != token.repository_id
        or row["run_id"] != token.run_id
        or (require_generation and row["generation"] != token.generation)
    ):
        raise OwnershipRefused("FENCE_REVOKED")
    from .integration_journal import assert_settled_tx
    assert_settled_tx(tx, row["preparation_id"])
    if row["parent_preparation_id"] is None:
        if durable_workspace_key(row["path"]) != durable_workspace_key(token.workspace):
            raise OwnershipRefused("FENCE_REVOKED")
        return
    parent = tx.execute(
        "SELECT w.* FROM context_workspaces w JOIN context_runs r "
        "ON r.repository_id=w.repository_id AND r.run_id=w.run_id "
        "AND r.preparation_id=w.preparation_id "
        "WHERE w.preparation_id=? AND w.repository_id=? AND w.run_id=?",
        (row["parent_preparation_id"], token.repository_id, token.run_id),
    ).fetchone()
    activity = tx.execute(
        "SELECT 1 FROM authority_activities WHERE id=? AND repository_id=? AND run_id=? "
        "AND generation=? AND state='active'",
        (row["parent_activity_id"], token.repository_id, token.run_id, token.generation),
    ).fetchone()
    expected_path, expected_branch = child_workspace_identity(
        Path(token.workspace), token.run_id, row["child_request_key"],
    )
    if (
        parent is None or activity is None or parent["state"] != "ready"
        or not parent["created_by_ffs"] or parent["generation"] != token.generation
        or durable_workspace_key(parent["path"]) != durable_workspace_key(token.workspace)
        or row["child_role"] not in {"worker", "reviewer", "recovery", "inventory"}
        or row["path"] != str(expected_path) or row["branch"] != expected_branch
        or row["common_dir"] != parent["common_dir"]
        or row["base_commit"] != parent["base_commit"]
    ):
        raise OwnershipRefused("FENCE_REVOKED")


def _guarded_workspace_effect(
    store, token: OwnerToken, preparation_id: str, admission_guard: Callable | None,
    *, expected_states: tuple[str, ...] | None = None,
) -> WorkspacePreparation:
    """Recheck delegated admission immediately before one external effect.

    Callers keep Git's administration lock outside this helper.  The store
    fence is intentionally short and reentrant for the same owner token, so a
    stale request cannot begin a worktree, overlay, READY, or unlock effect
    after its originating worker has completed or an ancestor has terminated.
    """
    with store.fenced_operation(token):
        with store.transaction() as tx:
            assert_owner(tx, token)
            row = tx.execute(
                "SELECT * FROM context_workspaces WHERE preparation_id=?", (preparation_id,),
            ).fetchone()
            _assert_preparation_binding(row, token, require_generation=True, tx=tx)
            if expected_states is not None and row["state"] not in expected_states:
                raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")
            if admission_guard is not None:
                admission_guard(tx)
            return _from_row(row)


def _registered_context_repository(tx, repository_id: str) -> bool:
    return tx.execute(
        "SELECT 1 FROM context_repositories WHERE repository_id = ?", (repository_id,),
    ).fetchone() is not None


def _assert_child_recovery_reconciled(tx, row) -> None:
    """A retained launch blocks workspace reuse until identity reconciliation."""
    if row["parent_preparation_id"] is None:
        return
    unresolved = tx.execute(
        "SELECT 1 FROM authority_child_bindings b JOIN authority_launch_intents i "
        "ON i.activity_id=b.activity_id WHERE b.workspace_preparation_id=? "
        "AND i.state NOT IN ('completed_succeeded','completed_failed') LIMIT 1",
        (row["preparation_id"],),
    ).fetchone()
    if unresolved is not None:
        raise WorkspaceRefused("INTENT_RECONCILIATION_REQUIRED")


def begin_workspace_preparation(
    store,
    token: OwnerToken,
    *,
    run_id: str,
    workspace: Path,
    branch: str,
    base_commit: str,
    selected_input_manifest: dict,
    repository_path: Path,
) -> WorkspacePreparation:
    """Persist preparation facts before any Git mutation."""
    if (
        run_id != token.run_id
        or durable_workspace_key(os.fspath(workspace)) != durable_workspace_key(token.workspace)
        or branch != f"ffs/runs/{token.run_id}"
    ):
        raise OwnershipRefused("FENCE_REVOKED")
    repository_path = Path(repository_path).resolve(strict=True)
    workspace = Path(workspace)
    if not workspace.is_absolute():
        raise WorkspaceRefused("WORKSPACE_PREPARE_FAILED")
    descriptor = resolve_repository(repository_path)
    encoded, manifest_hash = _manifest(selected_input_manifest)
    store.ensure_context_schema()
    with store.read_transaction() as tx:
        existing = tx.execute("SELECT * FROM context_workspaces WHERE repository_id=? AND run_id=? AND path_key=?", (token.repository_id, run_id, durable_workspace_key(os.fspath(workspace)))).fetchone()
    if existing is not None:
        if existing["selected_manifest_hash"] != manifest_hash:
            raise WorkspaceRefused("INPUT_SELECTION_CHANGED")
        return _from_row(existing)
    preparation_id = str(uuid.uuid4())
    path_existed = workspace.exists() or workspace.is_symlink()
    branch_existed = _branch_exists(repository_path, branch)
    registered_before = _registered(repository_path, workspace)
    now = _now()
    try:
        with store.transaction() as tx:
            assert_owner(tx, token)
            registered_repository = tx.execute(
                "SELECT common_dir FROM context_repositories WHERE repository_id = ?",
                (token.repository_id,),
            ).fetchone()
            if (
                registered_repository is not None
                and registered_repository["common_dir"] != os.fspath(descriptor.common_dir)
            ):
                raise OwnershipRefused("FENCE_REVOKED")
            tx.execute(
                "INSERT INTO context_workspaces "
                "(preparation_id, repository_id, run_id, path, path_key, branch, branch_key, "
                "base_commit, repository_path, common_dir, selected_manifest_json, "
                "selected_manifest_hash, path_existed_before, branch_existed_before, "
                "registered_before, generation, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'preparing', ?, ?)",
                (preparation_id, token.repository_id, run_id, os.fspath(workspace),
                 durable_workspace_key(os.fspath(workspace)), branch, _portable_key(branch),
                 base_commit, os.fspath(repository_path), os.fspath(descriptor.common_dir),
                 encoded, manifest_hash, int(path_existed), int(branch_existed),
                 int(registered_before), token.generation, now, now),
            )
            changed_run = tx.execute(
                "UPDATE context_runs SET preparation_id = ?, state = 'preparing', updated_at = ? "
                "WHERE repository_id = ? AND run_id = ? AND generation = ?",
                (preparation_id, now, token.repository_id, run_id, token.generation),
            ).rowcount
            if registered_repository is not None and changed_run != 1:
                raise OwnershipRefused("FENCE_REVOKED")
            full_manifest_hash = hashlib.sha256(encoded.encode()).hexdigest()
            capture = selected_input_manifest.get("capture", {})
            locator = capture.get("locator", "") if isinstance(capture, dict) else ""
            capture_hash = capture.get("files_hash", "") if isinstance(capture, dict) else ""
            tx.execute(
                "INSERT INTO context_input_snapshots "
                "(preparation_id,repository_id,run_id,base_commit,input_digest,full_manifest_hash,"
                "capture_locator,capture_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (preparation_id, token.repository_id, run_id, base_commit,
                 selected_input_manifest.get("input_digest", manifest_hash), full_manifest_hash,
                 locator, capture_hash, now),
            )
            tx.execute(
                "INSERT INTO control_events (event_type, payload) VALUES ('PREPARING', ?)",
                (json.dumps({"preparation_id": preparation_id, "run_id": run_id},
                            sort_keys=True, separators=(",", ":")),),
            )
    except sqlite3.IntegrityError as error:
        raise WorkspaceRefused("WORKSPACE_REGISTERED") from error
    return WorkspacePreparation(
        id=preparation_id, run_id=run_id, repository_id=token.repository_id,
        path=workspace, branch=branch, base_commit=base_commit,
        repository_path=repository_path, state="preparing", ready=False,
        generation=token.generation,
        selected_manifest_hash=manifest_hash,
        input_digest=(
            selected_input_manifest.get("input_digest", manifest_hash)
            if isinstance(selected_input_manifest.get("input_digest", manifest_hash), str)
            else manifest_hash
        ),
        selected_manifest_json=encoded,
        path_existed_before=path_existed,
        branch_existed_before=branch_existed,
        registered_before=registered_before,
    )


def _from_row(row) -> WorkspacePreparation:
    additions = {
        "parent_preparation_id", "parent_activity_id", "child_role", "child_request_key",
        "native_identity_json",
    }
    present = additions.intersection(row.keys())
    if present != additions:
        if present:
            raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")
        # Read the complete historical layout without writing or assigning
        # retrospective child/native ownership to its retained records.
        row = {**dict(row), **dict.fromkeys(additions)}
    return WorkspacePreparation(
        id=row["preparation_id"], run_id=row["run_id"], path=Path(row["path"]),
        repository_id=row["repository_id"],
        branch=row["branch"], base_commit=row["base_commit"],
        repository_path=Path(row["repository_path"]), state=row["state"],
        ready=row["state"] == "ready", generation=row["generation"],
        selected_manifest_hash=row["selected_manifest_hash"],
        input_digest=_stored_input_digest(row),
        selected_manifest_json=row["selected_manifest_json"],
        path_existed_before=bool(row["path_existed_before"]),
        branch_existed_before=bool(row["branch_existed_before"]),
        registered_before=bool(row["registered_before"]),
        created_by_ffs=bool(row["created_by_ffs"]),
        owned_resource_manifest=row["owned_manifest"],
        parent_preparation_id=row["parent_preparation_id"],
        parent_activity_id=row["parent_activity_id"], child_role=row["child_role"],
        child_request_key=row["child_request_key"],
        native_identity=_parse_native_identity(row["native_identity_json"]),
    )


def _parse_native_identity(value: str | None) -> tuple[int, int] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError) as error:
        raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH") from error
    if (not isinstance(parsed, list) or len(parsed) != 2
            or any(type(item) is not int or item < 0 for item in parsed)):
        raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")
    return tuple(parsed)


def _verify_native_identity(preparation: WorkspacePreparation) -> None:
    if preparation.native_identity is None:
        if preparation.parent_preparation_id is not None:
            raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")
        return  # Retained legacy parent records have no inferred ownership upgrade.
    directory = _open_directory_chain_raw(
        Path(preparation.path.anchor), preparation.path.parts[1:], create=False,
    )
    try:
        observed = os.fstat(directory)
        if (observed.st_dev, observed.st_ino) != preparation.native_identity:
            raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")
    finally:
        os.close(directory)


def _stored_input_digest(row) -> str:
    """Read the immutable digest retained in the selected-input manifest."""
    try:
        manifest = json.loads(row["selected_manifest_json"])
    except (TypeError, ValueError):
        return row["selected_manifest_hash"]
    value = manifest.get("input_digest") if isinstance(manifest, dict) else None
    return value if isinstance(value, str) else row["selected_manifest_hash"]



def inspect_workspace(store, preparation_id: str) -> WorkspacePreparation:
    with store.read_transaction() as tx:
        row = tx.execute(
            "SELECT * FROM context_workspaces WHERE preparation_id = ?", (preparation_id,),
        ).fetchone()
    if row is None:
        raise WorkspaceRefused("WORKSPACE_NOT_FOUND")
    return _from_row(row)


def _worktree_record(repository_path: Path, workspace: Path) -> list[str] | None:
    output = _git(repository_path, "worktree", "list", "--porcelain").stdout.strip()
    matches = [
        record.splitlines() for record in output.split("\n\n")
        if record.splitlines() and record.splitlines()[0] == f"worktree {workspace}"
    ]
    return matches[0] if len(matches) == 1 else None


def _verify_native_marker(preparation: WorkspacePreparation) -> None:
    _verify_native_identity(preparation)
    record = _worktree_record(preparation.repository_path, preparation.path)
    reason = f"locked ffs-preparation:{preparation.id}"
    if record is None or reason not in record or f"branch refs/heads/{preparation.branch}" not in record:
        raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")
    if f"HEAD {preparation.base_commit}" not in record:
        raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")
    try:
        descriptor = resolve_repository(preparation.path)
    except ContextRefused as error:
        raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH") from error
    expected = resolve_repository(preparation.repository_path)
    if descriptor.common_dir != expected.common_dir:
        raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")


def inspect_ready_layout(preparation: WorkspacePreparation) -> str:
    """Revalidate a committed READY record against current Git state."""
    if not preparation.path.is_dir():
        return "missing"
    try:
        _verify_native_identity(preparation)
    except (WorkspaceRefused, OSError):
        return "blocked"
    record = _worktree_record(preparation.repository_path, preparation.path)
    if (
        record is None
        or f"branch refs/heads/{preparation.branch}" not in record
        or f"HEAD {preparation.base_commit}" not in record
    ):
        return "blocked"
    try:
        observed = resolve_repository(preparation.path)
        expected = resolve_repository(preparation.repository_path)
    except ContextRefused:
        return "blocked"
    return "ready" if observed.common_dir == expected.common_dir else "blocked"


def _unlock_workspace_locked(
    store, token: OwnerToken, preparation: WorkspacePreparation,
    *, admission_guard: Callable | None = None,
) -> None:
    """Unlock one READY worktree while the caller holds its Git-admin lock."""
    with store.fenced_operation(token):
        _guarded_workspace_effect(
            store, token, preparation.id, admission_guard, expected_states=("ready",),
        )
        if inspect_ready_layout(preparation) != "ready":
            raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")
        record = _worktree_record(preparation.repository_path, preparation.path)
        if record is None:
            raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")
        locks = [line for line in record if line == "locked" or line.startswith("locked ")]
        if not locks:
            return
        if locks != [f"locked ffs-preparation:{preparation.id}"]:
            raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")
        if _git(
            preparation.repository_path,
            "worktree",
            "unlock",
            os.fspath(preparation.path),
            check=False,
        ).returncode:
            raise WorkspaceRefused("WORKSPACE_UNLOCK_PENDING", state="ready")


def finalize_ready_unlock(
    store, token: OwnerToken, preparation_id: str, *, admission_guard: Callable | None = None,
) -> None:
    """Verify durable READY evidence before unlocking under the current fence."""
    preparation = inspect_workspace(store, preparation_id)
    _verify_snapshot_complete(store, preparation, verify_workspace=False)
    descriptor = resolve_repository(preparation.repository_path)
    with git_admin_lock(descriptor.common_dir):
        _unlock_workspace_locked(store, token, preparation, admission_guard=admission_guard)


def revalidate_ready_fence(store, token: OwnerToken, preparation_id: str) -> WorkspacePreparation:
    """Bind an existing physical READY workspace to a newly reclaimed fence."""
    preparation = inspect_workspace(store, preparation_id)
    if (
        preparation.run_id != token.run_id
        or (preparation.parent_preparation_id is None
            and durable_workspace_key(os.fspath(preparation.path))
            != durable_workspace_key(token.workspace))
    ):
        raise OwnershipRefused("FENCE_REVOKED")
    # READY may contain legitimate run-local edits.  Only the immutable capture
    # and completion receipt are revalidated here, never the live overlay.
    _verify_snapshot_complete(store, preparation, verify_workspace=False)
    descriptor = resolve_repository(preparation.repository_path)
    with git_admin_lock(descriptor.common_dir):
        with store.fenced_operation(token):
            if preparation.state != "ready" or inspect_ready_layout(preparation) != "ready":
                raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")
            record = _worktree_record(preparation.repository_path, preparation.path)
            if record is None:
                raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")
            locks = [line for line in record if line == "locked" or line.startswith("locked ")]
            if locks and locks != [f"locked ffs-preparation:{preparation.id}"]:
                raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")
            with store.transaction() as tx:
                assert_owner(tx, token)
                row = tx.execute(
                    "SELECT * FROM context_workspaces WHERE preparation_id = ?",
                    (preparation_id,),
                ).fetchone()
                _assert_preparation_binding(row, token, require_generation=False, tx=tx)
                if row["generation"] != token.generation:
                    _assert_child_recovery_reconciled(tx, row)
                changed_workspace = tx.execute(
                    "UPDATE context_workspaces SET generation = ?, updated_at = ? "
                    "WHERE preparation_id = ? AND repository_id = ? AND run_id = ? AND state = 'ready'",
                    (token.generation, _now(), preparation_id, token.repository_id, token.run_id),
                ).rowcount
                changed = tx.execute(
                    "UPDATE context_runs SET generation = ?, updated_at = ? "
                    "WHERE repository_id = ? AND run_id = ? AND preparation_id = ? AND state = 'ready'",
                    (token.generation, _now(), token.repository_id, token.run_id, preparation_id),
                ).rowcount
                if (
                    changed_workspace != 1
                    or (preparation.parent_preparation_id is None
                        and _registered_context_repository(tx, token.repository_id) and changed != 1)
                    or (preparation.parent_preparation_id is not None and changed != 0)
                    or changed not in (0, 1)
                ):
                    raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")
                tx.execute(
                    "INSERT INTO control_events (event_type, payload) VALUES ('READY_REVALIDATED', ?)",
                    (json.dumps({"preparation_id": preparation_id, "run_id": token.run_id},
                                sort_keys=True, separators=(",", ":")),),
                )
            if locks and _git(
                preparation.repository_path,
                "worktree",
                "unlock",
                os.fspath(preparation.path),
                check=False,
            ).returncode:
                raise WorkspaceRefused("WORKSPACE_UNLOCK_PENDING", state="ready")
    return inspect_workspace(store, preparation_id)


def _mark_blocked(store, token: OwnerToken, preparation_id: str) -> None:
    with store.transaction() as tx:
        assert_owner(tx, token)
        row = tx.execute(
            "SELECT * FROM context_workspaces WHERE preparation_id = ?", (preparation_id,),
        ).fetchone()
        _assert_preparation_binding(row, token, require_generation=True, tx=tx)
        changed = tx.execute(
            "UPDATE context_workspaces SET state = 'blocked', updated_at = ? "
            "WHERE preparation_id = ? AND repository_id = ? AND run_id = ? AND state = 'preparing'",
            (_now(), preparation_id, token.repository_id, token.run_id),
        ).rowcount
        if changed != 1:
            raise OwnershipRefused("FENCE_REVOKED")
        tx.execute(
            "INSERT INTO control_events (event_type, payload) VALUES ('WORKSPACE_BLOCKED', ?)",
            (json.dumps({"preparation_id": preparation_id}, separators=(",", ":")),),
        )


def publish_workspace_ready(
    store, token: OwnerToken, preparation_id: str, *, admission_guard: Callable | None = None,
) -> WorkspacePreparation:
    preparation = inspect_workspace(store, preparation_id)
    if (
        preparation.run_id != token.run_id
        or (preparation.parent_preparation_id is None
            and durable_workspace_key(os.fspath(preparation.path))
            != durable_workspace_key(token.workspace))
    ):
        raise OwnershipRefused("FENCE_REVOKED")
    _verify_native_marker(preparation)
    _verify_snapshot_complete(store, preparation)
    # Publish removal authority at the same lifecycle boundary that proves the
    # native worktree.  The file is outside the removable workspace and is
    # harmless if a crash leaves it unreferenced before the database commit.
    # Historical READY rows keep ``owned_manifest=NULL`` and therefore remain
    # ineligible for destructive finalization until explicitly migrated.
    ownership_manifest = (
        preparation.owned_resource_manifest
        or _write_owned_manifest(store, preparation, created=True)
    )
    with store.fenced_operation(token):
        with store.transaction() as tx:
            assert_owner(tx, token)
            context = tx.execute(
                "SELECT upstream_json FROM context_runs WHERE repository_id=? AND run_id=?",
                (token.repository_id, token.run_id),
            ).fetchone()
            if context is not None:
                try:
                    upstream = json.loads(context["upstream_json"])
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise WorkspaceRefused("UPSTREAM_BINDING_INCOMPLETE") from error
                if isinstance(upstream, dict) and "runtime_manifest_sha256" in upstream:
                    required = {
                        "planning_root", "project", "workstream", "session_key",
                        "effective_session_key", "resolver_version", "runtime_digest",
                        "runtime_manifest_sha256",
                    }
                    if set(upstream) != required:
                        raise WorkspaceRefused("UPSTREAM_BINDING_INCOMPLETE")
            row = tx.execute(
                "SELECT * FROM context_workspaces WHERE preparation_id = ?",
                (preparation_id,),
            ).fetchone()
            _assert_preparation_binding(row, token, require_generation=True, tx=tx)
            if row["state"] not in ("preparing", "ready"):
                raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")
            if row["owned_manifest"] not in (None, ownership_manifest):
                raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")
            if admission_guard is not None:
                admission_guard(tx)
            changed_workspace = tx.execute(
                "UPDATE context_workspaces SET state = 'ready', created_by_ffs = 1, "
                "owned_manifest = ?, updated_at = ? "
                "WHERE preparation_id = ? AND repository_id = ? AND run_id = ? AND generation = ?",
                (ownership_manifest, _now(), preparation_id, token.repository_id,
                 token.run_id, token.generation),
            ).rowcount
            changed_run = tx.execute(
                "UPDATE context_runs SET state = 'ready', preparation_id = ?, updated_at = ? "
                "WHERE repository_id = ? AND run_id = ? AND generation = ? AND preparation_id = ?",
                (preparation_id, _now(), token.repository_id, token.run_id, token.generation, preparation_id),
            ).rowcount
            if (
                changed_workspace != 1
                or (preparation.parent_preparation_id is None
                    and _registered_context_repository(tx, token.repository_id) and changed_run != 1)
                or (preparation.parent_preparation_id is not None and changed_run != 0)
                or changed_run not in (0, 1)
            ):
                raise OwnershipRefused("FENCE_REVOKED")
            tx.execute(
                "INSERT INTO control_events (event_type, payload) VALUES ('WORKSPACE_READY', ?)",
                (json.dumps({"preparation_id": preparation_id, "run_id": token.run_id},
                            sort_keys=True, separators=(",", ":")),),
            )
    return inspect_workspace(store, preparation_id)


def adopt_workspace_preparation_fence(
    store, token: OwnerToken, preparation_id: str,
) -> WorkspacePreparation:
    """Move a proven orphaned native preparation to a reclaimed owner fence."""
    preparation = inspect_workspace(store, preparation_id)
    if (
        preparation.run_id != token.run_id
        or (preparation.parent_preparation_id is None
            and durable_workspace_key(os.fspath(preparation.path))
            != durable_workspace_key(token.workspace))
    ):
        raise OwnershipRefused("FENCE_REVOKED")
    descriptor = resolve_repository(preparation.repository_path)
    with git_admin_lock(descriptor.common_dir):
        _verify_native_marker(preparation)
        _verify_snapshot_complete(store, preparation)
        with store.transaction() as tx:
            assert_owner(tx, token)
            row = tx.execute(
                "SELECT * FROM context_workspaces WHERE preparation_id = ?",
                (preparation_id,),
            ).fetchone()
            _assert_preparation_binding(row, token, require_generation=False, tx=tx)
            _assert_child_recovery_reconciled(tx, row)
            if row is None or row["state"] not in ("preparing", "blocked"):
                raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")
            changed_workspace = tx.execute(
                "UPDATE context_workspaces SET generation = ?, state = 'preparing', updated_at = ? "
                "WHERE preparation_id = ? AND repository_id = ? AND run_id = ?",
                (token.generation, _now(), preparation_id, token.repository_id, token.run_id),
            ).rowcount
            changed_run = tx.execute(
                "UPDATE context_runs SET generation = ?, state = 'preparing', updated_at = ? "
                "WHERE repository_id = ? AND run_id = ? AND preparation_id = ?",
                (token.generation, _now(), token.repository_id, token.run_id, preparation_id),
            ).rowcount
            if (
                changed_workspace != 1
                or (preparation.parent_preparation_id is None
                    and _registered_context_repository(tx, token.repository_id) and changed_run != 1)
                or (preparation.parent_preparation_id is not None and changed_run != 0)
                or changed_run not in (0, 1)
            ):
                raise OwnershipRefused("FENCE_REVOKED")
            tx.execute(
                "INSERT INTO control_events (event_type, payload) VALUES ('PREPARATION_RECLAIMED', ?)",
                (json.dumps({"preparation_id": preparation_id, "run_id": token.run_id},
                            sort_keys=True, separators=(",", ":")),),
            )
    return inspect_workspace(store, preparation_id)


def adopt_unstarted_workspace_preparation_fence(
    store, token: OwnerToken, *, repository_path: Path,
    preparation_id: str | None = None,
) -> WorkspacePreparation | None:
    """Re-fence a crash window that is proven to precede every Git mutation."""
    descriptor = resolve_repository(Path(repository_path).resolve(strict=True))
    preparation = inspect_workspace(store, preparation_id) if preparation_id is not None else None
    workspace = preparation.path if preparation is not None else Path(token.workspace)
    branch = preparation.branch if preparation is not None else f"ffs/runs/{token.run_id}"
    if preparation is not None:
        recorded = resolve_repository(preparation.repository_path)
        if descriptor.common_dir != recorded.common_dir:
            raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")
    with git_admin_lock(descriptor.common_dir):
        if (
            workspace.exists() or workspace.is_symlink()
            or _branch_exists(descriptor.checkout, branch)
            or _registered(descriptor.checkout, workspace)
        ):
            raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")
        with store.transaction() as tx:
            assert_owner(tx, token)
            now = _now()
            if preparation_id is None:
                changed = tx.execute(
                    "UPDATE context_runs SET generation=?,state='preparing',updated_at=? "
                    "WHERE repository_id=? AND run_id=? AND workspace_key=? "
                    "AND preparation_id IS NULL AND state IN ('preparing','blocked')",
                    (token.generation, now, token.repository_id, token.run_id,
                     durable_workspace_key(os.fspath(workspace))),
                ).rowcount
                if changed != 1:
                    raise OwnershipRefused("FENCE_REVOKED")
                return None
            row = tx.execute(
                "SELECT * FROM context_workspaces WHERE preparation_id=?",
                (preparation_id,),
            ).fetchone()
            _assert_preparation_binding(row, token, require_generation=False, tx=tx)
            _assert_child_recovery_reconciled(tx, row)
            if (
                row["state"] not in ("preparing", "blocked")
                or row["path_existed_before"] or row["branch_existed_before"]
                or row["registered_before"]
            ):
                raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")
            changed_workspace = tx.execute(
                "UPDATE context_workspaces SET generation=?,state='preparing',updated_at=? "
                "WHERE preparation_id=? AND repository_id=? AND run_id=?",
                (token.generation, now, preparation_id, token.repository_id, token.run_id),
            ).rowcount
            changed_run = tx.execute(
                "UPDATE context_runs SET generation=?,state='preparing',updated_at=? "
                "WHERE repository_id=? AND run_id=? AND preparation_id=?",
                (token.generation, now, token.repository_id, token.run_id, preparation_id),
            ).rowcount
            if (changed_workspace != 1
                    or (preparation.parent_preparation_id is None and changed_run != 1)
                    or (preparation.parent_preparation_id is not None and changed_run != 0)):
                raise OwnershipRefused("FENCE_REVOKED")
            tx.execute(
                "INSERT INTO control_events(event_type,payload) VALUES('PREPARATION_RECLAIMED',?)",
                (json.dumps({"preparation_id": preparation_id, "run_id": token.run_id,
                             "unstarted": True}, sort_keys=True, separators=(",", ":")),),
            )
    return inspect_workspace(store, preparation_id)


def recover_workspace_preparation(
    store,
    token: OwnerToken,
    preparation_id: str,
    *,
    before_ready: Callable[[Path], None] | None = None,
    admission_guard: Callable | None = None,
) -> WorkspacePreparation:
    preparation = inspect_workspace(store, preparation_id)
    descriptor = resolve_repository(preparation.repository_path)
    with git_admin_lock(descriptor.common_dir):
        try:
            snapshot = load_input_snapshot(store, preparation)
            if snapshot is not None:
                with store.read_transaction() as tx:
                    durable = tx.execute(
                        "SELECT completion_hash FROM context_input_snapshots WHERE preparation_id=?",
                        (preparation_id,),
                    ).fetchone()
                if durable is None:
                    raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
                if durable["completion_hash"] is None:
                    # An interrupted overlay can have applied only a prefix of
                    # selected writes.  It has no durable receipt proving the
                    # effect set, so recovery must fail closed rather than
                    # overwrite or complete a potentially user-edited tree.
                    raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
            if before_ready is not None:
                with store.fenced_operation(token):
                    _guarded_workspace_effect(store, token, preparation_id, admission_guard)
                    before_ready(preparation.path)
            return publish_workspace_ready(
                store, token, preparation_id, admission_guard=admission_guard,
            )
        except WorkspaceRefused as error:
            if error.code.startswith("UPSTREAM_"):
                raise _record_failure(
                    store, token, preparation, refusal_code=error.code,
                ) from error
            if error.code == "WORKSPACE_OWNERSHIP_MISMATCH":
                _mark_blocked(store, token, preparation_id)
            raise


def _write_owned_manifest(store, preparation: WorkspacePreparation, *, created: bool) -> str:
    with store.read_transaction() as tx:
        registered = tx.execute(
            "SELECT evidence_root FROM context_runs WHERE repository_id = ? AND run_id = ?",
            (preparation.repository_id, preparation.run_id),
        ).fetchone()
    if registered is not None:
        directory = Path(registered["evidence_root"]) / "recovery"
    else:
        repository_key = hashlib.sha256(preparation.repository_id.encode()).hexdigest()
        run_key = hashlib.sha256(preparation.run_id.encode()).hexdigest()
        directory = store.db_path.parent / "runs" / repository_key / run_key / "recovery"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for parent in (store.db_path.parent / "runs", directory.parent.parent, directory.parent, directory):
        os.chmod(parent, 0o700)
    path = directory / f"{preparation.id}.json"
    temporary = directory / f".{preparation.id}.{uuid.uuid4().hex}.tmp"
    payload = {
        "schema_version": 1, "preparation_id": preparation.id,
        "run_id": preparation.run_id, "path": os.fspath(preparation.path),
        "branch": preparation.branch, "base_commit": preparation.base_commit,
        "created": created,
        "path_existed_before": preparation.path_existed_before,
        "branch_existed_before": preparation.branch_existed_before,
        "registered_before": preparation.registered_before,
    }
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        data = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise WorkspaceRefused("WORKSPACE_PREPARE_FAILED")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return os.fspath(path)


def _finalization_binding(store, repository_id: str, run_id: str, preparation_id: str) -> dict:
    """Read the explicit target and its existing authority in one snapshot."""
    with store.read_transaction() as tx:
        def one(query, parameters):
            row = tx.execute(query, parameters).fetchone()
            return dict(row) if row is not None else None

        workspace = one(
            "SELECT preparation_id, repository_id, run_id, path, path_key, branch, "
            "base_commit, common_dir, repository_path, generation, state, owned_manifest, "
            "created_by_ffs, path_existed_before, branch_existed_before, registered_before, "
            "parent_preparation_id, parent_activity_id, child_role, child_request_key, "
            "native_identity_json, updated_at FROM context_workspaces "
            "WHERE preparation_id=? AND repository_id=? AND run_id=?",
            (preparation_id, repository_id, run_id),
        )
        run = one(
            "SELECT repository_id, run_id, workspace, workspace_key, evidence_root, "
            "state, generation, activity_id, preparation_id, writer_version, "
            "result_json IS NOT NULL AS has_result, updated_at FROM context_runs "
            "WHERE repository_id=? AND run_id=?", (repository_id, run_id),
        )
        repository = one("SELECT * FROM context_repositories WHERE repository_id=?", (repository_id,))
        if workspace is None or run is None or repository is None:
            raise WorkspaceRefused("FINALIZATION_TARGET_UNAVAILABLE")
        parent = None
        activity_id = run["activity_id"]
        if workspace["parent_preparation_id"] is not None:
            parent = one(
                "SELECT preparation_id,repository_id,run_id,path,path_key,common_dir,"
                "generation,state,created_by_ffs FROM context_workspaces "
                "WHERE preparation_id=? AND repository_id=? AND run_id=?",
                (workspace["parent_preparation_id"], repository_id, run_id),
            )
            activity_id = workspace["parent_activity_id"]
        activity = one(
            "SELECT id, repository_id, run_id, state, generation, "
            "result_json IS NOT NULL AS has_result, updated_at FROM authority_activities "
            "WHERE id=? AND repository_id=? AND run_id=?",
            (activity_id, repository_id, run_id),
        )
        snapshot = one(
            "SELECT preparation_id, repository_id, run_id, base_commit, input_digest, "
            "full_manifest_hash, capture_locator, capture_hash, completion_locator, "
            "completion_hash FROM context_input_snapshots WHERE preparation_id=?",
            (preparation_id,),
        )
    is_child = workspace["parent_preparation_id"] is not None
    if is_child:
        child_fields = (workspace["parent_activity_id"], workspace["child_role"],
                        workspace["child_request_key"])
        expected_root = None if parent is None else Path(parent["path"]) / ".ffs-children"
        if (any(not isinstance(value, str) or not value for value in child_fields)
                or parent is None
                or run["preparation_id"] != parent["preparation_id"]
                or run["workspace"] != parent["path"]
                or run["workspace_key"] != parent["path_key"]
                or workspace["common_dir"] != parent["common_dir"]
                or workspace["generation"] != parent["generation"]
                or expected_root is None
                or workspace["path"] != str(expected_root / Path(workspace["path"]).name)
                or Path(workspace["path"]).parent != expected_root
                or workspace["branch"].split("/")[:3] != ["ffs", "children", run_id]):
            raise WorkspaceRefused("FINALIZATION_TARGET_BINDING")
    elif any(workspace[key] is not None for key in (
        "parent_activity_id", "child_role", "child_request_key",
    )):
        raise WorkspaceRefused("FINALIZATION_TARGET_BINDING")
    if (activity is None or activity["generation"] != run["generation"]
            or (not is_child and run["preparation_id"] != preparation_id)
            or (not is_child and run["workspace"] != workspace["path"])
            or (not is_child and run["workspace_key"] != workspace["path_key"])
            or type(workspace["generation"]) is not int or workspace["generation"] < 1
            or workspace["generation"] != run["generation"]
            or workspace["common_dir"] != repository["common_dir"]):
        raise WorkspaceRefused("FINALIZATION_TARGET_BINDING")
    for key in ("created_by_ffs", "path_existed_before", "branch_existed_before", "registered_before"):
        if type(workspace[key]) is not int or workspace[key] not in (0, 1):
            raise WorkspaceRefused("FINALIZATION_TARGET_BINDING")
    if snapshot is not None and any(snapshot[key] != workspace[key] for key in (
        "preparation_id", "repository_id", "run_id", "base_commit",
    )):
        raise WorkspaceRefused("FINALIZATION_TARGET_BINDING")
    return {"workspace": workspace, "run": run, "repository": repository,
            "activity": activity, "snapshot": snapshot, "parent": parent}


def finalization_preview(store, repository_id: str, run_id: str, preparation_id: str) -> dict:
    """Observe retained ownership metadata; never harvest, authorize or remove."""
    from run_context import ContextRefused, resolve_evidence

    if any(not isinstance(value, str) or not value or len(value) > 256 or "\0" in value
           for value in (repository_id, run_id, preparation_id)):
        raise WorkspaceRefused("FINALIZATION_TARGET_BINDING")
    try:
        if str(uuid.UUID(preparation_id)) != preparation_id:
            raise ValueError
        canonical_evidence = resolve_evidence(store.db_path.parent, run_id, repository_id)
    except (ValueError, ContextRefused) as error:
        raise WorkspaceRefused("FINALIZATION_TARGET_BINDING") from error
    store.validate_context_schema()
    before = _finalization_binding(store, repository_id, run_id, preparation_id)
    workspace, run = before["workspace"], before["run"]
    if run["evidence_root"] != str(canonical_evidence):
        raise WorkspaceRefused("FINALIZATION_EVIDENCE_BINDING")
    for raw in (workspace["path"], workspace["common_dir"], str(canonical_evidence)):
        path = Path(raw)
        if not path.is_absolute() or str(path) != raw or ".." in path.parts:
            raise WorkspaceRefused("FINALIZATION_TARGET_BINDING")
    evidence_identity = _directory_identity(canonical_evidence, ())
    result = {
        "target": {"repository_id": repository_id, "run_id": run_id,
                   "preparation_id": preparation_id, "workspace": workspace["path"],
                   "generation": workspace["generation"]},
        "states": {"run": run["state"], "preparation": workspace["state"],
                   "activity": before["activity"]["state"]},
        "apply_allowed": False, "evidence_harvest_complete": False,
        "removal_resources": [],
        "unmet": ["evidence_harvest_not_implemented", "owner_liveness_not_observed",
                  "launch_state_not_observed"],
        "retained_evidence": {},
    }
    if run["state"] not in ("complete", "failed", "aborted"):
        result["unmet"].append("run_not_terminal")
    if run["writer_version"] is None:
        result["unmet"].append("legacy_writer_ownership_not_transferred")
    snapshot = before["snapshot"]
    # These references require their own capture/result verifiers. Do not silently
    # omit them, read arbitrary locators, or call an observation a custody receipt.
    for key in ("capture_locator", "completion_locator"):
        declared = snapshot is not None and bool(snapshot[key])
        result["retained_evidence"][key] = "not_observed" if declared else "not_declared"
        if declared:
            result["unmet"].append(key + "_not_observed")
    if snapshot is None:
        result["unmet"].append("input_snapshot_record_unavailable")
    has_result = bool(run["has_result"] or before["activity"]["has_result"])
    result["retained_evidence"]["result"] = "not_observed" if has_result else "not_declared"
    if has_result:
        result["unmet"].append("result_evidence_not_observed")
    manifest = workspace["owned_manifest"]
    manifest_observation = None
    if manifest is None:
        result["unmet"].append("ownership_manifest_unavailable")
    else:
        relative = "recovery/" + preparation_id + ".json"
        expected = canonical_evidence / relative
        if manifest != str(expected):
            raise WorkspaceRefused("FINALIZATION_MANIFEST_BINDING")
        recovery_identity = _directory_identity(canonical_evidence, ("recovery",))

        def stable(info):
            return (info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode), info.st_nlink,
                    info.st_size, info.st_mtime_ns, info.st_ctime_ns)

        def closed_object(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate key")
                value[key] = item
            return value

        raw, identity = _read_anchored_regular_metadata(canonical_evidence, relative, max_bytes=1024 * 1024)
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=closed_object)
        except (UnicodeDecodeError, ValueError, RecursionError) as error:
            raise WorkspaceRefused("FINALIZATION_MANIFEST_INVALID") from error
        required = {
            "schema_version": 1, "preparation_id": preparation_id, "run_id": run_id,
            "path": workspace["path"], "branch": workspace["branch"],
            "base_commit": workspace["base_commit"], "created": bool(workspace["created_by_ffs"]),
            "path_existed_before": bool(workspace["path_existed_before"]),
            "branch_existed_before": bool(workspace["branch_existed_before"]),
            "registered_before": bool(workspace["registered_before"]),
        }
        if (type(value) is not dict or set(value) != set(required)
                or any(type(value[key]) is not type(item) or value[key] != item
                       for key, item in required.items())):
            raise WorkspaceRefused("FINALIZATION_MANIFEST_BINDING")
        reread, repeat = _read_anchored_regular_metadata(canonical_evidence, relative, max_bytes=1024 * 1024)
        if reread != raw or stable(repeat) != stable(identity):
            raise WorkspaceRefused("FINALIZATION_MANIFEST_CHANGED")
        manifest_observation = (relative, stable(identity), recovery_identity)
        result["ownership_manifest"] = {
            "locator": str(expected), "sha256": hashlib.sha256(raw).hexdigest(),
            "identity": list(stable(identity)), "created": value["created"],
        }
        result["unmet"].append("removal_identity_unproven")
    if _finalization_binding(store, repository_id, run_id, preparation_id) != before:
        raise WorkspaceRefused("FINALIZATION_TARGET_CHANGED")
    if _directory_identity(canonical_evidence, ()) != evidence_identity:
        raise WorkspaceRefused("FINALIZATION_MANIFEST_CHANGED")
    if manifest_observation is not None:
        relative, identity, recovery_identity = manifest_observation
        parent_fd = _open_directory_chain_raw(canonical_evidence, ("recovery",), create=False)
        try:
            info = os.fstat(parent_fd)
            current = os.stat(preparation_id + ".json", dir_fd=parent_fd, follow_symlinks=False)
            if (info.st_dev, info.st_ino) != recovery_identity or stable(current) != identity:
                raise WorkspaceRefused("FINALIZATION_MANIFEST_CHANGED")
        except OSError as error:
            raise WorkspaceRefused("FINALIZATION_MANIFEST_CHANGED") from error
        finally:
            os.close(parent_fd)
        _require_unchanged_directory_chain(canonical_evidence, ("recovery",), evidence_identity, recovery_identity)
    return result


_FINALIZATION_MAX_FILES = 100_000
_FINALIZATION_MAX_BYTES = 8 * 1024 * 1024 * 1024
_FINALIZATION_TERMINAL_RUN = frozenset({"complete", "failed", "aborted"})
_FINALIZATION_TERMINAL_ACTIVITY = frozenset({"succeeded", "failed", "aborted"})


def _stable_stat(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode),
            stat.S_IMODE(info.st_mode), info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _fsync_path_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_workspace_harvest(source: Path, destination: Path) -> list[dict]:
    """Copy a stable, closed regular-file tree without following links."""
    try:
        root_fd = _open_directory_chain_raw(Path(source.anchor), source.parts[1:], create=False)
    except (OSError, WorkspaceRefused) as error:
        raise WorkspaceRefused("FINALIZATION_HARVEST_UNSAFE") from error
    root_before = _stable_stat(os.fstat(root_fd))
    entries: list[dict] = []
    totals = {"files": 0, "bytes": 0}

    def visit(source_fd: int, target: Path, prefix: tuple[str, ...]) -> None:
        directory_before = _stable_stat(os.fstat(source_fd))
        try:
            names = sorted(os.listdir(source_fd))
        except OSError as error:
            raise WorkspaceRefused("FINALIZATION_HARVEST_UNCERTAIN") from error
        for name in names:
            if not name or name in (".", "..") or "/" in name or "\0" in name:
                raise WorkspaceRefused("FINALIZATION_HARVEST_UNCERTAIN")
            relative = prefix + (name,)
            relative_text = "/".join(relative)
            try:
                before = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            except OSError as error:
                raise WorkspaceRefused("FINALIZATION_HARVEST_CHANGED") from error
            if stat.S_ISLNK(before.st_mode) or not (
                stat.S_ISDIR(before.st_mode) or stat.S_ISREG(before.st_mode)
            ):
                raise WorkspaceRefused("FINALIZATION_HARVEST_UNSAFE")
            if stat.S_ISDIR(before.st_mode):
                child_fd = os.open(
                    name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0), dir_fd=source_fd,
                )
                try:
                    if _stable_stat(os.fstat(child_fd)) != _stable_stat(before):
                        raise WorkspaceRefused("FINALIZATION_HARVEST_CHANGED")
                    child_target = target / name
                    child_target.mkdir(mode=stat.S_IMODE(before.st_mode))
                    visit(child_fd, child_target, relative)
                    os.chmod(child_target, stat.S_IMODE(before.st_mode))
                    _fsync_path_directory(child_target)
                    after = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
                    if _stable_stat(after) != _stable_stat(before):
                        raise WorkspaceRefused("FINALIZATION_HARVEST_CHANGED")
                    entries.append({"path": relative_text, "type": "directory",
                                    "mode": stat.S_IMODE(before.st_mode)})
                finally:
                    os.close(child_fd)
                continue
            if before.st_nlink != 1:
                raise WorkspaceRefused("FINALIZATION_HARVEST_UNSAFE")
            totals["files"] += 1
            totals["bytes"] += before.st_size
            if (totals["files"] > _FINALIZATION_MAX_FILES
                    or totals["bytes"] > _FINALIZATION_MAX_BYTES):
                raise WorkspaceRefused("FINALIZATION_HARVEST_LIMIT")
            source_file = os.open(
                name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=source_fd,
            )
            target_file = os.open(
                target / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0), stat.S_IMODE(before.st_mode),
            )
            digest = hashlib.sha256()
            observed_size = 0
            try:
                if _stable_stat(os.fstat(source_file)) != _stable_stat(before):
                    raise WorkspaceRefused("FINALIZATION_HARVEST_CHANGED")
                while True:
                    block = os.read(source_file, 1024 * 1024)
                    if not block:
                        break
                    observed_size += len(block)
                    if observed_size > before.st_size:
                        raise WorkspaceRefused("FINALIZATION_HARVEST_CHANGED")
                    digest.update(block)
                    view = memoryview(block)
                    while view:
                        written = os.write(target_file, view)
                        if written <= 0:
                            raise WorkspaceRefused("FINALIZATION_HARVEST_UNCERTAIN")
                        view = view[written:]
                os.fchmod(target_file, stat.S_IMODE(before.st_mode))
                os.fsync(target_file)
                if observed_size != before.st_size:
                    raise WorkspaceRefused("FINALIZATION_HARVEST_CHANGED")
                if _stable_stat(os.fstat(source_file)) != _stable_stat(before):
                    raise WorkspaceRefused("FINALIZATION_HARVEST_CHANGED")
            finally:
                os.close(target_file)
                os.close(source_file)
            after = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            if _stable_stat(after) != _stable_stat(before):
                raise WorkspaceRefused("FINALIZATION_HARVEST_CHANGED")
            entries.append({"path": relative_text, "type": "file",
                            "mode": stat.S_IMODE(before.st_mode), "size": before.st_size,
                            "sha256": digest.hexdigest()})
        if _stable_stat(os.fstat(source_fd)) != directory_before:
            raise WorkspaceRefused("FINALIZATION_HARVEST_CHANGED")

    try:
        visit(root_fd, destination, ())
        if _stable_stat(os.fstat(root_fd)) != root_before:
            raise WorkspaceRefused("FINALIZATION_HARVEST_CHANGED")
    finally:
        os.close(root_fd)
    return sorted(entries, key=lambda item: item["path"])


def _validate_harvest_tree(root: Path, entries: list[dict]) -> None:
    if not isinstance(entries, list) or len(entries) > _FINALIZATION_MAX_FILES * 2:
        raise WorkspaceRefused("FINALIZATION_HARVEST_INVALID")
    try:
        root_info = root.lstat()
    except OSError as error:
        raise WorkspaceRefused("FINALIZATION_HARVEST_CHANGED") from error
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise WorkspaceRefused("FINALIZATION_HARVEST_INVALID")
    expected = set()
    for item in entries:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise WorkspaceRefused("FINALIZATION_HARVEST_INVALID")
        relative = item["path"]
        parts = tuple(relative.split("/"))
        if (not parts or any(not part or part in (".", "..") for part in parts)
                or relative in expected):
            raise WorkspaceRefused("FINALIZATION_HARVEST_INVALID")
        expected.add(relative)
        path = root.joinpath(*parts)
        try:
            info = path.lstat()
        except OSError as error:
            raise WorkspaceRefused("FINALIZATION_HARVEST_CHANGED") from error
        if item.get("type") == "directory":
            if set(item) != {"path", "type", "mode"} or not stat.S_ISDIR(info.st_mode):
                raise WorkspaceRefused("FINALIZATION_HARVEST_INVALID")
            if stat.S_IMODE(info.st_mode) != item["mode"]:
                raise WorkspaceRefused("FINALIZATION_HARVEST_CHANGED")
        elif item.get("type") == "file":
            if (set(item) != {"path", "type", "mode", "size", "sha256"}
                    or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != item["mode"]
                    or info.st_size != item["size"]):
                raise WorkspaceRefused("FINALIZATION_HARVEST_CHANGED")
            parts = _safe_relative(relative)
            parent_fd = _open_directory_chain(root, parts[:-1], create=False)
            file_fd = -1
            digest = hashlib.sha256()
            try:
                file_fd = os.open(
                    parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent_fd,
                )
                before = os.fstat(file_fd)
                if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                        or before.st_size != item["size"]):
                    raise WorkspaceRefused("FINALIZATION_HARVEST_CHANGED")
                while True:
                    block = os.read(file_fd, 1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                if _stable_stat(os.fstat(file_fd)) != _stable_stat(before):
                    raise WorkspaceRefused("FINALIZATION_HARVEST_CHANGED")
            except OSError as error:
                raise WorkspaceRefused("FINALIZATION_HARVEST_CHANGED") from error
            finally:
                if file_fd >= 0:
                    os.close(file_fd)
                os.close(parent_fd)
            if digest.hexdigest() != item["sha256"]:
                raise WorkspaceRefused("FINALIZATION_HARVEST_CHANGED")
        else:
            raise WorkspaceRefused("FINALIZATION_HARVEST_INVALID")
    observed = {
        str(path.relative_to(root)).replace(os.sep, "/")
        for path in root.rglob("*")
    }
    if observed != expected:
        raise WorkspaceRefused("FINALIZATION_HARVEST_CHANGED")


def _finalization_database_evidence(store, repository_id: str, run_id: str) -> list[dict]:
    with store.read_transaction() as tx:
        run = tx.execute(
            "SELECT result_json FROM context_runs WHERE repository_id=? AND run_id=?",
            (repository_id, run_id),
        ).fetchone()
        activities = tx.execute(
            "SELECT id,state,result_json FROM authority_activities "
            "WHERE repository_id=? AND run_id=? ORDER BY id", (repository_id, run_id),
        ).fetchall()
        intents = tx.execute(
            "SELECT i.id,i.state,i.completion_status,i.completion_evidence_json "
            "FROM authority_launch_intents i JOIN authority_activities a ON a.id=i.activity_id "
            "WHERE a.repository_id=? AND a.run_id=? ORDER BY i.id",
            (repository_id, run_id),
        ).fetchall()
    evidence: list[dict] = []

    def add(kind: str, identity: str, encoded: str | None) -> None:
        if encoded is None:
            return
        if len(encoded.encode("utf-8")) > 16 * 1024 * 1024:
            raise WorkspaceRefused("FINALIZATION_EVIDENCE_UNCERTAIN")
        try:
            json.loads(encoded)
        except (TypeError, ValueError, RecursionError) as error:
            raise WorkspaceRefused("FINALIZATION_EVIDENCE_UNCERTAIN") from error
        evidence.append({"kind": kind, "id": identity,
                         "sha256": hashlib.sha256(encoded.encode()).hexdigest()})

    if run is None:
        raise WorkspaceRefused("FINALIZATION_TARGET_CHANGED")
    add("run-result", run_id, run["result_json"])
    for row in activities:
        if row["state"] not in _FINALIZATION_TERMINAL_ACTIVITY:
            raise WorkspaceRefused("FINALIZATION_ACTIVITY_ACTIVE")
        add("activity-result", row["id"], row["result_json"])
    for row in intents:
        if row["state"] not in {"completed_succeeded", "completed_failed"}:
            raise WorkspaceRefused("FINALIZATION_INTENT_UNCERTAIN")
        if row["completion_status"] not in {"succeeded", "failed"}:
            raise WorkspaceRefused("FINALIZATION_INTENT_UNCERTAIN")
        add("launch-completion", row["id"], row["completion_evidence_json"])
    if not evidence:
        raise WorkspaceRefused("FINALIZATION_EVIDENCE_UNCERTAIN")
    return evidence


def _assert_finalization_control_quiet(store, before: dict) -> None:
    workspace, run = before["workspace"], before["run"]
    from run_state.managed import MANAGED_WRITER_VERSION
    if run["writer_version"] != MANAGED_WRITER_VERSION:
        raise WorkspaceRefused("FINALIZATION_WRITER_MISMATCH")
    if run["state"] not in _FINALIZATION_TERMINAL_RUN:
        raise WorkspaceRefused("FINALIZATION_RUN_ACTIVE")
    if before["activity"]["state"] not in _FINALIZATION_TERMINAL_ACTIVITY:
        raise WorkspaceRefused("FINALIZATION_ACTIVITY_ACTIVE")
    if workspace["state"] not in {"ready", "blocked", "finalized"}:
        raise WorkspaceRefused("FINALIZATION_TARGET_CHANGED")
    run_key = json.dumps(
        {"repository_id": workspace["repository_id"], "run_id": workspace["run_id"]},
        sort_keys=True, separators=(",", ":"),
    )
    with store.read_transaction() as tx:
        held = tx.execute(
            "SELECT 1 FROM control_reservations WHERE held=1 AND resource_type='run' "
            "AND resource_key=? LIMIT 1", (run_key,),
        ).fetchone()
        active_intent = tx.execute(
            "SELECT 1 FROM authority_launch_intents i JOIN authority_activities a "
            "ON a.id=i.activity_id WHERE a.repository_id=? AND a.run_id=? AND "
            "i.state NOT IN ('completed_succeeded','completed_failed') LIMIT 1",
            (workspace["repository_id"], workspace["run_id"]),
        ).fetchone()
        descendant = tx.execute(
            "SELECT 1 FROM context_workspaces WHERE parent_preparation_id=? "
            "AND state!='finalized' LIMIT 1", (workspace["preparation_id"],),
        ).fetchone()
    if held is not None:
        raise WorkspaceRefused("FINALIZATION_OWNER_ACTIVE")
    if active_intent is not None:
        raise WorkspaceRefused("FINALIZATION_INTENT_UNCERTAIN")
    if descendant is not None:
        raise WorkspaceRefused("FINALIZATION_CHILD_ACTIVE")


def _publish_finalization_harvest(
    store, before: dict, canonical_evidence: Path, manifest_sha256: str,
) -> tuple[Path, str, list[dict], list[dict]]:
    workspace = before["workspace"]
    finalization_root = canonical_evidence / "finalization"
    finalization_root.mkdir(mode=0o700, exist_ok=True)
    if finalization_root.is_symlink() or not finalization_root.is_dir():
        raise WorkspaceRefused("FINALIZATION_EVIDENCE_BINDING")
    os.chmod(finalization_root, 0o700)
    target = finalization_root / workspace["preparation_id"]
    if target.exists() or target.is_symlink():
        raw, _ = _read_anchored_regular_metadata(
            target, "receipt.json", max_bytes=64 * 1024 * 1024,
        )
        try:
            receipt = json.loads(raw)
        except (TypeError, ValueError, RecursionError) as error:
            raise WorkspaceRefused("FINALIZATION_HARVEST_INVALID") from error
        required = {
            "schema": "ffs.finalization-harvest/v1",
            "repository_id": workspace["repository_id"], "run_id": workspace["run_id"],
            "preparation_id": workspace["preparation_id"], "generation": workspace["generation"],
            "workspace": workspace["path"], "manifest_sha256": manifest_sha256,
        }
        if (not isinstance(receipt, dict)
                or any(receipt.get(key) != value for key, value in required.items())
                or not isinstance(receipt.get("entries"), list)
                or not isinstance(receipt.get("database_evidence"), list)):
            raise WorkspaceRefused("FINALIZATION_HARVEST_INVALID")
        # The receipt is harvest metadata.  Source bytes live below payload/, so
        # a source file named receipt.json is ordinary evidence and must be
        # included in this validation.
        _validate_harvest_tree(target / "payload", receipt["entries"])
        return (target, hashlib.sha256(raw).hexdigest(), receipt["entries"],
                receipt["database_evidence"])

    staging = finalization_root / f".{workspace['preparation_id']}.{uuid.uuid4().hex}.tmp"
    staging.mkdir(mode=0o700)
    try:
        payload = staging / "payload"
        payload.mkdir(mode=0o700)
        entries = _copy_workspace_harvest(Path(workspace["path"]), payload)
        database_evidence = _finalization_database_evidence(
            store, workspace["repository_id"], workspace["run_id"],
        )
        receipt = {
            "schema": "ffs.finalization-harvest/v1",
            "repository_id": workspace["repository_id"], "run_id": workspace["run_id"],
            "preparation_id": workspace["preparation_id"], "generation": workspace["generation"],
            "workspace": workspace["path"], "manifest_sha256": manifest_sha256,
            "entries": entries, "database_evidence": database_evidence, "created_at": _now(),
        }
        receipt_bytes = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
        receipt_fd = os.open(staging / "receipt.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            view = memoryview(receipt_bytes)
            while view:
                written = os.write(receipt_fd, view)
                if written <= 0:
                    raise WorkspaceRefused("FINALIZATION_HARVEST_UNCERTAIN")
                view = view[written:]
            os.fsync(receipt_fd)
        finally:
            os.close(receipt_fd)
        _fsync_path_directory(payload)
        _fsync_path_directory(staging)
        os.replace(staging, target)
        _fsync_path_directory(finalization_root)
    except BaseException:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging)
        raise
    return target, hashlib.sha256(receipt_bytes).hexdigest(), entries, database_evidence


def finalization_apply(
    store, repository_id: str, run_id: str, preparation_id: str, *,
    expected_generation: int, expected_manifest_sha256: str,
) -> dict:
    """Harvest a terminal managed workspace, then remove only manifest-owned Git resources.

    The expected generation and manifest digest are obtained from
    :func:`finalization_preview`; they make application an explicit second
    operation.  Source landing/publication is deliberately outside this API.
    """
    preview = finalization_preview(store, repository_id, run_id, preparation_id)
    if (type(expected_generation) is not int or expected_generation < 1
            or preview["target"]["generation"] != expected_generation
            or not isinstance(expected_manifest_sha256, str)
            or len(expected_manifest_sha256) != 64
            or preview.get("ownership_manifest", {}).get("sha256") != expected_manifest_sha256):
        raise WorkspaceRefused("FINALIZATION_PREVIEW_CHANGED")
    before = _finalization_binding(store, repository_id, run_id, preparation_id)
    _assert_finalization_control_quiet(store, before)
    workspace = before["workspace"]
    ownership = preview["ownership_manifest"]
    if (not ownership["created"] or not workspace["created_by_ffs"]
            or workspace["path_existed_before"] or workspace["branch_existed_before"]
            or workspace["registered_before"]):
        raise WorkspaceRefused("FINALIZATION_RESOURCE_UNOWNED")
    preparation = inspect_workspace(store, preparation_id)
    if (preparation.repository_id != repository_id or preparation.run_id != run_id
            or preparation.generation != expected_generation
            or str(preparation.path) != workspace["path"]):
        raise WorkspaceRefused("FINALIZATION_TARGET_CHANGED")
    path_present_before_harvest = preparation.path.exists() or preparation.path.is_symlink()
    if preparation.native_identity is None:
        raise WorkspaceRefused("FINALIZATION_RESOURCE_IDENTITY")
    if path_present_before_harvest:
        try:
            _verify_native_identity(preparation)
        except (OSError, WorkspaceRefused) as error:
            raise WorkspaceRefused("FINALIZATION_RESOURCE_IDENTITY") from error
    if before["snapshot"] is None:
        raise WorkspaceRefused("FINALIZATION_EVIDENCE_UNCERTAIN")
    _verify_snapshot_complete(store, preparation, verify_workspace=False)
    from run_context import resolve_evidence
    try:
        canonical_evidence = resolve_evidence(store.db_path.parent, run_id, repository_id)
    except ContextRefused as error:
        raise WorkspaceRefused("FINALIZATION_EVIDENCE_BINDING") from error
    if before["run"]["evidence_root"] != str(canonical_evidence):
        raise WorkspaceRefused("FINALIZATION_EVIDENCE_BINDING")

    evidence_identity = _directory_identity(canonical_evidence, ())
    harvest, receipt_sha256, entries, database_evidence = _publish_finalization_harvest(
        store, before, canonical_evidence, expected_manifest_sha256,
    )
    # Re-read every durable binding after harvesting and before recording the
    # custody boundary.  The receipt is external and remains useful if this
    # transaction or a later Git effect is interrupted.
    if _finalization_binding(store, repository_id, run_id, preparation_id) != before:
        raise WorkspaceRefused("FINALIZATION_TARGET_CHANGED")
    if _directory_identity(canonical_evidence, ()) != evidence_identity:
        raise WorkspaceRefused("FINALIZATION_EVIDENCE_CHANGED")
    _assert_finalization_control_quiet(store, before)
    if _finalization_database_evidence(store, repository_id, run_id) != database_evidence:
        raise WorkspaceRefused("FINALIZATION_EVIDENCE_CHANGED")
    with store.transaction() as tx:
        current = tx.execute(
            "SELECT generation,state,owned_manifest,created_by_ffs FROM context_workspaces "
            "WHERE preparation_id=? AND repository_id=? AND run_id=?",
            (preparation_id, repository_id, run_id),
        ).fetchone()
        if (current is None or current["generation"] != expected_generation
                or current["owned_manifest"] != before["workspace"]["owned_manifest"]
                or not current["created_by_ffs"]):
            raise WorkspaceRefused("FINALIZATION_TARGET_CHANGED")
        tx.execute(
            "INSERT INTO control_events(event_type,payload) VALUES('FINALIZATION_HARVESTED',?)",
            (json.dumps({"repository_id": repository_id, "run_id": run_id,
                         "preparation_id": preparation_id, "generation": expected_generation,
                         "receipt": str(harvest / "receipt.json"),
                         "receipt_sha256": receipt_sha256},
                        sort_keys=True, separators=(",", ":")),),
        )

    descriptor = resolve_repository(preparation.repository_path)
    with git_admin_lock(descriptor.common_dir):
        path_present = preparation.path.exists() or preparation.path.is_symlink()
        record = _worktree_record(preparation.repository_path, preparation.path)
        if path_present:
            _verify_native_identity(preparation)
            if record is None or f"branch refs/heads/{preparation.branch}" not in record:
                raise WorkspaceRefused("FINALIZATION_RESOURCE_IDENTITY")
            try:
                observed_repository = resolve_repository(preparation.path)
            except ContextRefused as error:
                raise WorkspaceRefused("FINALIZATION_RESOURCE_IDENTITY") from error
            if observed_repository.common_dir != descriptor.common_dir:
                raise WorkspaceRefused("FINALIZATION_RESOURCE_IDENTITY")
            observed_head = _git(preparation.path, "rev-parse", "HEAD").stdout.strip()
            branch_head = _git(
                preparation.repository_path, "rev-parse", "--verify",
                f"refs/heads/{preparation.branch}",
            ).stdout.strip()
            if observed_head != preparation.base_commit or branch_head != preparation.base_commit:
                raise WorkspaceRefused("FINALIZATION_RESOURCE_DRIFT")
            # Verify that no bytes changed after the harvested copy was made.
            check_root = harvest.parent / f".{preparation.id}.{uuid.uuid4().hex}.verify"
            check_root.mkdir(mode=0o700)
            try:
                current_entries = _copy_workspace_harvest(preparation.path, check_root)
            finally:
                shutil.rmtree(check_root)
            if current_entries != entries:
                raise WorkspaceRefused("FINALIZATION_HARVEST_CHANGED")
            removed = _git(
                preparation.repository_path, "worktree", "remove", "--force", "--",
                os.fspath(preparation.path), check=False,
            )
            if removed.returncode or preparation.path.exists() or preparation.path.is_symlink():
                raise WorkspaceRefused("FINALIZATION_REMOVAL_FAILED")
        elif record is not None:
            raise WorkspaceRefused("FINALIZATION_RESOURCE_IDENTITY")
        branch = _git(
            preparation.repository_path, "rev-parse", "--verify",
            f"refs/heads/{preparation.branch}", check=False,
        )
        if branch.returncode == 0:
            if branch.stdout.strip() != preparation.base_commit:
                raise WorkspaceRefused("FINALIZATION_RESOURCE_DRIFT")
            deleted = _git(
                preparation.repository_path, "branch", "-D", "--", preparation.branch,
                check=False,
            )
            if deleted.returncode:
                raise WorkspaceRefused("FINALIZATION_REMOVAL_FAILED")

    with store.transaction() as tx:
        changed = tx.execute(
            "UPDATE context_workspaces SET state='finalized',updated_at=? "
            "WHERE preparation_id=? AND repository_id=? AND run_id=? AND generation=? "
            "AND state IN ('ready','blocked','finalized')",
            (_now(), preparation_id, repository_id, run_id, expected_generation),
        ).rowcount
        if changed != 1:
            raise WorkspaceRefused("FINALIZATION_TARGET_CHANGED")
        tx.execute(
            "INSERT INTO control_events(event_type,payload) VALUES('FINALIZATION_APPLIED',?)",
            (json.dumps({"repository_id": repository_id, "run_id": run_id,
                         "preparation_id": preparation_id, "generation": expected_generation,
                         "receipt_sha256": receipt_sha256,
                         "removed": [workspace["path"], workspace["branch"]]},
                        sort_keys=True, separators=(",", ":")),),
        )
    return {
        "schema": "ffs.finalization-result/v1", "repository_id": repository_id,
        "run_id": run_id, "preparation_id": preparation_id,
        "generation": expected_generation, "evidence_harvest_complete": True,
        "harvest_receipt": str(harvest / "receipt.json"),
        "harvest_receipt_sha256": receipt_sha256,
        "removed_resources": [workspace["path"], workspace["branch"]],
        "landing_performed": False,
    }


def _record_failure(
    store,
    token: OwnerToken,
    preparation: WorkspacePreparation,
    *,
    refusal_code: str = "WORKSPACE_PREPARE_FAILED",
) -> WorkspaceRefused:
    # Refuse a stale or sibling token before creating any recovery artifact.  The
    # binding is checked again in the state transition below because the file and
    # SQLite effects cannot share one atomic commit.
    with store.transaction() as tx:
        assert_owner(tx, token)
        row = tx.execute(
            "SELECT * FROM context_workspaces WHERE preparation_id = ?", (preparation.id,),
        ).fetchone()
        _assert_preparation_binding(row, token, require_generation=True, tx=tx)
    created = False
    try:
        _verify_native_marker(preparation)
        created = True
    except WorkspaceRefused:
        pass
    manifest = _write_owned_manifest(store, preparation, created=created)
    with store.transaction() as tx:
        assert_owner(tx, token)
        row = tx.execute(
            "SELECT * FROM context_workspaces WHERE preparation_id = ?", (preparation.id,),
        ).fetchone()
        _assert_preparation_binding(row, token, require_generation=True, tx=tx)
        changed_workspace = tx.execute(
            "UPDATE context_workspaces SET state = 'blocked', owned_manifest = ?, "
            "created_by_ffs = ?, updated_at = ? WHERE preparation_id = ? "
            "AND repository_id = ? AND run_id = ? AND generation = ?",
            (manifest, int(created), _now(), preparation.id, token.repository_id,
             token.run_id, token.generation),
        ).rowcount
        changed_run = tx.execute(
            "UPDATE context_runs SET state = 'blocked', updated_at = ? "
            "WHERE repository_id = ? AND run_id = ? AND preparation_id = ?",
            (_now(), token.repository_id, token.run_id, preparation.id),
        ).rowcount
        if (
            changed_workspace != 1
            or (preparation.parent_preparation_id is None
                and _registered_context_repository(tx, token.repository_id) and changed_run != 1)
            or (preparation.parent_preparation_id is not None and changed_run != 0)
            or changed_run not in (0, 1)
        ):
            raise OwnershipRefused("FENCE_REVOKED")
        tx.execute(
            "INSERT INTO control_events (event_type, payload) VALUES ('ABORTED', ?)",
            (json.dumps({"preparation_id": preparation.id, "run_id": preparation.run_id},
                        sort_keys=True, separators=(",", ":")),),
        )
    return WorkspaceRefused(
        refusal_code, state="blocked", owned_resource_manifest=manifest,
    )


def _reject_ambient_filters(repository_path: Path) -> None:
    tracked = _git(repository_path, "ls-files", "-z").stdout.split("\0")
    tracked = [value for value in tracked if value]
    if not tracked:
        return
    result = _git(repository_path, "check-attr", "-z", "filter", "--", *tracked)
    values = result.stdout.split("\0")
    for index in range(2, len(values), 3):
        value = values[index]
        if value not in ("", "unspecified", "unset"):
            raise WorkspaceRefused("WORKSPACE_PREPARE_FAILED")


def _create_workspace_parents(path: Path) -> None:
    """Create missing workspace parents without following a symlink component."""
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                info = current.lstat()
            else:
                info = current.lstat()
                os.chmod(current, 0o700)
        if not stat.S_ISDIR(info.st_mode):
            raise WorkspaceRefused("WORKSPACE_PREPARE_FAILED")


def _create_registered_worktree(store, token, preparation, admin_fd, *, admission_guard: Callable | None = None):
    """Order a bounded Git creation effect after a verified live fence."""
    with store.fenced_operation(token):
        current = _guarded_workspace_effect(
            store, token, preparation.id, admission_guard, expected_states=("preparing",),
        )
        if current != preparation:
            raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")
        target = f"refs/heads/{preparation.branch}"
        target_key = _portable_key(target)
        if any(_portable_key(name) == target_key for name in _branch_names(preparation.repository_path)):
            raise WorkspaceRefused("WORKSPACE_PREPARE_FAILED")
        if preparation.path.exists() or preparation.path.is_symlink() or _registered(
            preparation.repository_path, preparation.path
        ):
            raise WorkspaceRefused("WORKSPACE_PREPARE_FAILED")
        _reject_ambient_filters(preparation.repository_path)
        _create_workspace_parents(preparation.path.parent)
        hooks = store.db_path.parent / "git-hooks-disabled"
        _create_workspace_parents(hooks)
        command = [
            "git", "-c", f"core.hooksPath={hooks}", "-c", "core.fsmonitor=false",
            "-c", "maintenance.auto=false", "-c", "gc.auto=0",
            "worktree", "add", "--lock",
            "--reason", f"ffs-preparation:{preparation.id}", "-q", "-b",
            preparation.branch, os.fspath(preparation.path), preparation.base_commit,
        ]
        try:
            process = subprocess.Popen(
                command, cwd=preparation.repository_path, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                pass_fds=(admin_fd,), start_new_session=True,
                env=sanitized_git_environment(),
            )
        except OSError as error:
            raise WorkspaceRefused("WORKSPACE_PREPARE_FAILED") from error
        try:
            process.communicate(timeout=_GIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate(timeout=5)
            raise WorkspaceRefused("WORKSPACE_PREPARE_FAILED")
        if process.returncode:
            raise WorkspaceRefused("WORKSPACE_PREPARE_FAILED")
        directory = _open_directory_chain_raw(
            Path(preparation.path.anchor), preparation.path.parts[1:], create=False,
        )
        try:
            info = os.fstat(directory)
            identity = json.dumps([info.st_dev, info.st_ino], separators=(",", ":"))
            with store.transaction() as tx:
                assert_owner(tx, token)
                if admission_guard is not None:
                    admission_guard(tx)
                changed = tx.execute(
                    "UPDATE context_workspaces SET native_identity_json=? WHERE preparation_id=? "
                    "AND generation=? AND state='preparing' AND native_identity_json IS NULL",
                    (identity, preparation.id, token.generation),
                ).rowcount
                if changed != 1:
                    raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")
                row = tx.execute(
                    "SELECT * FROM context_workspaces WHERE preparation_id=?", (preparation.id,),
                ).fetchone()
                return _from_row(row)
        finally:
            os.close(directory)


def prepare_workspace(
    store,
    token: OwnerToken,
    preparation: WorkspacePreparation,
    *,
    input_snapshot: "InputSnapshot | None" = None,
    before_ready: Callable[[Path], None] | None = None,
    admission_guard: Callable | None = None,
) -> WorkspacePreparation:
    """Create, verify, publish, then unlock one worktree under an inherited flock."""
    descriptor = resolve_repository(preparation.repository_path)
    with git_admin_lock(descriptor.common_dir) as admin_handles:
        try:
            preparation = _create_registered_worktree(
                store, token, preparation, admin_handles.lock_fd,
                admission_guard=admission_guard,
            )
            if input_snapshot is not None:
                with store.fenced_operation(token):
                    _apply_input_snapshot_locked(
                        store, token, preparation.id, input_snapshot, admission_guard=admission_guard,
                    )
            if before_ready is not None:
                # The callback is intentionally inside the Git-admin lock and
                # fenced operation: external scope resolution must finish and
                # bind its result before READY is published or unlocked.
                with store.fenced_operation(token):
                    _guarded_workspace_effect(store, token, preparation.id, admission_guard)
                    before_ready(preparation.path)
            ready = publish_workspace_ready(
                store, token, preparation.id, admission_guard=admission_guard,
            )
            _unlock_workspace_locked(store, token, ready, admission_guard=admission_guard)
            return ready
        except (WorkspaceRefused, ContextRefused) as error:
            current = inspect_workspace(store, preparation.id)
            if current.state == "ready":
                raise
            # Resolver callbacks use WorkspaceRefused with a typed UPSTREAM_*
            # code.  Retain that public cause while recording the same owned
            # blocked-workspace evidence as any other preparation failure.
            refusal_code = (
                error.code
                if isinstance(error, WorkspaceRefused) and error.code.startswith("UPSTREAM_")
                else "WORKSPACE_PREPARE_FAILED"
            )
            raise _record_failure(
                store, token, preparation, refusal_code=refusal_code,
            )

@dataclass(frozen=True)
class InputSnapshot:
    selection: InputSelection
    staging: Path
    selection_manifest_hash: str
    input_digest: str
    _manifest_json: str

    @property
    def manifest(self) -> dict:
        """Return a fresh projection; callers cannot mutate captured material."""
        return json.loads(self._manifest_json)


def parse_input_selection(value: object) -> InputSelection:
    try:
        return _parse_selection(value)
    except SelectionRefused as error:
        code = error.code
        if code in {"RESERVED_SELECTION_PATH", "UNSAFE_SELECTION_PATH"}:
            code = "UNSAFE_SELECTION_PATH"
        raise WorkspaceRefused(code) from error


def _safe_relative(path: str) -> tuple[str, ...]:
    parts = tuple(path.split("/"))
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise WorkspaceRefused("UNSAFE_SELECTION_PATH")
    return parts


def _walk_regular(root: Path, relative: str) -> Path:
    current = root
    for part in _safe_relative(relative):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            raise WorkspaceRefused("SELECTION_INPUT_MISSING", candidates=[relative]) from None
        if stat.S_ISLNK(info.st_mode):
            raise WorkspaceRefused("UNSAFE_SELECTION_PATH")
    return current


def _open_directory_chain_raw(root: Path, parts: tuple[str, ...], *, create: bool) -> int:
    """Open a complete no-follow directory chain from filesystem root."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    root = Path(root)
    if not root.is_absolute():
        raise WorkspaceRefused("UNSAFE_SELECTION_PATH")
    fd = os.open(root.anchor, flags)
    try:
        for component in root.parts[1:]:
            child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        for part in parts:
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise WorkspaceRefused("SELECTION_INPUT_MISSING") from None
                os.mkdir(part, 0o700, dir_fd=fd)
                child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except FileNotFoundError as error:
        os.close(fd)
        raise WorkspaceRefused("SELECTION_INPUT_MISSING") from error
    except OSError as error:
        os.close(fd)
        raise WorkspaceRefused("UNSAFE_SELECTION_PATH") from error
    except BaseException:
        os.close(fd)
        raise


def _open_directory_chain(root: Path, parts: tuple[str, ...], *, create: bool) -> int:
    """Public seam for initial descriptor acquisition in bounded race tests."""
    return _open_directory_chain_raw(root, parts, create=create)


def _directory_identity(root: Path, parts: tuple[str, ...]) -> tuple[int, int]:
    fd = _open_directory_chain_raw(root, parts, create=False)
    try:
        info = os.fstat(fd)
        return info.st_dev, info.st_ino
    finally:
        os.close(fd)


def _require_unchanged_directory_chain(
    root: Path,
    parts: tuple[str, ...],
    root_identity: tuple[int, int],
    parent_identity: tuple[int, int],
) -> None:
    try:
        observed_root = _directory_identity(root, ())
        observed_parent = _directory_identity(root, parts)
    except WorkspaceRefused as error:
        raise WorkspaceRefused("SOURCE_CHANGED") from error
    if observed_root != root_identity or observed_parent != parent_identity:
        raise WorkspaceRefused("SOURCE_CHANGED")


def _read_anchored_regular_metadata(
    root: Path,
    relative: str,
    *,
    max_bytes: int | None = None,
) -> tuple[bytes, os.stat_result]:
    """Read one stable, single-link regular file through a no-follow chain."""
    parts = _safe_relative(relative)
    root_identity = _directory_identity(root, ())
    parent_fd = _open_directory_chain(root, parts[:-1], create=False)
    file_fd = -1
    try:
        parent_info = os.fstat(parent_fd)
        parent_identity = (parent_info.st_dev, parent_info.st_ino)
        _require_unchanged_directory_chain(
            root, parts[:-1], root_identity, parent_identity,
        )
        try:
            file_fd = os.open(
                parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=parent_fd,
            )
        except FileNotFoundError as error:
            raise WorkspaceRefused("SELECTION_INPUT_MISSING") from error
        except OSError as error:
            raise WorkspaceRefused("UNSAFE_SELECTION_PATH") from error
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise WorkspaceRefused("UNSAFE_SELECTION_PATH")
        if max_bytes is not None and before.st_size > max_bytes:
            raise WorkspaceRefused("INVALID_SELECTION")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(file_fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise WorkspaceRefused("INVALID_SELECTION")
            chunks.append(chunk)
        after = os.fstat(file_fd)
        _require_unchanged_directory_chain(
            root, parts[:-1], root_identity, parent_identity,
        )
        before_identity = (
            before.st_dev,
            before.st_ino,
            stat.S_IMODE(before.st_mode),
            before.st_nlink,
            before.st_mtime_ns,
            before.st_ctime_ns,
            before.st_size,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            stat.S_IMODE(after.st_mode),
            after.st_nlink,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_size,
        )
        if before_identity != after_identity:
            raise WorkspaceRefused("SOURCE_CHANGED")
        return b"".join(chunks), before
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(parent_fd)


def _read_anchored_regular(root: Path, relative: str) -> bytes:
    data, _metadata = _read_anchored_regular_metadata(root, relative)
    return data


def _base_entry_material(
    repository: Path,
    base_oid: str,
    relative: str,
) -> tuple[bytes, str]:
    """Read one declared file from the selected Git base, including its mode."""
    try:
        tree = subprocess.run(
            ["git", "ls-tree", base_oid, "--", relative],
            cwd=repository,
            env=sanitized_git_environment(),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkspaceRefused("SOURCE_CHANGED") from error
    fields = tree.stdout.rstrip("\n").split("\t", 1)
    if tree.returncode or len(fields) != 2 or fields[1] != relative:
        raise WorkspaceRefused("SOURCE_CHANGED")
    header = fields[0].split()
    if len(header) != 3 or header[1] != "blob" or header[0] not in {"100644", "100755"}:
        raise WorkspaceRefused("SOURCE_CHANGED")
    try:
        blob = subprocess.run(
            ["git", "cat-file", "blob", header[2]],
            cwd=repository,
            env=sanitized_git_environment(),
            capture_output=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkspaceRefused("SOURCE_CHANGED") from error
    if blob.returncode:
        raise WorkspaceRefused("SOURCE_CHANGED")
    return blob.stdout, header[0]


def read_operator_selection_manifest(path: Path) -> object:
    """Read a bounded operator manifest before Git or control-state access."""
    candidate = Path(path)
    if not candidate.is_absolute() or candidate.name in {"", ".", ".."}:
        raise WorkspaceRefused("UNSAFE_SELECTION_PATH")
    try:
        payload, _metadata = _read_anchored_regular_metadata(
            candidate.parent,
            candidate.name,
            max_bytes=1024 * 1024,
        )
    except WorkspaceRefused:
        raise
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkspaceRefused("INVALID_SELECTION") from error


def _atomic_snapshot_write(root: Path, relative: str, data: bytes, mode: int) -> None:
    parts = _safe_relative(relative)
    root_identity = _directory_identity(root, ())
    parent_fd = _open_directory_chain(root, parts[:-1], create=True)
    temporary = f".{parts[-1]}.{uuid.uuid4().hex}.tmp"
    try:
        parent_info = os.fstat(parent_fd)
        parent_identity = (parent_info.st_dev, parent_info.st_ino)
        _require_unchanged_directory_chain(
            root, parts[:-1], root_identity, parent_identity,
        )
        try:
            existing = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and stat.S_ISLNK(existing.st_mode):
            raise WorkspaceRefused("UNSAFE_SELECTION_PATH")
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            mode,
            dir_fd=parent_fd,
        )
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise WorkspaceRefused("WORKSPACE_PREPARE_FAILED")
                view = view[written:]
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
        _require_unchanged_directory_chain(
            root, parts[:-1], root_identity, parent_identity,
        )
        os.replace(temporary, parts[-1], src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
        _require_unchanged_directory_chain(
            root, parts[:-1], root_identity, parent_identity,
        )
    finally:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def _anchored_delete(root: Path, relative: str) -> None:
    parts = _safe_relative(relative)
    root_identity = _directory_identity(root, ())
    parent_fd = _open_directory_chain(root, parts[:-1], create=False)
    try:
        parent_info = os.fstat(parent_fd)
        parent_identity = (parent_info.st_dev, parent_info.st_ino)
        _require_unchanged_directory_chain(
            root, parts[:-1], root_identity, parent_identity,
        )
        try:
            info = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode):
            raise WorkspaceRefused("UNSAFE_SELECTION_PATH")
        os.unlink(parts[-1], dir_fd=parent_fd)
        os.fsync(parent_fd)
        _require_unchanged_directory_chain(
            root, parts[:-1], root_identity, parent_identity,
        )
    finally:
        os.close(parent_fd)


def _remove_new_capture(staging: Path, parent: Path, *, parent_created: bool) -> None:
    """Remove only this invocation's newly-created capture on a failed write."""
    try:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging)
        if parent_created:
            parent.rmdir()
    except OSError:
        # The original refusal remains authoritative.  A later invocation sees
        # a non-empty/foreign capture name as INPUT_SELECTION_CHANGED.
        pass


def validate_selected_inputs(primary: Path, selection: InputSelection) -> dict[str, bytes]:
    """Read and validate registered selected material without writing state.

    Returned copy bytes are an instantaneous capture, not permission to skip
    validation later. Snapshot preparation revalidates under its live fence.
    """
    primary = Path(primary).resolve(strict=True)
    descriptor = resolve_repository(primary)
    try:
        registered_identity = registered_repository_identity(descriptor)
    except ContextRefused as error:
        if error.code == "REPOSITORY_NOT_REGISTERED":
            raise WorkspaceRefused("REPOSITORY_NOT_REGISTERED") from error
        raise WorkspaceRefused("SELECTION_REPOSITORY_MISMATCH") from error
    if selection.repository_id != registered_identity:
        raise WorkspaceRefused("SELECTION_REPOSITORY_MISMATCH")

    if _git(primary, "rev-parse", "HEAD").stdout.strip() != selection.base_oid:
        raise WorkspaceRefused("SELECTION_BASE_MISMATCH")

    selected_paths = {entry.path for entry in selection.entries}
    for required in selection.required_context:
        if required.path in selected_paths:
            continue
        tracked_at_base = _git(
            primary,
            "cat-file",
            "-e",
            f"{selection.base_oid}:{required.path}",
            check=False,
        ).returncode == 0
        if not tracked_at_base:
            required_path = primary / required.path
            if required_path.exists() or required_path.is_symlink():
                raise WorkspaceRefused("INPUT_SELECTION_REQUIRED", candidates=[required.path])
            raise WorkspaceRefused("SELECTION_INPUT_MISSING", candidates=[required.path])
        changed = _git(
            primary,
            "diff",
            "--quiet",
            selection.base_oid,
            "--",
            required.path,
            check=False,
        )
        if changed.returncode != 0:
            raise WorkspaceRefused("INPUT_SELECTION_REQUIRED", candidates=[required.path])

    # Capture every source before creating any staging directory. A rejection
    # caused by a source path or descriptor race therefore leaves no capture
    # footprint at all.
    source_bytes: dict[str, bytes] = {}
    for entry in selection.entries:
        if entry.operation == "delete":
            data, base_mode = _base_entry_material(primary, selection.base_oid, entry.path)
            if hashlib.sha256(data).hexdigest() != entry.sha256 or base_mode != entry.git_mode:
                raise WorkspaceRefused("SOURCE_CHANGED")
            continue
        data, metadata = _read_anchored_regular_metadata(primary, entry.path)
        # Git stores only the owner-execute bit for regular-file modes.  The
        # fixture filesystem may retain group/other permissions (for example
        # 0664), which must not turn a canonical 100644 selection into a
        # spurious source mismatch.
        observed_git_mode = "100755" if metadata.st_mode & stat.S_IXUSR else "100644"
        if (
            hashlib.sha256(data).hexdigest() != entry.sha256
            or observed_git_mode != entry.git_mode
        ):
            raise WorkspaceRefused("SOURCE_CHANGED")
        source_bytes[entry.path] = data

    return source_bytes


def snapshot_inputs(primary: Path, selection: InputSelection, staging: Path) -> InputSnapshot:
    source_bytes = validate_selected_inputs(primary, selection)
    parent = Path(staging)
    if parent.exists() and not parent.is_dir():
        raise WorkspaceRefused("INPUT_SELECTION_CHANGED")
    parent_created = not parent.exists()
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    capture_root = parent / selection.manifest_sha256
    try:
        capture_root.mkdir(mode=0o700)
    except FileExistsError as error:
        raise WorkspaceRefused("INPUT_SELECTION_CHANGED") from error
    try:
        files = capture_root / "files"
        files.mkdir(mode=0o700)
        captured: list[dict] = []
        for entry in selection.entries:
            row = {
                "operation": entry.operation,
                "path": entry.path,
                "sha256": entry.sha256,
                "git_mode": entry.git_mode,
            }
            if entry.operation == "copy":
                _atomic_snapshot_write(
                    files,
                    entry.path,
                    source_bytes[entry.path],
                    0o755 if entry.git_mode == "100755" else 0o644,
                )
            captured.append(row)
    except BaseException:
        _remove_new_capture(capture_root, parent, parent_created=parent_created)
        raise
    capture_files = [
        {"path": row["path"], "sha256": row["sha256"], "git_mode": row["git_mode"]}
        for row in captured if row["operation"] == "copy"
    ]
    capture_payload = json.dumps(capture_files, sort_keys=True, separators=(",", ":"))
    manifest = {
        "schema": "ffs.input-snapshot/v1",
        "selection_manifest_hash": selection.manifest_sha256,
        "input_digest": selection.input_digest,
        "base_oid": selection.base_oid,
        "repository_id": selection.repository_id,
        "entries": captured,
        "required_context": [
            {"path": item.path, "reason": item.reason}
            for item in selection.required_context
        ],
        "capture": {
            "locator": os.fspath(capture_root),
            "files_hash": hashlib.sha256(capture_payload.encode()).hexdigest(),
        },
        "upstream": {
            "project": selection.upstream.project,
            "workstream": selection.upstream.workstream,
            "session_key": selection.upstream.session_key,
        },
    }
    return InputSnapshot(
        selection=selection,
        staging=capture_root,
        selection_manifest_hash=selection.manifest_sha256,
        input_digest=selection.input_digest,
        _manifest_json=json.dumps(manifest, sort_keys=True, separators=(",", ":")),
    )


def load_input_snapshot(store, preparation: WorkspacePreparation) -> InputSnapshot | None:
    """Reconstruct the immutable staged snapshot retained for a PREPARING retry."""
    try:
        manifest = json.loads(preparation.selected_manifest_json)
    except (TypeError, ValueError) as error:
        raise WorkspaceRefused("SNAPSHOT_INCOMPLETE") from error
    if not isinstance(manifest, dict) or manifest.get("schema") != "ffs.input-snapshot/v1":
        return None
    capture = manifest.get("capture")
    if not isinstance(capture, dict) or not isinstance(capture.get("locator"), str):
        raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
    selection_value = {
        "schema": "ffs.input-selection/v1",
        "base_oid": manifest.get("base_oid"),
        "repository_id": manifest.get("repository_id"),
        "entries": manifest.get("entries"),
        "required_context": manifest.get("required_context", []),
        "upstream": manifest.get("upstream"),
    }
    try:
        selection = _parse_selection(selection_value)
    except SelectionRefused as error:
        raise WorkspaceRefused("SNAPSHOT_INCOMPLETE") from error
    if selection.manifest_sha256 != preparation.selected_manifest_hash:
        # The original selection hash is deliberately independent of the
        # private capture locator, but must match the recorded selection.
        raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
    return InputSnapshot(
        selection=selection,
        staging=Path(capture["locator"]),
        selection_manifest_hash=preparation.selected_manifest_hash,
        input_digest=preparation.input_digest,
        _manifest_json=json.dumps(manifest, sort_keys=True, separators=(",", ":")),
    )


def _verify_snapshot_complete(
    store,
    preparation: WorkspacePreparation,
    *,
    verify_workspace: bool = True,
) -> None:
    try:
        manifest = json.loads(preparation.selected_manifest_json)
    except (TypeError, ValueError) as error:
        raise WorkspaceRefused("SNAPSHOT_INCOMPLETE") from error
    if not isinstance(manifest, dict):
        raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
    if manifest.get("schema") != "ffs.input-snapshot/v1":
        # A literal legacy empty manifest retains M3's no-overlay behavior.
        # Every other non-snapshot shape is an incomplete selected capture and
        # must never publish or adopt READY.
        if set(manifest) == {"entries"} and manifest["entries"] == []:
            return
        raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
    entries = manifest.get("entries")
    capture = manifest.get("capture")
    if not isinstance(entries, list) or not isinstance(capture, dict):
        raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    with store.read_transaction() as tx:
        durable = tx.execute(
            "SELECT * FROM context_input_snapshots WHERE preparation_id=?", (preparation.id,),
        ).fetchone()
    if durable is None or durable["full_manifest_hash"] != hashlib.sha256(encoded.encode()).hexdigest():
        raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
    locator = capture.get("locator")
    if not isinstance(locator, str) or locator != durable["capture_locator"]:
        raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
    capture_root = Path(locator)
    observed_files: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"operation", "path", "sha256", "git_mode"}:
            raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
        if entry["operation"] == "copy":
            try:
                data = _read_anchored_regular(capture_root / "files", entry["path"])
            except WorkspaceRefused as error:
                raise WorkspaceRefused("SNAPSHOT_INCOMPLETE") from error
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
            observed_files.append({"path": entry["path"], "sha256": entry["sha256"], "git_mode": entry["git_mode"]})
    capture_hash = hashlib.sha256(json.dumps(observed_files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if capture_hash != durable["capture_hash"]:
        raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
    if durable["completion_hash"] is None or not isinstance(durable["completion_locator"], str):
        raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
    receipt_path = Path(durable["completion_locator"])
    try:
        receipt = _read_anchored_regular(receipt_path.parent, receipt_path.name)
    except WorkspaceRefused as error:
        raise WorkspaceRefused("SNAPSHOT_INCOMPLETE") from error
    if hashlib.sha256(receipt).hexdigest() != durable["completion_hash"]:
        raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
    try:
        receipt_value = json.loads(receipt)
    except (TypeError, ValueError) as error:
        raise WorkspaceRefused("SNAPSHOT_INCOMPLETE") from error
    if not isinstance(receipt_value, dict) or any((
        receipt_value.get("preparation_id") != preparation.id,
        receipt_value.get("repository_id") != preparation.repository_id,
        receipt_value.get("run_id") != preparation.run_id,
        receipt_value.get("base_commit") != preparation.base_commit,
        receipt_value.get("selection_manifest_hash") != preparation.selected_manifest_hash,
        receipt_value.get("input_digest") != preparation.input_digest,
        receipt_value.get("entries") != entries,
    )):
        raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
    # Before READY, compare the actual overlay.  READY revalidation and
    # finalization prove the immutable capture/receipt only, preserving later
    # run-local workspace edits.
    if not verify_workspace:
        return
    for entry in entries:
        if entry["operation"] == "delete":
            try:
                _read_anchored_regular(preparation.path, entry["path"])
            except WorkspaceRefused as error:
                if error.code == "SELECTION_INPUT_MISSING":
                    continue
                raise WorkspaceRefused("SNAPSHOT_INCOMPLETE") from error
            raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
        try:
            data = _read_anchored_regular(preparation.path, entry["path"])
        except WorkspaceRefused as error:
            raise WorkspaceRefused("SNAPSHOT_INCOMPLETE") from error
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")


def _apply_input_snapshot_locked(
    store, token: OwnerToken, preparation_id: str, snapshot: InputSnapshot,
    *, admission_guard: Callable | None = None,
) -> None:
    """Apply a snapshot while the caller holds Git-admin and authority guards."""
    manifest = snapshot.manifest
    encoded, manifest_hash = _manifest(manifest)
    with store.transaction() as tx:
        assert_owner(tx, token)
        row = tx.execute(
            "SELECT * FROM context_workspaces WHERE preparation_id = ?", (preparation_id,),
        ).fetchone()
        _assert_preparation_binding(row, token, require_generation=True, tx=tx)
        if admission_guard is not None:
            admission_guard(tx)
        if (
            row is None
            or row["state"] != "preparing"
            or row["selected_manifest_hash"] != snapshot.selection_manifest_hash
            or row["selected_manifest_hash"] != manifest_hash
            or row["selected_manifest_json"] != encoded
        ):
            raise WorkspaceRefused("INPUT_SELECTION_CHANGED")
        preparation = _from_row(row)
    for entry in manifest["entries"]:
        if entry["operation"] == "delete":
            _anchored_delete(preparation.path, entry["path"])
            continue
        data = _read_anchored_regular(snapshot.staging / "files", entry["path"])
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
        _atomic_snapshot_write(
            preparation.path,
            entry["path"],
            data,
            0o755 if entry["git_mode"] == "100755" else 0o644,
        )
    receipt = {
        "schema": "ffs.input-snapshot-completion/v1",
        "repository_id": preparation.repository_id,
        "run_id": preparation.run_id,
        "preparation_id": preparation.id,
        "base_commit": preparation.base_commit,
        "selection_manifest_hash": snapshot.selection_manifest_hash,
        "input_digest": snapshot.input_digest,
        "entries": manifest["entries"],
    }
    receipt_bytes = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
    # Captured input bytes may be shared by independent child preparations.
    # A preparation-specific receipt must not replace a parent's or sibling's
    # already-hashed completion evidence.
    receipt_relative = f"completions/{preparation.id}.json"
    receipt_path = snapshot.staging / receipt_relative
    _atomic_snapshot_write(snapshot.staging, receipt_relative, receipt_bytes, 0o600)
    receipt_hash = hashlib.sha256(receipt_bytes).hexdigest()
    with store.transaction() as tx:
        assert_owner(tx, token)
        if admission_guard is not None:
            admission_guard(tx)
        row = tx.execute("SELECT * FROM context_input_snapshots WHERE preparation_id=?", (preparation_id,)).fetchone()
        if row is None or row["full_manifest_hash"] != hashlib.sha256(encoded.encode()).hexdigest():
            raise WorkspaceRefused("INPUT_SELECTION_CHANGED")
        changed = tx.execute(
            "UPDATE context_input_snapshots SET completion_locator=?,completion_hash=?,applied_json=?,completed_at=? "
            "WHERE preparation_id=? AND completion_hash IS NULL",
            (os.fspath(receipt_path), receipt_hash, receipt_bytes.decode(), _now(), preparation_id),
        ).rowcount
        if changed != 1:
            raise WorkspaceRefused("INPUT_SELECTION_CHANGED")



def apply_input_snapshot(
    store, token: OwnerToken, preparation_id: str, snapshot: InputSnapshot,
) -> None:
    """Apply an immutable snapshot under the canonical lock order.

    Git administration is acquired before the cross-effect authority guard.
    The private helper has no caller-controlled bypass.
    """
    preparation = inspect_workspace(store, preparation_id)
    descriptor = resolve_repository(preparation.repository_path)
    with git_admin_lock(descriptor.common_dir):
        with store.fenced_operation(token):
            _apply_input_snapshot_locked(store, token, preparation_id, snapshot)
