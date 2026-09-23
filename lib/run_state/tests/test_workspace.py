"""Real Git workspace readiness and compensation acceptance contract."""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import textwrap
import time

import pytest

LIB = Path(__file__).resolve().parents[2]


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "primary"
    root.mkdir()
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "workspace@example.test", cwd=root)
    _git("config", "user.name", "Workspace Fixture", cwd=root)
    (root / "tracked.txt").write_text("base\n")
    _git("add", "tracked.txt", cwd=root)
    _git("commit", "-qm", "base", cwd=root)
    return root


def _env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    for key in ("FFS_RUN_ID", "GSD_RUN_ID", "GSD_RESUME", "GSD_WORKSTREAM",
                "GSD_PROJECT", "GSD_SESSION_KEY", "CLAUDE_CONFIG_DIR", "GSD_HOME"):
        env.pop(key, None)
    home = tmp_path / "home"
    (home / "tmp").mkdir(parents=True)
    env.update(HOME=str(home), TMPDIR=str(home / "tmp"),
               PYTHONPATH=f"{LIB}:{env.get('PYTHONPATH', '')}")
    return env


def _start(repo: Path, state_root: Path, run_id: str, env: dict[str, str]):
    return subprocess.run(
        [sys.executable, "-m", "run_state.cli", "start", "--skill", "fix",
         "--objective", f"workspace {run_id}", "--activity", "plan", "--run-id", run_id,
         "--json", "--state-root", str(state_root)],
        cwd=repo, env=env, capture_output=True, text=True,
    )


def _ref_names(repo: Path) -> list[str]:
    return _git("for-each-ref", "--format=%(refname)", "refs/heads", cwd=repo).stdout.splitlines()


