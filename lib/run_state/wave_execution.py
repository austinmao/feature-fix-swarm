"""Owner-side snapshot and patch boundaries for supervised GSD waves."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
import unicodedata

from run_context import (
    ContextRefused,
    registered_repository_identity,
    resolve_repository,
    sanitized_git_environment,
)
from .ownership import OwnerToken, assert_owner
from .worker_channel import WorkerChannelRefused, parse_gsd_wave_manifest
from .workspace import (
    InputSnapshot,
    WorkspacePreparation,
    WorkspaceRefused,
    _base_entry_material,
    _read_anchored_regular_metadata,
    inspect_workspace,
    parse_input_selection,
    snapshot_inputs,
)


_GIT_TIMEOUT = 30.0
_SHA1 = re.compile(r"[0-9a-f]{40}")
_WAVE_REQUEST_PREFIX = ".planning/.ffs-wave-requests/"
_INTERNAL_DIRECTORY_ROOTS = (
    ".ffs-observer-tmp",
    ".planning/.ffs-wave-requests",
    ".planning/.ffs-worker-channel",
    ".planning/.ffs-supervised",
)


@dataclass(frozen=True)
class HarvestedPatch:
    """An immutable, scope-checked worker result and its retained evidence."""

    patch: str
    changed_files: tuple[str, ...]
    modified_files: tuple[str, ...]
    deleted_files: tuple[str, ...]
    evidence_path: Path
    sha256: str


@dataclass(frozen=True)
class _Inventory:
    modified: tuple[str, ...]
    deleted: tuple[str, ...]
    untracked: tuple[str, ...]

    @property
    def changed(self) -> tuple[str, ...]:
        return tuple(sorted((*self.modified, *self.deleted, *self.untracked)))


@dataclass(frozen=True)
class _Material:
    data: bytes
    mode: str


def _internal_path(relative: str) -> bool:
    return relative.startswith(".ffs-wave-") or any(
        relative == root or relative.startswith(root + "/")
        for root in _INTERNAL_DIRECTORY_ROOTS
    )


def _git_bytes(workspace: Path, *args: str, allowed: tuple[int, ...] = (0,)) -> bytes:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=workspace,
            env=sanitized_git_environment(),
            capture_output=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkspaceRefused("WAVE_GIT_FAILED") from error
    if result.returncode not in allowed:
        raise WorkspaceRefused("WAVE_GIT_FAILED")
    return result.stdout


def _head(workspace: Path) -> str:
    try:
        value = (
            _git_bytes(workspace, "rev-parse", "--verify", "HEAD^{commit}")
            .decode("ascii")
            .strip()
        )
    except UnicodeDecodeError as error:
        raise WorkspaceRefused("WAVE_HEAD_MISMATCH") from error
    if _SHA1.fullmatch(value) is None:
        raise WorkspaceRefused("WAVE_HEAD_MISMATCH")
    return value


def _path(value: bytes | str) -> str:
    try:
        decoded = value.decode("utf-8") if isinstance(value, bytes) else value
    except UnicodeDecodeError as error:
        raise WorkspaceRefused("UNSAFE_SELECTION_PATH") from error
    candidate = PurePosixPath(decoded)
    if (
        not decoded
        or candidate.is_absolute()
        or not candidate.parts
        or any(part in {"", ".", "..", ".git"} for part in candidate.parts)
        or "\\" in decoded
        or "\0" in decoded
        or any(ord(character) < 32 for character in decoded)
        or unicodedata.normalize("NFC", decoded) != decoded
    ):
        raise WorkspaceRefused("UNSAFE_SELECTION_PATH")
    return decoded


def _split_nul(raw: bytes) -> tuple[bytes, ...]:
    if raw and not raw.endswith(b"\0"):
        raise WorkspaceRefused("WAVE_GIT_FAILED")
    return tuple(item for item in raw.split(b"\0") if item)


def _inventory(workspace: Path, base: str) -> _Inventory:
    if _git_bytes(workspace, "ls-files", "-u", "-z"):
        raise WorkspaceRefused("WAVE_CONFLICTS_PRESENT")
    fields = _split_nul(
        _git_bytes(
            workspace,
            "--literal-pathspecs",
            "diff",
            "--name-status",
            "-z",
            "--no-renames",
            base,
            "--",
        )
    )
    if len(fields) % 2:
        raise WorkspaceRefused("WAVE_GIT_FAILED")
    modified: list[str] = []
    deleted: list[str] = []
    for index in range(0, len(fields), 2):
        try:
            status_code = fields[index].decode("ascii")
        except UnicodeDecodeError as error:
            raise WorkspaceRefused("WAVE_GIT_FAILED") from error
        relative = _path(fields[index + 1])
        if _internal_path(relative):
            continue
        if status_code == "D":
            deleted.append(relative)
        elif status_code in {"A", "M", "T"}:
            modified.append(relative)
        else:
            # --no-renames leaves only net adds/modifications/deletions for a
            # conflict-free tree. Refuse any future or repository-specific
            # status instead of guessing how to snapshot it.
            raise WorkspaceRefused("WAVE_CHANGE_UNSUPPORTED")
    untracked = [
        _path(item)
        for item in _split_nul(
            _git_bytes(
                workspace,
                "--literal-pathspecs",
                "ls-files",
                "--others",
                "-z",
                "--",
            )
        )
    ]
    untracked = [item for item in untracked if not _internal_path(item)]
    all_paths = [*modified, *deleted, *untracked]
    portable = [unicodedata.normalize("NFC", item).casefold() for item in all_paths]
    if len(portable) != len(set(portable)):
        raise WorkspaceRefused("SELECTION_CONFLICT")
    _refuse_untracked_specials(workspace)
    return _Inventory(
        tuple(sorted(modified)),
        tuple(sorted(deleted)),
        tuple(sorted(untracked)),
    )


def _refuse_untracked_specials(workspace: Path) -> None:
    """Find filesystem nodes Git omits from its untracked inventory."""
    pending = [(workspace, "")]
    while pending:
        directory, prefix = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as error:
            raise WorkspaceRefused("SOURCE_CHANGED") from error
        for entry in entries:
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            if relative == ".git" or _internal_path(relative):
                continue
            if entry.name == ".git":
                raise WorkspaceRefused("UNSAFE_SELECTION_PATH")
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise WorkspaceRefused("SOURCE_CHANGED") from error
            if stat.S_ISDIR(info.st_mode):
                pending.append((Path(entry.path), relative))
            elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                continue
            else:
                raise WorkspaceRefused("UNSAFE_SELECTION_PATH")


def _copy_entry(workspace: Path, relative: str) -> dict[str, str]:
    data, metadata = _read_anchored_regular_metadata(workspace, relative)
    return {
        "operation": "copy",
        "path": relative,
        "sha256": hashlib.sha256(data).hexdigest(),
        "git_mode": "100755" if metadata.st_mode & stat.S_IXUSR else "100644",
    }


def _delete_entry(workspace: Path, base: str, relative: str) -> dict[str, str]:
    data, mode = _base_entry_material(workspace, base, relative)
    return {
        "operation": "delete",
        "path": relative,
        "sha256": hashlib.sha256(data).hexdigest(),
        "git_mode": mode,
    }


def _material_entries(
    workspace: Path, base: str, inventory: _Inventory
) -> tuple[dict[str, str], ...]:
    entries = [
        *(
            _copy_entry(workspace, relative)
            for relative in (*inventory.modified, *inventory.untracked)
        ),
        *(_delete_entry(workspace, base, relative) for relative in inventory.deleted),
    ]
    return tuple(sorted(entries, key=lambda entry: entry["path"]))


def _upstream(preparation: WorkspacePreparation) -> dict[str, str | None]:
    try:
        retained = json.loads(preparation.selected_manifest_json)
    except (TypeError, ValueError) as error:
        raise WorkspaceRefused("SNAPSHOT_INCOMPLETE") from error
    upstream = retained.get("upstream") if isinstance(retained, dict) else None
    if not isinstance(upstream, dict) or set(upstream) != {
        "project",
        "workstream",
        "session_key",
    }:
        raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
    # The pure parser below performs the closed-schema identifier validation.
    return dict(upstream)


def _evidence_directory(evidence_root: Path, child: str) -> Path:
    root = Path(evidence_root)
    if not root.is_absolute():
        raise WorkspaceRefused("EVIDENCE_PATH_UNSAFE")
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or root.resolve() != root:
            raise WorkspaceRefused("EVIDENCE_PATH_UNSAFE")
        info = root.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise WorkspaceRefused("EVIDENCE_PATH_UNSAFE")
        destination = root / child
        destination.mkdir(mode=0o700, exist_ok=True)
        child_info = destination.lstat()
        if (
            destination.is_symlink()
            or destination.resolve() != destination
            or not stat.S_ISDIR(child_info.st_mode)
            or child_info.st_uid != os.getuid()
            or stat.S_IMODE(child_info.st_mode) != 0o700
        ):
            raise WorkspaceRefused("EVIDENCE_PATH_UNSAFE")
        return destination
    except WorkspaceRefused:
        raise
    except OSError as error:
        raise WorkspaceRefused("EVIDENCE_PATH_UNSAFE") from error


def _validated_wave(manifest: dict) -> dict:
    try:
        encoded = json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        parsed, _canonical = parse_gsd_wave_manifest(encoded)
    except (TypeError, ValueError, WorkerChannelRefused) as error:
        raise WorkspaceRefused("WAVE_MANIFEST_INVALID") from error
    return parsed


def capture_wave_snapshot(
    store,
    token: OwnerToken,
    parent_preparation: WorkspacePreparation,
    manifest: dict,
    evidence_root: Path,
) -> InputSnapshot:
    """Capture the parent's complete current overlay for one supervised wave."""
    wave = _validated_wave(manifest)
    current = inspect_workspace(store, parent_preparation.id)
    admission = wave["admission"]
    if (
        wave["orchestrator_root"] != str(current.path)
        or admission["workspace"] != str(current.path)
        or admission["repository_id"] != token.repository_id
        or admission["run_id"] != token.run_id
        or admission["generation"] != token.generation
    ):
        raise WorkspaceRefused("WAVE_ADMISSION_MISMATCH")
    return _capture_bound_snapshot(store, token, parent_preparation, current, admission,
                                   wave['initial_head'], wave, evidence_root)


