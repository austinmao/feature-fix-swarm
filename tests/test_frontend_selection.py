"""Actual source validation for explicit frontend selections; disposable fixtures only."""
from __future__ import annotations

import hashlib
import json
import os

import pytest

from run_context import register_repository, resolve_repository
from run_state import frontend_selection
from run_state.frontend_selection import build_frontend_selection, resume_frontend_selection
from run_state.state import ControlStore
from run_state.workspace import WorkspaceRefused
from test_m4_workspace_acceptance import _git, _repository

UPSTREAM = {"project": None, "workstream": None, "session_key": "frontend-test"}


@pytest.fixture
def registered(tmp_path):
    primary = _repository(tmp_path)
    store = ControlStore(tmp_path / "authority" / "control.sqlite3")
    identity = register_repository(store, resolve_repository(primary), tmp_path / "authority")
    return primary, store, identity


def _build(primary, **changes):
    args = dict(selected_files=(), deleted_files=(), required_context=(), upstream=UPSTREAM)
    args.update(changes)
    return build_frontend_selection(primary, **args)


def _authority(store, primary):
    return (
        store.db_path.read_bytes(),
        _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=primary).stdout,
        _git("worktree", "list", "--porcelain", cwd=primary).stdout,
        sorted(str(p.relative_to(primary.parent)) for p in primary.parent.rglob("*")
               if ".git" not in p.parts and p.name != "control.sqlite3"),
    )


def test_explicit_copy_delete_mode_and_required_context_without_authority_effects(registered):
    primary, store, identity = registered
    selected = primary / "src/selected.sh"
    selected.write_bytes(b"explicit executable\n")
    selected.chmod(0o755)
    (primary / "src/delete.txt").unlink()
    (primary / "src/unrelated.txt").write_bytes(b"unselected dirty input\n")
    before = _authority(store, primary)
    selection = _build(primary, selected_files=("src/selected.sh",),
                       deleted_files=("src/delete.txt",), required_context=("src/selected.sh",))
    assert selection.repository_id == identity
    assert selection.base_oid == _git("rev-parse", "HEAD", cwd=primary).stdout.strip()
    assert {(e.operation, e.path, e.sha256, e.git_mode) for e in selection.entries} == {
        ("copy", "src/selected.sh", hashlib.sha256(selected.read_bytes()).hexdigest(), "100755"),
        ("delete", "src/delete.txt", hashlib.sha256(b"base-delete\n").hexdigest(), "100644"),
    }
    assert [r.path for r in selection.required_context] == ["src/selected.sh"]
    assert _authority(store, primary) == before
    assert _build(primary).entries == ()  # Dirty files are never inferred.


@pytest.mark.parametrize("path", [None, 7, "", "../outside", "/tmp/outside", "src//selected.sh",
                                  "src/./selected.sh", "src\\selected.sh", ".git/config",
                                  ".planning/run-state/input", "src/e\u0301.txt"])
def test_invalid_vocabulary_refuses_before_selected_file_io(registered, monkeypatch, path):
    primary, store, _identity = registered
    before = _authority(store, primary)
    def unexpected(*args, **kwargs):
        pytest.fail("invalid vocabulary reached selected-source I/O")
    monkeypatch.setattr(frontend_selection, "_read_anchored_regular_metadata", unexpected)
    monkeypatch.setattr(frontend_selection, "_base_entry_material", unexpected)
    with pytest.raises(WorkspaceRefused):
        _build(primary, selected_files=(path,))
    assert _authority(store, primary) == before


@pytest.mark.parametrize("changes", [
    {"selected_files": ("src/selected.sh", "SRC/selected.sh")},
    {"selected_files": ("src/selected.sh",), "deleted_files": ("src/selected.sh",)},
    {"required_context": ("src/../outside",)},
    {"upstream": {"project": "../escape", "workstream": None, "session_key": None}},
    {"selected_files": None},
])
def test_invalid_collection_scope_or_alias_refuses_before_source_io(registered, monkeypatch, changes):
    primary, store, _identity = registered
    before = _authority(store, primary)
    monkeypatch.setattr(frontend_selection, "_read_anchored_regular_metadata",
                        lambda *a, **k: pytest.fail("invalid draft reached file read"))
    with pytest.raises(WorkspaceRefused):
        _build(primary, **changes)
    assert _authority(store, primary) == before