def _worktree_record(repo: Path, workspace: Path) -> list[str]:
    records = _git("worktree", "list", "--porcelain", cwd=repo).stdout.strip().split("\n\n")
    matches = [
        record.splitlines()
        for record in records
        if record.splitlines() and record.splitlines()[0] == f"worktree {workspace}"
    ]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.parametrize(
    ("window", "patched_name"),
    [
        ("context-committed-before-preparation", "begin_workspace_preparation"),
        ("preparation-committed-before-git", "prepare_workspace"),
    ],
)
def test_resume_recovers_real_pre_git_crash_without_duplicate_allocation(
    tmp_path: Path, window: str, patched_name: str,
) -> None:
    from run_context import repository_identity, resolve_repository
    from run_state.ownership import ControlStore

    primary = _repo(tmp_path)
    state_root = tmp_path / "authority"
    run_id = f"pre-git-{window}"
    objective = f"recover {window}"
    env = _env(tmp_path)
    crash_program = textwrap.dedent(
        """
        import json, os, signal, sys
        import run_state.workspace as workspace

        patched_name, state_root, run_id, objective = sys.argv[1:]
        def crash_at_boundary(*args, **kwargs):
            print(json.dumps({"boundary": patched_name, "pid": os.getpid()}), flush=True)
            os.kill(os.getpid(), signal.SIGKILL)
        setattr(workspace, patched_name, crash_at_boundary)
        from run_state.cli import main
        raise SystemExit(main([
            "start", "--skill", "fix", "--objective", objective,
            "--activity", "plan", "--run-id", run_id, "--json",
            "--state-root", state_root,
        ]))
        """
    )
    crashed = subprocess.run(
        [sys.executable, "-c", crash_program, patched_name, str(state_root), run_id, objective],
        cwd=primary, env=env, capture_output=True, text=True, timeout=20,
    )
    assert crashed.returncode == -signal.SIGKILL
    barrier = json.loads(crashed.stdout)
    assert barrier["boundary"] == patched_name
    assert isinstance(barrier["pid"], int) and barrier["pid"] > 0

    workspace_root = primary.parent / ".ffs-workspaces"
    assert not list(workspace_root.glob(f"*/{run_id}"))
    assert f"refs/heads/ffs/runs/{run_id}" not in _git(
        "for-each-ref", "--format=%(refname)", "refs/heads/ffs/runs", cwd=primary,
    ).stdout.splitlines()

    repository_id = repository_identity(resolve_repository(primary))
    store = ControlStore(state_root / "control.sqlite3")
    control_before = store.get_run_control(run_id, repository_id=repository_id)
    events_before = list(store.enumerate_events(
        run_id=run_id, repository_id=repository_id,
    ))
    refs_before = _ref_names(primary)
    worktrees_before = _git("worktree", "list", "--porcelain", cwd=primary).stdout
    refused = subprocess.run(
        [sys.executable, "-m", "run_state.cli", "start", "--skill", "fix",
         "--objective", objective, "--activity", "plan", "--run-id", run_id,
         "--json", "--state-root", str(state_root)],
        cwd=primary, env=env, capture_output=True, text=True, timeout=20,
    )
    assert refused.returncode != 0
    assert json.loads(refused.stdout)["code"] == "RESUME_REQUIRED"
    assert store.get_run_control(run_id, repository_id=repository_id) == control_before
    assert list(store.enumerate_events(
        run_id=run_id, repository_id=repository_id,
    )) == events_before
    assert _ref_names(primary) == refs_before
    assert _git("worktree", "list", "--porcelain", cwd=primary).stdout == worktrees_before

    competing = subprocess.run(
        [sys.executable, "-m", "run_state.cli", "start", "--skill", "fix",
         "--objective", objective, "--activity", "plan", "--run-id", f"{run_id}-other",
         "--json", "--state-root", str(state_root)],
        cwd=primary, env=env, capture_output=True, text=True, timeout=20,
    )
    assert competing.returncode != 0
    assert json.loads(competing.stdout)["code"] == "OBJECTIVE_RESERVED"

    resumed = subprocess.run(
        [sys.executable, "-m", "run_state.cli", "start", "--skill", "fix",
         "--objective", objective, "--activity", "plan", "--run-id", run_id,
         "--resume", "--json", "--state-root", str(state_root)],
        cwd=primary, env=env, capture_output=True, text=True, timeout=30,
    )
    assert resumed.returncode == 0, resumed.stderr or resumed.stdout
    ready = json.loads(resumed.stdout)
    workspace = Path(ready["workspace"])
    assert ready["code"] == "RUN_READY" and ready["ready"] is True
    assert workspace.is_dir()
    refs = _git(
        "for-each-ref", "--format=%(refname)", "refs/heads/ffs/runs", cwd=primary,
    ).stdout.splitlines()
    assert refs.count(f"refs/heads/ffs/runs/{run_id}") == 1
    porcelain = _git("worktree", "list", "--porcelain", cwd=primary).stdout
    assert porcelain.count(f"worktree {workspace}\n") == 1


def test_ready_workspace_is_registered_distinct_and_preserves_primary_sentinels(tmp_path: Path) -> None:
    primary = _repo(tmp_path)
    sentinel = primary / ".planning" / "fixture-sentinel"
    sentinel.parent.mkdir()
    sentinel.write_text("do-not-copy-or-change\n")
    dirty = primary / "tracked.txt"
    dirty.write_text("dirty primary sentinel\n")
    before_status = _git("status", "--porcelain=v1", cwd=primary).stdout

    result = _start(primary, tmp_path / "authority", "ready-workspace", _env(tmp_path))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    workspace = Path(payload["workspace"])
    assert workspace != primary and workspace.exists()
    assert (workspace / "tracked.txt").read_text() == "base\n"
    assert not (workspace / ".planning" / "fixture-sentinel").exists()
    assert sentinel.read_text() == "do-not-copy-or-change\n"
    assert dirty.read_text() == "dirty primary sentinel\n"
    assert _git("status", "--porcelain=v1", cwd=primary).stdout == before_status
    assert payload["workspace_state"] == "ready"
    assert payload["selected_input_count"] == 0
    assert len(payload["selected_input_manifest_hash"]) == 64
    int(payload["selected_input_manifest_hash"], 16)