def capture_prelaunch_snapshot(store, token, parent_preparation, *, activity_id,
                               runtime_identity, evidence_root):
    """Freeze a registered parent's overlay before a native group launches.

    This uses the same authority and source-race checks as wave capture without
    inventing a wave request before GSD has emitted one.
    """
    import uuid
    current = inspect_workspace(store, parent_preparation.id)
    admission = {'activity_id': activity_id, 'runtime_identity': runtime_identity}
    binding = {'schema': 'ffs.prelaunch-snapshot/v1', 'repository_id': token.repository_id,
               'run_id': token.run_id, 'generation': token.generation, 'preparation_id': current.id,
               'activity_id': activity_id, 'runtime_identity': runtime_identity,
               'initial_head': current.base_commit, 'capture_id': str(uuid.uuid4())}
    return _capture_bound_snapshot(store, token, parent_preparation, current, admission,
                                   current.base_commit, binding, evidence_root)


def _capture_bound_snapshot(store, token, parent_preparation, current, admission,
                            expected_head, binding, evidence_root):
    with store.transaction() as tx:
        assert_owner(tx, token)
        row = tx.execute(
            "SELECT created_by_ffs,parent_preparation_id,parent_activity_id,child_role "
            "FROM context_workspaces WHERE preparation_id=? AND repository_id=? "
            "AND run_id=? AND path=? AND generation=? AND state='ready'",
            (
                current.id,
                token.repository_id,
                token.run_id,
                str(current.path),
                token.generation,
            ),
        ).fetchone()
        if row is None or not row["created_by_ffs"]:
            raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")
        from .integration_journal import assert_settled_tx
        assert_settled_tx(tx, current.id)
        if row["parent_preparation_id"] is None:
            # Retain the direct fixture/root seam only when the manifest names
            # the run's exact current active activity and registered root.
            authority = tx.execute(
                "SELECT 1 FROM context_runs r JOIN authority_activities a ON a.id=r.activity_id "
                "WHERE r.repository_id=? AND r.run_id=? AND r.preparation_id=? AND r.workspace=? "
                "AND r.activity_id=? AND r.generation=? AND r.state='ready' "
                "AND a.repository_id=r.repository_id AND a.run_id=r.run_id "
                "AND a.generation=r.generation AND a.state='active'",
                (
                    token.repository_id,
                    token.run_id,
                    current.id,
                    str(current.path),
                    admission["activity_id"],
                    token.generation,
                ),
            ).fetchone()
        else:
            authority = tx.execute(
                "SELECT 1 FROM authority_activities a JOIN authority_child_bindings b "
                "ON b.activity_id=a.id WHERE a.id=? AND a.repository_id=? AND a.run_id=? "
                "AND a.generation=? AND a.state='active' AND b.workspace_preparation_id=? "
                "AND b.workspace_binding=? AND b.runtime_identity=? AND b.parent_activity_id=? "
                "AND b.role=?",
                (
                    admission["activity_id"],
                    token.repository_id,
                    token.run_id,
                    token.generation,
                    current.id,
                    str(current.path),
                    admission["runtime_identity"],
                    row["parent_activity_id"],
                    row["child_role"],
                ),
            ).fetchone()
        if authority is None:
            raise WorkspaceRefused("WAVE_ADMISSION_MISMATCH")
    if current != parent_preparation:
        raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")
    if _head(current.path) != expected_head:
        raise WorkspaceRefused("WAVE_HEAD_MISMATCH")
    before = _inventory(current.path, expected_head)
    entries = _material_entries(current.path, expected_head, before)
    selection = parse_input_selection(
        {
            "schema": "ffs.input-selection/v1",
            "base_oid": expected_head,
            "repository_id": current.repository_id,
            "entries": list(entries),
            "required_context": [],
            "upstream": _upstream(current),
        }
    )
    canonical_wave = json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
    staging = (
        _evidence_directory(Path(evidence_root), "wave-input-snapshots")
        / hashlib.sha256(canonical_wave).hexdigest()
    )
    snapshot = snapshot_inputs(current.path, selection, staging)
    if (
        _head(current.path) != expected_head
        or _inventory(current.path, expected_head) != before
        or _material_entries(current.path, expected_head, before) != entries
    ):
        raise WorkspaceRefused("SOURCE_CHANGED")
    return snapshot


