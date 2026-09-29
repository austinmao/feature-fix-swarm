"""F43: one private TMPDIR per managed Codex launch, outside every protected root.

A managed Codex orchestrator ran pytest inside its workspace-write sandbox.
TMPDIR sat in the runtime home, which the sandbox excluded, so Python fell back
to the cwd and wrote ``pytest-of-<user>/`` into the workspace; the candidate
chain then refused the self-referencing ``*current`` symlink.
"""
from __future__ import annotations

import contextlib
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

from host_capabilities import DISABLED_NATIVE_FEATURES, codex_environment_policy_hash
from process_identity import DEAD, ProcessIdentity, probe_identity
from run_state import codex_host
from run_state.codex_host import CodexHostAdapter, CodexHostRefused
from run_state.tests.test_codex_host import _runtime, _valid_stream


PRIVATE_ROOT = Path("/tmp").resolve()
requires_darwin_sandbox = pytest.mark.skipif(
    sys.platform != "darwin" or not os.path.isfile("/usr/bin/sandbox-exec"),
    reason="the sandboxed tempfile probe needs Darwin sandbox-exec",
)


def _adapter(tmp_path: Path, monkeypatch, **kwargs):
    runtime, binary, workspace = _runtime(tmp_path)
    monkeypatch.setattr(codex_host, "_binary_chain", lambda path: {"launcher_sha256": "a" * 64})
    return CodexHostAdapter(runtime, binary, "0.158.0", **kwargs), workspace


def _writable_roots(argv: tuple[str, ...]) -> list[str]:
    """The workspace-write roots the argv grants with both exclusions on."""
    roots = [argv[argv.index("--cd") + 1]]
    return roots + [argv[index + 1] for index, item in enumerate(argv) if item == "--add-dir"]


def _release_quietly(*materials) -> None:
    for material in materials:
        with contextlib.suppress(CodexHostRefused):
            CodexHostAdapter.release_launch_material(material)


@requires_darwin_sandbox
def test_sandboxed_tempfile_lands_in_the_private_tmpdir_and_the_workspace_stays_clean(tmp_path, monkeypatch):
    adapter, workspace = _adapter(tmp_path, monkeypatch)
    material = adapter.build_launch_material("do work", attempt=0)
    try:
        tmpdir = material.execution_environment()["TMPDIR"]
        assert "sandbox_workspace_write.exclude_slash_tmp=true" in material.argv
        assert "sandbox_workspace_write.exclude_tmpdir_env_var=true" in material.argv
        assert _writable_roots(material.argv) == [str(workspace), tmpdir]
        # Model the workspace-write sandbox from the real argv: writes are
        # denied everywhere except the granted roots, exactly as the orchestrator ran.
        rules = " ".join(f"(subpath {json.dumps(root)})" for root in _writable_roots(material.argv))
        profile = f'(version 1)(allow default)(deny file-write*)(allow file-write* {rules} (literal "/dev/null"))'
        probe = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", profile, "--", sys.executable, "-B", "-c",
             "import tempfile; print(tempfile.mkstemp()[1])"],
            cwd=material.cwd, env=material.execution_environment(),
            capture_output=True, text=True, timeout=60, check=False,
        )
        assert probe.returncode == 0, probe.stderr
        assert Path(probe.stdout.strip()).parent == Path(tmpdir)
        assert os.listdir(workspace) == []
    finally:
        _release_quietly(material)


