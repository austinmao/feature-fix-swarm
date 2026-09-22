"""Selected material preflight must leave disposable repository/state untouched."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from run_state.workspace import WorkspaceRefused, parse_input_selection, snapshot_inputs, validate_selected_inputs
from test_m4_upstream_context_acceptance import _manifest, _register, _repository


def _inventory(root: Path):
    return {str(p.relative_to(root)): (
        p.stat().st_mode, hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None,
    ) for p in root.rglob("*")}


@pytest.mark.parametrize("fault,code", [
    ("source", "SOURCE_CHANGED"),
    ("required", "SELECTION_INPUT_MISSING"),
    ("base", "SELECTION_BASE_MISMATCH"),
    ("identity", "SELECTION_REPOSITORY_MISMATCH"),
])
def test_preflight_rejects_material_without_writes(tmp_path, fault, code):
    primary = _repository(tmp_path)
    repository_id = _register(primary, tmp_path / "registered-authority")
    value = _manifest(primary, repository_id, upstream={"project": None, "workstream": None, "session_key": None}, selected=b"base input\n")
    if fault == "source":
        (primary / "src/input.txt").write_bytes(b"changed\n")
    elif fault == "required":
        value["required_context"] = [{"path": "missing/context.txt", "reason": "required"}]
    elif fault == "base":
        value["base_oid"] = "0" * 40
    else:
        value["repository_id"] = "f14f9463-83a2-4c49-8c79-60b0045e684d"
    selection = parse_input_selection(value)
    before = _inventory(tmp_path)
    with pytest.raises(WorkspaceRefused) as refused:
        validate_selected_inputs(primary, selection)
    assert refused.value.code == code
    assert _inventory(tmp_path) == before
    assert not (tmp_path / "new-authority").exists()


def test_preflight_does_not_register_an_unregistered_repository(tmp_path):
    primary = _repository(tmp_path)
    value = _manifest(primary, "f14f9463-83a2-4c49-8c79-60b0045e684d", upstream={"project": None, "workstream": None, "session_key": None})
    before = _inventory(tmp_path)
    with pytest.raises(WorkspaceRefused, match="REPOSITORY_NOT_REGISTERED"):
        validate_selected_inputs(primary, parse_input_selection(value))
    assert _inventory(tmp_path) == before


def test_snapshot_rechecks_source_after_successful_preflight(tmp_path):
    primary = _repository(tmp_path)
    repository_id = _register(primary, tmp_path / "registered-authority")
    selection = parse_input_selection(_manifest(primary, repository_id, upstream={"project": None, "workstream": None, "session_key": None}, selected=b"base input\n"))
    before = _inventory(tmp_path)
    assert validate_selected_inputs(primary, selection) == {"src/input.txt": b"base input\n"}
    assert _inventory(tmp_path) == before
    (primary / "src/input.txt").write_bytes(b"changed after preflight\n")
    staging = tmp_path / "capture"
    with pytest.raises(WorkspaceRefused, match="SOURCE_CHANGED"):
        snapshot_inputs(primary, selection, staging)
    assert not staging.exists()
