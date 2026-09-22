"""Remaining independent 06-02 completion and ingress contracts.

This file stays separate from the frozen seven-node lineage contract. Every
filesystem, Git, and state mutation is confined to ``tmp_path``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading

import pytest

from test_m4_workspace_acceptance import (
    _cli,
    _copy,
    _delete,
    _env,
    _git,
    _owner,
    _repository,
    _selection,
)
from test_m4_workspace_hardening import (
    _retained_completion,
    _snapshot_preparation,
    _track_planning_context,
)
from test_m4_upstream_context_acceptance import _runtime_flags


def test_copy_selection_refuses_declared_executable_mode_mismatch_before_staging(
    tmp_path: Path,
) -> None:
    from run_state.workspace import WorkspaceRefused, parse_input_selection, snapshot_inputs

    primary = _repository(tmp_path)
    _store, _owned, _workspace, repository_id = _owner(tmp_path, primary, "mode-mismatch")
    selected = b"selected-mode\n"
    path = primary / "src" / "selected.sh"
    path.write_bytes(selected)
    path.chmod(0o644)
    manifest = _selection(
        primary, repository_id, entries=[_copy("src/selected.sh", selected, "100755")],
    )
    staging = tmp_path / "mode-capture"

    with pytest.raises(WorkspaceRefused) as refused:
        snapshot_inputs(primary, parse_input_selection(manifest), staging)
    assert refused.value.code == "SOURCE_CHANGED"
    assert not staging.exists()


@pytest.mark.parametrize("mismatch", ["hash", "mode"])
def test_delete_selection_binds_expected_base_material_before_staging(
    tmp_path: Path, mismatch: str,
) -> None:
    from run_state.workspace import WorkspaceRefused, parse_input_selection, snapshot_inputs

    primary = _repository(tmp_path)
    _store, _owned, _workspace, repository_id = _owner(tmp_path, primary, f"delete-{mismatch}")
    entry = _delete("src/delete.txt", b"base-delete\n")
    if mismatch == "hash":
        entry["sha256"] = "f" * 64
    else:
        entry["git_mode"] = "100755"
    staging = tmp_path / f"delete-{mismatch}-capture"

    with pytest.raises(WorkspaceRefused) as refused:
        snapshot_inputs(
            primary,
            parse_input_selection(_selection(primary, repository_id, entries=[entry])),
            staging,
        )
    assert refused.value.code == "SOURCE_CHANGED"
    assert not staging.exists()
    assert (primary / "src" / "delete.txt").read_bytes() == b"base-delete\n"


@pytest.mark.parametrize("change", ["modified", "deleted"])
def test_staged_required_context_is_not_treated_as_clean(
    tmp_path: Path, change: str,
) -> None:
    from run_state.workspace import WorkspaceRefused, parse_input_selection, snapshot_inputs

    primary = _repository(tmp_path)
    _store, _owned, _workspace, repository_id = _owner(tmp_path, primary, f"staged-{change}")
    required = primary / "src" / "unrelated.txt"
    if change == "modified":
        required.write_text("staged change\n", encoding="utf-8")
        _git("add", "src/unrelated.txt", cwd=primary)
    else:
        _git("rm", "-q", "src/unrelated.txt", cwd=primary)
    staging = tmp_path / f"staged-{change}-capture"
    manifest = _selection(
        primary,
        repository_id,
        required_context=[{"path": "src/unrelated.txt", "reason": "required fixture context"}],
    )

    with pytest.raises(WorkspaceRefused) as refused:
        snapshot_inputs(primary, parse_input_selection(manifest), staging)
    assert refused.value.code == "INPUT_SELECTION_REQUIRED"
    assert not staging.exists()


def test_nonempty_selection_projects_durable_count_digest_and_manifest_hash(
    tmp_path: Path,
) -> None:
    from run_context import register_repository, resolve_repository
    from run_state.ownership import ControlStore
    from run_state.selection import parse_input_selection

    primary = _repository(tmp_path)
    _track_planning_context(primary)
    state_root = tmp_path / "authority"
    store = ControlStore(state_root / "control.sqlite3")
    repository_id = register_repository(store, resolve_repository(primary), state_root)
    selected = b"projected-selected-input\n"
    (primary / "src" / "selected.sh").write_bytes(selected)
    manifest_value = _selection(
        primary, repository_id, entries=[_copy("src/selected.sh", selected)],
    )
    selected_contract = parse_input_selection(manifest_value)
    manifest_path = tmp_path / "selection.json"
    manifest_path.write_text(json.dumps(manifest_value), encoding="utf-8")

    result = _cli(
        state_root,
        primary,
        "start", "--skill", "fix", "--objective", "project selected context",
        "--activity", "plan", "--run-id", "project-selected-context", "--json",
        "--selection-manifest", str(manifest_path), *_runtime_flags(),
        env=_env(tmp_path),
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["selected_input_count"] == 1
    assert payload["selected_input_manifest_hash"] == selected_contract.manifest_sha256
    assert payload["selection_manifest_hash"] == selected_contract.manifest_sha256
    assert payload["input_digest"] == selected_contract.input_digest


@pytest.mark.parametrize("operation", ["revalidate", "finalize"])
def test_corrupt_completion_blocks_every_ready_rebind_or_unlock_path(
    tmp_path: Path, operation: str,
) -> None:
    from run_state.workspace import (
        WorkspaceRefused,
        apply_input_snapshot,
        finalize_ready_unlock,
        publish_workspace_ready,
        revalidate_ready_fence,
    )

    _primary, store, owner, _workspace, preparation, snapshot = _snapshot_preparation(
        tmp_path, f"ready-{operation}",
    )
    apply_input_snapshot(store, owner.token, preparation.id, snapshot)
    ready = publish_workspace_ready(store, owner.token, preparation.id)
    completion = _retained_completion(store, preparation, snapshot)
    completion.write_text('{"schema":"corrupt-ready-completion"}\n', encoding="utf-8")

    with pytest.raises(WorkspaceRefused) as refused:
        if operation == "revalidate":
            revalidate_ready_fence(store, owner.token, ready.id)
        else:
            finalize_ready_unlock(store, owner.token, ready.id)
    assert refused.value.code == "SNAPSHOT_INCOMPLETE"
    assert ready.path.is_dir()


@pytest.mark.parametrize(
    ("kind", "expected_code"),
    [
        ("symlink", "UNSAFE_SELECTION_PATH"),
        ("hardlink", "UNSAFE_SELECTION_PATH"),
        ("missing", "SELECTION_INPUT_MISSING"),
        ("oversize", "INVALID_SELECTION"),
    ],
)
def test_selection_manifest_ingress_refuses_unsafe_or_unbounded_file_before_state(
    tmp_path: Path, kind: str, expected_code: str,
) -> None:
    primary = _repository(tmp_path)
    state_root = tmp_path / "authority"
    safe = tmp_path / "manifest-source.json"
    safe.write_text("{}", encoding="utf-8")
    manifest = tmp_path / "selection.json"
    if kind == "symlink":
        manifest.symlink_to(safe)
    elif kind == "hardlink":
        os.link(safe, manifest)
    elif kind == "oversize":
        manifest.write_bytes(b" " * (1024 * 1024 + 1))
    # missing deliberately leaves the path absent.

    result = _cli(
        state_root,
        primary,
        "start", "--skill", "fix", "--objective", "manifest ingress",
        "--activity", "plan", "--run-id", f"manifest-{kind}", "--json",
        "--selection-manifest", str(manifest),
        env=_env(tmp_path),
    )

    assert result.returncode == 2, (result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["code"] == expected_code
    assert not state_root.exists()
    assert not (primary / ".git" / "ffs" / "repository.json").exists()
    assert safe.read_text(encoding="utf-8") == "{}"


def test_selection_manifest_fifo_is_refused_without_blocking_or_state_mutation(
    tmp_path: Path,
) -> None:
    primary = _repository(tmp_path)
    state_root = tmp_path / "authority"
    manifest = tmp_path / "selection.fifo"
    os.mkfifo(manifest, 0o600)
    observed = {}
    finished = threading.Event()

    def invoke() -> None:
        try:
            observed["result"] = _cli(
                state_root,
                primary,
                "start", "--skill", "fix", "--objective", "fifo manifest ingress",
                "--activity", "plan", "--run-id", "manifest-fifo", "--json",
                "--selection-manifest", str(manifest),
                env=_env(tmp_path),
            )
        finally:
            finished.set()

    worker = threading.Thread(target=invoke, daemon=False)
    worker.start()
    completed_without_writer = finished.wait(1.0)
    if not completed_without_writer:
        writer = os.open(manifest, os.O_WRONLY | os.O_NONBLOCK)
        os.write(writer, b"{}")
        os.close(writer)
    worker.join(5)
    assert not worker.is_alive(), "fixture CLI did not terminate after FIFO release"
    assert completed_without_writer, "manifest FIFO blocked before typed refusal"
    result = observed["result"]
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert json.loads(result.stdout)["code"] == "UNSAFE_SELECTION_PATH"
    assert not state_root.exists()
    assert not (primary / ".git" / "ffs" / "repository.json").exists()
