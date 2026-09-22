"""Final independent 06-02 selected-input and repository-lineage contracts.

These tests extend the frozen workspace tranches. Every Git, FIFO, and state
mutation is confined to ``tmp_path``; imports of prospective APIs remain inside
test bodies so collection stays useful before implementation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import threading
import uuid

import pytest

from test_m4_workspace_acceptance import (
    _cli,
    _copy,
    _env,
    _git,
    _git_text,
    _repository,
    _selection,
    _sha,
)


def _marker_path(primary: Path) -> Path:
    return primary / ".git" / "ffs" / "repository.json"


def test_registered_repository_identity_is_uuid4_and_stable_across_commits(
    tmp_path: Path,
) -> None:
    from run_context import (
        register_repository,
        registered_repository_identity,
        resolve_repository,
    )
    from run_state.ownership import ControlStore

    primary = _repository(tmp_path)
    state_root = tmp_path / "authority"
    store = ControlStore(state_root / "control.sqlite3")
    descriptor = resolve_repository(primary)
    repository_id = register_repository(store, descriptor, state_root)

    parsed = uuid.UUID(repository_id)
    assert parsed.version == 4
    assert registered_repository_identity(descriptor) == repository_id
    marker_before = _marker_path(primary).read_bytes()
    (primary / "src" / "later.txt").write_text("later\n", encoding="utf-8")
    _git("add", "src/later.txt", cwd=primary)
    _git("commit", "-qm", "later commit", cwd=primary)
    assert registered_repository_identity(resolve_repository(primary)) == repository_id
    assert _marker_path(primary).read_bytes() == marker_before


def test_registered_repository_identity_refuses_unregistered_without_writes(
    tmp_path: Path,
) -> None:
    from run_context import ContextRefused, registered_repository_identity, resolve_repository

    primary = _repository(tmp_path)
    before = sorted(path.relative_to(primary / ".git").as_posix()
                    for path in (primary / ".git").rglob("*"))
    with pytest.raises(ContextRefused) as refused:
        registered_repository_identity(resolve_repository(primary))
    assert refused.value.code == "REPOSITORY_NOT_REGISTERED"
    assert not _marker_path(primary).exists()
    assert sorted(path.relative_to(primary / ".git").as_posix()
                  for path in (primary / ".git").rglob("*")) == before


def test_repository_admin_replacement_cannot_reuse_identity_when_inode_is_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from run_context import register_repository, resolve_repository
    from run_state.ownership import ControlStore

    primary = _repository(tmp_path)
    state_root = tmp_path / "authority"
    store = ControlStore(state_root / "control.sqlite3")
    original = resolve_repository(primary)
    original_info = original.common_dir.stat()
    original_id = register_repository(store, original, state_root)

    shutil.rmtree(primary / ".git")
    _git("init", "-q", cwd=primary)
    _git("config", "user.email", "replacement@example.test", cwd=primary)
    _git("config", "user.name", "Replacement", cwd=primary)
    (primary / "replacement.txt").write_text("replacement\n", encoding="utf-8")
    _git("add", "replacement.txt", cwd=primary)
    _git("commit", "-qm", "replacement", cwd=primary)
    replacement = resolve_repository(primary)

    real_stat = Path.stat

    def forced_inode(path: Path, *args, **kwargs):
        if Path(path) == replacement.common_dir:
            return original_info
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", forced_inode)
    replacement_id = register_repository(store, replacement, state_root)
    assert uuid.UUID(replacement_id).version == 4
    assert replacement_id != original_id


def test_snapshot_refuses_provisional_repository_identity_before_staging(
    tmp_path: Path,
) -> None:
    from run_context import repository_identity, resolve_repository
    from run_state.workspace import WorkspaceRefused, parse_input_selection, snapshot_inputs

    primary = _repository(tmp_path)
    selected = b"unregistered-selection\n"
    selected_path = primary / "src" / "selected.sh"
    selected_path.write_bytes(selected)
    provisional = repository_identity(resolve_repository(primary))
    selection = parse_input_selection(_selection(
        primary, provisional, entries=[_copy("src/selected.sh", selected)],
    ))
    staging = tmp_path / "capture"

    with pytest.raises(WorkspaceRefused) as refused:
        snapshot_inputs(primary, selection, staging)
    assert refused.value.code == "REPOSITORY_NOT_REGISTERED"
    assert not staging.exists()
    assert not _marker_path(primary).exists()


def test_selected_fifo_is_refused_without_blocking_before_capture(tmp_path: Path) -> None:
    from run_state.workspace import WorkspaceRefused, parse_input_selection, snapshot_inputs
    from test_m4_workspace_acceptance import _owner

    primary = _repository(tmp_path)
    _store, _owner_record, _workspace, repository_id = _owner(
        tmp_path, primary, "fifo-selection",
    )
    fifo = primary / "src" / "selected.fifo"
    os.mkfifo(fifo, 0o600)
    selection = parse_input_selection(_selection(
        primary, repository_id,
        entries=[_copy("src/selected.fifo", b"")],
    ))
    staging = tmp_path / "fifo-capture"
    observed: dict[str, BaseException] = {}
    finished = threading.Event()

    def capture() -> None:
        try:
            snapshot_inputs(primary, selection, staging)
        except BaseException as error:
            observed["error"] = error
        finally:
            finished.set()

    worker = threading.Thread(target=capture, daemon=False)
    worker.start()
    completed_without_writer = finished.wait(1.0)
    if not completed_without_writer:
        # Release a defective blocking reader without leaving a fixture thread.
        writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(writer)
    worker.join(5)
    assert not worker.is_alive(), "fixture-owned FIFO reader did not terminate"
    assert completed_without_writer, "selected FIFO blocked during descriptor acquisition"
    assert isinstance(observed.get("error"), WorkspaceRefused)
    assert observed["error"].code == "UNSAFE_SELECTION_PATH"
    assert not staging.exists()


@pytest.mark.parametrize("with_manifest", [False, True], ids=["raw-only", "raw-plus-manifest"])
def test_legacy_raw_selected_input_refuses_before_repository_or_state_mutation(
    tmp_path: Path, with_manifest: bool,
) -> None:
    primary = _repository(tmp_path)
    state_root = tmp_path / "authority"
    env = _env(tmp_path)
    args = [
        "start", "--skill", "fix", "--objective", "legacy raw selection",
        "--activity", "plan", "--run-id", "legacy-raw-selection", "--json",
        "--selected-input", "src/selected.sh",
    ]
    if with_manifest:
        manifest = tmp_path / "selection.json"
        manifest.write_text(json.dumps(_selection(
            primary,
            "f14f9463-83a2-4c49-8c79-60b0045e684d",
            entries=[_copy("src/selected.sh", b"base-selected\n")],
        )), encoding="utf-8")
        args.extend(("--selection-manifest", str(manifest)))
    git_before = sorted(path.relative_to(primary / ".git").as_posix()
                        for path in (primary / ".git").rglob("*"))

    result = _cli(state_root, primary, *args, env=env)

    assert result.returncode == 5, (result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == "SELECTED_INPUT_UNSUPPORTED"
    assert payload["recovery_action"]["action"] == "remove_selected_inputs"
    assert not state_root.exists()
    assert not _marker_path(primary).exists()
    assert sorted(path.relative_to(primary / ".git").as_posix()
                  for path in (primary / ".git").rglob("*")) == git_before
