from pathlib import Path
import subprocess
import pytest

from run_state.workspace import child_workspace_identity
from run_state.workspace import WorkspaceRefused, begin_child_workspace_preparation
from run_state.cli import _cmd_fixture_start
from test_managed_preparation import _args
from test_m4_upstream_context_acceptance import _repository
from test_run_context_acceptance import INHERITED_CONTEXT_KEYS


def git(path, *args):
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


def test_child_identity_is_replay_stable_and_not_nested_under_parent_ref(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    one = child_workspace_identity(parent, "run-1", "wave:one")
    assert one == child_workspace_identity(parent, "run-1", "wave:one")
    two = child_workspace_identity(parent, "run-1", "wave:two")
    assert one[0] != two[0] and one[1] != two[1]
    assert one[1].startswith("ffs/children/run-1/")
    assert not one[1].startswith("ffs/runs/")


def test_child_preparation_registers_replays_and_preserves_parent_pointer(tmp_path, monkeypatch):
    primary = _repository(tmp_path)
    monkeypatch.chdir(primary)
    for key in INHERITED_CONTEXT_KEYS:
        monkeypatch.delenv(key, raising=False)
    observed = {}

    def callback(store, token, context):
        base = git(primary, "rev-parse", "HEAD")
        with store.read_transaction() as tx:
            before = tx.execute("SELECT preparation_id FROM context_runs WHERE repository_id=? AND run_id=?", (token.repository_id, token.run_id)).fetchone()[0]
        common = {"entries": []}
        first = begin_child_workspace_preparation(store, token, parent_activity_id=context.activity_id,
            request_key="worker-one", role="worker", base_commit=base, selected_input_manifest=common, repository_path=primary)
        replay = begin_child_workspace_preparation(store, token, parent_activity_id=context.activity_id,
            request_key="worker-one", role="worker", base_commit=base, selected_input_manifest=common, repository_path=primary)
        second = begin_child_workspace_preparation(store, token, parent_activity_id=context.activity_id,
            request_key="worker-two", role="reviewer", base_commit=base, selected_input_manifest=common, repository_path=primary)
        with pytest.raises(WorkspaceRefused):
            begin_child_workspace_preparation(store, token, parent_activity_id=context.activity_id,
                request_key="worker-one", role="reviewer", base_commit=base, selected_input_manifest=common, repository_path=primary)
        with store.read_transaction() as tx:
            after = tx.execute("SELECT preparation_id FROM context_runs WHERE repository_id=? AND run_id=?", (token.repository_id, token.run_id)).fetchone()[0]
        observed.update(before=before, after=after, first=first, replay=replay, second=second, parent=context.workspace, base=base)
        return 0

    assert _cmd_fixture_start(_args(tmp_path / "authority"), on_ready=callback) == 0
    assert observed["before"] == observed["after"]
    assert observed["first"].id == observed["replay"].id
    assert observed["first"].path != observed["second"].path
    assert observed["first"].path.is_relative_to(Path(observed["parent"]))
    assert observed["second"].base_commit == observed["base"]
