"""Managed finalization harvest/application acceptance."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from test_m4_workspace_acceptance import _owner, _repository, _selection


def _prepared_target(tmp_path: Path):
    from run_state import workspace

    primary = _repository(tmp_path)
    store, owner, path, repository_id = _owner(tmp_path, primary, "finalization-apply")
    selected = workspace.parse_input_selection(_selection(primary, repository_id))
    snapshot = workspace.snapshot_inputs(primary, selected, tmp_path / "capture")
    preparation = workspace.begin_workspace_preparation(
        store, owner.token, run_id=owner.run_id, workspace=path,
        branch=f"ffs/runs/{owner.run_id}", base_commit=selected.base_oid,
        selected_input_manifest=snapshot.manifest, repository_path=primary,
    )
    ready = workspace.prepare_workspace(
        store, owner.token, preparation, input_snapshot=snapshot,
    )
    return primary, store, owner, ready


def _make_terminal(store, owner, preparation) -> None:
    from run_state.managed import MANAGED_WRITER_VERSION
    from run_state.ownership import release_owner

    result = json.dumps({"status": "succeeded", "evidence": "retained"}, sort_keys=True)
    with store.transaction() as tx:
        run = tx.execute(
            "SELECT activity_id FROM context_runs WHERE repository_id=? AND run_id=?",
            (owner.token.repository_id, owner.run_id),
        ).fetchone()
        tx.execute(
            "UPDATE context_runs SET state='complete',writer_version=?,result_json=? "
            "WHERE repository_id=? AND run_id=?",
            (MANAGED_WRITER_VERSION, result, owner.token.repository_id, owner.run_id),
        )
        tx.execute(
            "UPDATE authority_activities SET state='succeeded',result_json=? WHERE id=?",
            (result, run["activity_id"]),
        )
        tx.execute(
            "UPDATE context_activities SET state='succeeded',result_json=? WHERE activity_id=?",
            (result, run["activity_id"]),
        )
        release_owner(tx, owner.token)


def _apply(store, owner, preparation):
    from run_state.workspace import finalization_apply, finalization_preview

    preview = finalization_preview(
        store, owner.token.repository_id, owner.run_id, preparation.id,
    )
    return finalization_apply(
        store, owner.token.repository_id, owner.run_id, preparation.id,
        expected_generation=preview["target"]["generation"],
        expected_manifest_sha256=preview["ownership_manifest"]["sha256"],
    )


def test_apply_harvests_workspace_before_removing_only_manifest_resources(tmp_path: Path) -> None:
    from run_state.workspace import inspect_workspace

    primary, store, owner, preparation = _prepared_target(tmp_path)
    tracked = preparation.path / "src" / "selected.sh"
    tracked.write_text("terminal managed change\n")
    evidence = preparation.path / ".planning" / "wall" / "result.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text('{"passed":true}\n')
    (preparation.path / "receipt.json").write_text("root workspace receipt\n")
    nested_receipt = preparation.path / "nested" / "receipt.json"
    nested_receipt.parent.mkdir()
    nested_receipt.write_text("nested workspace receipt\n")
    sibling = tmp_path / "active-sibling"
    sibling.mkdir()
    (sibling / "keep").write_text("unchanged\n")
    primary_before = (primary / "src" / "selected.sh").read_bytes()
    manifest = Path(preparation.owned_resource_manifest)
    manifest_before = manifest.read_bytes()
    writer_before = None
    _make_terminal(store, owner, preparation)
    with store.read_transaction() as tx:
        writer_before = tx.execute(
            "SELECT writer_version FROM context_runs WHERE repository_id=? AND run_id=?",
            (owner.token.repository_id, owner.run_id),
        ).fetchone()["writer_version"]

    result = _apply(store, owner, preparation)

    assert result["evidence_harvest_complete"] is True
    assert result["landing_performed"] is False
    assert not preparation.path.exists()
    assert (sibling / "keep").read_text() == "unchanged\n"
    assert (primary / "src" / "selected.sh").read_bytes() == primary_before
    assert manifest.read_bytes() == manifest_before
    receipt_path = Path(result["harvest_receipt"])
    receipt_bytes = receipt_path.read_bytes()
    assert hashlib.sha256(receipt_bytes).hexdigest() == result["harvest_receipt_sha256"]
    receipt = json.loads(receipt_bytes)
    payload = receipt_path.parent / "payload"
    archived = payload / ".planning" / "wall" / "result.json"
    assert archived.read_text() == '{"passed":true}\n'
    assert (payload / "receipt.json").read_text() == "root workspace receipt\n"
    assert (payload / "nested" / "receipt.json").read_text() == "nested workspace receipt\n"
    assert any(row["path"] == "src/selected.sh" for row in receipt["entries"])
    assert inspect_workspace(store, preparation.id).state == "finalized"
    with store.read_transaction() as tx:
        run = tx.execute(
            "SELECT writer_version,state FROM context_runs WHERE repository_id=? AND run_id=?",
            (owner.token.repository_id, owner.run_id),
        ).fetchone()
        event_names = [row[0] for row in tx.execute(
            "SELECT event_type FROM control_events WHERE event_type LIKE 'FINALIZATION_%' ORDER BY id",
        ).fetchall()]
    assert dict(run) == {"writer_version": writer_before, "state": "complete"}
    assert event_names[-2:] == ["FINALIZATION_HARVESTED", "FINALIZATION_APPLIED"]


def test_production_cli_applies_exact_preview_fence(tmp_path: Path) -> None:
    from test_m4_workspace_acceptance import _cli, _env
    from run_state.workspace import finalization_preview

    primary, store, owner, preparation = _prepared_target(tmp_path)
    (preparation.path / "cli-result.txt").write_text("retained through CLI\n")
    _make_terminal(store, owner, preparation)
    preview = finalization_preview(
        store, owner.token.repository_id, owner.run_id, preparation.id,
    )
    invoked = _cli(
        store.db_path.parent, primary, "finalize-apply",
        "--repository-id", owner.token.repository_id,
        "--run-id", owner.run_id,
        "--preparation-id", preparation.id,
        "--expected-generation", str(preview["target"]["generation"]),
        "--expected-manifest-sha256", preview["ownership_manifest"]["sha256"],
        env=_env(tmp_path),
    )
    assert invoked.returncode == 0, (invoked.stdout, invoked.stderr)
    result = json.loads(invoked.stdout)
    assert result["evidence_harvest_complete"] is True
    assert result["landing_performed"] is False
    assert (Path(result["harvest_receipt"]).parent / "payload" / "cli-result.txt").read_text() == "retained through CLI\n"
    assert not preparation.path.exists()


def test_apply_refuses_live_owner_before_harvest_or_removal(tmp_path: Path) -> None:
    from run_state.managed import MANAGED_WRITER_VERSION
    from run_state.workspace import WorkspaceRefused, finalization_apply, finalization_preview

    _, store, owner, preparation = _prepared_target(tmp_path)
    with store.transaction() as tx:
        run = tx.execute(
            "SELECT activity_id FROM context_runs WHERE repository_id=? AND run_id=?",
            (owner.token.repository_id, owner.run_id),
        ).fetchone()
        tx.execute(
            "UPDATE context_runs SET state='complete',writer_version=?,result_json='{}' "
            "WHERE repository_id=? AND run_id=?",
            (MANAGED_WRITER_VERSION, owner.token.repository_id, owner.run_id),
        )
        tx.execute(
            "UPDATE authority_activities SET state='succeeded',result_json='{}' WHERE id=?",
            (run["activity_id"],),
        )
    preview = finalization_preview(store, owner.token.repository_id, owner.run_id, preparation.id)
    with pytest.raises(WorkspaceRefused, match="FINALIZATION_OWNER_ACTIVE"):
        finalization_apply(
            store, owner.token.repository_id, owner.run_id, preparation.id,
            expected_generation=preview["target"]["generation"],
            expected_manifest_sha256=preview["ownership_manifest"]["sha256"],
        )
    assert preparation.path.is_dir()
    assert not list((Path(store.db_path).parent / "runs").glob("**/finalization/*"))


@pytest.mark.parametrize("hazard", ["symlink", "hardlink", "branch-drift"])
def test_apply_refuses_unstable_or_drifted_owned_resources(tmp_path: Path, hazard: str) -> None:
    from run_state.workspace import WorkspaceRefused

    primary, store, owner, preparation = _prepared_target(tmp_path)
    _make_terminal(store, owner, preparation)
    if hazard == "symlink":
        outside = tmp_path / "outside"
        outside.write_text("outside\n")
        (preparation.path / "unsafe-link").symlink_to(outside)
    elif hazard == "hardlink":
        outside = tmp_path / "outside"
        outside.write_text("outside\n")
        os.link(outside, preparation.path / "unsafe-hardlink")
    else:
        from test_m4_workspace_acceptance import _git
        (preparation.path / "drift.txt").write_text("drift\n")
        _git("add", "drift.txt", cwd=preparation.path)
        _git("commit", "-qm", "fixture drift", cwd=preparation.path)
    with pytest.raises(WorkspaceRefused) as refused:
        _apply(store, owner, preparation)
    assert refused.value.code in {
        "FINALIZATION_HARVEST_UNSAFE", "FINALIZATION_RESOURCE_DRIFT",
    }
    assert preparation.path.is_dir()
    if hazard != "branch-drift":
        assert (tmp_path / "outside").read_text() == "outside\n"


def test_apply_requires_exact_preview_generation_and_manifest_digest(tmp_path: Path) -> None:
    from run_state.workspace import WorkspaceRefused, finalization_apply, finalization_preview

    _, store, owner, preparation = _prepared_target(tmp_path)
    _make_terminal(store, owner, preparation)
    preview = finalization_preview(store, owner.token.repository_id, owner.run_id, preparation.id)
    for generation, digest in [
        (preview["target"]["generation"] + 1, preview["ownership_manifest"]["sha256"]),
        (preview["target"]["generation"], "0" * 64),
    ]:
        with pytest.raises(WorkspaceRefused, match="FINALIZATION_PREVIEW_CHANGED"):
            finalization_apply(
                store, owner.token.repository_id, owner.run_id, preparation.id,
                expected_generation=generation, expected_manifest_sha256=digest,
            )
    assert preparation.path.is_dir()


def test_apply_resumes_from_retained_harvest_after_worktree_removal_crash(
    tmp_path: Path, monkeypatch,
) -> None:
    from run_state import workspace

    _, store, owner, preparation = _prepared_target(tmp_path)
    (preparation.path / "result.txt").write_text("must survive\n")
    (preparation.path / "receipt.json").write_text("root receipt survives resume\n")
    nested_receipt = preparation.path / "nested" / "receipt.json"
    nested_receipt.parent.mkdir()
    nested_receipt.write_text("nested receipt survives resume\n")
    _make_terminal(store, owner, preparation)
    original_git = workspace._git

    def interrupt_before_branch_delete(cwd, *args, **kwargs):
        if args[:3] == ("branch", "-D", "--"):
            raise workspace.WorkspaceRefused("INJECTED_FINALIZATION_CRASH")
        return original_git(cwd, *args, **kwargs)

    monkeypatch.setattr(workspace, "_git", interrupt_before_branch_delete)
    with pytest.raises(workspace.WorkspaceRefused, match="INJECTED_FINALIZATION_CRASH"):
        _apply(store, owner, preparation)
    assert not preparation.path.exists()
    with store.read_transaction() as tx:
        row = tx.execute(
            "SELECT state FROM context_workspaces WHERE preparation_id=?", (preparation.id,),
        ).fetchone()
    assert row["state"] == "ready"

    monkeypatch.setattr(workspace, "_git", original_git)
    result = _apply(store, owner, preparation)
    assert result["evidence_harvest_complete"] is True
    payload = Path(result["harvest_receipt"]).parent / "payload"
    assert (payload / "result.txt").read_text() == "must survive\n"
    assert (payload / "receipt.json").read_text() == "root receipt survives resume\n"
    assert (payload / "nested" / "receipt.json").read_text() == "nested receipt survives resume\n"
    with store.read_transaction() as tx:
        row = tx.execute(
            "SELECT state FROM context_workspaces WHERE preparation_id=?", (preparation.id,),
        ).fetchone()
    assert row["state"] == "finalized"
