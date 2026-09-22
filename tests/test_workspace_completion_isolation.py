"""Shared immutable captures retain one completion receipt per preparation."""
from __future__ import annotations

from pathlib import Path

from run_state.workspace import (
    begin_child_workspace_preparation, begin_workspace_preparation, parse_input_selection, prepare_workspace,
    revalidate_ready_fence, snapshot_inputs,
)
from test_m4_workspace_acceptance import _copy, _owner, _repository, _selection
from test_child_workspace_recovery import _invoke


def test_two_preparations_sharing_capture_preserve_first_completion(tmp_path):
    primary = _repository(tmp_path)
    first_store, first_owner, first_path, repository_id = _owner(tmp_path, primary, "receipt-one")
    content = b"shared selected input\n"
    (primary / "src/selected.sh").write_bytes(content)
    manifest = _selection(primary, repository_id, entries=[_copy("src/selected.sh", content)])
    snapshot = snapshot_inputs(primary, parse_input_selection(manifest), tmp_path / "capture")

    def prepare(store, owner, path):
        pending = begin_workspace_preparation(
            store, owner.token, run_id=owner.run_id, workspace=path,
            branch=f"ffs/runs/{owner.run_id}", base_commit=manifest["base_oid"],
            selected_input_manifest=snapshot.manifest, repository_path=primary,
        )
        ready = prepare_workspace(store, owner.token, pending, input_snapshot=snapshot)
        with store.read_transaction() as tx:
            locator = tx.execute(
                "SELECT completion_locator FROM context_input_snapshots WHERE preparation_id=?",
                (ready.id,),
            ).fetchone()[0]
        return ready, Path(locator)

    first, first_receipt = prepare(first_store, first_owner, first_path)
    first_bytes = first_receipt.read_bytes()
    second_store, second_owner, second_path, _ = _owner(tmp_path, primary, "receipt-two")
    second, second_receipt = prepare(second_store, second_owner, second_path)
    assert second.id != first.id
    assert first_receipt != second_receipt
    assert first_receipt.read_bytes() == first_bytes
    revalidate_ready_fence(first_store, first_owner.token, first.id)
    revalidate_ready_fence(second_store, second_owner.token, second.id)


def test_registered_children_share_selected_bytes_but_not_receipts(tmp_path, monkeypatch):
    def execute(primary, store, token, context):
        original = (primary / "src/input.txt").read_bytes()
        selected = b"selected child bytes\n"
        (primary / "src/input.txt").write_bytes(selected)
        manifest = _selection(primary, token.repository_id, entries=[_copy("src/input.txt", selected)])
        snapshot = snapshot_inputs(primary, parse_input_selection(manifest), tmp_path / "child-capture")
        # Preparation must use captured bytes even if the source later changes.
        (primary / "src/input.txt").write_bytes(original)
        preparations = []
        receipts = []
        for key in ("child-one", "child-two"):
            pending = begin_child_workspace_preparation(
                store, token, parent_activity_id=context.activity_id, request_key=key,
                role="worker", base_commit=manifest["base_oid"], repository_path=primary,
                selected_input_manifest=snapshot.manifest,
            )
            ready = prepare_workspace(store, token, pending, input_snapshot=snapshot)
            preparations.append(ready)
            assert (ready.path / "src/input.txt").read_bytes() == selected
            with store.read_transaction() as tx:
                locator = tx.execute(
                    "SELECT completion_locator FROM context_input_snapshots WHERE preparation_id=?", (ready.id,),
                ).fetchone()[0]
            receipts.append((Path(locator), Path(locator).read_bytes()))
        assert receipts[0][0] != receipts[1][0]
        for preparation, (path, recorded) in zip(preparations, receipts):
            assert path.read_bytes() == recorded
            revalidate_ready_fence(store, token, preparation.id)
        assert (primary / "src/input.txt").read_bytes() == original
        assert (Path(context.workspace) / "src/input.txt").read_bytes() == original
    _invoke(tmp_path, monkeypatch, execute)