@pytest.mark.parametrize("kind", ["symlink", "parent-symlink", "hardlink", "fifo", "missing"])
def test_anchored_selected_source_refuses_unsafe_files_without_effects(registered, tmp_path, kind):
    primary, store, _identity = registered
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "selected.sh"
    sentinel.write_bytes(b"outside sentinel\n")
    path = primary / "src/selected.sh"
    if kind == "parent-symlink":
        (primary / "src").rename(primary / "held-src")
        (primary / "src").symlink_to(outside, target_is_directory=True)
    else:
        path.unlink()
        if kind == "symlink":
            path.symlink_to(sentinel)
        elif kind == "hardlink":
            os.link(sentinel, path)
        elif kind == "fifo":
            os.mkfifo(path)
    before = _authority(store, primary)
    with pytest.raises(WorkspaceRefused) as refused:
        _build(primary, selected_files=("src/selected.sh",))
    assert refused.value.code == ("SELECTION_INPUT_MISSING" if kind == "missing"
                                  else "UNSAFE_SELECTION_PATH")
    assert sentinel.read_bytes() == b"outside sentinel\n"
    assert _authority(store, primary) == before


@pytest.mark.parametrize("kind", ["untracked", "modified", "missing"])
def test_required_context_cannot_be_silently_omitted(registered, kind):
    primary, store, _identity = registered
    path = "src/unrelated.txt" if kind == "modified" else "src/context.txt"
    if kind != "missing":
        (primary / path).write_bytes(b"required dirty context\n")
    before = _authority(store, primary)
    with pytest.raises(WorkspaceRefused) as refused:
        _build(primary, required_context=(path,))
    assert refused.value.code == ("SELECTION_INPUT_MISSING" if kind == "missing"
                                  else "INPUT_SELECTION_REQUIRED")
    assert _authority(store, primary) == before


def test_clean_tracked_required_context_is_valid_without_overlay(registered):
    primary, store, _identity = registered
    before = _authority(store, primary)
    selection = _build(primary, required_context=("src/unrelated.txt",))
    assert selection.entries == ()
    assert [r.path for r in selection.required_context] == ["src/unrelated.txt"]
    assert _authority(store, primary) == before


def test_source_change_between_construction_and_validation_refuses(registered, monkeypatch):
    primary, store, _identity = registered
    before = _authority(store, primary)
    original = frontend_selection._read_anchored_regular_metadata
    def mutate_after_read(root, relative, **kwargs):
        captured = original(root, relative, **kwargs)
        (root / relative).write_bytes(b"changed after initial capture\n")
        return captured
    monkeypatch.setattr(frontend_selection, "_read_anchored_regular_metadata", mutate_after_read)
    with pytest.raises(WorkspaceRefused) as refused:
        _build(primary, selected_files=("src/selected.sh",))
    assert refused.value.code == "SOURCE_CHANGED"
    assert _authority(store, primary) == before


def test_unregistered_repository_is_not_bootstrapped(tmp_path):
    from run_context import ContextRefused
    primary = _repository(tmp_path)
    with pytest.raises(ContextRefused) as refused:
        _build(primary)
    assert refused.value.code == "REPOSITORY_NOT_REGISTERED"
    assert not (primary / ".git/ffs/repository.json").exists()
    assert not (tmp_path / "authority").exists()


def test_missing_repository_path_is_a_typed_refusal(tmp_path):
    with pytest.raises(WorkspaceRefused) as refused:
        _build(tmp_path / "missing-repository")
    assert refused.value.code == "SELECTION_INPUT_MISSING"
    assert not (tmp_path / "missing-repository").exists()


def test_source_permission_error_is_a_typed_refusal(registered, monkeypatch):
    primary, store, _identity = registered
    before = _authority(store, primary)
    def denied(*args, **kwargs):
        raise PermissionError("fixture selected source denied")
    monkeypatch.setattr(frontend_selection, "_read_anchored_regular_metadata", denied)
    with pytest.raises(WorkspaceRefused) as refused:
        _build(primary, selected_files=("src/selected.sh",))
    assert refused.value.code == "UNSAFE_SELECTION_PATH"
    assert _authority(store, primary) == before


def test_nested_directory_uses_repository_relative_anchor(registered):
    primary, store, _identity = registered
    (primary / "src/src").mkdir()
    (primary / "src/src/selected.sh").write_bytes(b"shifted path must never be selected\n")
    before = _authority(store, primary)
    original = _build(primary, selected_files=("src/selected.sh",))
    nested = _build(primary / "src", selected_files=("src/selected.sh",))
    assert nested == original
    assert nested.manifest_sha256 == original.manifest_sha256
    assert _authority(store, primary) == before


