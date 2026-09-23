"""Independent descriptor-race and finalization authority regressions.

Native execution requires a registered controller epoch. Test resources stay in
tmp_path; the implementer does not own this acceptance file.
"""
from dataclasses import replace
import os
from pathlib import Path

import pytest

from test_m4_workspace_acceptance import _git
from test_m4_workspace_hardening import _snapshot_preparation


@pytest.mark.parametrize("change", ["mode", "link"])
def test_capture_rejects_material_change_during_descriptor_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    import run_state.workspace as workspace

    source = tmp_path / "selected.txt"
    source.write_bytes(b"selected material\n")
    source.chmod(0o600)
    original_read = os.read
    target = source.stat()
    changed = False

    def changing_read(fd: int, length: int) -> bytes:
        nonlocal changed
        result = original_read(fd, length)
        info = os.fstat(fd)
        if not changed and (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
            changed = True
            if change == "mode":
                source.chmod(0o700)
            else:
                os.link(source, tmp_path / "selected-alias.txt")
        return result

    monkeypatch.setattr(os, "read", changing_read)
    with pytest.raises(workspace.WorkspaceRefused) as refused:
        workspace._read_anchored_regular(tmp_path, "selected.txt")
    assert changed, "the descriptor-read race was not exercised"
    assert refused.value.code == "SOURCE_CHANGED"


@pytest.mark.parametrize("authority", ["revoked", "forged", "worker"])
def test_finalize_requires_current_supervisor_fence_before_git_unlock(
    tmp_path: Path, authority: str,
) -> None:
    from run_state.ownership import OwnershipRefused, release_owner
    from run_state.workspace import (
        apply_input_snapshot,
        finalize_ready_unlock,
        publish_workspace_ready,
    )

    primary, store, owner, path, preparation, snapshot = _snapshot_preparation(
        tmp_path, f"finalize-{authority}",
    )
    apply_input_snapshot(store, owner.token, preparation.id, snapshot)
    ready = publish_workspace_ready(store, owner.token, preparation.id)
    token = owner.token
    if authority == "revoked":
        with store.transaction() as tx:
            release_owner(tx, token)
    elif authority == "forged":
        token = replace(token, nonce="forged-finalization-nonce")
    else:
        token = replace(token, role="worker")
    before = _git("worktree", "list", "--porcelain", cwd=primary).stdout
    assert f"worktree {path}\n" in before
    assert f"locked ffs-preparation:{ready.id}" in before
    events_before = list(store.enumerate_events())

    with pytest.raises(OwnershipRefused) as refused:
        finalize_ready_unlock(store, token, ready.id)

    assert refused.value.code == "FENCE_REVOKED"
    assert _git("worktree", "list", "--porcelain", cwd=primary).stdout == before
    assert list(store.enumerate_events()) == events_before