def capture_integration_candidate(store, token, journal, evidence_root):
    """Capture the full output overlay predicted by one immutable wave input.

    The caller holds its workspace effect lock. No SQL writer spans capture
    or Git inspection, and the caller publishes this result under its fence.
    """
    from .integration_journal import validate_journal_tx
    from .workspace import _from_row, _verify_snapshot_complete, load_input_snapshot
    with store.read_transaction() as tx:
        _row, contract = validate_journal_tx(store, tx, token, journal)
        retained = tx.execute('SELECT e.payload FROM authority_event_keys k JOIN control_events e '
            'ON e.id=k.event_id WHERE k.activity_id=? AND k.idempotency_key=?',
            (journal['activity_id'], journal['wave_key'] + ':prepared')).fetchone()
        prepared = json.loads(retained['payload'])['data']
        preparations = []
        for plan in prepared['plans']:
            row = tx.execute('SELECT w.* FROM context_workspaces w JOIN authority_child_bindings b '
                'ON b.workspace_preparation_id=w.preparation_id WHERE b.activity_id=? AND w.preparation_id=? '
                'AND w.repository_id=? AND w.run_id=?',
                (plan['activity_id'], plan['workspace_preparation_id'], token.repository_id, token.run_id)).fetchone()
            if row is None:
                raise WorkspaceRefused('WAVE_CANDIDATE_BINDING_INVALID')
            preparations.append(_from_row(row))
    if not preparations or any(item.input_digest != prepared['input_digest'] for item in preparations):
        raise WorkspaceRefused('WAVE_CANDIDATE_BINDING_INVALID')
    before = preparations[0]
    _verify_snapshot_complete(store, before, verify_workspace=False)
    snapshot = load_input_snapshot(store, before)
    if snapshot is None:
        raise WorkspaceRefused('WAVE_CANDIDATE_BINDING_INVALID')
    workspace = Path(contract['authority']['workspace'])
    base = contract['authority']['base_commit']
    expected = {item['path']: item for item in snapshot.manifest['entries']}
    for path, after in contract['expected_after'].items():
        original = _material_record(_base_material(workspace, base, path))
        if after == original:
            expected.pop(path, None)
        elif after is None:
            if original is None:
                expected.pop(path, None)
            else:
                expected[path] = {'operation':'delete', 'path':path, **original}
        else:
            expected[path] = {'operation':'copy', 'path':path, **after}
    entries = tuple(sorted(expected.values(), key=lambda item: item['path']))
    observed = _inventory(workspace, base)
    if _head(workspace) != base or _material_entries(workspace, base, observed) != entries:
        raise WorkspaceRefused('WAVE_CANDIDATE_UNRELATED_CHANGE')
    selection = parse_input_selection({'schema':'ffs.input-selection/v1', 'base_oid':base,
        'repository_id':token.repository_id, 'entries':list(entries), 'required_context':[],
        'upstream':snapshot.manifest['upstream']})
    stage = _evidence_directory(Path(evidence_root), 'wave-candidate-snapshots') / journal['contract_sha256']
    output = snapshot_inputs(workspace, selection, stage)
    if (_head(workspace) != base or _inventory(workspace, base) != observed
            or _material_entries(workspace, base, observed) != entries):
        raise WorkspaceRefused('SOURCE_CHANGED')
    material = {'schema':'ffs.wave-candidate-output/v1', 'journal_sha256':journal['contract_sha256'],
        'input_digest':prepared['input_digest'], 'output_digest':output.input_digest,
        'base_commit':base, 'workspace':str(workspace), 'manifest':output.manifest}
    path, digest = _write_integration_evidence(evidence_root, _canonical_integration(material))
    return {'input_digest':prepared['input_digest'], 'output_digest':output.input_digest,
            'journal_sha256':journal['contract_sha256'], 'evidence':{'locator':str(path),'sha256':digest}}