@pytest.fixture
def retained_frontend(registered, tmp_path):
    from test_m4_upstream_context_acceptance import _registered_runtime, _runtime_flags
    from test_m4_workspace_acceptance import _cli, _env
    from test_m4_workspace_hardening import _track_planning_context
    from run_state.upstream import UpstreamRuntime

    primary, store, _identity = registered
    _track_planning_context(primary)
    (primary / "src/selected.sh").write_bytes(b"retained explicit input\n")
    selection = _build(primary, selected_files=("src/selected.sh",),
                       deleted_files=("src/delete.txt",), required_context=("src/selected.sh",))
    manifest = tmp_path / "frontend-selection.json"
    # Serialize the public canonical selection projection, excluding capture locators.
    value = {
        "schema": "ffs.input-selection/v1", "base_oid": selection.base_oid,
        "repository_id": selection.repository_id,
        "entries": [{"operation": e.operation, "path": e.path, "sha256": e.sha256,
                     "git_mode": e.git_mode} for e in selection.entries],
        "required_context": [{"path": r.path, "reason": r.reason} for r in selection.required_context],
        "upstream": UPSTREAM,
    }
    manifest.write_text(json.dumps(value))
    result = _cli(primary.parent / "authority", primary, "start", "--skill", "fix",
                  "--objective", "retain frontend material", "--activity", "plan",
                  "--run-id", "frontend-retained", "--request-key", "frontend-retained-request",
                  "--selection-manifest", str(manifest), *_runtime_flags(), "--json", env=_env(tmp_path))
    assert result.returncode == 0, (result.stdout, result.stderr)
    runtime_path, runtime_sha = _registered_runtime()
    runtime = UpstreamRuntime.from_manifest(json.loads(runtime_path.read_bytes()))
    runtime.verify()
    args = dict(state_root=primary.parent / "authority", run_id="frontend-retained",
                selected_files=("src/selected.sh",), deleted_files=("src/delete.txt",),
                required_context=("src/selected.sh",), upstream=UPSTREAM,
                runtime=runtime, runtime_manifest_sha256=runtime_sha,
                request_key="frontend-retained-request")
    return primary, store, selection, args


@pytest.mark.parametrize("change", ["removed", "modified", "head-ahead"])
def test_resume_selection_uses_retained_material_without_current_source_or_head(retained_frontend, change):
    primary, store, original, args = retained_frontend
    if change == "removed":
        (primary / "src/selected.sh").unlink()
    elif change == "modified":
        (primary / "src/selected.sh").write_bytes(b"today's unrelated replacement\n")
    else:
        (primary / "src/unrelated.txt").write_bytes(b"new fixture HEAD\n")
        _git("add", "src/unrelated.txt", cwd=primary)
        _git("commit", "-qm", "fixture advanced HEAD", cwd=primary)
        assert _git("rev-parse", "HEAD", cwd=primary).stdout.strip() != original.base_oid
    before = _authority(store, primary)
    resumed = resume_frontend_selection(primary / "src", **args)
    assert resumed == original
    assert resumed.manifest_sha256 == original.manifest_sha256
    assert resumed.input_digest == original.input_digest
    assert _authority(store, primary) == before


@pytest.mark.parametrize("changes", [
    {"selected_files": ("src/unrelated.txt",)},
    {"selected_files": (), "deleted_files": ("src/delete.txt", "src/selected.sh")},
    {"required_context": ()},
    {"upstream": {"project": "changed-project", "workstream": None, "session_key": "frontend-test"}},
])
@pytest.mark.parametrize("key_kind", ["existing", "fresh"])
def test_resume_refuses_changed_explicit_paths_operations_context_or_scope(retained_frontend, changes, key_kind):
    from run_state.upstream import UpstreamRefused
    primary, store, _original, args = retained_frontend
    if key_kind == "fresh":
        args = args | {"request_key": "fresh-changed-resume-request"}
    before = _authority(store, primary)
    exception = WorkspaceRefused if key_kind == "existing" else UpstreamRefused
    with pytest.raises(exception) as refused:
        resume_frontend_selection(primary, **(args | changes))
    assert refused.value.code == ("IDEMPOTENCY_CONFLICT" if key_kind == "existing" else "UPSTREAM_CHANGED")
    assert _authority(store, primary) == before


