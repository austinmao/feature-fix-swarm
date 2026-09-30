from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

import pytest

from run_state.cli import _cmd_fixture_start
from run_state.wave_execution import capture_wave_snapshot, harvest_scoped_patch
from run_state.workspace import (
    WorkspaceRefused, begin_child_workspace_preparation, inspect_workspace,
    parse_input_selection, prepare_workspace, snapshot_inputs,
)
from test_managed_preparation import _args
from test_m4_upstream_context_acceptance import _register, _repository
from test_run_context_acceptance import INHERITED_CONTEXT_KEYS


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def wave_manifest(token, context, head: str) -> dict:
    prompt = "Implement only the declared plan scope."
    return {
        "schema": "ffs.gsd-supervised-dispatch/v1",
        "mode": "ffs-supervised-process",
        "phase": "14",
        "wave": 2,
        "initial_head": head,
        "commit_mode": "patches",
        "apply_between_waves": True,
        "orchestrator_root": token.workspace,
        "admission": {
            "schema": "ffs.supervisor-admission/v1",
            "available": True,
            "repository_id": token.repository_id,
            "run_id": token.run_id,
            "activity_id": context.activity_id,
            "generation": token.generation,
            "workspace": token.workspace,
            "runtime_identity": "runtime-fixture",
        },
        "plans": [{
            "id": "14-02",
            "initial_head": head,
            "prompt": prompt,
            "prompt_fresh": True,
            "prompt_nonce": "wave-two-fixture",
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "files_modified": ["src/input.txt", "src/later.txt"],
            "files_deleted": [".planning/tracked-context.txt"],
            "depends_on": [],
        }],
    }


def test_later_wave_snapshot_captures_complete_dirty_parent_and_preserves_upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _repository(tmp_path)
    monkeypatch.chdir(primary)
    for key in INHERITED_CONTEXT_KEYS:
        monkeypatch.delenv(key, raising=False)

    def capture(store, token, context):
        with store.transaction() as tx:
            preparation_id = tx.execute(
                "SELECT preparation_id FROM context_runs WHERE repository_id=? AND run_id=?",
                (token.repository_id, token.run_id),
            ).fetchone()[0]
            retained = json.dumps({
                "schema": "ffs.input-snapshot/v1",
                "upstream": {
                    "project": "fixture-project",
                    "workstream": "feature-014",
                    "session_key": "fixture-session",
                },
            }, sort_keys=True, separators=(",", ":"))
            tx.execute(
                "UPDATE context_workspaces SET selected_manifest_json=? WHERE preparation_id=?",
                (retained, preparation_id),
            )
        preparation = inspect_workspace(store, preparation_id)
        parent = Path(token.workspace)
        (parent / "src/input.txt").write_bytes(b"accepted wave one\n")
        (parent / "src/later.txt").write_bytes(b"new in wave one\n")
        (parent / ".planning/tracked-context.txt").unlink()
        requests = parent / ".planning/.ffs-wave-requests"
        requests.mkdir(mode=0o700)
        (requests / "transport.json").write_text("transport only\n")
        observer = parent / ".ffs-observer-tmp"
        observer.mkdir(mode=0o700)
        (observer / "telemetry.jsonl").write_text("supervisor only\n")
        channel = parent / ".planning/.ffs-worker-channel/channel"
        channel.mkdir(mode=0o700, parents=True)
        (channel / "request.json").write_text("routing only\n")
        (parent / ".ffs-wave-1-result.json").write_text("{}\n")
        head = git(parent, "rev-parse", "HEAD")

        snapshot = capture_wave_snapshot(
            store, token, preparation, wave_manifest(token, context, head),
            tmp_path / "evidence",
        )

        assert snapshot.manifest["upstream"] == {
            "project": "fixture-project",
            "workstream": "feature-014",
            "session_key": "fixture-session",
        }
        assert [(entry["operation"], entry["path"]) for entry in snapshot.manifest["entries"]] == [
            ("delete", ".planning/tracked-context.txt"),
            ("copy", "src/input.txt"),
            ("copy", "src/later.txt"),
        ]
        assert (snapshot.staging / "files/src/input.txt").read_bytes() == b"accepted wave one\n"
        assert not any("ffs" in entry["path"] for entry in snapshot.manifest["entries"])
        return 0

    assert _cmd_fixture_start(_args(tmp_path / "authority"), on_ready=capture) == 0


def test_wave_snapshot_accepts_exact_active_registered_orchestrator_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _repository(tmp_path)
    monkeypatch.chdir(primary)
    for key in INHERITED_CONTEXT_KEYS:
        monkeypatch.delenv(key, raising=False)

    def capture(store, token, context):
        parent = Path(token.workspace)
        head = git(parent, "rev-parse", "HEAD")
        selection = parse_input_selection({
            "schema": "ffs.input-selection/v1", "base_oid": head,
            "repository_id": token.repository_id, "entries": [], "required_context": [],
            "upstream": {"project": "fixture", "workstream": "wave", "session_key": "session"},
        })
        origin = snapshot_inputs(parent, selection, tmp_path / "origin")
        pending = begin_child_workspace_preparation(
            store, token, parent_activity_id=context.activity_id,
            request_key="orchestrator-child", role="worker", base_commit=head,
            selected_input_manifest=origin.manifest, repository_path=primary,
        )
        ready = prepare_workspace(store, token, pending, input_snapshot=origin)
        store.configure_run_limits(token, dispatch_limit=2, token_limit=100, worker_capacity=2)
        activity = store.create_child_activity(
            token, parent_activity_id=context.activity_id, role="worker",
            request_key="orchestrator-child", candidate_hash=ready.input_digest,
            contract_hash="c" * 64, runtime_identity="runtime-fixture",
            workspace_binding=str(ready.path), workspace_preparation_id=ready.id,
        )
        store.transition_activity(token, activity.id, expected="pending", new="active")
        (ready.path / "src/input.txt").write_bytes(b"orchestrator accepted edit\n")
        manifest = wave_manifest(token, context, head)
        manifest["orchestrator_root"] = str(ready.path)
        manifest["admission"]["workspace"] = str(ready.path)
        with pytest.raises(WorkspaceRefused, match="WAVE_ADMISSION_MISMATCH"):
            capture_wave_snapshot(
                store, token, ready, manifest, tmp_path / "wrong-child-evidence",
            )
        manifest["admission"]["activity_id"] = activity.id

        captured = capture_wave_snapshot(
            store, token, ready, manifest, tmp_path / "child-evidence",
        )

        assert captured.manifest["entries"] == [{
            "operation": "copy", "path": "src/input.txt",
            "sha256": hashlib.sha256(b"orchestrator accepted edit\n").hexdigest(),
            "git_mode": "100644",
        }]
        return 0

    assert _cmd_fixture_start(_args(tmp_path / "authority"), on_ready=capture) == 0