def _declared_paths(values, *, field: str) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise WorkspaceRefused("WAVE_SCOPE_INVALID")
    result = tuple(
        _path(value) if isinstance(value, str) else _path(b"") for value in values
    )
    keys = [unicodedata.normalize("NFC", item).casefold() for item in result]
    if len(keys) != len(set(keys)):
        raise WorkspaceRefused("WAVE_SCOPE_INVALID")
    return tuple(sorted(result))


def _current_material(workspace: Path, relative: str) -> _Material | None:
    try:
        data, metadata = _read_anchored_regular_metadata(workspace, relative)
    except WorkspaceRefused as error:
        if error.code == "SELECTION_INPUT_MISSING":
            return None
        raise
    return _Material(data, "100755" if metadata.st_mode & stat.S_IXUSR else "100644")


def _base_material(workspace: Path, base: str, relative: str) -> _Material | None:
    raw = _git_bytes(
        workspace, "--literal-pathspecs", "ls-tree", "-z", base, "--", relative
    )
    if not raw:
        return None
    fields = _split_nul(raw)
    if len(fields) != 1:
        raise WorkspaceRefused("SOURCE_CHANGED")
    try:
        header, observed = fields[0].split(b"\t", 1)
        mode, kind, _object_id = header.decode("ascii").split()
        observed_path = observed.decode("utf-8")
    except (UnicodeDecodeError, ValueError) as error:
        raise WorkspaceRefused("SOURCE_CHANGED") from error
    if observed_path != relative or kind != "blob" or mode not in {"100644", "100755"}:
        raise WorkspaceRefused("UNSAFE_SELECTION_PATH")
    data, verified_mode = _base_entry_material(workspace, base, relative)
    if verified_mode != mode:
        raise WorkspaceRefused("SOURCE_CHANGED")
    return _Material(data, mode)