def test_prepare_failure_retains_owned_manifest_and_unrelated_branch(tmp_path: Path) -> None:
    primary = _repo(tmp_path)
    run_id = "occupied-branch"
    _git("branch", f"ffs/runs/{run_id}", cwd=primary)
    branch_before = _git("rev-parse", f"ffs/runs/{run_id}", cwd=primary).stdout
    result = _start(primary, tmp_path / "authority", run_id, _env(tmp_path))
    assert result.returncode == 5
    refusal = json.loads(result.stdout)
    assert refusal["ok"] is False
    assert refusal["code"] == "WORKSPACE_PREPARE_FAILED"
    assert refusal["workspace_state"] == "blocked"
    manifest = Path(refusal["owned_resource_manifest"])
    assert manifest.is_file()
    owned = json.loads(manifest.read_text())
    assert owned["run_id"] == run_id
    assert owned["branch"] == f"ffs/runs/{run_id}"
    assert owned["created"] is False
    assert _git("rev-parse", f"ffs/runs/{run_id}", cwd=primary).stdout == branch_before


def test_state_root_inside_repository_is_rejected_before_creation(tmp_path: Path) -> None:
    primary = _repo(tmp_path)
    state_root = primary / ".private-authority"
    result = _start(primary, state_root, "unsafe-state-root", _env(tmp_path))
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["code"] == "UNSAFE_STATE_ROOT"
    assert not state_root.exists()