def fixture_repo(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-q")
    git(repository, "config", "user.email", "wave@example.test")
    git(repository, "config", "user.name", "Wave Fixture")
    (repository / "tracked.txt").write_text("base\n")
    (repository / "deleted.txt").write_text("remove\n")
    (repository / "binary.bin").write_bytes(b"base\0binary")
    git(repository, "add", "tracked.txt", "deleted.txt", "binary.bin")
    git(repository, "commit", "-qm", "base")
    return repository.resolve(), git(repository, "rev-parse", "HEAD")


def test_harvest_includes_modified_new_deleted_and_binary_as_applicable_patch(tmp_path: Path) -> None:
    repository, head = fixture_repo(tmp_path)
    (repository / "tracked.txt").write_text("changed\n")
    (repository / "deleted.txt").unlink()
    (repository / "binary.bin").write_bytes(b"changed\0binary\xff")
    (repository / "new.bin").write_bytes(b"new\0binary\xfe")

    result = harvest_scoped_patch(
        repository, head,
        ("tracked.txt", "binary.bin", "new.bin"), ("deleted.txt",),
        tmp_path / "evidence",
    )

    assert result.changed_files == ("binary.bin", "deleted.txt", "new.bin", "tracked.txt")
    assert result.modified_files == ("binary.bin", "new.bin", "tracked.txt")
    assert result.deleted_files == ("deleted.txt",)
    assert result.patch.count("diff --git ") == 4
    assert "GIT binary patch" in result.patch
    assert stat.S_IMODE(result.evidence_path.stat().st_mode) == 0o600
    applied = tmp_path / "applied"
    subprocess.run(["git", "clone", "-q", str(repository), str(applied)], check=True)
    subprocess.run(
        ["git", "apply", "--binary", str(result.evidence_path)],
        cwd=applied, check=True,
    )
    assert (applied / "tracked.txt").read_text() == "changed\n"
    assert not (applied / "deleted.txt").exists()
    assert (applied / "new.bin").read_bytes() == b"new\0binary\xfe"


def test_sibling_workspaces_can_harvest_same_relative_path_independently(tmp_path: Path) -> None:
    repository, head = fixture_repo(tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    git(repository, "worktree", "add", "-q", "-b", "first", str(first), head)
    git(repository, "worktree", "add", "-q", "-b", "second", str(second), head)
    (first / "tracked.txt").write_text("first\n")
    (second / "tracked.txt").write_text("second\n")

    one = harvest_scoped_patch(first.resolve(), head, ("tracked.txt",), (), tmp_path / "evidence-one")
    two = harvest_scoped_patch(second.resolve(), head, ("tracked.txt",), (), tmp_path / "evidence-two")

    assert one.changed_files == two.changed_files == ("tracked.txt",)
    assert one.patch != two.patch


def test_harvest_is_relative_to_parent_snapshot_and_applies_to_parent_overlay(tmp_path: Path) -> None:
    repository, head = fixture_repo(tmp_path)
    repository_id = _register(repository, tmp_path / "authority")
    (repository / "tracked.txt").write_bytes(b"accepted parent change\n")
    (repository / "binary.bin").write_bytes(b"accepted\0parent\xff")
    (repository / "inherited-new.txt").write_bytes(b"accepted new\n")
    (repository / "deleted.txt").unlink()
    entries = [
        {"operation": "copy", "path": path, "sha256": hashlib.sha256(data).hexdigest(), "git_mode": "100644"}
        for path, data in (
            ("tracked.txt", b"accepted parent change\n"),
            ("binary.bin", b"accepted\0parent\xff"),
            ("inherited-new.txt", b"accepted new\n"),
        )
    ]
    entries.append({
        "operation": "delete", "path": "deleted.txt",
        "sha256": hashlib.sha256(b"remove\n").hexdigest(), "git_mode": "100644",
    })
    snapshot = snapshot_inputs(repository, parse_input_selection({
        "schema": "ffs.input-selection/v1", "base_oid": head,
        "repository_id": repository_id, "entries": entries, "required_context": [],
        "upstream": {"project": "fixture", "workstream": "wave", "session_key": "session"},
    }), tmp_path / "snapshot")
    child = tmp_path / "child"
    git(repository, "worktree", "add", "-q", "-b", "snapshot-child", str(child), head)
    for entry in snapshot.manifest["entries"]:
        target = child / entry["path"]
        if entry["operation"] == "delete":
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((snapshot.staging / "files" / entry["path"]).read_bytes())

    # The binary inherited path remains untouched and must not leak into the
    # plan result merely because it differs from HEAD.
    (child / "tracked.txt").write_bytes(b"plan changed parent path\n")
    (child / "inherited-new.txt").unlink()
    (child / "deleted.txt").write_bytes(b"plan restored deleted path\n")
    (child / "plan-new.txt").write_bytes(b"plan addition\n")
    result = harvest_scoped_patch(
        child.resolve(), head,
        ("tracked.txt", "deleted.txt", "plan-new.txt"), ("inherited-new.txt",),
        tmp_path / "evidence", baseline_snapshot=snapshot,
    )

    assert result.changed_files == (
        "deleted.txt", "inherited-new.txt", "plan-new.txt", "tracked.txt",
    )
    assert "binary.bin" not in result.patch
    from run_state.wave_execution import prepare_integration_material
    index_before = (repository / ".git/index").read_bytes()
    prepared = prepare_integration_material(repository.resolve(), head, [{
        "status": "complete", "patch": result.patch,
        "changed_files": list(result.changed_files),
    }], tmp_path / "journal-evidence")
    assert (repository / ".git/index").read_bytes() == index_before
    assert (repository / "tracked.txt").read_bytes() == b"accepted parent change\n"
    for relative in result.changed_files:
        target = child / relative
        expected = ({"sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                     "git_mode": "100644"} if target.exists() else None)
        assert prepared["expected_after"][relative] == expected
    subprocess.run(
        ["git", "apply", "--binary", str(result.evidence_path)],
        cwd=repository, check=True,
    )
    for relative in ("tracked.txt", "binary.bin", "deleted.txt", "plan-new.txt"):
        assert (repository / relative).read_bytes() == (child / relative).read_bytes()
    assert not (repository / "inherited-new.txt").exists()


def test_harvest_rejects_out_of_scope_and_unsafe_files(tmp_path: Path) -> None:
    repository, head = fixture_repo(tmp_path)
    (repository / "undeclared.txt").write_text("outside\n")
    with pytest.raises(WorkspaceRefused, match="WAVE_SCOPE_VIOLATION"):
        harvest_scoped_patch(repository, head, (), (), tmp_path / "scope-evidence")
    (repository / "undeclared.txt").unlink()
    os.symlink("tracked.txt", repository / "linked.txt")
    with pytest.raises(WorkspaceRefused, match="UNSAFE_SELECTION_PATH"):
        harvest_scoped_patch(repository, head, ("linked.txt",), (), tmp_path / "link-evidence")
    (repository / "linked.txt").unlink()
    os.mkfifo(repository / "special.pipe")
    with pytest.raises(WorkspaceRefused, match="UNSAFE_SELECTION_PATH"):
        harvest_scoped_patch(repository, head, ("special.pipe",), (), tmp_path / "special-evidence")


def test_empty_harvest_is_honest_and_retained(tmp_path: Path) -> None:
    repository, head = fixture_repo(tmp_path)
    result = harvest_scoped_patch(repository, head, (), (), tmp_path / "evidence")
    assert result.patch == ""
    assert result.changed_files == result.modified_files == result.deleted_files == ()
    assert result.evidence_path.read_bytes() == b""
    from run_state.wave_execution import prepare_integration_material
    prepared = prepare_integration_material(repository.resolve(), head, [{
        "status": "complete", "patch": "", "changed_files": [],
    }], tmp_path / "empty-journal")
    assert prepared["before"] == prepared["expected_after"] == {}
    assert stat.S_IMODE(result.evidence_path.stat().st_mode) == 0o600


def test_harvest_rejects_head_drift_without_evidence(tmp_path: Path) -> None:
    repository, head = fixture_repo(tmp_path)
    (repository / "tracked.txt").write_text("next\n")
    git(repository, "add", "tracked.txt")
    git(repository, "commit", "-qm", "unexpected commit")
    with pytest.raises(WorkspaceRefused, match="WAVE_HEAD_MISMATCH"):
        harvest_scoped_patch(repository, head, ("tracked.txt",), (), tmp_path / "evidence")
    assert not (tmp_path / "evidence").exists()


def _write_gsd_sentinel(root: Path) -> None:
    sentinel = root / ".gsd/dispatch-isolation-sentinel.json"
    sentinel.parent.mkdir(exist_ok=True)
    sentinel.write_text('{"step": "dispatch-isolation"}\n')


def test_gsd_runtime_state_does_not_enter_wave_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _repository(tmp_path)
    monkeypatch.chdir(primary)
    for key in INHERITED_CONTEXT_KEYS:
        monkeypatch.delenv(key, raising=False)

    def capture(store, token, context):
        with store.transaction() as tx:
            preparation_id = tx.execute(
                "SELECT preparation_id FROM context_runs WHERE repository_id=? AND run_id=?",
                (token.repository_id, token.run_id),
            ).fetchone()[0]
            retained = json.dumps({
                "schema": "ffs.input-snapshot/v1",
                "upstream": {
                    "project": "fixture-project",
                    "workstream": "feature-014",
                    "session_key": "fixture-session",
                },
            }, sort_keys=True, separators=(",", ":"))
            tx.execute(
                "UPDATE context_workspaces SET selected_manifest_json=? WHERE preparation_id=?",
                (retained, preparation_id),
            )
        preparation = inspect_workspace(store, preparation_id)
        parent = Path(token.workspace)
        (parent / "src/input.txt").write_bytes(b"accepted wave one\n")
        (parent / "newfile.py").write_text("deliverable\n")
        head = git(parent, "rev-parse", "HEAD")
        manifest = wave_manifest(token, context, head)

        clean = capture_wave_snapshot(
            store, token, preparation, manifest, tmp_path / "clean-evidence",
        )
        _write_gsd_sentinel(parent)
        with_gsd = capture_wave_snapshot(
            store, token, preparation, manifest, tmp_path / "gsd-evidence",
        )

        def overlay(snapshot) -> list[str]:
            files = snapshot.staging / "files"
            return sorted(
                item.relative_to(files).as_posix()
                for item in files.rglob("*") if item.is_file()
            )

        assert overlay(with_gsd) == overlay(clean) == ["newfile.py", "src/input.txt"]
        assert with_gsd.manifest["entries"] == clean.manifest["entries"]
        assert with_gsd.input_digest == clean.input_digest
        return 0

    assert _cmd_fixture_start(_args(tmp_path / "authority"), on_ready=capture) == 0


def test_gsd_runtime_state_does_not_enter_wave_output(tmp_path: Path) -> None:
    from run_state.wave_execution import _inventory, _material_entries

    repository, head = fixture_repo(tmp_path)
    (repository / "tracked.txt").write_text("changed\n")
    (repository / "newfile.py").write_text("deliverable\n")
    clean_entries = _material_entries(repository, head, _inventory(repository, head))
    _write_gsd_sentinel(repository)

    assert _inventory(repository, head).untracked == ("newfile.py",)
    assert _material_entries(repository, head, _inventory(repository, head)) == clean_entries
    result = harvest_scoped_patch(
        repository, head, ("tracked.txt", "newfile.py"), (), tmp_path / "evidence",
    )
    assert result.changed_files == ("newfile.py", "tracked.txt")
    assert ".gsd" not in result.patch


def test_gsd_exemption_is_not_blanket_for_deliverable_untracked_files(tmp_path: Path) -> None:
    from run_state.wave_execution import _inventory

    repository, head = fixture_repo(tmp_path)
    deliverables = ("newfile.py", ".gsdfoo.txt", "pkg/.gsd/kept.py")
    for relative in deliverables:
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("deliverable\n")

    assert _inventory(repository, head).untracked == tuple(sorted(deliverables))
    result = harvest_scoped_patch(repository, head, deliverables, (), tmp_path / "evidence")
    assert result.changed_files == tuple(sorted(deliverables))


def _commit_tracked_gsd(repository: Path) -> str:
    (repository / ".gsd").mkdir(exist_ok=True)
    (repository / ".gsd/config.json").write_text('{"mode": "base"}\n')
    (repository / ".gsd/gone.json").write_text("{}\n")
    git(repository, "add", ".gsd/config.json", ".gsd/gone.json")
    git(repository, "commit", "-qm", "track gsd files")
    return git(repository, "rev-parse", "HEAD")


def test_tracked_gsd_changes_stay_in_wave_inventory_and_harvest(tmp_path: Path) -> None:
    from run_state.wave_execution import _inventory

    repository, _ = fixture_repo(tmp_path)
    head = _commit_tracked_gsd(repository)
    (repository / ".gsd/config.json").write_text('{"mode": "changed"}\n')
    (repository / ".gsd/gone.json").unlink()
    _write_gsd_sentinel(repository)

    inventory = _inventory(repository, head)
    assert inventory.modified == (".gsd/config.json",)
    assert inventory.deleted == (".gsd/gone.json",)
    assert inventory.untracked == ()
    result = harvest_scoped_patch(
        repository, head, (".gsd/config.json",), (".gsd/gone.json",), tmp_path / "evidence",
    )
    assert result.changed_files == (".gsd/config.json", ".gsd/gone.json")


def test_tracked_gsd_changes_stay_in_wave_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _repository(tmp_path)
    _commit_tracked_gsd(primary)
    monkeypatch.chdir(primary)
    for key in INHERITED_CONTEXT_KEYS:
        monkeypatch.delenv(key, raising=False)

    def capture(store, token, context):
        with store.transaction() as tx:
            preparation_id = tx.execute(
                "SELECT preparation_id FROM context_runs WHERE repository_id=? AND run_id=?",
                (token.repository_id, token.run_id),
            ).fetchone()[0]
            retained = json.dumps({
                "schema": "ffs.input-snapshot/v1",
                "upstream": {
                    "project": "fixture-project",
                    "workstream": "feature-014",
                    "session_key": "fixture-session",
                },
            }, sort_keys=True, separators=(",", ":"))
            tx.execute(
                "UPDATE context_workspaces SET selected_manifest_json=? WHERE preparation_id=?",
                (retained, preparation_id),
            )
        preparation = inspect_workspace(store, preparation_id)
        parent = Path(token.workspace)
        (parent / ".gsd/config.json").write_text('{"mode": "changed"}\n')
        (parent / ".gsd/gone.json").unlink()
        _write_gsd_sentinel(parent)
        head = git(parent, "rev-parse", "HEAD")

        snapshot = capture_wave_snapshot(
            store, token, preparation, wave_manifest(token, context, head),
            tmp_path / "evidence",
        )

        assert [(entry["operation"], entry["path"]) for entry in snapshot.manifest["entries"]] == [
            ("copy", ".gsd/config.json"),
            ("delete", ".gsd/gone.json"),
        ]
        return 0

    assert _cmd_fixture_start(_args(tmp_path / "authority"), on_ready=capture) == 0


@pytest.mark.parametrize("kind", ["symlink", "file", "fifo-inside", "symlink-inside"])
def test_unsafe_gsd_nodes_are_refused(tmp_path: Path, kind: str) -> None:
    from run_state.wave_execution import _inventory

    repository, head = fixture_repo(tmp_path)
    if kind == "symlink":
        os.symlink("tracked.txt", repository / ".gsd")
    elif kind == "file":
        (repository / ".gsd").write_text("not a directory\n")
    else:
        (repository / ".gsd").mkdir()
        if kind == "fifo-inside":
            os.mkfifo(repository / ".gsd/pipe")
        else:
            os.symlink("../tracked.txt", repository / ".gsd/link")

    with pytest.raises(WorkspaceRefused, match="UNSAFE_SELECTION_PATH"):
        _inventory(repository, head)


@pytest.mark.parametrize("alias", [".GSD", ".Gsd"])
def test_gsd_case_alias_is_refused_not_exempted(tmp_path: Path, alias: str) -> None:
    from run_state.wave_execution import _inventory

    repository, head = fixture_repo(tmp_path)
    (repository / alias).mkdir()
    (repository / alias / "x").write_text("alias\n")

    with pytest.raises(WorkspaceRefused, match="UNSAFE_SELECTION_PATH"):
        _inventory(repository, head)


def test_tracked_unchanged_gsd_symlink_is_refused(tmp_path: Path) -> None:
    from run_state.wave_execution import _inventory

    repository, _ = fixture_repo(tmp_path)
    (repository / "real").mkdir()
    (repository / "real/config.json").write_text("{}\n")
    os.symlink("real", repository / ".gsd")
    git(repository, "add", "real/config.json", ".gsd")
    git(repository, "commit", "-qm", "track gsd symlink")
    head = git(repository, "rev-parse", "HEAD")

    with pytest.raises(WorkspaceRefused, match="UNSAFE_SELECTION_PATH"):
        _inventory(repository, head)


def test_tracked_unchanged_gsd_directory_gives_empty_inventory(tmp_path: Path) -> None:
    from run_state.wave_execution import _inventory

    repository, _ = fixture_repo(tmp_path)
    head = _commit_tracked_gsd(repository)

    inventory = _inventory(repository, head)
    assert inventory.changed == ()


# F45: untracked wave inventories honor the repository's own in-tree .gitignore.

_CACHE_IGNORES = ".coverage\n.pytest_cache/\n.ruff_cache/\n__pycache__/\n"


def ignoring_repo(
    tmp_path: Path, extra: str = "", base: str = _CACHE_IGNORES,
) -> tuple[Path, str]:
    repository, _ = fixture_repo(tmp_path)
    (repository / ".gitignore").write_text(base + extra)
    git(repository, "add", ".gitignore")
    git(repository, "commit", "-qm", "ignore tool caches")
    return repository, git(repository, "rev-parse", "HEAD")


def write_tool_caches(root: Path) -> None:
    (root / ".coverage").write_bytes(b"coverage\0")
    for relative in (
        ".pytest_cache/x", ".ruff_cache/y", "__pycache__/m.pyc", "tests/__pycache__/m.pyc",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"cache\0")


def test_ignored_tool_caches_are_not_wave_changes(tmp_path: Path) -> None:
    from run_state.wave_execution import _inventory

    repository, head = ignoring_repo(tmp_path)
    (repository / "tracked.txt").write_text("changed\n")
    write_tool_caches(repository)

    assert _inventory(repository, head).untracked == ()
    result = harvest_scoped_patch(
        repository, head, ("tracked.txt",), (), tmp_path / "evidence",
    )
    assert result.changed_files == result.modified_files == ("tracked.txt",)
    assert result.patch.count("diff --git ") == 1
    for name in (".coverage", "pytest_cache", "ruff_cache", "__pycache__", ".pyc"):
        assert name not in result.patch


def test_ignored_caches_do_not_hide_an_out_of_scope_untracked_file(tmp_path: Path) -> None:
    repository, head = ignoring_repo(tmp_path)
    write_tool_caches(repository)
    assert harvest_scoped_patch(repository, head, (), (), tmp_path / "clean").changed_files == ()

    (repository / "undeclared.txt").write_text("outside\n")
    with pytest.raises(WorkspaceRefused, match="WAVE_SCOPE_VIOLATION"):
        harvest_scoped_patch(repository, head, (), (), tmp_path / "scope-evidence")


def test_self_ignoring_tool_cache_dirs_are_not_wave_changes(tmp_path: Path) -> None:
    from run_state.wave_execution import _inventory

    # pytest and ruff each write a .gitignore containing "*" into their cache
    # directory. The tracked .gitignore here does not list either directory.
    repository, head = ignoring_repo(tmp_path, base=".coverage\n__pycache__/\n")
    (repository / "tracked.txt").write_text("changed\n")
    for relative, content in (
        (".pytest_cache/.gitignore", "# Created by pytest automatically.\n*\n"),
        (".pytest_cache/v/x", "cache\n"),
        (".ruff_cache/.gitignore", "*\n"),
        (".ruff_cache/0/y", "cache\n"),
        ("__pycache__/m.pyc", "cache\n"),
    ):
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    (repository / ".coverage").write_bytes(b"coverage\0")

    assert _inventory(repository, head).untracked == ()
    result = harvest_scoped_patch(
        repository, head, ("tracked.txt",), (), tmp_path / "evidence",
    )
    assert result.changed_files == result.modified_files == ("tracked.txt",)
    assert result.patch.count("diff --git ") == 1
    for name in ("pytest_cache", "ruff_cache", "__pycache__", ".coverage"):
        assert name not in result.patch


def test_self_ignoring_untracked_gitignore_hides_itself_and_is_not_integrated(
    tmp_path: Path,
) -> None:
    from run_state.wave_execution import _inventory

    repository, head = fixture_repo(tmp_path)
    (repository / "sub").mkdir()
    (repository / "sub/.gitignore").write_text("*\n")
    (repository / "sub/evil.py").write_text("evil\n")

    assert _inventory(repository, head).changed == ()
    result = harvest_scoped_patch(repository, head, (), (), tmp_path / "evidence")
    assert result.changed_files == ()
    assert result.patch == ""


def test_untracked_gitignore_that_is_not_self_ignoring_is_an_ordinary_change(
    tmp_path: Path,
) -> None:
    from run_state.wave_execution import _inventory

    repository, head = fixture_repo(tmp_path)
    (repository / "sub").mkdir()
    (repository / "sub/.gitignore").write_text("*.log\n")
    (repository / "sub/noise.log").write_text("hidden\n")

    assert _inventory(repository, head).untracked == ("sub/.gitignore",)
    with pytest.raises(WorkspaceRefused, match="WAVE_SCOPE_VIOLATION"):
        harvest_scoped_patch(repository, head, (), (), tmp_path / "scope-evidence")
    result = harvest_scoped_patch(
        repository, head, ("sub/.gitignore",), (), tmp_path / "evidence",
    )
    assert result.changed_files == ("sub/.gitignore",)
    assert "noise.log" not in result.patch


def test_tracked_gitignore_change_is_scope_checked_like_any_tracked_change(
    tmp_path: Path,
) -> None:
    repository, head = ignoring_repo(tmp_path)
    (repository / ".gitignore").write_text(_CACHE_IGNORES + "newfile.py\n")
    (repository / "newfile.py").write_text("hidden by a tracked edit\n")

    with pytest.raises(WorkspaceRefused, match="WAVE_SCOPE_VIOLATION"):
        harvest_scoped_patch(repository, head, (), (), tmp_path / "evidence")


@pytest.mark.parametrize("source", ["global-excludes", "info-exclude"])
def test_host_local_excludes_do_not_hide_untracked_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    from run_state.wave_execution import _inventory

    repository, head = fixture_repo(tmp_path)
    (repository / "newfile.py").write_text("deliverable\n")
    if source == "global-excludes":
        ignored = tmp_path / "global.ignore"
        ignored.write_text("newfile.py\n")
        config = tmp_path / "global.gitconfig"
        config.write_text(f"[core]\n\texcludesFile = {ignored}\n")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    else:
        exclude = Path(git(
            repository, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude",
        ))
        exclude.parent.mkdir(exist_ok=True)
        exclude.write_text("newfile.py\n")

    # Control: the host-local source really would hide the file.
    assert git(repository, "ls-files", "--others", "--exclude-standard") == ""
    assert _inventory(repository, head).untracked == ("newfile.py",)
    result = harvest_scoped_patch(
        repository, head, ("newfile.py",), (), tmp_path / "evidence",
    )
    assert result.changed_files == ("newfile.py",)


@pytest.mark.parametrize(
    "kind",
    ["symlink", "fifo-inside", "fifo-elsewhere", "case-alias", "fifo-under-untracked-rule"],
)
def test_ignored_unsafe_nodes_are_still_refused(tmp_path: Path, kind: str) -> None:
    from run_state.wave_execution import _inventory

    repository, head = ignoring_repo(tmp_path, extra=".gsd\n.GSD\ncache/\n")
    if kind == "symlink":
        os.symlink("tracked.txt", repository / ".gsd")
    elif kind == "fifo-inside":
        (repository / ".gsd").mkdir()
        os.mkfifo(repository / ".gsd/pipe")
    elif kind == "fifo-elsewhere":
        (repository / "cache").mkdir()
        os.mkfifo(repository / "cache/pipe")
    elif kind == "fifo-under-untracked-rule":
        (repository / "sub").mkdir()
        (repository / "sub/.gitignore").write_text("*\n")
        os.mkfifo(repository / "sub/pipe")
    else:
        (repository / ".GSD").mkdir()
        (repository / ".GSD/x").write_text("alias\n")

    with pytest.raises(WorkspaceRefused, match="UNSAFE_SELECTION_PATH"):
        _inventory(repository, head)


def test_ignored_caches_do_not_change_wave_material_entries(tmp_path: Path) -> None:
    from run_state.wave_execution import _inventory, _material_entries

    repository, head = ignoring_repo(tmp_path)
    (repository / "tracked.txt").write_text("changed\n")
    (repository / "newfile.py").write_text("deliverable\n")
    clean = _material_entries(repository, head, _inventory(repository, head))
    write_tool_caches(repository)

    assert _inventory(repository, head).changed == ("newfile.py", "tracked.txt")
    assert _material_entries(repository, head, _inventory(repository, head)) == clean


def test_ignored_caches_do_not_change_snapshot_input_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _repository(tmp_path)
    (primary / ".gitignore").write_text(_CACHE_IGNORES)
    git(primary, "add", ".gitignore")
    git(primary, "commit", "-qm", "ignore tool caches")
    monkeypatch.chdir(primary)
    for key in INHERITED_CONTEXT_KEYS:
        monkeypatch.delenv(key, raising=False)

    def capture(store, token, context):
        with store.transaction() as tx:
            preparation_id = tx.execute(
                "SELECT preparation_id FROM context_runs WHERE repository_id=? AND run_id=?",
                (token.repository_id, token.run_id),
            ).fetchone()[0]
            retained = json.dumps({
                "schema": "ffs.input-snapshot/v1",
                "upstream": {
                    "project": "fixture-project",
                    "workstream": "feature-014",
                    "session_key": "fixture-session",
                },
            }, sort_keys=True, separators=(",", ":"))
            tx.execute(
                "UPDATE context_workspaces SET selected_manifest_json=? WHERE preparation_id=?",
                (retained, preparation_id),
            )
        preparation = inspect_workspace(store, preparation_id)
        parent = Path(token.workspace)
        (parent / "src/input.txt").write_bytes(b"accepted wave one\n")
        (parent / "newfile.py").write_text("deliverable\n")
        manifest = wave_manifest(token, context, git(parent, "rev-parse", "HEAD"))

        clean = capture_wave_snapshot(
            store, token, preparation, manifest, tmp_path / "clean-evidence",
        )
        write_tool_caches(parent)
        with_caches = capture_wave_snapshot(
            store, token, preparation, manifest, tmp_path / "cache-evidence",
        )

        files = with_caches.staging / "files"
        assert sorted(
            item.relative_to(files).as_posix() for item in files.rglob("*") if item.is_file()
        ) == ["newfile.py", "src/input.txt"]
        assert with_caches.manifest["entries"] == clean.manifest["entries"]
        assert with_caches.input_digest == clean.input_digest
        return 0

    assert _cmd_fixture_start(_args(tmp_path / "authority"), on_ready=capture) == 0


def test_declared_output_hidden_by_an_ignore_rule_is_refused(tmp_path: Path) -> None:
    from run_state.wave_execution import _inventory

    repository, head = ignoring_repo(tmp_path, extra="build/\n")
    (repository / "tracked.txt").write_text("changed\n")
    declared = ("build/out.txt", "tracked.txt")

    # Guard: a declared output that was never written is an ordinary partial result.
    result = harvest_scoped_patch(repository, head, declared, (), tmp_path / "absent")
    assert result.changed_files == ("tracked.txt",)

    (repository / "build").mkdir()
    (repository / "build/out.txt").write_text("deliverable\n")
    # The shared inventory stays scope-blind: it only leaves the file out.
    assert _inventory(repository, head).untracked == ()
    with pytest.raises(WorkspaceRefused, match="WAVE_SCOPE_VIOLATION"):
        harvest_scoped_patch(repository, head, declared, (), tmp_path / "hidden")


def test_undeclared_ignored_file_does_not_block_a_harvest(tmp_path: Path) -> None:
    repository, head = ignoring_repo(tmp_path, extra="build/\n")
    (repository / "tracked.txt").write_text("changed\n")
    (repository / "build").mkdir()
    (repository / "build/out.txt").write_text("scratch\n")

    result = harvest_scoped_patch(
        repository, head, ("tracked.txt",), (), tmp_path / "evidence",
    )
    assert result.changed_files == ("tracked.txt",)


@pytest.mark.parametrize("filtered, unfiltered, code", [
    ([".GSD/x"], [], "UNSAFE_SELECTION_PATH|SOURCE_CHANGED"),
    (["newfile.py"], [], "SOURCE_CHANGED"),
    (["newfile.py"], ["other.py"], "SOURCE_CHANGED"),
])
def test_filtered_listing_must_be_a_subset_of_the_unfiltered_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    filtered: list[str], unfiltered: list[str], code: str,
) -> None:
    import run_state.wave_execution as module

    repository, head = fixture_repo(tmp_path)
    monkeypatch.setattr(
        module, "_untracked_paths",
        lambda workspace, *options: list(filtered if options else unfiltered),
    )

    with pytest.raises(WorkspaceRefused, match=code):
        module._inventory(repository, head)


def test_ignore_matching_is_case_sensitive_whatever_the_repository_config(
    tmp_path: Path,
) -> None:
    from run_state.wave_execution import _inventory

    repository, head = ignoring_repo(tmp_path, extra="*.tsbuildinfo\n")
    (repository / "X.TSBUILDINFO").write_text("case mismatch\n")

    observed = {}
    for value in ("false", "true"):
        git(repository, "config", "core.ignoreCase", value)
        observed[value] = _inventory(repository, head)
    assert observed["true"] == observed["false"]
    assert observed["true"].untracked == ("X.TSBUILDINFO",)

    with pytest.raises(WorkspaceRefused, match="WAVE_SCOPE_VIOLATION"):
        harvest_scoped_patch(repository, head, (), (), tmp_path / "scope-evidence")


@pytest.mark.parametrize("declared", ["build//out.txt", "./build/out.txt", "Build/Out.txt"])
def test_declared_output_hidden_by_an_ignore_rule_is_refused_for_any_spelling(
    tmp_path: Path, declared: str,
) -> None:
    repository, head = ignoring_repo(tmp_path, extra="build/\n")
    (repository / "tracked.txt").write_text("changed\n")
    (repository / "build").mkdir()
    (repository / "build/out.txt").write_text("deliverable\n")

    with pytest.raises(WorkspaceRefused, match="WAVE_SCOPE_VIOLATION"):
        harvest_scoped_patch(
            repository, head, (declared, "tracked.txt"), (), tmp_path / "evidence",
        )


def _create_during_harvest(
    monkeypatch: pytest.MonkeyPatch, repository: Path, stage: str, relative: str,
) -> None:
    """Create ``relative`` between two of harvest's inventories."""
    import run_state.wave_execution as module

    original = getattr(module, stage)

    def hooked(*args, **kwargs):
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("appeared during harvest\n")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, stage, hooked)


@pytest.mark.parametrize("stage", ["_patch", "_write_evidence"])
def test_unrelated_ignored_file_created_during_harvest_is_harmless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    repository, head = ignoring_repo(tmp_path, extra="build/\ncache/\n")
    (repository / "tracked.txt").write_text("changed\n")
    _create_during_harvest(monkeypatch, repository, stage, "cache/noise")

    result = harvest_scoped_patch(
        repository, head, ("build/out.txt", "tracked.txt"), (), tmp_path / "evidence",
    )
    assert result.changed_files == ("tracked.txt",)


@pytest.mark.parametrize("stage", ["_patch", "_write_evidence"])
def test_declared_output_that_becomes_hidden_during_harvest_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    repository, head = ignoring_repo(tmp_path, extra="build/\n")
    (repository / "tracked.txt").write_text("changed\n")
    _create_during_harvest(monkeypatch, repository, stage, "build/out.txt")

    with pytest.raises(WorkspaceRefused, match="WAVE_SCOPE_VIOLATION"):
        harvest_scoped_patch(
            repository, head, ("build/out.txt", "tracked.txt"), (), tmp_path / "evidence",
        )


@pytest.mark.parametrize("filtered, unfiltered", [
    (["Tracked.TXT"], ["Tracked.TXT"]),
    ([], ["Tracked.TXT"]),
])
def test_untracked_name_that_case_folds_onto_a_tracked_path_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    filtered: list[str], unfiltered: list[str],
) -> None:
    import run_state.wave_execution as module

    # tracked.txt is tracked; a case-insensitive volume can report a case-only
    # rename as an untracked name while the tracked diff still knows the old one.
    repository, head = fixture_repo(tmp_path)
    monkeypatch.setattr(
        module, "_untracked_paths",
        lambda workspace, *options: list(filtered if options else unfiltered),
    )

    with pytest.raises(WorkspaceRefused, match="UNSAFE_SELECTION_PATH"):
        module._inventory(repository, head)


def test_untracked_name_unrelated_to_tracked_paths_is_not_a_case_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import run_state.wave_execution as module

    repository, head = fixture_repo(tmp_path)
    monkeypatch.setattr(module, "_untracked_paths", lambda workspace, *options: ["newfile.py"])

    assert module._inventory(repository, head).untracked == ("newfile.py",)


# F49: gsd-core's ephemeral workflow._auto_chain_active=false write is no change.
#
# execute-phase tells the orchestrator to run
# `gsd_run query config-set workflow._auto_chain_active false`, and gsd-core's
# setConfigValue (gsd-core/bin/lib/config.cjs) rewrites the tracked
# .planning/config.json with JSON.stringify(config, null, 2). FFS treats exactly
# that one edit as no change and every other config edit as a modification.

_CONFIG = ".planning/config.json"
_FLAG = "_auto_chain_active"


def _gsd_json(config, *, indent: int = 2, suffix: str = "") -> bytes:
    # indent=2, ensure_ascii=False and no trailing newline is JSON.stringify(x, null, 2).
    return (json.dumps(config, indent=indent, ensure_ascii=False) + suffix).encode()


def _gsd_config(**workflow) -> dict:
    return {
        "mode": "interactive", "retries": 1,
        "workflow": {"research": True, "plan_check": True, **workflow},
    }


def _track_config(repository: Path, data: bytes) -> str:
    target = repository / _CONFIG
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(data)
    git(repository, "add", _CONFIG)
    git(repository, "commit", "-qm", "track gsd config")
    return git(repository, "rev-parse", "HEAD")


def _gsd_sets_flag(root: Path, value=False, relative: str = _CONFIG) -> bytes:
    """Leave on disk what `config-set workflow._auto_chain_active <value>` leaves."""
    target = root / relative
    config = json.loads(target.read_bytes())
    config["workflow"][_FLAG] = value
    data = _gsd_json(config)
    target.write_bytes(data)
    return data


@pytest.mark.parametrize("indent, suffix", [(2, ""), (2, "\n"), (4, "\n")])
def test_gsd_auto_chain_flag_write_is_no_inventory_change(
    tmp_path: Path, indent: int, suffix: str,
) -> None:
    from run_state.wave_execution import _inventory, _material_entries

    repository, _ = fixture_repo(tmp_path)
    base = _gsd_json(_gsd_config(), indent=indent, suffix=suffix)
    head = _track_config(repository, base)
    (repository / _CONFIG).chmod(0o664)
    flagged = _gsd_sets_flag(repository)
    assert flagged != base

    inventory = _inventory(repository, head)

    assert inventory.changed == ()
    assert _material_entries(repository, head, inventory) == ()
    assert (repository / _CONFIG).read_bytes() == base
    assert stat.S_IMODE((repository / _CONFIG).stat().st_mode) == 0o664
    assert sorted(item.name for item in (repository / ".planning").iterdir()) == ["config.json"]


def test_worker_gsd_auto_chain_flag_write_is_not_out_of_scope(tmp_path: Path) -> None:
    repository, _ = fixture_repo(tmp_path)
    base = _gsd_json(_gsd_config())
    head = _track_config(repository, base)
    (repository / "tracked.txt").write_text("planned change\n")
    _gsd_sets_flag(repository)

    result = harvest_scoped_patch(
        repository, head, ("tracked.txt",), (), tmp_path / "evidence",
    )

    assert result.changed_files == ("tracked.txt",)
    assert _CONFIG not in result.patch
    assert (repository / _CONFIG).read_bytes() == base


def _flagged(value, *, top=None, **workflow) -> dict:
    config = _gsd_config(**workflow)
    config.update(top or {})
    config["workflow"][_FLAG] = value
    return config


_STILL_COUNTS = [
    pytest.param(_gsd_config(), _gsd_json(_flagged(True)), id="flag-true"),
    pytest.param(_gsd_config(), _gsd_json(_flagged(None)), id="flag-null"),
    pytest.param(_gsd_config(), _gsd_json(_flagged(0)), id="flag-zero"),
    pytest.param(_gsd_config(), _gsd_json(_flagged("false")), id="flag-string"),
    pytest.param(
        _gsd_config(), _gsd_json(_flagged(False, research=False)),
        id="flag-false-and-workflow-key-changed",
    ),
    pytest.param(
        _gsd_config(), _gsd_json(_flagged(False, top={"mode": "yolo"})),
        id="flag-false-and-top-level-key-changed",
    ),
    pytest.param(
        _gsd_config(), _gsd_json(_flagged(False, top={"retries": True})),
        id="flag-false-and-one-became-true",
    ),
    pytest.param(
        _gsd_config(), _gsd_json({**_gsd_config(), _FLAG: False}),
        id="flag-false-at-top-level",
    ),
    pytest.param(_gsd_config(), _gsd_json(_gsd_config(), indent=4), id="formatting-only-no-flag"),
    pytest.param(_flagged(True), _gsd_json(_flagged(False)), id="base-flag-true"),
    pytest.param(
        _gsd_config(),
        _gsd_json(_flagged(False)).replace(
            b'"_auto_chain_active": false',
            b'"_auto_chain_active": false, "_auto_chain_active": false',
        ),
        id="duplicate-key",
    ),
    pytest.param(
        {"mode": "interactive"},
        _gsd_json({"mode": "interactive", "workflow": {_FLAG: False}}),
        id="base-without-workflow",
    ),
]


@pytest.mark.parametrize("base_config, candidate", _STILL_COUNTS)
def test_anything_but_the_false_flag_write_still_counts_as_modified(
    tmp_path: Path, base_config: dict, candidate: bytes,
) -> None:
    from run_state.wave_execution import _inventory

    repository, _ = fixture_repo(tmp_path)
    head = _track_config(repository, _gsd_json(base_config))
    (repository / _CONFIG).write_bytes(candidate)

    assert _inventory(repository, head).modified == (_CONFIG,)
    assert (repository / _CONFIG).read_bytes() == candidate


def test_flag_write_with_a_mode_change_still_counts_as_modified(tmp_path: Path) -> None:
    from run_state.wave_execution import _inventory

    repository, _ = fixture_repo(tmp_path)
    head = _track_config(repository, _gsd_json(_gsd_config()))
    flagged = _gsd_sets_flag(repository)
    (repository / _CONFIG).chmod(0o755)

    assert _inventory(repository, head).modified == (_CONFIG,)
    assert (repository / _CONFIG).read_bytes() == flagged


def test_flag_write_to_an_untracked_config_stays_untracked(tmp_path: Path) -> None:
    from run_state.wave_execution import _inventory

    repository, head = fixture_repo(tmp_path)
    (repository / ".planning").mkdir()
    (repository / _CONFIG).write_bytes(_gsd_json(_flagged(False)))

    assert _inventory(repository, head).untracked == (_CONFIG,)
    assert (repository / _CONFIG).read_bytes() == _gsd_json(_flagged(False))


def test_symlinked_config_is_left_alone(tmp_path: Path) -> None:
    from run_state.wave_execution import _inventory

    repository, _ = fixture_repo(tmp_path)
    head = _track_config(repository, _gsd_json(_gsd_config()))
    flagged = _gsd_json(_flagged(False))
    (repository / "real-config.json").write_bytes(flagged)
    (repository / _CONFIG).unlink()
    os.symlink("../real-config.json", repository / _CONFIG)

    assert _CONFIG in _inventory(repository, head).modified
    assert (repository / _CONFIG).is_symlink()
    assert (repository / "real-config.json").read_bytes() == flagged


def test_symlinked_planning_directory_is_left_alone(tmp_path: Path) -> None:
    from run_state.wave_execution import _inventory

    repository, _ = fixture_repo(tmp_path)
    head = _track_config(repository, _gsd_json(_gsd_config()))
    (repository / ".planning").rename(repository / "real-planning")
    real = repository / "real-planning/config.json"
    flagged = _gsd_sets_flag(repository, relative="real-planning/config.json")
    os.symlink("real-planning", repository / ".planning")

    try:
        _inventory(repository, head)
    except WorkspaceRefused:
        pass
    assert (repository / ".planning").is_symlink()
    assert real.read_bytes() == flagged


def test_case_aliased_planning_directory_is_left_alone(tmp_path: Path) -> None:
    from run_state.wave_execution import _inventory

    repository, _ = fixture_repo(tmp_path)
    head = _track_config(repository, _gsd_json(_gsd_config()))
    flagged = _gsd_sets_flag(repository)
    # On a case-insensitive volume `.Planning` still opens as `.planning`; on a
    # case-sensitive one the config is simply gone from its tracked path.
    (repository / ".planning").rename(repository / ".Planning")

    try:
        _inventory(repository, head)
    except WorkspaceRefused:
        pass
    assert (repository / ".Planning/config.json").read_bytes() == flagged


def _with_orchestrator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, primary: Path, body) -> None:
    monkeypatch.chdir(primary)
    for key in INHERITED_CONTEXT_KEYS:
        monkeypatch.delenv(key, raising=False)

    def capture(store, token, context):
        with store.transaction() as tx:
            preparation_id = tx.execute(
                "SELECT preparation_id FROM context_runs WHERE repository_id=? AND run_id=?",
                (token.repository_id, token.run_id),
            ).fetchone()[0]
            retained = json.dumps({
                "schema": "ffs.input-snapshot/v1",
                "upstream": {
                    "project": "fixture-project",
                    "workstream": "feature-014",
                    "session_key": "fixture-session",
                },
            }, sort_keys=True, separators=(",", ":"))
            tx.execute(
                "UPDATE context_workspaces SET selected_manifest_json=? WHERE preparation_id=?",
                (retained, preparation_id),
            )
        body(store, token, context, inspect_workspace(store, preparation_id))
        return 0

    assert _cmd_fixture_start(_args(tmp_path / "authority"), on_ready=capture) == 0