@pytest.mark.parametrize("kind", ["bytes", "symlink", "hardlink"])
def test_resume_refuses_corrupt_or_unsafe_retained_capture(retained_frontend, tmp_path, kind):
    primary, store, _original, args = retained_frontend
    with store.read_transaction() as tx:
        row = tx.execute("SELECT snapshot_json FROM context_run_material WHERE run_id=?",
                         (args["run_id"],)).fetchone()
    from pathlib import Path
    manifest = json.loads(row["snapshot_json"])
    capture = Path(manifest["capture"]["locator"]) / "files/src/selected.sh"
    if kind == "bytes":
        capture.write_bytes(b"corrupt retained capture\n")
    else:
        outside = tmp_path / "outside-capture"
        outside.write_bytes(capture.read_bytes())
        capture.unlink()
        if kind == "symlink":
            capture.symlink_to(outside)
        else:
            os.link(outside, capture)
    before = _authority(store, primary)
    with pytest.raises(WorkspaceRefused):
        resume_frontend_selection(primary, **args)
    assert _authority(store, primary) == before


def test_resume_accepts_new_request_key_without_state_writes(retained_frontend):
    primary, store, original, args = retained_frontend
    before = _authority(store, primary)
    resumed = resume_frontend_selection(primary, **(args | {"request_key": "new-resume-request"}))
    assert resumed == original
    assert _authority(store, primary) == before


def test_resume_refuses_existing_request_key_bound_to_different_run(retained_frontend):
    primary, store, _original, args = retained_frontend
    from test_m4_upstream_context_acceptance import _runtime_flags
    from test_m4_workspace_acceptance import _cli, _env
    # Allocate the other key through a real independent registered run.
    other = _cli(
        args["state_root"], primary, "start", "--skill", "fix", "--objective", "other frontend run",
        "--activity", "plan", "--run-id", "other-run", "--request-key", "other-run-request",
        "--selection-manifest", str(primary.parent / "frontend-selection.json"),
        *_runtime_flags(), "--json", env=_env(primary.parent),
    )
    assert other.returncode == 0, (other.stdout, other.stderr)
    before = _authority(store, primary)
    with pytest.raises(WorkspaceRefused) as refused:
        resume_frontend_selection(primary, **(args | {"request_key": "other-run-request"}))
    assert refused.value.code == "UPSTREAM_BINDING_INCOMPLETE"
    assert _authority(store, primary) == before


def test_resume_missing_authority_does_not_initialize_store(registered):
    primary, store, _identity = registered
    missing = primary.parent / "missing-authority"
    before = _authority(store, primary)
    with pytest.raises(WorkspaceRefused):
        resume_frontend_selection(primary, state_root=missing, run_id="unknown", selected_files=(),
                                  deleted_files=(), required_context=(), upstream=UPSTREAM,
                                  runtime=None, runtime_manifest_sha256="0" * 64)
    assert not missing.exists()
    assert _authority(store, primary) == before


@pytest.mark.parametrize("key_kind", ["existing", "fresh"])
def test_resume_changed_valid_descriptor_pin_obeys_request_key_precedence(retained_frontend, tmp_path, key_kind):
    from test_m4_upstream_context_acceptance import _registered_runtime
    from run_state.upstream import UpstreamRefused, UpstreamRuntime
    primary, store, _original, args = retained_frontend
    path, original_sha = _registered_runtime()
    changed = tmp_path / "valid-runtime-different-serialization.json"
    changed.write_bytes(path.read_bytes() + b" \n")
    digest = hashlib.sha256(changed.read_bytes()).hexdigest()
    assert digest != original_sha
    runtime = UpstreamRuntime.from_manifest(json.loads(changed.read_bytes()))
    runtime.verify()  # A real closed runtime, rather than fabricated digest evidence.
    args = args | {"runtime": runtime, "runtime_manifest_sha256": digest}
    if key_kind == "fresh":
        args = args | {"request_key": "fresh-runtime-change-request"}
    before = _authority(store, primary)
    exception = WorkspaceRefused if key_kind == "existing" else UpstreamRefused
    with pytest.raises(exception) as refused:
        resume_frontend_selection(primary, **args)
    assert refused.value.code == ("IDEMPOTENCY_CONFLICT" if key_kind == "existing"
                                  else "UPSTREAM_RUNTIME_CHANGED")
    assert _authority(store, primary) == before
