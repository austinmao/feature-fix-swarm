"""Supplemental descriptor, fence, and durable snapshot contracts for M4.

These cases were authored after the first 06-02 implementation pass.  They
extend, rather than replace, the frozen 13-node and 11-node workspace suites.
Every filesystem and Git mutation is confined to ``tmp_path``.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import threading

import pytest

from test_m4_workspace_acceptance import (
    _cli,
    _copy,
    _env,
    _git,
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


def _snapshot_preparation(tmp_path: Path, run_id: str):
    from run_state.workspace import (
        begin_workspace_preparation,
        parse_input_selection,
        snapshot_inputs,
    )

    primary = _repository(tmp_path)
    store, owner, workspace, repository_id = _owner(
        tmp_path, primary, run_id,
    )
    selected = f"selected-{run_id}\n".encode()
    (primary / "src" / "selected.sh").write_bytes(selected)
    manifest = _selection(
        primary,
        repository_id,
        entries=[_copy("src/selected.sh", selected)],
    )
    snapshot = snapshot_inputs(
        primary,
        parse_input_selection(manifest),
        tmp_path / f"snapshot-{run_id}",
    )
    preparation = begin_workspace_preparation(
        store,
        owner.token,
        run_id=owner.run_id,
        workspace=workspace,
        branch=f"ffs/runs/{owner.run_id}",
        base_commit=manifest["base_oid"],
        selected_input_manifest=snapshot.manifest,
        repository_path=primary,
    )
    _git(
        "worktree",
        "add",
        "--lock",
        "--reason",
        f"ffs-preparation:{preparation.id}",
        "-q",
        "-b",
        preparation.branch,
        str(workspace),
        preparation.base_commit,
        cwd=primary,
    )
    return primary, store, owner, workspace, preparation, snapshot


def _event_projection(store) -> list[tuple[int, str, str]]:
    return [
        (row["id"], row["event_type"], row["payload"])
        for row in store.enumerate_events()
    ]


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _track_planning_context(primary: Path) -> None:
    # The real resolver requires an anchored .planning root in the worktree.
    # Retain the selection's existing project/workstream/session vocabulary.
    planning = primary / ".planning"
    planning.mkdir()
    (planning / "fixture-context.txt").write_text("tracked resolver context\n")
    _git("add", ".planning/fixture-context.txt", cwd=primary)
    _git("commit", "-qm", "fixture planning context", cwd=primary)


def _assert_no_completion(store, preparation, snapshot) -> None:
    with store.read_transaction() as tx:
        row = tx.execute(
            "SELECT completion_locator, completion_hash, applied_json, completed_at "
            "FROM context_input_snapshots WHERE preparation_id=?", (preparation.id,),
        ).fetchone()
    assert row is not None
    assert tuple(row) == (None, None, None, None)
    assert not (snapshot.staging / "completions" / f"{preparation.id}.json").exists()


def _retained_completion(store, preparation, snapshot) -> Path:
    with store.read_transaction() as tx:
        row = tx.execute(
            "SELECT completion_locator, completion_hash, applied_json "
            "FROM context_input_snapshots WHERE preparation_id=?", (preparation.id,),
        ).fetchone()
    assert row is not None
    completion = Path(row["completion_locator"])
    assert completion == snapshot.staging / "completions" / f"{preparation.id}.json"
    preimage = completion.read_bytes()
    assert hashlib.sha256(preimage).hexdigest() == row["completion_hash"]
    assert preimage.decode() == row["applied_json"]
    assert json.loads(preimage) == {
        "schema": "ffs.input-snapshot-completion/v1",
        "repository_id": preparation.repository_id,
        "run_id": preparation.run_id,
        "preparation_id": preparation.id,
        "base_commit": preparation.base_commit,
        "selection_manifest_hash": snapshot.selection_manifest_hash,
        "input_digest": snapshot.input_digest,
        "entries": snapshot.manifest["entries"],
    }
    return completion


@pytest.mark.parametrize("replacement", ["symlink", "broken-symlink"])
def test_snapshot_capture_refuses_source_parent_symlink_or_broken_link(
    tmp_path: Path, replacement: str,
) -> None:
    from run_state.workspace import WorkspaceRefused, parse_input_selection, snapshot_inputs

    primary = _repository(tmp_path)
    selected = b"selected-source\n"
    (primary / "src" / "selected.sh").write_bytes(selected)
    selection = parse_input_selection(_selection(
        primary,
        _registered_repository(primary, tmp_path / "source-symlink-authority"),
        entries=[_copy("src/selected.sh", selected)],
    ))
    held = primary / "src-held"
    (primary / "src").rename(held)
    outside = tmp_path / "outside-source"
    outside.mkdir()
    sentinel = outside / "selected.sh"
    sentinel.write_bytes(selected)
    target = outside if replacement == "symlink" else tmp_path / "missing-source"
    (primary / "src").symlink_to(target, target_is_directory=True)

    with pytest.raises(WorkspaceRefused) as refused:
        snapshot_inputs(primary, selection, tmp_path / "capture")
    assert refused.value.code == "UNSAFE_SELECTION_PATH"
    assert sentinel.read_bytes() == selected
    assert not (tmp_path / "capture").exists()


def test_snapshot_capture_refuses_source_ancestor_swap_after_descriptor_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import run_state.workspace as workspace_module
    from run_state.workspace import WorkspaceRefused, parse_input_selection, snapshot_inputs

    primary = _repository(tmp_path)
    selected = b"selected-before-parent-swap\n"
    (primary / "src" / "selected.sh").write_bytes(selected)
    selection = parse_input_selection(_selection(
        primary,
        _registered_repository(primary, tmp_path / "source-swap-authority"),
        entries=[_copy("src/selected.sh", selected)],
    ))
    parent_open = threading.Event()
    continue_capture = threading.Event()
    original = workspace_module._open_directory_chain

    def held_open(root: Path, parts: tuple[str, ...], *, create: bool) -> int:
        descriptor = original(root, parts, create=create)
        if Path(root) == primary and parts == ("src",):
            parent_open.set()
            if not continue_capture.wait(5):
                raise AssertionError("source parent swap barrier timed out")
        return descriptor

    monkeypatch.setattr(workspace_module, "_open_directory_chain", held_open)
    observed: dict[str, object] = {}

    def capture() -> None:
        try:
            snapshot_inputs(primary, selection, tmp_path / "capture")
        except BaseException as error:
            observed["error"] = error

    thread = threading.Thread(target=capture, daemon=False)
    thread.start()
    outside = tmp_path / "outside-source"
    try:
        assert parent_open.wait(5), "capture did not open the selected parent"
        (primary / "src").rename(primary / "src-held")
        outside.mkdir()
        (outside / "selected.sh").write_bytes(selected)
        (primary / "src").symlink_to(outside, target_is_directory=True)
    finally:
        continue_capture.set()
        thread.join(5)
    assert not thread.is_alive()
    assert isinstance(observed.get("error"), WorkspaceRefused)
    assert observed["error"].code == "SOURCE_CHANGED"
    assert (outside / "selected.sh").read_bytes() == selected


@pytest.mark.parametrize("replacement", ["symlink", "broken-symlink"])
def test_snapshot_apply_refuses_destination_parent_symlink_or_broken_link(
    tmp_path: Path, replacement: str,
) -> None:
    from run_state.workspace import WorkspaceRefused, apply_input_snapshot

    (_primary, store, owner, workspace, preparation, snapshot) = (
        _snapshot_preparation(tmp_path, f"destination-{replacement}")
    )
    held = workspace / "src-held"
    (workspace / "src").rename(held)
    outside = tmp_path / "outside-destination"
    outside.mkdir()
    sentinel = outside / "selected.sh"
    sentinel.write_bytes(b"outside-sentinel\n")
    target = outside if replacement == "symlink" else tmp_path / "missing-destination"
    (workspace / "src").symlink_to(target, target_is_directory=True)

    with pytest.raises(WorkspaceRefused) as refused:
        apply_input_snapshot(store, owner.token, preparation.id, snapshot)
    assert refused.value.code == "UNSAFE_SELECTION_PATH"
    assert sentinel.read_bytes() == b"outside-sentinel\n"
    _assert_no_completion(store, preparation, snapshot)


def test_snapshot_apply_refuses_destination_ancestor_swap_after_descriptor_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import run_state.workspace as workspace_module
    from run_state.workspace import WorkspaceRefused, apply_input_snapshot

    (_primary, store, owner, workspace, preparation, snapshot) = (
        _snapshot_preparation(tmp_path, "destination-parent-swap")
    )
    parent_open = threading.Event()
    continue_apply = threading.Event()
    original = workspace_module._open_directory_chain

    def held_open(root: Path, parts: tuple[str, ...], *, create: bool) -> int:
        descriptor = original(root, parts, create=create)
        if Path(root) == workspace and parts == ("src",):
            parent_open.set()
            if not continue_apply.wait(5):
                raise AssertionError("destination parent swap barrier timed out")
        return descriptor

    monkeypatch.setattr(workspace_module, "_open_directory_chain", held_open)
    observed: dict[str, object] = {}

    def apply() -> None:
        try:
            apply_input_snapshot(store, owner.token, preparation.id, snapshot)
        except BaseException as error:
            observed["error"] = error

    thread = threading.Thread(target=apply, daemon=False)
    thread.start()
    outside = tmp_path / "outside-destination"
    try:
        assert parent_open.wait(5), "apply did not open the destination parent"
        (workspace / "src").rename(workspace / "src-held")
        outside.mkdir()
        sentinel = outside / "selected.sh"
        sentinel.write_bytes(b"outside-sentinel\n")
        (workspace / "src").symlink_to(outside, target_is_directory=True)
    finally:
        continue_apply.set()
        thread.join(5)
    assert not thread.is_alive()
    assert isinstance(observed.get("error"), WorkspaceRefused)
    assert observed["error"].code == "SOURCE_CHANGED"
    assert (outside / "selected.sh").read_bytes() == b"outside-sentinel\n"
    _assert_no_completion(store, preparation, snapshot)


@pytest.mark.parametrize("stale_field", ["nonce", "generation"])
def test_snapshot_apply_rejects_forged_or_stale_token_before_any_write(
    tmp_path: Path, stale_field: str,
) -> None:
    from run_state.ownership import OwnershipRefused
    from run_state.workspace import apply_input_snapshot

    (_primary, store, owner, workspace, preparation, snapshot) = (
        _snapshot_preparation(tmp_path, f"stale-{stale_field}")
    )
    token = (
        replace(owner.token, nonce="0" * 64)
        if stale_field == "nonce"
        else replace(owner.token, generation=owner.token.generation + 1)
    )
    destination_before = (workspace / "src" / "selected.sh").read_bytes()
    events_before = _event_projection(store)

    with pytest.raises(OwnershipRefused) as refused:
        apply_input_snapshot(store, token, preparation.id, snapshot)
    assert refused.value.code == "FENCE_REVOKED"
    assert (workspace / "src" / "selected.sh").read_bytes() == destination_before
    _assert_no_completion(store, preparation, snapshot)
    assert _event_projection(store) == events_before


def test_snapshot_apply_rejects_forged_manifest_projection_before_any_write(
    tmp_path: Path,
) -> None:
    from run_state.workspace import WorkspaceRefused, apply_input_snapshot

    (_primary, store, owner, workspace, preparation, snapshot) = (
        _snapshot_preparation(tmp_path, "forged-snapshot")
    )
    manifest = snapshot.manifest
    manifest["entries"][0]["sha256"] = "f" * 64
    forged = replace(
        snapshot,
        _manifest_json=json.dumps(manifest, sort_keys=True, separators=(",", ":")),
    )
    destination_before = (workspace / "src" / "selected.sh").read_bytes()
    events_before = _event_projection(store)

    with pytest.raises(WorkspaceRefused) as refused:
        apply_input_snapshot(store, owner.token, preparation.id, forged)
    assert refused.value.code == "INPUT_SELECTION_CHANGED"
    assert (workspace / "src" / "selected.sh").read_bytes() == destination_before
    _assert_no_completion(store, preparation, snapshot)
    assert _event_projection(store) == events_before


@pytest.mark.parametrize("entrypoint", ["publish", "recover", "adopt"])
def test_wrong_schema_saved_snapshot_cannot_advance_any_ready_path(
    tmp_path: Path, entrypoint: str,
) -> None:
    from run_state.workspace import (
        WorkspaceRefused,
        adopt_workspace_preparation_fence,
        begin_workspace_preparation,
        inspect_workspace,
        publish_workspace_ready,
        recover_workspace_preparation,
    )

    primary, store, owner, workspace, _valid, snapshot = _snapshot_preparation(
        tmp_path, f"wrong-schema-donor-{entrypoint}",
    )
    # Use a second owner because the donor helper already persisted one valid
    # preparation.  The malformed manifest reaches the same public boundary.
    store, owner, workspace, _repository_id = _owner(
        tmp_path / "malformed", primary, f"wrong-schema-{entrypoint}",
    )
    malformed = snapshot.manifest
    malformed["schema"] = "ffs.input-snapshot/v999"
    preparation = begin_workspace_preparation(
        store,
        owner.token,
        run_id=owner.run_id,
        workspace=workspace,
        branch=f"ffs/runs/{owner.run_id}",
        base_commit=malformed["base_oid"],
        selected_input_manifest=malformed,
        repository_path=primary,
    )
    _git(
        "worktree", "add", "--lock", "--reason",
        f"ffs-preparation:{preparation.id}", "-q", "-b", preparation.branch,
        str(workspace), preparation.base_commit, cwd=primary,
    )
    action = {
        "publish": lambda: publish_workspace_ready(store, owner.token, preparation.id),
        "recover": lambda: recover_workspace_preparation(store, owner.token, preparation.id),
        "adopt": lambda: adopt_workspace_preparation_fence(
            store, owner.token, preparation.id,
        ),
    }[entrypoint]
    events_before = _event_projection(store)

    with pytest.raises(WorkspaceRefused) as refused:
        action()
    assert refused.value.code == "SNAPSHOT_INCOMPLETE"
    assert inspect_workspace(store, preparation.id).ready is False
    assert _event_projection(store) == events_before


@pytest.mark.parametrize("entrypoint", ["publish", "recover", "adopt"])
def test_corrupt_completion_receipt_cannot_advance_any_ready_path(
    tmp_path: Path, entrypoint: str,
) -> None:
    from run_state.workspace import (
        WorkspaceRefused,
        adopt_workspace_preparation_fence,
        apply_input_snapshot,
        inspect_workspace,
        publish_workspace_ready,
        recover_workspace_preparation,
    )

    (_primary, store, owner, _workspace, preparation, snapshot) = (
        _snapshot_preparation(tmp_path, f"corrupt-completion-{entrypoint}")
    )
    apply_input_snapshot(store, owner.token, preparation.id, snapshot)
    _retained_completion(store, preparation, snapshot).write_bytes(b'{"corrupt":true}\n')
    action = {
        "publish": lambda: publish_workspace_ready(store, owner.token, preparation.id),
        "recover": lambda: recover_workspace_preparation(store, owner.token, preparation.id),
        "adopt": lambda: adopt_workspace_preparation_fence(
            store, owner.token, preparation.id,
        ),
    }[entrypoint]
    events_before = _event_projection(store)

    with pytest.raises(WorkspaceRefused) as refused:
        action()
    assert refused.value.code == "SNAPSHOT_INCOMPLETE"
    assert inspect_workspace(store, preparation.id).ready is False
    assert _event_projection(store) == events_before


@pytest.mark.parametrize("selected", [False, True])
def test_required_context_accepts_clean_tracked_or_exact_selected_input(
    tmp_path: Path, selected: bool,
) -> None:
    from run_state.workspace import parse_input_selection, snapshot_inputs

    primary = _repository(tmp_path)
    repository_id = _registered_repository(primary, tmp_path / "required-authority")
    relative = "src/unrelated.txt"
    entries = []
    expected = b"base-unrelated\n"
    if selected:
        expected = b"selected-required-context\n"
        (primary / relative).write_bytes(expected)
        entries = [_copy(relative, expected)]
    selection = parse_input_selection(_selection(
        primary,
        repository_id,
        entries=entries,
        required_context=[{
            "path": relative,
            "reason": "required workspace context",
        }],
    ))

    snapshot = snapshot_inputs(primary, selection, tmp_path / "required-capture")

    assert snapshot.manifest["required_context"] == [{
        "path": relative,
        "reason": "required workspace context",
    }]
    if selected:
        assert (snapshot.staging / "files" / relative).read_bytes() == expected
    else:
        assert snapshot.manifest["entries"] == []


@pytest.mark.parametrize("release_store", ["same-instance", "second-instance"])
def test_fenced_operation_orders_concurrent_release_after_external_effect(
    tmp_path: Path, release_store: str,
) -> None:
    from run_state.ownership import OwnershipRefused, assert_owner, release_owner
    from run_state.state import ControlStore

    primary = _repository(tmp_path)
    store, owner, _workspace, _repository_id = _owner(
        tmp_path, primary, f"guard-{release_store}",
    )
    releasing_store = (
        store if release_store == "same-instance" else ControlStore(store.db_path)
    )
    effect_entered = threading.Event()
    release_attempted = threading.Event()
    allow_effect_return = threading.Event()
    effect_returned = threading.Event()
    released = threading.Event()
    sequence: list[str] = []
    errors: list[BaseException] = []

    def held_effect() -> None:
        try:
            with store.fenced_operation(owner.token):
                sequence.append("effect-entered")
                effect_entered.set()
                if not allow_effect_return.wait(5):
                    raise AssertionError("fenced effect release barrier timed out")
            sequence.append("effect-returned")
            effect_returned.set()
        except BaseException as error:
            errors.append(error)

    def revoke() -> None:
        try:
            if not effect_entered.wait(5):
                raise AssertionError("fenced effect did not start")
            sequence.append("release-attempted")
            release_attempted.set()
            with releasing_store.transaction() as tx:
                release_owner(tx, owner.token)
            sequence.append("released")
            released.set()
        except BaseException as error:
            errors.append(error)

    effect_thread = threading.Thread(target=held_effect, daemon=False)
    release_thread = threading.Thread(target=revoke, daemon=False)
    effect_thread.start()
    release_thread.start()
    try:
        assert effect_entered.wait(5)
        assert release_attempted.wait(5)
        assert not released.wait(0.25), (
            "release committed while the fenced filesystem effect was held"
        )
    finally:
        allow_effect_return.set()
        effect_thread.join(5)
        release_thread.join(5)
    assert not effect_thread.is_alive()
    assert not release_thread.is_alive()
    assert errors == []
    assert effect_returned.is_set() and released.is_set()
    assert sequence.index("effect-returned") < sequence.index("released")
    with pytest.raises(OwnershipRefused) as refused:
        with store.transaction() as tx:
            assert_owner(tx, owner.token)
    assert refused.value.code == "FENCE_REVOKED"


def test_fenced_operation_allows_only_same_thread_same_token_nesting(
    tmp_path: Path,
) -> None:
    from run_state.state import ControlStoreRefused

    primary = _repository(tmp_path)
    store, owner, _workspace, _repository_id = _owner(
        tmp_path, primary, "guard-owner-a",
    )
    _other_store, other, _other_workspace, _same_repository_id = _owner(
        tmp_path, primary, "guard-owner-b",
    )

    with store.fenced_operation(owner.token):
        with store.fenced_operation(owner.token):
            pass
        with pytest.raises(ControlStoreRefused) as refused:
            with store.fenced_operation(other.token):
                pytest.fail("a different token entered the held fenced operation")
        assert refused.value.code == "STORE_BUSY"
        with pytest.raises(TypeError):
            with store.fenced_operation(owner.token, already_locked=True):
                pytest.fail("a caller-supplied boolean bypassed the public guard")


@pytest.mark.parametrize("change", ["selection", "upstream"])
def test_cached_request_refuses_changed_selected_material_before_any_effect(
    tmp_path: Path, change: str,
) -> None:
    from run_state.ownership import ControlStore

    primary = _repository(tmp_path)
    _track_planning_context(primary)
    state_root = tmp_path / "authority"
    env = _env(tmp_path)
    repository_id = _registered_repository(primary, state_root)
    original = _selection(primary, repository_id)
    original_path = tmp_path / "selection-original.json"
    original_path.write_text(json.dumps(original, sort_keys=True))
    run_id = f"cached-{change}"
    objective = f"cached {change} request"
    request_key = f"cached-{change}-request"
    started = _cli(
        state_root, primary, "start", "--skill", "fix", "--objective", objective,
        "--activity", "plan", "--run-id", run_id, "--request-key", request_key,
        "--selection-manifest", str(original_path), "--json", *_runtime_flags(), env=env,
    )
    assert started.returncode == 0, (
        f"stdout={started.stdout!r}; stderr={started.stderr!r}"
    )

    changed = json.loads(json.dumps(original))
    if change == "selection":
        changed["entries"] = [_copy("src/selected.sh", b"base-selected\n")]
    else:
        changed["upstream"]["session_key"] = "changed-session"
    changed_path = tmp_path / f"selection-{change}.json"
    changed_path.write_text(json.dumps(changed, sort_keys=True))
    store = ControlStore(state_root / "control.sqlite3")
    database_before = _file_sha256(store.db_path)
    events_before = _event_projection(store)
    refs_before = _git(
        "for-each-ref", "--format=%(refname) %(objectname)", cwd=primary,
    ).stdout
    worktrees_before = _git("worktree", "list", "--porcelain", cwd=primary).stdout

    replay = _cli(
        state_root, primary, "start", "--skill", "fix", "--objective", objective,
        "--activity", "plan", "--run-id", run_id, "--request-key", request_key,
        "--selection-manifest", str(changed_path), "--json", *_runtime_flags(), env=env,
    )

    assert replay.returncode == 2, (
        f"stdout={replay.stdout!r}; stderr={replay.stderr!r}"
    )
    assert json.loads(replay.stdout)["code"] == "IDEMPOTENCY_CONFLICT"
    assert _file_sha256(store.db_path) == database_before
    assert _event_projection(store) == events_before
    assert _git(
        "for-each-ref", "--format=%(refname) %(objectname)", cwd=primary,
    ).stdout == refs_before
    assert _git("worktree", "list", "--porcelain", cwd=primary).stdout == worktrees_before


def test_cached_ready_request_refuses_unbound_latest_activity_without_effects(
    tmp_path: Path,
) -> None:
    from run_state.ownership import ControlStore

    primary = _repository(tmp_path)
    _track_planning_context(primary)
    state_root = tmp_path / "authority"
    env = _env(tmp_path)
    repository_id = _registered_repository(primary, state_root)
    selected = _selection(primary, repository_id)
    selection_path = tmp_path / "selection.json"
    selection_path.write_text(json.dumps(selected, sort_keys=True))
    run_id = "cached-ready-unbound"
    objective = "cached ready must reject unbound activity"
    cached_key = "cached-ready-plan"
    started = _cli(
        state_root, primary, "start", "--skill", "fix", "--objective", objective,
        "--activity", "plan", "--run-id", run_id, "--request-key", cached_key,
        "--selection-manifest", str(selection_path), "--json", *_runtime_flags(), env=env,
    )
    assert started.returncode == 0, (
        f"stdout={started.stdout!r}; stderr={started.stderr!r}"
    )
    completed = _cli(
        state_root, primary, "complete", run_id, "--json",
        "--result-locator", "fixture://cached-ready/plan",
        "--result-sha256", "9" * 64, env=env,
    )
    assert completed.returncode == 0, (
        f"stdout={completed.stdout!r}; stderr={completed.stderr!r}"
    )
    crash_program = textwrap.dedent(
        """
        import json, os, signal, sys
        from run_state.state import ControlStore
        original = ControlStore.transition_activity
        def crash_after_select(self, token, activity_id, **kwargs):
            selected = original(self, token, activity_id, **kwargs)
            if kwargs.get('new') == 'active':
                print(json.dumps({'boundary':'active-before-context-pointer',
                                  'activity_id':activity_id,'pid':os.getpid()}), flush=True)
                os.kill(os.getpid(), signal.SIGKILL)
            return selected
        ControlStore.transition_activity = crash_after_select
        from run_state.cli import main
        raise SystemExit(main(sys.argv[1:]))
        """
    )
    crashed = subprocess.run(
        [sys.executable, "-c", crash_program, "start", "--skill", "fix",
         "--objective", objective, "--activity", "execute", "--run-id", run_id,
         "--request-key", "interrupted-execute", "--selection-manifest",
         str(selection_path), "--json", *_runtime_flags(), "--state-root", str(state_root)],
        cwd=primary, env=env, capture_output=True, text=True, timeout=30,
    )
    assert crashed.returncode == -signal.SIGKILL, (
        f"stdout={crashed.stdout!r}; stderr={crashed.stderr!r}"
    )
    assert json.loads(crashed.stdout)["boundary"] == "active-before-context-pointer"

    store = ControlStore(state_root / "control.sqlite3")
    database_before = _file_sha256(store.db_path)
    events_before = _event_projection(store)
    refs_before = _git(
        "for-each-ref", "--format=%(refname) %(objectname)", cwd=primary,
    ).stdout
    worktrees_before = _git("worktree", "list", "--porcelain", cwd=primary).stdout
    replay = _cli(
        state_root, primary, "start", "--skill", "fix", "--objective", objective,
        "--activity", "plan", "--run-id", run_id, "--request-key", cached_key,
        "--selection-manifest", str(selection_path), "--json", *_runtime_flags(), env=env,
    )

    assert replay.returncode == 3, (
        f"stdout={replay.stdout!r}; stderr={replay.stderr!r}"
    )
    assert json.loads(replay.stdout)["code"] == "RESUME_REQUIRED"
    assert _file_sha256(store.db_path) == database_before
    assert _event_projection(store) == events_before
    assert _git(
        "for-each-ref", "--format=%(refname) %(objectname)", cwd=primary,
    ).stdout == refs_before
    assert _git("worktree", "list", "--porcelain", cwd=primary).stdout == worktrees_before