def test_gsd_auto_chain_flag_write_does_not_enter_wave_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _repository(tmp_path)
    base = _gsd_json(_gsd_config(), suffix="\n")
    _track_config(primary, base)

    def body(store, token, context, preparation) -> None:
        parent = Path(token.workspace)
        head = git(parent, "rev-parse", "HEAD")
        manifest = wave_manifest(token, context, head)
        clean = capture_wave_snapshot(store, token, preparation, manifest, tmp_path / "clean")
        _gsd_sets_flag(parent)

        snapshot = capture_wave_snapshot(store, token, preparation, manifest, tmp_path / "flagged")

        assert snapshot.manifest["entries"] == clean.manifest["entries"] == []
        assert snapshot.input_digest == clean.input_digest
        assert (parent / _CONFIG).read_bytes() == base

    _with_orchestrator(tmp_path, monkeypatch, primary, body)


def test_gsd_auto_chain_flag_write_leaves_real_wave_changes_in_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _repository(tmp_path)
    base = _gsd_json(_gsd_config())
    _track_config(primary, base)

    def body(store, token, context, preparation) -> None:
        parent = Path(token.workspace)
        (parent / "src/input.txt").write_bytes(b"accepted wave one\n")
        head = git(parent, "rev-parse", "HEAD")
        manifest = wave_manifest(token, context, head)
        clean = capture_wave_snapshot(store, token, preparation, manifest, tmp_path / "clean")
        _gsd_sets_flag(parent)
        flagged = capture_wave_snapshot(store, token, preparation, manifest, tmp_path / "flagged")

        assert [entry["path"] for entry in flagged.manifest["entries"]] == ["src/input.txt"]
        assert flagged.manifest["entries"] == clean.manifest["entries"]
        assert flagged.input_digest == clean.input_digest
        assert (parent / _CONFIG).read_bytes() == base

    _with_orchestrator(tmp_path, monkeypatch, primary, body)


def test_gsd_auto_chain_flag_write_does_not_enter_prelaunch_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from run_state.wave_execution import capture_prelaunch_snapshot

    primary = _repository(tmp_path)
    base = _gsd_json(_gsd_config())
    _track_config(primary, base)

    def body(store, token, context, preparation) -> None:
        parent = Path(token.workspace)

        def capture(label: str):
            return capture_prelaunch_snapshot(
                store, token, preparation, activity_id=context.activity_id,
                runtime_identity="runtime-fixture", evidence_root=tmp_path / label,
            )

        clean = capture("clean")
        _gsd_sets_flag(parent)

        snapshot = capture("flagged")

        assert snapshot.manifest["entries"] == clean.manifest["entries"] == []
        assert snapshot.input_digest == clean.input_digest
        assert (parent / _CONFIG).read_bytes() == base

    _with_orchestrator(tmp_path, monkeypatch, primary, body)
