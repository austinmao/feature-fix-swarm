"""Independent read-only production finalization preview acceptance."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

import pytest

from test_m4_workspace_acceptance import _cli, _env, _owner, _repository, _selection


def _inventory(root):
    result = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            material = hashlib.sha256(path.read_bytes()).hexdigest()
        elif stat.S_ISLNK(info.st_mode):
            material = os.readlink(path)
        else:
            material = None
        result[str(path.relative_to(root))] = (info.st_mode, info.st_ino, info.st_nlink, material)
    return result


def _target(tmp_path, *, failed=True):
    from run_state import workspace
    from run_state.state import ControlStore

    primary = _repository(tmp_path)
    store, owner, path, repository_id = _owner(tmp_path, primary, "preview-acceptance")
    selection = _selection(primary, repository_id)
    snapshot = None
    if not failed:
        snapshot = workspace.snapshot_inputs(
            primary, workspace.parse_input_selection(selection), tmp_path / "capture",
        )
        selection = snapshot.manifest
    preparation = workspace.begin_workspace_preparation(
        store, owner.token, run_id=owner.run_id, workspace=path,
        branch=f"ffs/runs/{owner.run_id}", base_commit=(snapshot.selection.base_oid if snapshot else selection["base_oid"]),
        selected_input_manifest=selection, repository_path=primary,
    )
    if failed:
        refusal = workspace._record_failure(store, owner.token, preparation)
        manifest = Path(refusal.owned_resource_manifest)
    else:
        workspace.prepare_workspace(store, owner.token, preparation, input_snapshot=snapshot)
        manifest = None
    observer = ControlStore.open_read_only(store.db_path)
    # Every adversarial test starts from a supported, positively observed target.
    from run_state.supervisor import Supervisor
    baseline = Supervisor.observe_finalization(observer, repository_id, owner.run_id, preparation.id)
    assert baseline["apply_allowed"] is False
    if failed:
        assert "ownership_manifest" in baseline
    else:
        assert baseline["ownership_manifest"]["created"] is True
    return primary, store, observer, repository_id, owner.run_id, preparation.id, manifest


def _preview(target):
    from run_state.supervisor import Supervisor
    _, _, observer, repository_id, run_id, preparation_id, _ = target
    return Supervisor.observe_finalization(observer, repository_id, run_id, preparation_id)


def _refused(target):
    from run_state.workspace import WorkspaceRefused
    with pytest.raises(WorkspaceRefused):
        _preview(target)


def test_real_failure_preview_and_production_cli_leave_all_resources_unchanged(tmp_path):
    target = _target(tmp_path)
    primary, store, _, repository_id, run_id, preparation_id, manifest = target
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    (sibling / "keep").write_bytes(b"unrelated sibling")
    before = _inventory(tmp_path)
    result = _preview(target)
    assert result["apply_allowed"] is False
    assert result["evidence_harvest_complete"] is False
    assert result["removal_resources"] == []
    assert result["ownership_manifest"]["sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert result["ownership_manifest"]["created"] is False
    assert "removal_identity_unproven" in result["unmet"]
    cli = _cli(store.db_path.parent, primary, "finalize-preview", "--repository-id",
               repository_id, "--run-id", run_id, "--preparation-id", preparation_id,
               env=_env(tmp_path))
    assert cli.returncode == 0, (cli.stdout, cli.stderr)
    payload = json.loads(cli.stdout)
    assert payload["apply_allowed"] is False
    assert _inventory(tmp_path) == before


def test_success_ready_reports_owned_but_remains_ineligible_while_nonterminal(tmp_path):
    target = _target(tmp_path, failed=False)
    before = _inventory(tmp_path)
    result = _preview(target)
    assert result["states"]["preparation"] == "ready"
    assert result["states"]["run"] not in ("complete", "failed", "aborted")
    assert result["apply_allowed"] is False
    assert result["removal_resources"] == []
    assert result["ownership_manifest"]["created"] is True
    assert "removal_identity_unproven" in result["unmet"]
    assert _inventory(tmp_path) == before


@pytest.mark.parametrize("field,value", [
    ("entries", [{"path": "/arbitrary", "owned": True}]),
    ("schema_version", True),
    ("created", "false"),
    ("created", True),
    ("run_id", "sibling-run"),
    ("preparation_id", "sibling-preparation"),
    ("path", "/another/workspace"),
    ("branch", "ffs/runs/another"),
    ("base_commit", "0" * 40),
    ("path_existed_before", True),
    ("branch_existed_before", True),
    ("registered_before", True),
])
def test_manifest_closed_shape_and_every_ownership_fact_are_bound(tmp_path, field, value):
    target = _target(tmp_path)
    manifest = target[-1]
    content = json.loads(manifest.read_bytes())
    assert content.get(field) != value or type(content.get(field)) is not type(value)
    content[field] = value
    manifest.write_text(json.dumps(content))
    before = _inventory(tmp_path)
    _refused(target)
    assert _inventory(tmp_path) == before


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "oversize", "invalid_utf8", "array", "object_array", "null", "missing"])
def test_manifest_unsafe_leaf_and_bounded_decoding_refuse_without_writes(tmp_path, kind):
    target = _target(tmp_path)
    manifest = target[-1]
    original = manifest.read_bytes()
    if kind in ("symlink", "hardlink"):
        other = tmp_path / "outside-manifest"
        other.write_bytes(original)
        manifest.unlink()
        if kind == "symlink":
            manifest.symlink_to(other)
        else:
            os.link(other, manifest)
    elif kind == "oversize":
        manifest.write_bytes(b" " * (1024 * 1024 + 1))
    elif kind == "invalid_utf8":
        manifest.write_bytes(b"\xff")
    elif kind == "array":
        manifest.write_bytes(b"[]")
    elif kind == "object_array":
        manifest.write_bytes(b"[{}]")
    elif kind == "null":
        manifest.write_bytes(b"null")
    else:
        manifest.unlink()
    before = _inventory(tmp_path)
    _refused(target)
    assert _inventory(tmp_path) == before


def test_leaf_replacement_after_anchored_read_is_refused(tmp_path, monkeypatch):
    from run_state import workspace
    target = _target(tmp_path)
    manifest = target[-1]
    original = workspace._read_anchored_regular_metadata
    replaced = False

    def swap_after_read(*args, **kwargs):
        nonlocal replaced
        result = original(*args, **kwargs)
        if not replaced:
            replacement = manifest.with_name("replacement")
            replacement.write_bytes(manifest.read_bytes())
            replacement.replace(manifest)
            replaced = True
        return result

    monkeypatch.setattr(workspace, "_read_anchored_regular_metadata", swap_after_read)
    _refused(target)
    assert replaced


def test_cross_repository_and_sibling_preparation_cannot_resolve_target(tmp_path):
    from run_state.supervisor import Supervisor
    from run_state.workspace import WorkspaceRefused
    target = _target(tmp_path)
    before = _inventory(tmp_path)
    _, _, observer, repository_id, run_id, preparation_id, _ = target
    for rid, run, prep in [(str(uuid.uuid4()), run_id, preparation_id),
                           (repository_id, "sibling", preparation_id),
                           (repository_id, run_id, "sibling")]:
        with pytest.raises(WorkspaceRefused):
            Supervisor.observe_finalization(observer, rid, run, prep)
    assert _inventory(tmp_path) == before


def test_evidence_root_replacement_during_read_is_refused(tmp_path, monkeypatch):
    from run_state import workspace
    target = _target(tmp_path)
    manifest = target[-1]
    root = manifest.parent.parent
    original = workspace._read_anchored_regular_metadata
    replaced = False

    def swap_root_after_read(*args, **kwargs):
        nonlocal replaced
        result = original(*args, **kwargs)
        if not replaced:
            old_mode = stat.S_IMODE(root.stat().st_mode)
            old_root = root.with_name(root.name + "-old")
            root.rename(old_root)
            root.mkdir(mode=old_mode)
            # Preserve the recovery parent and leaf identities: only the root changes.
            (old_root / "recovery").rename(root / "recovery")
            replaced = True
        return result

    monkeypatch.setattr(workspace, "_read_anchored_regular_metadata", swap_root_after_read)
    _refused(target)
    assert replaced


@pytest.mark.parametrize("column,value", [
    ("generation", 99), ("path", "/different/workspace"),
    ("created_by_ffs", 1), ("state", "preparing"),
])
def test_durable_binding_changed_after_read_is_not_reported_as_old_snapshot(
    tmp_path, monkeypatch, column, value,
):
    from run_state import workspace
    target = _target(tmp_path)
    store = target[1]
    preparation_id = target[5]
    original = workspace._read_anchored_regular_metadata
    changed = False

    def change_after_read(*args, **kwargs):
        nonlocal changed
        result = original(*args, **kwargs)
        if not changed:
            with store.transaction() as tx:
                tx.execute(f"UPDATE context_workspaces SET {column}=? WHERE preparation_id=?",
                           (value, preparation_id))
            changed = True
        return result

    monkeypatch.setattr(workspace, "_read_anchored_regular_metadata", change_after_read)
    _refused(target)
    assert changed


def test_durable_terminal_state_is_observed_without_granting_removal(tmp_path):
    target = _target(tmp_path)
    store, repository_id, run_id = target[1], target[3], target[4]
    # Persist a historical terminal record; preview never performs this transition.
    with store.transaction() as tx:
        tx.execute("UPDATE context_runs SET state='aborted' WHERE repository_id=? AND run_id=?",
                   (repository_id, run_id))
    before = _inventory(tmp_path)
    result = _preview(target)
    assert result["states"]["run"] == "aborted"
    assert result["apply_allowed"] is False
    assert result["evidence_harvest_complete"] is False
    assert result["removal_resources"] == []
    assert _inventory(tmp_path) == before


def test_production_cli_refuses_cross_checkout_even_with_target_repository_assertion(tmp_path):
    target = _target(tmp_path)
    store, repository_id, run_id, preparation_id = target[1:2] + target[3:6]
    other_root = tmp_path / "other-checkout"
    other_root.mkdir()
    other = _repository(other_root)
    env = _env(tmp_path)
    before = _inventory(tmp_path)
    result = _cli(store.db_path.parent, other, "finalize-preview", "--repository-id",
                  repository_id, "--run-id", run_id, "--preparation-id", preparation_id,
                  env=env)
    assert result.returncode != 0
    assert _inventory(tmp_path) == before


@pytest.mark.parametrize("field,value", [("schema_version", "1"), ("created", "false")])
def test_duplicate_json_ownership_keys_are_refused_even_when_values_agree(tmp_path, field, value):
    target = _target(tmp_path)
    manifest = target[-1]
    original = manifest.read_bytes().rstrip()
    assert original.endswith(b"}")
    manifest.write_bytes(original[:-1] + b',' + json.dumps(field).encode() + b':' + value.encode() + b'}')
    before = _inventory(tmp_path)
    _refused(target)
    assert _inventory(tmp_path) == before


def test_core_observer_never_spawns_or_acquires_a_mutating_store_transaction(tmp_path, monkeypatch):
    import subprocess
    from run_state.state import ControlStore
    target = _target(tmp_path)
    before = _inventory(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("read-only preview attempted a process or write transaction")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(ControlStore, "transaction", forbidden)
    result = _preview(target)
    assert result["apply_allowed"] is False
    assert _inventory(tmp_path) == before