def test_private_tmpdir_is_owned_0700_and_outside_every_protected_root(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    adapter, workspace = _adapter(tmp_path, monkeypatch, state_root=state)
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    material = adapter.build_launch_material("do work", attempt=0)
    try:
        private = Path(material.temporary_dir)
        info = private.lstat()
        assert private.parent == PRIVATE_ROOT and private.name.startswith("ffs-codex-")
        assert stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700
        assert info.st_uid == os.getuid()
        assert (info.st_dev, info.st_ino) == (material.temporary_device, material.temporary_inode)
        assert material.execution_environment()["TMPDIR"] == str(private)
        for protected in (workspace, workspace / ".git", state, tmp_path / "home", Path.home()):
            resolved = protected.resolve()
            assert not private.is_relative_to(resolved) and not resolved.is_relative_to(private)
    finally:
        _release_quietly(material)


@pytest.mark.parametrize("target", ["workspace", "common-git-dir", "state", "user-home", "runtime-home"])
def test_private_root_overlapping_a_protected_root_is_refused(tmp_path, monkeypatch, target):
    state, user_home = tmp_path / "state", tmp_path / "user-home"
    state.mkdir()
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    adapter, workspace = _adapter(tmp_path, monkeypatch, state_root=state)
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    root = {"workspace": workspace, "common-git-dir": workspace / ".git", "state": state,
            "user-home": user_home, "runtime-home": tmp_path / "home"}[target]
    before = sorted(os.listdir(root))
    monkeypatch.setattr(codex_host, "codex_private_tmp_root", lambda: root)
    with pytest.raises(CodexHostRefused, match="PRIVATE_TMPDIR_UNSAFE"):
        adapter.build_launch_material("do work", attempt=0)
    assert sorted(os.listdir(root)) == before


def test_private_root_overlap_is_also_detected_by_filesystem_identity(tmp_path, monkeypatch):
    adapter, workspace = _adapter(tmp_path, monkeypatch)
    alias = Path(str(workspace).swapcase())
    # A case-insensitive volume names the workspace under another spelling;
    # only (st_dev, st_ino) proves the two paths are one directory.
    root = alias if alias.exists() else workspace
    monkeypatch.setattr(codex_host, "codex_private_tmp_root", lambda: root)
    with pytest.raises(CodexHostRefused, match="PRIVATE_TMPDIR_UNSAFE"):
        adapter.build_launch_material("do work", attempt=0)
    assert os.listdir(workspace) == []


def test_release_removes_the_private_tmpdir_and_its_durable_record(tmp_path, monkeypatch):
    records = tmp_path / "records"
    adapter, _workspace = _adapter(tmp_path, monkeypatch, tmp_records=records, activity_id="activity-1")
    material = adapter.build_launch_material("do work", attempt=0)
    private = Path(material.temporary_dir)
    try:
        record_path = records / (private.name + ".json")
        record = json.loads(record_path.read_text())
        info = private.lstat()
        assert stat.S_IMODE(record_path.lstat().st_mode) == 0o600
        assert record["path"] == str(private)
        assert (record["device"], record["inode"]) == (info.st_dev, info.st_ino)
        assert record["activity_id"] == "activity-1"
        assert record["owner"] == asdict(ProcessIdentity.current())
        CodexHostAdapter.release_launch_material(material)
        assert not os.path.lexists(private)
        assert list(records.iterdir()) == []
    finally:
        _release_quietly(material)


def test_retained_observation_bound_to_the_runtime_home_is_refused_typed(tmp_path, monkeypatch):
    records = tmp_path / "records"
    adapter, _workspace = _adapter(tmp_path, monkeypatch, tmp_records=records, activity_id="old")
    home = tmp_path / "home"
    old_policy = codex_host.codex_closed_environment(
        home, home / "ffs-codex-policy-tmp", adapter.binary, {"launcher_sha256": "a" * 64},
    )
    old = replace(adapter.runtime, observation=(("environment_sha256", codex_environment_policy_hash(old_policy)),))
    with pytest.raises(CodexHostRefused, match="ENVIRONMENT_POLICY_DRIFT"):
        CodexHostAdapter(old, adapter.binary, "0.158.0", tmp_records=records,
                         activity_id="old").build_launch_material("do work", attempt=0)
    assert list(records.iterdir()) == []


def test_release_refuses_material_whose_tmpdir_sits_in_the_runtime_home(tmp_path, monkeypatch):
    adapter, _workspace = _adapter(tmp_path, monkeypatch)
    material = adapter.build_launch_material("do work", attempt=0)
    legacy = tmp_path / "home" / "ffs-codex-legacy"
    legacy.mkdir(mode=0o700)
    info = legacy.lstat()
    try:
        old = replace(material, temporary_dir=str(legacy), temporary_device=info.st_dev,
                      temporary_inode=info.st_ino)
        with pytest.raises(CodexHostRefused, match="LAUNCH_MATERIAL_INVALID"):
            CodexHostAdapter.release_launch_material(old)
        assert legacy.is_dir()
    finally:
        _release_quietly(material)


def test_observer_and_adapter_bind_the_same_private_tmpdir_policy(tmp_path, monkeypatch):
    from test_codex_runtime_observer import _runtime as observer_runtime, observer
    runtime, binary = observer_runtime(tmp_path)
    runtime.chmod(0o700)
    (runtime / "auth.json").write_text("{}\n")
    (runtime / "auth.json").chmod(0o600)
    worktree = (tmp_path / "work").resolve()
    worktree.mkdir()
    seed = observer.prepare_qualification_seed(runtime)
    predicted = observer.preview_qualification_runtime(
        seed, runtime, binary, worktree, model="gpt-5.6-sol", effort="xhigh", roots=[str(worktree)],
    )
    assert dict(predicted.execution)["disabled_features"] == list(DISABLED_NATIVE_FEATURES)
    monkeypatch.setattr(codex_host, "_binary_chain", lambda path: dict(predicted.binary))
    material = CodexHostAdapter(predicted, binary, "0.158.0").build_launch_material("do work", attempt=0)
    try:
        assert Path(material.execution_environment()["TMPDIR"]).parent == PRIVATE_ROOT
    finally:
        _release_quietly(material)


def _dead_identity() -> ProcessIdentity:
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        identity = ProcessIdentity.from_pid(process.pid)
    finally:
        process.kill()
        process.wait()
    assert probe_identity(identity) == DEAD
    return identity


def _set_owner(records: Path, material, owner: ProcessIdentity) -> None:
    path = records / (Path(material.temporary_dir).name + ".json")
    record = json.loads(path.read_text())
    record["owner"] = asdict(owner)
    path.write_text(json.dumps(record))


def test_resume_reaps_only_recorded_dirs_whose_owner_and_launch_are_provably_dead(tmp_path, monkeypatch):
    records = tmp_path / "records"
    adapter, _workspace = _adapter(tmp_path, monkeypatch, tmp_records=records, activity_id="dead-launch")
    running_adapter = CodexHostAdapter(adapter.runtime, adapter.binary, "0.158.0",
                                       tmp_records=records, activity_id="running-launch")
    dead = adapter.build_launch_material("dead", attempt=0)
    live_owner = adapter.build_launch_material("live owner", attempt=1)
    running = running_adapter.build_launch_material("running", attempt=0)
    replaced = adapter.build_launch_material("replaced", attempt=2)
    owner = _dead_identity()
    for material in (dead, running, replaced):
        _set_owner(records, material, owner)
    # Keep the recorded inode alive while a new directory takes its name, so
    # the replacement cannot reuse the recorded identity.
    replaced_path = Path(replaced.temporary_dir)
    aside = replaced_path.with_name(replaced_path.name + "-aside")
    os.rename(replaced_path, aside)
    replaced_path.mkdir(mode=0o700)
    foreign = Path(tempfile.mkdtemp(prefix="ffs-codex-", dir=PRIVATE_ROOT))
    try:
        reaped = codex_host.reap_orphan_private_tmpdirs(records, lambda activity: activity == "dead-launch")
        assert reaped == (dead.temporary_dir,)
        assert not os.path.lexists(dead.temporary_dir)
        for kept in (live_owner.temporary_dir, running.temporary_dir, replaced_path, foreign, aside):
            assert os.path.isdir(kept)
        assert sorted(path.name for path in records.iterdir()) == sorted(
            Path(material.temporary_dir).name + ".json" for material in (live_owner, running)
        )
    finally:
        _release_quietly(live_owner, running)
        for path in (dead.temporary_dir, replaced_path, foreign, aside):
            shutil.rmtree(path, ignore_errors=True)


def test_launch_is_provably_dead_only_when_every_intent_child_is_dead():
    from run_state.supervisor import _launches_provably_dead

    dead, live = _dead_identity(), ProcessIdentity.current()

    def store(*children):
        rows = [{"child_host_id": None, "child_boot_id": None, "child_pid": None, "child_start_token": None}
                if child is None else {"child_host_id": child.host_id, "child_boot_id": child.boot_id,
                                       "child_pid": child.pid, "child_start_token": child.start_token}
                for child in children]
        cursor = SimpleNamespace(fetchall=lambda: rows)
        return SimpleNamespace(read_transaction=lambda: contextlib.nullcontext(
            SimpleNamespace(execute=lambda _sql, _params: cursor)))

    assert _launches_provably_dead(store(), "never-launched") is True
    assert _launches_provably_dead(store(dead, dead), "settled") is True
    assert _launches_provably_dead(store(dead, live), "running") is False
    assert _launches_provably_dead(store(dead, None), "unacknowledged") is False


def test_supervisor_finish_releases_a_settled_worker_launch(tmp_path, monkeypatch):
    from run_state.supervisor import ProcessHandle, Supervisor

    records = tmp_path / "records"
    adapter, _workspace = _adapter(tmp_path, monkeypatch, tmp_records=records, activity_id="worker-1")
    material = adapter.build_launch_material("plan prompt", attempt=1)
    try:
        # The guarded exec revokes the credential copy before a receipt settles.
        Path(material.auth_path).unlink()
        evidence = tmp_path / "evidence" / "intent-1"
        evidence.mkdir(parents=True, mode=0o700)
        stdout, stderr = evidence / "stdout.log", evidence / "stderr.log"
        stdout.write_bytes(_valid_stream())
        stderr.write_bytes(b"")
        identities = {key: (path.stat().st_dev, path.stat().st_ino)
                      for key, path in (("stdout", stdout), ("stderr", stderr))}
        process = subprocess.Popen([sys.executable, "-c", ""])
        process.wait()
        handle = ProcessHandle(process, "intent-1", "worker-1", ProcessIdentity.current(), "head", 0.0,
                               stdout, stderr, identities, codex_material=material)
        completed = []
        store = SimpleNamespace(complete_launch=lambda intent_id, _token, **_kwargs: completed.append(intent_id))
        supervisor = Supervisor(store, SimpleNamespace(), evidence_root=tmp_path / "evidence")
        supervisor._handles[handle.intent_id] = handle
        monkeypatch.setattr(supervisor, "_wait_admitted", lambda owned, _timeout: owned.process.wait())
        supervisor.finish(handle)
        assert completed == ["intent-1"]
        assert not os.path.lexists(material.temporary_dir)
        assert list(records.iterdir()) == []
    finally:
        _release_quietly(material)
