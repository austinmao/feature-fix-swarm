"""Read-only explicit selected-input construction for managed frontend ingress."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
import stat

from run_context import registered_repository_identity, resolve_repository, validate_state_root
from run_state.selection import InputSelection
from run_state.workspace import (
    WorkspaceRefused,
    _base_entry_material,
    _git,
    _read_anchored_regular_metadata,
    parse_input_selection,
    validate_selected_inputs,
)


def build_frontend_selection(
    repository_path: str | Path,
    *,
    selected_files: tuple[str, ...],
    deleted_files: tuple[str, ...],
    required_context: tuple[str, ...],
    upstream: dict,
) -> InputSelection:
    """Validate explicit vocabulary before I/O, then bind exact source material.

    The draft hashes exist only for pure vocabulary validation. Returned
    selections contain real hashes and modes, revalidated against the source;
    workspace preparation must still capture them under its own live fence.
    """
    if not all(isinstance(paths, tuple) for paths in (
        selected_files, deleted_files, required_context,
    )):
        raise WorkspaceRefused("INVALID_SELECTION")
    try:
        repository = Path(repository_path).resolve(strict=True)
        descriptor = resolve_repository(repository)
        repository = descriptor.checkout
        identity = registered_repository_identity(descriptor)
        base = _git(repository, "rev-parse", "HEAD").stdout.strip()
        value = {
            "schema": "ffs.input-selection/v1",
            "base_oid": base,
            "repository_id": identity,
            "entries": [
                {"operation": operation, "path": path, "sha256": "0" * 64,
                 "git_mode": "100644"}
                for operation, paths in (("copy", selected_files), ("delete", deleted_files))
                for path in paths
            ],
            "required_context": [
                {"path": path, "reason": "frontend bootstrap context"}
                for path in required_context
            ],
            "upstream": upstream,
        }
        draft = parse_input_selection(value)
        entries = []
        for entry in draft.entries:
            if entry.operation == "copy":
                data, metadata = _read_anchored_regular_metadata(repository, entry.path)
                mode = "100755" if metadata.st_mode & stat.S_IXUSR else "100644"
            else:
                data, mode = _base_entry_material(repository, base, entry.path)
            entries.append({
                "operation": entry.operation, "path": entry.path,
                "sha256": hashlib.sha256(data).hexdigest(), "git_mode": mode,
            })
        value["entries"] = entries
        selection = parse_input_selection(value)
        validate_selected_inputs(repository, selection)
        return selection
    except FileNotFoundError as error:
        raise WorkspaceRefused("SELECTION_INPUT_MISSING") from error
    except OSError as error:
        raise WorkspaceRefused("UNSAFE_SELECTION_PATH") from error


def resume_frontend_selection(
    repository_path: str | Path,
    *,
    state_root: str | Path,
    run_id: str,
    selected_files: tuple[str, ...],
    deleted_files: tuple[str, ...],
    required_context: tuple[str, ...],
    upstream: dict,
    runtime,
    runtime_manifest_sha256: str,
    request_key: str | None = None,
) -> InputSelection:
    """Recover exact retained selection bytes without consulting current sources.

    Explicit paths and scope must still match the original selection. This
    read-only preflight does not replace the later ownership and READY fences.
    """
    from run_state.cli import _read_captured_material
    from run_state.state import ControlStore, ControlStoreRefused
    from run_state.upstream import UpstreamRefused
    from run_state.workspace import inspect_workspace, load_input_snapshot

    if not all(isinstance(paths, tuple) for paths in (
        selected_files, deleted_files, required_context,
    )):
        raise WorkspaceRefused("INVALID_SELECTION")
    try:
        descriptor = resolve_repository(Path(repository_path).resolve(strict=True))
        identity = registered_repository_identity(descriptor)
        root = validate_state_root(Path(state_root), descriptor)
        store = ControlStore.open_read_only(root / "control.sqlite3")
        with store.read_transaction() as tx:
            run = tx.execute(
                "SELECT * FROM context_runs WHERE repository_id=? AND run_id=?",
                (identity, run_id),
            ).fetchone()
            material = tx.execute(
                "SELECT snapshot_json, runtime_manifest_sha256 FROM context_run_material WHERE repository_id=? AND run_id=?",
                (identity, run_id),
            ).fetchone()
            cached = None if request_key is None else tx.execute(
                "SELECT run_id FROM context_requests WHERE repository_id=? AND request_key=?",
                (identity, request_key),
            ).fetchone()
            child_pointer = None if run is None else tx.execute(
                "SELECT 1 FROM authority_child_bindings WHERE activity_id=?", (run["activity_id"],),
            ).fetchone()
        if run is None or material is None or child_pointer is not None:
            raise WorkspaceRefused("UPSTREAM_BINDING_INCOMPLETE")
        if cached is not None and cached["run_id"] != run_id:
            raise WorkspaceRefused("UPSTREAM_BINDING_INCOMPLETE")
        if cached is not None and material["runtime_manifest_sha256"] != runtime_manifest_sha256:
            raise WorkspaceRefused("IDEMPOTENCY_CONFLICT")
        try:
            manifest = json.loads(material["snapshot_json"])
            if not isinstance(manifest, dict) or manifest.get("schema") != "ffs.input-snapshot/v1":
                raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
            retained = parse_input_selection({
                "schema": "ffs.input-selection/v1",
                "base_oid": manifest.get("base_oid"), "repository_id": manifest.get("repository_id"),
                "entries": manifest.get("entries"), "required_context": manifest.get("required_context"),
                "upstream": manifest.get("upstream"),
            })
        except (TypeError, ValueError, WorkspaceRefused) as error:
            raise WorkspaceRefused("SNAPSHOT_INCOMPLETE") from error
        if retained.repository_id != identity:
            raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
        draft = parse_input_selection({
            "schema": "ffs.input-selection/v1", "base_oid": retained.base_oid, "repository_id": identity,
            "entries": [
                {"operation": operation, "path": path, "sha256": "0" * 64, "git_mode": "100644"}
                for operation, paths in (("copy", selected_files), ("delete", deleted_files))
                for path in paths
            ],
            "required_context": [
                {"path": path, "reason": "frontend bootstrap context"} for path in required_context
            ],
            "upstream": upstream,
        })
        if (
            {(e.operation, e.path) for e in draft.entries}
            != {(e.operation, e.path) for e in retained.entries}
            or {r.path for r in draft.required_context} != {r.path for r in retained.required_context}
            or draft.upstream != retained.upstream
        ):
            if cached is not None:
                raise WorkspaceRefused("IDEMPOTENCY_CONFLICT")
            raise UpstreamRefused("UPSTREAM_CHANGED")
        snapshot = _read_captured_material(
            store, repository_id=identity, run_id=run_id, selection=retained,
            runtime=runtime, runtime_manifest_sha256=runtime_manifest_sha256,
            context_input_digest=run["input_digest"], preparation_id=run["preparation_id"],
        )
        if run["preparation_id"] is not None:
            preparation = inspect_workspace(store, run["preparation_id"])
            loaded = load_input_snapshot(store, preparation)
            if loaded is None or loaded.manifest != snapshot.manifest:
                raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
        copied = []
        for entry in retained.entries:
            if entry.operation != "copy":
                continue
            data, metadata = _read_anchored_regular_metadata(snapshot.staging / "files", entry.path)
            mode = "100755" if metadata.st_mode & stat.S_IXUSR else "100644"
            if hashlib.sha256(data).hexdigest() != entry.sha256 or mode != entry.git_mode:
                raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
            copied.append({"path": entry.path, "sha256": entry.sha256, "git_mode": entry.git_mode})
        files_hash = hashlib.sha256(json.dumps(copied, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if snapshot.manifest["capture"].get("files_hash") != files_hash:
            raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
        return retained
    except (ControlStoreRefused, sqlite3.Error) as error:
        raise WorkspaceRefused("UPSTREAM_BINDING_INCOMPLETE") from error
    except FileNotFoundError as error:
        raise WorkspaceRefused("SELECTION_INPUT_MISSING") from error
    except OSError as error:
        raise WorkspaceRefused("UNSAFE_SELECTION_PATH") from error