def _snapshot_overlay(
    workspace: Path,
    expected_head: str,
    snapshot: InputSnapshot | None,
) -> dict[str, _Material | None]:
    if snapshot is None:
        return {}
    manifest = snapshot.manifest
    try:
        repository_id = registered_repository_identity(resolve_repository(workspace))
    except ContextRefused as error:
        raise WorkspaceRefused("SNAPSHOT_INCOMPLETE") from error
    if (
        snapshot.selection.base_oid != expected_head
        or snapshot.selection.repository_id != repository_id
        or manifest.get("schema") != "ffs.input-snapshot/v1"
        or manifest.get("base_oid") != expected_head
        or manifest.get("selection_manifest_hash") != snapshot.selection_manifest_hash
        or manifest.get("input_digest") != snapshot.input_digest
        or manifest.get("repository_id") != snapshot.selection.repository_id
        or manifest.get("entries") != snapshot.selection.canonical_manifest["entries"]
    ):
        raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
    overlay: dict[str, _Material | None] = {}
    for entry in manifest["entries"]:
        relative = _path(entry["path"])
        if entry["operation"] == "copy":
            data, metadata = _read_anchored_regular_metadata(
                snapshot.staging / "files", relative
            )
            mode = "100755" if metadata.st_mode & stat.S_IXUSR else "100644"
            if (
                hashlib.sha256(data).hexdigest() != entry["sha256"]
                or mode != entry["git_mode"]
            ):
                raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
            overlay[relative] = _Material(data, mode)
        elif entry["operation"] == "delete":
            base = _base_material(workspace, expected_head, relative)
            if (
                base is None
                or hashlib.sha256(base.data).hexdigest() != entry["sha256"]
                or base.mode != entry["git_mode"]
            ):
                raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
            overlay[relative] = None
        else:
            raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
    return overlay


def _virtual_changes(
    workspace: Path,
    expected_head: str,
    raw: _Inventory,
    overlay: dict[str, _Material | None],
) -> tuple[_Inventory, tuple[tuple[str, _Material | None, _Material | None], ...]]:
    candidates = tuple(sorted(set(raw.changed) | set(overlay)))
    changed: list[tuple[str, _Material | None, _Material | None]] = []
    modified: list[str] = []
    deleted: list[str] = []
    for relative in candidates:
        baseline = (
            overlay[relative]
            if relative in overlay
            else _base_material(
                workspace,
                expected_head,
                relative,
            )
        )
        current = _current_material(workspace, relative)
        if baseline == current:
            continue
        changed.append((relative, baseline, current))
        (modified if current is not None else deleted).append(relative)
    return _Inventory(tuple(modified), tuple(deleted), ()), tuple(changed)


def _write_material(root: Path, relative: str, material: _Material) -> Path:
    destination = root / relative
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.write_bytes(material.data)
    destination.chmod(0o755 if material.mode == "100755" else 0o644)
    return destination