def test_symlink_state_root_is_rejected_without_touching_target(tmp_path: Path) -> None:
    primary = _repo(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    marker = target / "marker"
    marker.write_text("unchanged\n")
    link = tmp_path / "authority-link"
    link.symlink_to(target, target_is_directory=True)
    result = _start(primary, link, "symlink-root", _env(tmp_path))
    assert result.returncode == 2
    assert json.loads(result.stdout)["code"] == "UNSAFE_STATE_ROOT"
    assert marker.read_text() == "unchanged\n"
    assert list(target.iterdir()) == [marker]


def test_casefold_run_id_cannot_alias_registered_ready_workspace_or_ref(
    tmp_path: Path,
) -> None:
    primary = _repo(tmp_path)
    state_root = tmp_path / "authority"
    env = _env(tmp_path)
    first = _start(primary, state_root, "CaseFoldRun", env)
    assert first.returncode == 0, first.stderr
    ready = json.loads(first.stdout)
    workspace = Path(ready["workspace"])
    assert not any(
        line == "locked" or line.startswith("locked ")
        for line in _worktree_record(primary, workspace)
    )
    before_worktrees = _git("worktree", "list", "--porcelain", cwd=primary).stdout
    before_refs = _ref_names(primary)
    assert "refs/heads/ffs/runs/CaseFoldRun" in before_refs

    aliased = _start(primary, state_root, "casefoldrun", env)
    assert aliased.returncode == 3
    refusal = json.loads(aliased.stdout)
    assert refusal["ok"] is False
    assert refusal["code"] == "WORKSPACE_REGISTERED"
    assert _git("worktree", "list", "--porcelain", cwd=primary).stdout == before_worktrees
    assert _ref_names(primary) == before_refs
    assert "refs/heads/ffs/runs/casefoldrun" not in before_refs


def test_casefold_run_id_cannot_bypass_existing_packed_ref(tmp_path: Path) -> None:
    primary = _repo(tmp_path)
    _git("branch", "ffs/runs/PackedCase", cwd=primary)
    _git("pack-refs", "--all", "--prune", cwd=primary)
    git_dir = Path(_git("rev-parse", "--absolute-git-dir", cwd=primary).stdout.strip())
    packed = git_dir / "packed-refs"
    packed_before = packed.read_bytes()
    assert b"refs/heads/ffs/runs/PackedCase" in packed_before
    assert not (git_dir / "refs" / "heads" / "ffs" / "runs" / "PackedCase").exists()
    refs_before = _ref_names(primary)
    worktrees_before = _git("worktree", "list", "--porcelain", cwd=primary).stdout

    result = _start(primary, tmp_path / "authority", "packedcase", _env(tmp_path))
    assert result.returncode == 5
    refusal = json.loads(result.stdout)
    assert refusal["ok"] is False
    assert refusal["code"] == "WORKSPACE_PREPARE_FAILED"
    assert packed.read_bytes() == packed_before
    assert _ref_names(primary) == refs_before
    assert "refs/heads/ffs/runs/packedcase" not in refs_before
    assert _git("worktree", "list", "--porcelain", cwd=primary).stdout == worktrees_before


def _reserved_workspace(tmp_path: Path, primary: Path, run_id: str):
    from run_state.ownership import ControlStore, ProcessIdentity, StartRequest, reserve_resources

    workspace = tmp_path / f"workspace-{run_id}"
    store = ControlStore(tmp_path / "authority" / "control.sqlite3")
    owned = reserve_resources(store, StartRequest(
        run_id, str(workspace), f"objective-{run_id}", ProcessIdentity.current(),
        repository_id="fixture-repository", planning_scope="fixture-scope",
    ))
    return store, owned, workspace


def test_interrupted_git_before_ready_is_recovered_without_new_allocation(tmp_path: Path) -> None:
    from run_state.ownership import ControlStore
    from run_state.workspace import (
        begin_workspace_preparation, inspect_workspace, recover_workspace_preparation,
    )

    primary = _repo(tmp_path)
    store, owned, workspace = _reserved_workspace(tmp_path, primary, "interrupted")
    base = _git("rev-parse", "HEAD", cwd=primary).stdout.strip()
    preparation = begin_workspace_preparation(
        store, owned.token, run_id="interrupted", workspace=workspace,
        branch="ffs/runs/interrupted", base_commit=base,
        selected_input_manifest={"entries": []}, repository_path=primary,
    )
    assert preparation.id and preparation.id != owned.token.nonce
    assert preparation.path_existed_before is False
    assert preparation.branch_existed_before is False
    assert preparation.registered_before is False
    _git("worktree", "add", "--lock", "--reason", f"ffs-preparation:{preparation.id}",
         "-q", "-b", "ffs/runs/interrupted", str(workspace), base,
         cwd=primary)

    reopened = ControlStore(store.db_path)
    interrupted = inspect_workspace(reopened, preparation.id)
    assert interrupted.state == "preparing"
    assert interrupted.path == workspace
    recovered = recover_workspace_preparation(reopened, owned.token, preparation.id)
    assert recovered.state == "ready"
    assert recovered.path == workspace
    assert _git("worktree", "list", "--porcelain", cwd=primary).stdout.count(
        f"worktree {workspace}\n"
    ) == 1
    assert f"locked ffs-preparation:{preparation.id}" in _git(
        "worktree", "list", "--porcelain", cwd=primary,
    ).stdout


def test_distinct_owner_cannot_mutate_another_runs_workspace_preparation(
    tmp_path: Path,
) -> None:
    from run_state.ownership import (
        ControlStore, OwnershipRefused, ProcessIdentity, StartRequest,
        assert_owner, reserve_resources,
    )
    from run_state.workspace import (
        WorkspaceRefused, adopt_workspace_preparation_fence,
        begin_workspace_preparation, inspect_workspace, prepare_workspace,
        publish_workspace_ready, recover_workspace_preparation,
    )

    primary = _repo(tmp_path)
    db = tmp_path / "authority" / "control.sqlite3"
    store = ControlStore(db)
    base = _git("rev-parse", "HEAD", cwd=primary).stdout.strip()
    outcomes = {}
    for operation in ("begin", "adopt", "recover", "publish", "failure-block"):
        suffix = operation.replace("-", "")
        a_run = f"owner-a-{suffix}"
        b_run = f"owner-b-{suffix}"
        a_workspace = tmp_path / f"workspace-{a_run}"
        b_workspace = tmp_path / f"workspace-{b_run}"
        owner_a = reserve_resources(store, StartRequest(
            a_run, str(a_workspace), f"objective-{a_run}", ProcessIdentity.current(),
            repository_id="fixture-repository", planning_scope="fixture-scope",
        ))
        owner_b = reserve_resources(store, StartRequest(
            b_run, str(b_workspace), f"objective-{b_run}", ProcessIdentity.current(),
            repository_id="fixture-repository", planning_scope="fixture-scope",
        ))
        branch = f"ffs/runs/{a_run}"
        preparation = begin_workspace_preparation(
            store, owner_a.token, run_id=a_run, workspace=a_workspace,
            branch=branch, base_commit=base, selected_input_manifest={"entries": []},
            repository_path=primary,
        )
        _git(
            "worktree", "add", "--lock", "--reason",
            f"ffs-preparation:{preparation.id}", "-q", "-b", branch,
            str(a_workspace), base, cwd=primary,
        )
        before = inspect_workspace(store, preparation.id)
        before_events = list(ControlStore.open_read_only(db).enumerate_events())
        before_worktrees = _git("worktree", "list", "--porcelain", cwd=primary).stdout
        before_refs = _ref_names(primary)

        try:
            if operation == "begin":
                begin_workspace_preparation(
                    store, owner_b.token, run_id=a_run, workspace=a_workspace,
                    branch=branch, base_commit=base,
                    selected_input_manifest={"entries": []}, repository_path=primary,
                )
            elif operation == "adopt":
                adopt_workspace_preparation_fence(store, owner_b.token, preparation.id)
            elif operation == "recover":
                recover_workspace_preparation(store, owner_b.token, preparation.id)
            elif operation == "publish":
                publish_workspace_ready(store, owner_b.token, preparation.id)
            else:
                prepare_workspace(store, owner_b.token, preparation)
            code = "NO_REFUSAL"
        except (OwnershipRefused, WorkspaceRefused) as error:
            code = error.code

        reservations_unchanged = True
        try:
            with store.transaction() as tx:
                assert_owner(tx, owner_a.token)
                assert_owner(tx, owner_b.token)
        except OwnershipRefused:
            reservations_unchanged = False
        outcomes[operation] = {
            "code": code,
            "preparation_unchanged": inspect_workspace(store, preparation.id) == before,
            "events_unchanged": list(
                ControlStore.open_read_only(db).enumerate_events()
            ) == before_events,
            "worktrees_unchanged": _git(
                "worktree", "list", "--porcelain", cwd=primary,
            ).stdout == before_worktrees,
            "refs_unchanged": _ref_names(primary) == before_refs,
            "reservations_unchanged": reservations_unchanged,
        }

    expected = {
        "code": "FENCE_REVOKED",
        "preparation_unchanged": True,
        "events_unchanged": True,
        "worktrees_unchanged": True,
        "refs_unchanged": True,
        "reservations_unchanged": True,
    }
    assert outcomes == {operation: expected for operation in outcomes}


def test_cli_sigkill_orphan_git_child_retains_admin_lock_and_resume_adopts_once(
    tmp_path: Path,
) -> None:
    from process_identity import LIVE, ProcessIdentity, probe_identity

    primary = _repo(tmp_path)
    state_root = tmp_path / "authority"
    run_id = "orphan-lock"
    barrier = tmp_path / "git-child-ready.json"
    release = tmp_path / "release-git-child"
    wrapper_dir = tmp_path / "git-wrapper"
    wrapper_dir.mkdir()
    real_git = shutil.which("git")
    assert real_git is not None and Path(real_git).is_absolute()
    wrapper = wrapper_dir / "git"
    wrapper_trace = tmp_path / "git-wrapper-invocations.jsonl"
    wrapper.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json, os, sys, time
            from pathlib import Path

            real_git = {real_git!r}
            arguments = sys.argv[1:]
            with Path({str(wrapper_trace)!r}).open("a", encoding="utf-8") as trace:
                trace.write(json.dumps({{
                    "argv": arguments,
                    "PATH": os.environ.get("PATH"),
                    "barrier_env_present": "FFS_TEST_GIT_CHILD_BARRIER" in os.environ,
                    "release_env_present": "FFS_TEST_GIT_CHILD_RELEASE" in os.environ,
                }}) + "\\n")
            if "worktree" in arguments and "add" in arguments and "ffs/runs/{run_id}" in arguments:
                barrier = Path(os.environ["FFS_TEST_GIT_CHILD_BARRIER"])
                release = Path(os.environ["FFS_TEST_GIT_CHILD_RELEASE"])
                barrier.write_text(json.dumps({{"pid": os.getpid()}}))
                deadline = time.monotonic() + 30
                while not release.exists():
                    if time.monotonic() >= deadline:
                        raise SystemExit(124)
                    time.sleep(0.01)
            os.execv(real_git, [real_git, *arguments])
            """
        )
    )
    wrapper.chmod(0o700)
    first_env = _env(tmp_path / "first-env")
    first_env.update(
        PATH=f"{wrapper_dir}:{first_env['PATH']}",
        FFS_TEST_GIT_CHILD_BARRIER=str(barrier),
        FFS_TEST_GIT_CHILD_RELEASE=str(release),
    )
    # The native policy permits the pinned Python interpreter, not executable
    # files generated inside the fixture. Preserve the real CLI/Git process
    # boundary while invoking this one test wrapper through that interpreter.
    bootstrap = textwrap.dedent(
        f"""\
        import json, os, runpy, shutil, stat, subprocess, sys
        from pathlib import Path
        import run_state.workspace as workspace

        fixture_wrapper = Path({str(wrapper)!r})
        bootstrap_trace = Path({str(tmp_path / "bootstrap-popen.jsonl")!r})
        def record(value):
            with bootstrap_trace.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(value) + "\\n")
        real_popen = subprocess.Popen
        def fixture_popen(argv, *args, **kwargs):
            environment = kwargs.get("env") or os.environ
            record({{"event": "entered", "argv_type": type(argv).__name__,
                    "argv": argv, "env_path": environment.get("PATH")}})
            resolved = None
            rewritten = False
            if isinstance(argv, (list, tuple)) and argv and argv[0] == "git":
                resolved = shutil.which("git", path=environment.get("PATH"))
                # X_OK is denied for generated scripts by the native policy.
                # Match only this fixture's explicit first PATH entry; the
                # executable is still the separately admitted interpreter.
                first_directory = environment.get("PATH", "").split(os.pathsep)[0]
                if first_directory == str(fixture_wrapper.parent):
                    info = fixture_wrapper.lstat()
                    assert stat.S_ISREG(info.st_mode) and info.st_mode & stat.S_IXUSR
                    argv = [sys.executable, str(fixture_wrapper), *argv[1:]]
                    rewritten = True
            record({{"event": "dispatch", "resolved_git": resolved,
                    "rewrite": rewritten, "argv": argv}})
            try:
                child = real_popen(argv, *args, **kwargs)
            except OSError as error:
                record({{"event": "raised", "exception_type": type(error).__name__,
                        "errno": error.errno, "filename": error.filename}})
                raise
            record({{"event": "started", "pid": child.pid}})
            return child
        workspace.subprocess.Popen = fixture_popen
        subprocess.Popen = fixture_popen
        sys.argv = ["run_state.cli", *sys.argv[1:]]
        runpy.run_module("run_state.cli", run_name="__main__")
        """
    )
    start = [
        sys.executable, "-c", bootstrap, "start", "--skill", "fix",
        "--objective", "orphan Git child recovery", "--activity", "plan",
        "--run-id", run_id, "--scope", "orphan-scope", "--json",
        "--state-root", str(state_root),
    ]
    launch_trace = {
        "argv": start,
        "PATH": first_env.get("PATH"),
        "barrier_env_present": "FFS_TEST_GIT_CHILD_BARRIER" in first_env,
        "release_env_present": "FFS_TEST_GIT_CHILD_RELEASE" in first_env,
    }
    first = subprocess.Popen(
        start, cwd=primary, env=first_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    competitor = None
    git_child = None
    try:
        deadline = time.monotonic() + 10
        while not barrier.exists() and first.poll() is None:
            assert time.monotonic() < deadline, "Git-child barrier deadline exceeded"
            time.sleep(0.01)
        if not barrier.exists():
            stdout, stderr = first.communicate(timeout=5)
            raise AssertionError(
                "CLI exited before the Git-child barrier: "
                f"returncode={first.returncode}; stdout={stdout!r}; stderr={stderr!r}; "
                f"launch_trace={launch_trace!r}; "
                f"wrapper_trace={wrapper_trace.read_text() if wrapper_trace.exists() else 'MISSING'!r}; "
                f"bootstrap_trace={(tmp_path / 'bootstrap-popen.jsonl').read_text() if (tmp_path / 'bootstrap-popen.jsonl').exists() else 'MISSING'!r}"
            )
        git_child = ProcessIdentity.from_pid(json.loads(barrier.read_text())["pid"])
        assert probe_identity(git_child) == LIVE

        os.kill(first.pid, signal.SIGKILL)
        assert first.wait(timeout=10) == -signal.SIGKILL
        assert probe_identity(git_child) == LIVE

        common_raw = _git("rev-parse", "--git-common-dir", cwd=primary).stdout.strip()
        common_dir = Path(common_raw)
        if not common_dir.is_absolute():
            common_dir = (primary / common_dir).resolve()
        competitor_program = textwrap.dedent(
            """
            import json, subprocess, sys
            from pathlib import Path
            from run_context import git_admin_lock

            common_dir, repository, git = map(Path, sys.argv[1:])
            print(json.dumps({"status": "attempting"}), flush=True)
            with git_admin_lock(common_dir, timeout=10):
                subprocess.run(
                    [str(git), "branch", "admin-lock-probe"], cwd=repository,
                    check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                print(json.dumps({"status": "mutated"}), flush=True)
            """
        )
        competitor = subprocess.Popen(
            [sys.executable, "-c", competitor_program, str(common_dir),
             str(primary), real_git],
            env=_env(tmp_path / "competitor-env"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert competitor.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(competitor.stdout, selectors.EVENT_READ)
            assert selector.select(5), "competitor did not reach admin-lock attempt"
            assert json.loads(competitor.stdout.readline()) == {"status": "attempting"}
            assert not selector.select(0.25), "competing Git mutation bypassed inherited lock"
        assert competitor.poll() is None

        release.touch()
        with selectors.DefaultSelector() as selector:
            selector.register(competitor.stdout, selectors.EVENT_READ)
            assert selector.select(10), "competing Git mutation did not resume after release"
            assert json.loads(competitor.stdout.readline()) == {"status": "mutated"}
        assert competitor.wait(timeout=10) == 0
        assert "refs/heads/admin-lock-probe" in _ref_names(primary)

        child_deadline = time.monotonic() + 10
        while probe_identity(git_child) == LIVE and time.monotonic() < child_deadline:
            time.sleep(0.01)
        assert probe_identity(git_child) != LIVE

        inspected = subprocess.run(
            [sys.executable, "-m", "run_state.cli", "context", "--state-root",
             str(state_root), "--run-id", run_id, "--json"],
            cwd=primary, env=_env(tmp_path / "inspect-env"),
            capture_output=True, text=True, timeout=10,
        )
        assert inspected.returncode == 0, inspected.stderr
        interrupted = json.loads(inspected.stdout)
        assert interrupted["workspace_state"] == "preparing"
        original_generation = interrupted["generation"]
        allocated_workspace = Path(interrupted["workspace"])

        successor = subprocess.run(
            [*start, "--resume"], cwd=primary,
            env=_env(tmp_path / "successor-env"),
            capture_output=True, text=True, timeout=20,
        )
        assert successor.returncode == 0, successor.stderr
        resumed = json.loads(successor.stdout)
        assert resumed["code"] == "RUN_READY"
        assert resumed["workspace"] == str(allocated_workspace)
        assert resumed["generation"] > original_generation
        assert _git("worktree", "list", "--porcelain", cwd=primary).stdout.count(
            f"worktree {allocated_workspace}\n"
        ) == 1
        assert _ref_names(primary).count(f"refs/heads/ffs/runs/{run_id}") == 1
    finally:
        release.touch(exist_ok=True)
        if first.poll() is None:
            first.kill()
            first.wait(timeout=5)
        if competitor is not None and competitor.poll() is None:
            competitor.terminate()
            try:
                competitor.wait(timeout=5)
            except subprocess.TimeoutExpired:
                competitor.kill()
                competitor.wait(timeout=5)
        if git_child is not None and probe_identity(git_child) == LIVE:
            os.kill(git_child.pid, signal.SIGTERM)
            deadline = time.monotonic() + 5
            while probe_identity(git_child) == LIVE and time.monotonic() < deadline:
                time.sleep(0.01)
            if probe_identity(git_child) == LIVE:
                os.kill(git_child.pid, signal.SIGKILL)


def test_stale_fence_cannot_publish_real_registered_workspace_ready(tmp_path: Path) -> None:
    from run_state.ownership import OwnershipRefused, release_owner
    from run_state.workspace import (
        begin_workspace_preparation, inspect_workspace, publish_workspace_ready,
    )

    primary = _repo(tmp_path)
    store, owned, workspace = _reserved_workspace(tmp_path, primary, "stale-ready")
    base = _git("rev-parse", "HEAD", cwd=primary).stdout.strip()
    preparation = begin_workspace_preparation(
        store, owned.token, run_id="stale-ready", workspace=workspace,
        branch="ffs/runs/stale-ready", base_commit=base,
        selected_input_manifest={"entries": []}, repository_path=primary,
    )
    _git("worktree", "add", "--lock", "--reason", f"ffs-preparation:{preparation.id}",
         "-q", "-b", "ffs/runs/stale-ready", str(workspace), base,
         cwd=primary)
    with store.transaction() as tx:
        release_owner(tx, owned.token)
    with pytest.raises(OwnershipRefused) as rejected:
        publish_workspace_ready(store, owned.token, preparation.id)
    assert rejected.value.code == "FENCE_REVOKED"
    observed = inspect_workspace(store, preparation.id)
    assert observed.state == "preparing"
    assert observed.ready is False


@pytest.mark.parametrize("marker", ["absent", "mismatched"])
def test_recovery_rejects_and_preserves_unowned_same_layout_worktree(
    tmp_path: Path, marker: str,
) -> None:
    from run_state.workspace import (
        WorkspaceRefused, begin_workspace_preparation, inspect_workspace,
        recover_workspace_preparation,
    )

    primary = _repo(tmp_path)
    run_id = f"lookalike-{marker}"
    store, owned, workspace = _reserved_workspace(tmp_path, primary, run_id)
    base = _git("rev-parse", "HEAD", cwd=primary).stdout.strip()
    branch = f"ffs/runs/{run_id}"
    preparation = begin_workspace_preparation(
        store, owned.token, run_id=run_id, workspace=workspace, branch=branch,
        base_commit=base, selected_input_manifest={"entries": []},
        repository_path=primary,
    )
    command = ["worktree", "add"]
    if marker == "mismatched":
        command += ["--lock", "--reason", "ffs-preparation:different-id"]
    command += ["-q", "-b", branch, str(workspace), base]
    _git(*command, cwd=primary)
    dirty = workspace / "unowned-dirty-sentinel"
    dirty.write_text("preserve me\n")
    before = _git("worktree", "list", "--porcelain", cwd=primary).stdout

    with pytest.raises(WorkspaceRefused) as rejected:
        recover_workspace_preparation(store, owned.token, preparation.id)
    assert rejected.value.code == "WORKSPACE_OWNERSHIP_MISMATCH"
    assert workspace.is_dir()
    assert dirty.read_text() == "preserve me\n"
    assert _git("worktree", "list", "--porcelain", cwd=primary).stdout == before
    observed = inspect_workspace(store, preparation.id)
    assert observed.state == "blocked"
    assert observed.ready is False
