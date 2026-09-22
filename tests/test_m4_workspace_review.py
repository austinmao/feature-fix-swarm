"""Additional independent 06-02 workspace snapshot acceptance contracts.

These cases extend the fixed 13-node tracer without changing that adapter
selection. Prospective imports stay inside test functions, and every Git or
authority mutation is confined to ``tmp_path``.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import threading

import pytest

from test_m4_workspace_acceptance import (
    _copy,
    _cli,
    _env,
    _git,
    _git_text,
    _owner,
    _repository,
    _selection,
)
from test_m4_upstream_context_acceptance import _runtime_flags


def _registered_repository(primary: Path, state_root: Path) -> str:
    from run_context import register_repository, resolve_repository
    from run_state.ownership import ControlStore

    store = ControlStore(state_root / "control.sqlite3")
    return register_repository(store, resolve_repository(primary), state_root)


OTHER_REPOSITORY_ID = "cd0e912b-4187-410d-a740-c82f10f387f2"


def _captured_snapshot(tmp_path: Path, primary: Path, repository_id: str, data: bytes):
    from run_state.workspace import parse_input_selection, snapshot_inputs

    (primary / "src" / "selected.sh").write_bytes(data)
    manifest = _selection(
        primary, repository_id, entries=[_copy("src/selected.sh", data)],
    )
    snapshot = snapshot_inputs(
        primary, parse_input_selection(manifest), tmp_path / f"stage-{data.hex()[:12]}",
    )
    return manifest, snapshot


def test_descriptor_capture_refuses_source_mutation_after_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import run_state.workspace as workspace_module
    from run_state.workspace import WorkspaceRefused, parse_input_selection, snapshot_inputs

    primary = _repository(tmp_path)
    selected_path = primary / "src" / "selected.sh"
    selected_path.write_bytes(b"capture-before\n")
    repository_id = _registered_repository(primary, tmp_path / "capture-authority")
    selection = parse_input_selection(_selection(
        primary, repository_id, entries=[_copy("src/selected.sh", b"capture-before\n")],
    ))
    original_read = workspace_module.os.read
    descriptor_open = threading.Event()
    allow_read = threading.Event()
    observed = {}

    def held_read(fd: int, count: int) -> bytes:
        if not descriptor_open.is_set():
            descriptor_open.set()
            assert allow_read.wait(5)
        return original_read(fd, count)

    monkeypatch.setattr(workspace_module.os, "read", held_read)

    def capture() -> None:
        try:
            snapshot_inputs(primary, selection, tmp_path / "capture-stage")
        except BaseException as error:  # retained for assertion in the test thread
            observed["error"] = error

    thread = threading.Thread(target=capture, daemon=False)
    thread.start()
    try:
        assert descriptor_open.wait(5), "capture did not reach descriptor-backed read"
        selected_path.write_bytes(b"capture-after\n")
    finally:
        allow_read.set()
        thread.join(5)
    assert not thread.is_alive()
    assert isinstance(observed.get("error"), WorkspaceRefused)
    assert observed["error"].code == "SOURCE_CHANGED"
    assert selected_path.read_bytes() == b"capture-after\n"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("repository_id", OTHER_REPOSITORY_ID, "SELECTION_REPOSITORY_MISMATCH"),
        ("base_oid", "f" * 40, "SELECTION_BASE_MISMATCH"),
    ],
)
def test_snapshot_refuses_repository_or_base_mismatch_before_staging(
    tmp_path: Path, field: str, value: str, code: str,
) -> None:
    from run_state.workspace import WorkspaceRefused, parse_input_selection, snapshot_inputs

    primary = _repository(tmp_path)
    repository_id = _registered_repository(primary, tmp_path / "mismatch-authority")
    manifest = _selection(primary, repository_id)
    manifest[field] = value
    staging = tmp_path / "mismatch-stage"
    with pytest.raises(WorkspaceRefused) as refused:
        snapshot_inputs(primary, parse_input_selection(manifest), staging)
    assert refused.value.code == code
    assert not staging.exists() or list(staging.iterdir()) == []


@pytest.mark.parametrize(
    ("condition", "code"),
    [
        ("tracked-dirty", "INPUT_SELECTION_REQUIRED"),
        ("tracked-deleted", "INPUT_SELECTION_REQUIRED"),
        ("missing", "SELECTION_INPUT_MISSING"),
    ],
)
def test_required_context_state_is_checked_directly_even_without_status_entry(
    tmp_path: Path, condition: str, code: str,
) -> None:
    from run_state.workspace import WorkspaceRefused, parse_input_selection, snapshot_inputs

    primary = _repository(tmp_path)
    repository_id = _registered_repository(primary, tmp_path / "required-authority")
    relative = "src/unrelated.txt" if condition != "missing" else "context/absent.txt"
    target = primary / relative
    if condition == "tracked-dirty":
        target.write_bytes(b"required-dirty\n")
    elif condition == "tracked-deleted":
        target.unlink()
    manifest = _selection(primary, repository_id, required_context=[{
        "path": relative, "reason": f"required {condition} context",
    }])
    with pytest.raises(WorkspaceRefused) as refused:
        snapshot_inputs(
            primary, parse_input_selection(manifest), tmp_path / f"required-{condition}",
        )
    assert refused.value.code == code
    assert refused.value.candidates == [relative]


def test_preparation_exact_replay_is_idempotent_but_manifest_drift_refuses(
    tmp_path: Path,
) -> None:
    from run_state.workspace import (
        WorkspaceRefused, begin_workspace_preparation, inspect_workspace,
    )

    primary = _repository(tmp_path)
    store, owner, workspace, repository_id = _owner(tmp_path, primary, "manifest-replay")
    manifest_a, snapshot_a = _captured_snapshot(
        tmp_path, primary, repository_id, b"selection-a\n",
    )
    preparation = begin_workspace_preparation(
        store, owner.token, run_id=owner.run_id, workspace=workspace,
        branch=f"ffs/runs/{owner.run_id}", base_commit=manifest_a["base_oid"],
        selected_input_manifest=snapshot_a.manifest, repository_path=primary,
    )
    replay = begin_workspace_preparation(
        store, owner.token, run_id=owner.run_id, workspace=workspace,
        branch=f"ffs/runs/{owner.run_id}", base_commit=manifest_a["base_oid"],
        selected_input_manifest=snapshot_a.manifest, repository_path=primary,
    )
    assert replay.id == preparation.id

    _manifest_b, snapshot_b = _captured_snapshot(
        tmp_path, primary, repository_id, b"selection-b\n",
    )
    with pytest.raises(WorkspaceRefused) as refused:
        begin_workspace_preparation(
            store, owner.token, run_id=owner.run_id, workspace=workspace,
            branch=f"ffs/runs/{owner.run_id}", base_commit=manifest_a["base_oid"],
            selected_input_manifest=snapshot_b.manifest, repository_path=primary,
        )
    assert refused.value.code == "INPUT_SELECTION_CHANGED"
    assert inspect_workspace(store, preparation.id).selected_manifest_hash == (
        snapshot_a.selection_manifest_hash
    )


def test_ready_revalidate_and_finalize_preserve_authorized_run_edits(tmp_path: Path) -> None:
    from run_state.workspace import (
        begin_workspace_preparation, finalize_ready_unlock, prepare_workspace,
        revalidate_ready_fence,
    )

    primary = _repository(tmp_path)
    store, owner, workspace, repository_id = _owner(tmp_path, primary, "ready-review")
    manifest, snapshot = _captured_snapshot(
        tmp_path, primary, repository_id, b"initial-selected\n",
    )
    preparation = begin_workspace_preparation(
        store, owner.token, run_id=owner.run_id, workspace=workspace,
        branch=f"ffs/runs/{owner.run_id}", base_commit=manifest["base_oid"],
        selected_input_manifest=snapshot.manifest, repository_path=primary,
    )
    ready = prepare_workspace(store, owner.token, preparation, input_snapshot=snapshot)
    own_edit = b"authorized-run-edit\n"
    (workspace / "src" / "selected.sh").write_bytes(own_edit)
    _git("worktree", "lock", "--reason", f"ffs-preparation:{preparation.id}",
         str(workspace), cwd=primary)

    revalidated = revalidate_ready_fence(store, owner.token, preparation.id)
    finalize_ready_unlock(store, owner.token, revalidated.id)

    assert revalidated.ready is True
    assert (workspace / "src" / "selected.sh").read_bytes() == own_edit
    record = _git("worktree", "list", "--porcelain", cwd=primary).stdout
    selected = next(
        block for block in record.strip().split("\n\n")
        if block.startswith(f"worktree {workspace}\n")
    )
    assert "locked" not in selected


def test_two_simultaneous_selected_preparations_keep_distinct_snapshots(
    tmp_path: Path,
) -> None:
    from run_state.workspace import begin_workspace_preparation, prepare_workspace

    primary = _repository(tmp_path)
    store_a, owner_a, workspace_a, repository_id = _owner(tmp_path, primary, "parallel-a")
    manifest_a, snapshot_a = _captured_snapshot(
        tmp_path, primary, repository_id, b"parallel-a\n",
    )
    store_b, owner_b, workspace_b, repository_id_b = _owner(
        tmp_path, primary, "parallel-b",
    )
    assert repository_id_b == repository_id
    manifest_b, snapshot_b = _captured_snapshot(
        tmp_path, primary, repository_id, b"parallel-b\n",
    )
    primary_before = _git("status", "--porcelain=v1", cwd=primary).stdout
    barrier = threading.Barrier(2)

    def prepare(store, owner, workspace, manifest, snapshot):
        preparation = begin_workspace_preparation(
            store, owner.token, run_id=owner.run_id, workspace=workspace,
            branch=f"ffs/runs/{owner.run_id}", base_commit=manifest["base_oid"],
            selected_input_manifest=snapshot.manifest, repository_path=primary,
        )
        barrier.wait(timeout=5)
        return prepare_workspace(
            store, owner.token, preparation, input_snapshot=snapshot,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(
            prepare, store_a, owner_a, workspace_a, manifest_a, snapshot_a,
        )
        future_b = pool.submit(
            prepare, store_b, owner_b, workspace_b, manifest_b, snapshot_b,
        )
        ready_a, ready_b = future_a.result(timeout=30), future_b.result(timeout=30)
    assert ready_a.ready is ready_b.ready is True
    assert (workspace_a / "src" / "selected.sh").read_bytes() == b"parallel-a\n"
    assert (workspace_b / "src" / "selected.sh").read_bytes() == b"parallel-b\n"
    assert _git("status", "--porcelain=v1", cwd=primary).stdout == primary_before


def test_resume_uses_persisted_upstream_and_explicit_change_is_drift(tmp_path: Path) -> None:
    primary = _repository(tmp_path)
    planning = primary / ".planning"
    planning.mkdir()
    (planning / "fixture-context.txt").write_text("tracked resolver context\n")
    _git("add", ".planning/fixture-context.txt", cwd=primary)
    _git("commit", "-qm", "fixture planning context", cwd=primary)
    state_root = tmp_path / "authority"
    env = _env(tmp_path)
    env.update(
        GSD_PROJECT="fixture-project",
        GSD_WORKSTREAM="fixture-workstream",
        GSD_SESSION_KEY="fixture-session",
    )
    repository_id = _registered_repository(primary, state_root)
    selected = _selection(primary, repository_id)
    selection_path = tmp_path / "selection.json"
    selection_path.write_text(json.dumps(selected, sort_keys=True))
    started = _cli(
        state_root, primary, "start", "--skill", "fix", "--objective",
        "persist upstream", "--activity", "plan", "--run-id", "persist-upstream",
        "--selection-manifest", str(selection_path), *_runtime_flags(), "--json", env=env,
    )
    assert started.returncode == 0, started.stderr

    changed_env = dict(env)
    changed_env.update(
        GSD_PROJECT="different-project",
        GSD_WORKSTREAM="different-workstream",
        GSD_SESSION_KEY="different-session",
    )
    inspected = _cli(
        state_root, primary, "context", "--run-id", "persist-upstream", "--json",
        env=changed_env,
    )
    assert inspected.returncode == 0, inspected.stderr
    upstream = json.loads(inspected.stdout)["upstream"]
    assert upstream == json.loads(started.stdout)["upstream"]
    assert {key: upstream[key] for key in ("project", "workstream", "session_key")} == {
        "project": "fixture-project",
        "workstream": "fixture-workstream",
        "session_key": "fixture-session",
    }

    changed = dict(selected)
    changed["upstream"] = {
        "project": "different-project",
        "workstream": "different-workstream",
        "session_key": "different-session",
    }
    changed_path = tmp_path / "changed-selection.json"
    changed_path.write_text(json.dumps(changed, sort_keys=True))
    refused = _cli(
        state_root, primary, "start", "--skill", "fix", "--objective",
        "persist upstream", "--activity", "plan", "--run-id", "persist-upstream",
        "--resume", "--selection-manifest", str(changed_path), *_runtime_flags(), "--json", env=changed_env,
    )
    assert refused.returncode == 3
    assert json.loads(refused.stdout)["code"] == "UPSTREAM_CHANGED"


def test_post_selection_stale_context_refuses_without_git_or_event_effects(
    tmp_path: Path,
) -> None:
    from run_state.ownership import ControlStore

    primary = _repository(tmp_path)
    planning = primary / ".planning"
    planning.mkdir()
    (planning / "fixture-context.txt").write_text("tracked resolver context\n")
    _git("add", ".planning/fixture-context.txt", cwd=primary)
    _git("commit", "-qm", "fixture planning context", cwd=primary)
    state_root = tmp_path / "authority"
    env = _env(tmp_path)
    repository_id = _registered_repository(primary, state_root)
    selected = _selection(primary, repository_id)
    selection_path = tmp_path / "selection.json"
    selection_path.write_text(json.dumps(selected, sort_keys=True))
    run_id = "stale-selection-context"
    objective = "stale selected context"
    started = _cli(
        state_root, primary, "start", "--skill", "fix", "--objective", objective,
        "--activity", "plan", "--run-id", run_id, "--request-key", "stale-select",
        "--selection-manifest", str(selection_path), *_runtime_flags(), "--json", env=env,
    )
    assert started.returncode == 0, started.stderr
    evidence = tmp_path / "completed-plan.json"
    evidence.write_text('{"fixture":"completed plan"}\n')
    completed = _cli(
        state_root, primary, "complete", run_id, "--json",
        "--result-locator", str(evidence),
        "--result-sha256", hashlib.sha256(evidence.read_bytes()).hexdigest(), env=env,
    )
    assert completed.returncode == 0, completed.stderr
    crash_program = textwrap.dedent(
        """
        import json, os, signal, sys
        from run_state.state import ControlStore
        original = ControlStore.transition_activity
        def crash_after_latest_selection(self, token, activity_id, **kwargs):
            selected = original(self, token, activity_id, **kwargs)
            if kwargs.get('new') == 'active':
                print(json.dumps({'boundary':'selection-active-before-context-pointer',
                                  'activity_id':activity_id,'pid':os.getpid()}), flush=True)
                os.kill(os.getpid(), signal.SIGKILL)
            return selected
        ControlStore.transition_activity = crash_after_latest_selection
        from run_state.cli import main
        raise SystemExit(main(sys.argv[1:]))
        """
    )
    crashed = subprocess.run(
        [sys.executable, "-c", crash_program, "start", "--skill", "fix",
         "--objective", objective, "--activity", "execute", "--run-id", run_id,
         "--request-key", "stale-select-execute", "--selection-manifest",
         str(selection_path), *_runtime_flags(), "--json", "--state-root", str(state_root)],
        cwd=primary, env=env, capture_output=True, text=True, timeout=30,
    )
    assert crashed.returncode == -signal.SIGKILL
    assert json.loads(crashed.stdout)["boundary"] == "selection-active-before-context-pointer"

    store = ControlStore(state_root / "control.sqlite3")
    events_before = list(store.enumerate_events(
        run_id=run_id, repository_id=repository_id,
    ))
    refs_before = _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=primary).stdout
    worktrees_before = _git("worktree", "list", "--porcelain", cwd=primary).stdout
    refused = _cli(
        state_root, primary, "start", "--skill", "fix", "--objective", objective,
        "--activity", "execute", "--run-id", run_id, "--resume",
        "--request-key", "stale-select-execute", "--selection-manifest",
        str(selection_path), *_runtime_flags(), "--json", env=env,
    )
    assert refused.returncode == 3
    assert json.loads(refused.stdout)["code"] == "FENCE_REVOKED"
    assert list(store.enumerate_events(
        run_id=run_id, repository_id=repository_id,
    )) == events_before
    assert _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=primary).stdout == refs_before
    assert _git("worktree", "list", "--porcelain", cwd=primary).stdout == worktrees_before