def _one_virtual_patch(
    temporary: Path,
    relative: str,
    baseline: _Material | None,
    current: _Material | None,
) -> bytes:
    before = (
        Path("/dev/null")
        if baseline is None
        else _write_material(
            temporary / "before",
            relative,
            baseline,
        ).relative_to(temporary)
    )
    after = (
        Path("/dev/null")
        if current is None
        else _write_material(
            temporary / "after",
            relative,
            current,
        ).relative_to(temporary)
    )
    payload = _git_bytes(
        temporary,
        "-c",
        "core.quotePath=false",
        "--literal-pathspecs",
        "diff",
        "--no-index",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--no-textconv",
        "--",
        str(before),
        str(after),
        allowed=(1,),
    )
    lines = payload.splitlines(keepends=True)
    if not lines or not lines[0].startswith(b"diff --git "):
        raise WorkspaceRefused("PATCH_PATH_MISMATCH")
    encoded = relative.encode("utf-8")
    lines[0] = b"diff --git a/" + encoded + b" b/" + encoded + b"\n"
    old_header = b"--- /dev/null\n" if baseline is None else b"--- a/" + encoded + b"\n"
    new_header = b"+++ /dev/null\n" if current is None else b"+++ b/" + encoded + b"\n"
    for index, line in enumerate(lines[1:], 1):
        if line.startswith(b"--- "):
            lines[index] = old_header
            break
        if line.startswith((b"@@ ", b"GIT binary patch")):
            break
    for index, line in enumerate(lines[1:], 1):
        if line.startswith(b"+++ "):
            lines[index] = new_header
            break
        if line.startswith((b"@@ ", b"GIT binary patch")):
            break
    return b"".join(lines)


def _patch(
    changes: tuple[tuple[str, _Material | None, _Material | None], ...],
    evidence_root: Path,
) -> bytes:
    scratch = _evidence_directory(evidence_root, "patch-material")
    with tempfile.TemporaryDirectory(prefix="wave-", dir=scratch) as directory:
        temporary = Path(directory)
        payload = b"".join(
            _one_virtual_patch(temporary, relative, baseline, current)
            for relative, baseline, current in changes
        )
    changed_paths = tuple(relative for relative, _baseline, _current in changes)
    expected_headers = {
        b"diff --git a/"
        + relative.encode("utf-8")
        + b" b/"
        + relative.encode("utf-8"): relative
        for relative in changed_paths
    }
    headers = [line for line in payload.splitlines() if line.startswith(b"diff --git ")]
    try:
        observed = tuple(sorted(expected_headers[line] for line in headers))
    except KeyError as error:
        raise WorkspaceRefused("PATCH_PATH_MISMATCH") from error
    if len(headers) != len(expected_headers) or observed != changed_paths:
        raise WorkspaceRefused("PATCH_PATH_MISMATCH")
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise WorkspaceRefused("PATCH_ENCODING_UNSUPPORTED") from error
    return payload


def _write_evidence(evidence_root: Path, payload: bytes) -> tuple[Path, str]:
    digest = hashlib.sha256(payload).hexdigest()
    root = _evidence_directory(Path(evidence_root), "harvested-patches")
    destination = root / f"{digest}.patch"
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except FileExistsError:
        try:
            info = destination.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or destination.read_bytes() != payload
            ):
                raise WorkspaceRefused("EVIDENCE_CONFLICT")
        except OSError as error:
            raise WorkspaceRefused("EVIDENCE_CONFLICT") from error
    else:
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise WorkspaceRefused("EVIDENCE_WRITE_FAILED")
                view = view[written:]
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return destination, digest


def harvest_scoped_patch(
    workspace: Path,
    expected_head: str,
    declared_modified,
    declared_deleted,
    evidence_root: Path,
    *,
    baseline_snapshot: InputSnapshot | None = None,
) -> HarvestedPatch:
    """Return a scoped patch relative to HEAD plus an optional captured overlay."""
    workspace = Path(workspace)
    if (
        not workspace.is_absolute()
        or workspace.is_symlink()
        or workspace.resolve() != workspace
    ):
        raise WorkspaceRefused("WORKSPACE_OWNERSHIP_MISMATCH")
    if _SHA1.fullmatch(expected_head) is None or _head(workspace) != expected_head:
        raise WorkspaceRefused("WAVE_HEAD_MISMATCH")
    modified_scope = _declared_paths(declared_modified, field="declared_modified")
    deleted_scope = _declared_paths(declared_deleted, field="declared_deleted")
    if set(modified_scope) & set(deleted_scope):
        raise WorkspaceRefused("WAVE_SCOPE_INVALID")
    raw = _inventory(workspace, expected_head)
    overlay = _snapshot_overlay(workspace, expected_head, baseline_snapshot)
    inventory, material = _virtual_changes(workspace, expected_head, raw, overlay)
    actual_modified = inventory.modified
    if not set(actual_modified).issubset(modified_scope) or not set(
        inventory.deleted
    ).issubset(deleted_scope):
        raise WorkspaceRefused("WAVE_SCOPE_VIOLATION")
    payload = _patch(material, Path(evidence_root)) if inventory.changed else b""
    after_raw = _inventory(workspace, expected_head)
    after_overlay = _snapshot_overlay(workspace, expected_head, baseline_snapshot)
    after_inventory, after_material = _virtual_changes(
        workspace,
        expected_head,
        after_raw,
        after_overlay,
    )
    if (
        _head(workspace) != expected_head
        or after_raw != raw
        or after_overlay != overlay
        or after_inventory != inventory
        or after_material != material
    ):
        raise WorkspaceRefused("SOURCE_CHANGED")
    evidence_path, digest = _write_evidence(Path(evidence_root), payload)
    final_raw = _inventory(workspace, expected_head)
    final_overlay = _snapshot_overlay(workspace, expected_head, baseline_snapshot)
    final_inventory, final_material = _virtual_changes(
        workspace,
        expected_head,
        final_raw,
        final_overlay,
    )
    if (
        _head(workspace) != expected_head
        or final_raw != raw
        or final_overlay != overlay
        or final_inventory != inventory
        or final_material != material
    ):
        raise WorkspaceRefused("SOURCE_CHANGED")
    return HarvestedPatch(
        patch=payload.decode("utf-8"),
        changed_files=inventory.changed,
        modified_files=actual_modified,
        deleted_files=inventory.deleted,
        evidence_path=evidence_path,
        sha256=digest,
    )


