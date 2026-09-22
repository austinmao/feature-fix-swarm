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
        ["rtk", "proxy", "git", *args], cwd=path, check=True,
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
    subprocess.run(["rtk", "proxy", "git", "clone", "-q", str(repository), str(applied)], check=True)
    subprocess.run(
        ["rtk", "proxy", "git", "apply", "--binary", str(result.evidence_path)],
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
        ["rtk", "proxy", "git", "apply", "--binary", str(result.evidence_path)],
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