def _material_record(material: _Material | None) -> dict[str, str] | None:
    if material is None:
        return None
    return {
        "sha256": hashlib.sha256(material.data).hexdigest(),
        "git_mode": material.mode,
    }


def prepare_integration_material(
    workspace: Path,
    expected_head: str,
    results: list[dict],
    evidence_root: Path,
) -> dict[str, object]:
    """Calculate exact before/expected-after material in a disposable index.

    This is the pre-apply half of the integration journal.  It never writes the
    registered worktree or holds ControlStore/registry locks while Git hashes.
    """
    workspace = Path(workspace)
    if _head(workspace) != expected_head:
        raise WorkspaceRefused("WAVE_INTEGRATION_WORKSPACE_CHANGED")
    patches, paths = [], []
    for result in results:
        if result.get("status") != "complete":
            continue
        patch, changed = result.get("patch"), result.get("changed_files")
        if not isinstance(patch, str) or not isinstance(changed, list):
            raise WorkspaceRefused("WAVE_INTEGRATION_INVALID")
        if patch:
            patches.append(patch.encode("utf-8"))
            paths.extend(_declared_paths(changed, field="changed_files"))
        elif changed:
            raise WorkspaceRefused("WAVE_INTEGRATION_INVALID")
    if len(paths) != len(set(paths)):
        raise WorkspaceRefused("WAVE_INTEGRATION_CONFLICT")
    paths = sorted(paths)
    before = {
        path: _material_record(_current_material(workspace, path)) for path in paths
    }
    payload = b"".join(patches)
    scratch = _evidence_directory(Path(evidence_root), "integration-index")
    with tempfile.TemporaryDirectory(prefix="intent-", dir=scratch) as directory:
        environment = sanitized_git_environment()
        environment["GIT_INDEX_FILE"] = str(Path(directory) / "index")
        objects = Path(directory) / "objects"
        objects.mkdir(mode=0o700)
        location = subprocess.run(
            ["git", "rev-parse", "--git-path", "objects"], cwd=workspace,
            env=environment, capture_output=True, timeout=_GIT_TIMEOUT, check=False,
        )
        if location.returncode:
            raise WorkspaceRefused("WAVE_INTEGRATION_CONFLICT")
        original_objects = Path(location.stdout.decode().strip())
        if not original_objects.is_absolute():
            original_objects = workspace / original_objects
        environment["GIT_OBJECT_DIRECTORY"] = str(objects)
        environment["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(original_objects.resolve())

        def index_command(arguments, input_data=None):
            completed = subprocess.run(
                ["git", *arguments],
                cwd=workspace,
                env=environment,
                input=input_data,
                capture_output=True,
                timeout=_GIT_TIMEOUT,
                check=False,
            )
            if completed.returncode:
                raise WorkspaceRefused("WAVE_INTEGRATION_CONFLICT")
            return completed.stdout

        index_command(["read-tree", expected_head])
        # Worker patches are relative to the frozen overlay, which can differ
        # from HEAD. Seed only their paths without touching the real index or
        # object database.
        for path in paths:
            current = _current_material(workspace, path)
            if _material_record(current) != before[path]:
                raise WorkspaceRefused("WAVE_INTEGRATION_WORKSPACE_CHANGED")
            if current is None:
                index_command(["update-index", "--force-remove", "--", path])
            else:
                blob = index_command(["hash-object", "-w", "--stdin"], current.data).decode().strip()
                index_command(["update-index", "--add", "--cacheinfo", before[path]["git_mode"], blob, path])
        if payload:
            index_command(["apply", "--cached", "--whitespace=nowarn", "-"], payload)
        after = {}
        for path in paths:
            listing = subprocess.run(
                ["git", "ls-files", "-s", "--", path],
                cwd=workspace,
                env=environment,
                capture_output=True,
                timeout=_GIT_TIMEOUT,
                check=False,
            )
            if listing.returncode:
                raise WorkspaceRefused("WAVE_INTEGRATION_CONFLICT")
            if not listing.stdout:
                after[path] = None
                continue
            mode, _object, _stage_path = (
                listing.stdout.decode("utf-8").strip().split(None, 2)
            )
            content = subprocess.run(
                ["git", "show", ":" + path],
                cwd=workspace,
                env=environment,
                capture_output=True,
                timeout=_GIT_TIMEOUT,
                check=False,
            )
            if content.returncode:
                raise WorkspaceRefused("WAVE_INTEGRATION_CONFLICT")
            after[path] = {
                "sha256": hashlib.sha256(content.stdout).hexdigest(),
                "git_mode": mode,
            }
    if _head(workspace) != expected_head or any(
        _material_record(_current_material(workspace, path)) != before[path]
        for path in paths
    ):
        raise WorkspaceRefused("WAVE_INTEGRATION_WORKSPACE_CHANGED")
    return {
        "before": before,
        "expected_after": after,
        "patch_sha256": hashlib.sha256(payload).hexdigest(),
        "paths": paths,
    }


def integrate_wave_patches(
    workspace: Path,
    expected_head: str,
    results: list[dict],
    evidence_root: Path,
) -> dict[str, object]:
    """Apply one accepted wave atomically to its registered parent workspace."""
    workspace = Path(workspace)
    if (
        not workspace.is_absolute()
        or workspace.is_symlink()
        or workspace.resolve() != workspace
        or _head(workspace) != expected_head
    ):
        raise WorkspaceRefused("WAVE_INTEGRATION_WORKSPACE_CHANGED")
    patches: list[bytes] = []
    paths: list[str] = []
    for result in results:
        if result.get("status") != "complete":
            continue
        patch = result.get("patch")
        changed = result.get("changed_files")
        if not isinstance(patch, str) or not isinstance(changed, list):
            raise WorkspaceRefused("WAVE_INTEGRATION_INVALID")
        if patch:
            patches.append(patch.encode("utf-8"))
            paths.extend(_declared_paths(changed, field="changed_files"))
        elif changed:
            raise WorkspaceRefused("WAVE_INTEGRATION_INVALID")
    if len(paths) != len(set(paths)):
        raise WorkspaceRefused("WAVE_INTEGRATION_CONFLICT")
    before = {
        relative: _material_record(_current_material(workspace, relative))
        for relative in sorted(paths)
    }
    payload = b"".join(patches)
    if payload:
        environment = sanitized_git_environment()
        for arguments in (
            ("apply", "--check", "--whitespace=nowarn", "-"),
            ("apply", "--whitespace=nowarn", "-"),
        ):
            try:
                completed = subprocess.run(
                    ["git", *arguments],
                    cwd=workspace,
                    env=environment,
                    input=payload,
                    capture_output=True,
                    timeout=_GIT_TIMEOUT,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise WorkspaceRefused("WAVE_INTEGRATION_FAILED") from error
            if completed.returncode != 0:
                raise WorkspaceRefused("WAVE_INTEGRATION_CONFLICT")
    if _head(workspace) != expected_head:
        raise WorkspaceRefused("WAVE_HEAD_MISMATCH")
    after = {
        relative: _material_record(_current_material(workspace, relative))
        for relative in sorted(paths)
    }
    if any(before[relative] == after[relative] for relative in paths):
        raise WorkspaceRefused("WAVE_INTEGRATION_INVALID")
    evidence = {
        "schema": "ffs.wave-integration/v1",
        "workspace": str(workspace),
        "initial_head": expected_head,
        "patch_sha256": hashlib.sha256(payload).hexdigest(),
        "changed_files": sorted(paths),
        "before": before,
        "after": after,
    }
    raw = _canonical_integration(evidence)
    path, digest = _write_integration_evidence(Path(evidence_root), raw)
    return {"locator": str(path), "sha256": digest, "material": evidence}


def _canonical_integration(value: dict[str, object]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def _write_integration_evidence(
    evidence_root: Path, payload: bytes
) -> tuple[Path, str]:
    digest = hashlib.sha256(payload).hexdigest()
    root = _evidence_directory(evidence_root, "wave-integrations")
    destination = root / f"{digest}.json"
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except FileExistsError:
        if destination.read_bytes() != payload:
            raise WorkspaceRefused("EVIDENCE_CONFLICT")
    else:
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return destination, digest
