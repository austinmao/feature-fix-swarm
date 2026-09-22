"""Independent public run-context and real-worktree acceptance oracles."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "lib"
sys.path.insert(0, str(LIB))
CONTEXT_KEYS = (
    "FFS_RUN_ID", "GSD_RUN_ID", "GSD_RESUME", "GSD_WORKSTREAM",
    "GSD_PROJECT", "GSD_SESSION_KEY", "CLAUDE_CONFIG_DIR", "GSD_HOME",
)


def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["rtk", "proxy", "git", *args], cwd=cwd, check=check,
                          capture_output=True, text=True)


def _repo(tmp_path: Path, name: str = "primary") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "context@example.test", cwd=repo)
    _git("config", "user.name", "Context Fixture", cwd=repo)
    (repo / "tracked.txt").write_text("base\n")
    _git("add", "tracked.txt", cwd=repo)
    _git("commit", "-qm", "base", cwd=repo)
    return repo


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    for key in CONTEXT_KEYS:
        env.pop(key, None)
    home = tmp_path / "home"
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    env.update(HOME=str(home), TMPDIR=str(home / "tmp"),
               PYTHONPATH=f"{LIB}:{env.get('PYTHONPATH', '')}")
    env.update(extra)
    return env


def _cli(state_root: Path, cwd: Path, *args: str,
         env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "run_state.cli", *args, "--state-root", str(state_root)],
        cwd=cwd, env=env, capture_output=True, text=True,
    )


def _start(state_root: Path, cwd: Path, run_id: str, *, env: dict[str, str]):
    return _cli(state_root, cwd, "start", "--skill", "fix", "--objective",
                f"objective {run_id}", "--activity", "plan", "--run-id", run_id, "--json",
                env=env)


def _start_request(
    state_root: Path,
    cwd: Path,
    run_id: str,
    *,
    objective: str,
    scope: str,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return _cli(
        state_root,
        cwd,
        "start",
        "--skill",
        "fix",
        "--objective",
        objective,
        "--activity",
        "plan",
        "--run-id",
        run_id,
        "--scope",
        scope,
        "--json",
        env=env,
    )


def _json(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.stdout, result.stderr
    return json.loads(result.stdout)


def _git_admin_snapshot(repo: Path) -> dict[str, tuple[str, bytes | str | None]]:
    common = Path(_git("rev-parse", "--absolute-git-dir", cwd=repo).stdout.strip())
    snapshot: dict[str, tuple[str, bytes | str | None]] = {}
    for entry in sorted(common.rglob("*")):
        relative = entry.relative_to(common).as_posix()
        if entry.is_symlink():
            snapshot[relative] = ("symlink", os.readlink(entry))
        elif entry.is_dir():
            snapshot[relative] = ("directory", None)
        else:
            snapshot[relative] = ("file", entry.read_bytes())
    return snapshot


def _worktree_record(repo: Path, workspace: Path) -> list[str]:
    records = _git("worktree", "list", "--porcelain", cwd=repo).stdout.strip().split("\n\n")
    prefix = f"worktree {workspace}\n"
    matches = [record.splitlines() for record in records if (record + "\n").startswith(prefix)]
    assert len(matches) == 1
    return matches[0]


def test_context_cli_real_worktree_tracer(tmp_path: Path) -> None:
    primary = _repo(tmp_path)
    linked = tmp_path / "linked"
    _git("worktree", "add", "-q", "-b", "fixture-linked", str(linked), cwd=primary)
    state_root = tmp_path / "authority"
    env = _env(tmp_path)

    started = _start(state_root, linked, "context-tracer", env=env)
    assert started.returncode == 0, started.stderr
    ready = _json(started)
    assert ready["ok"] is True and ready["code"] == "RUN_READY"
    workspace = Path(ready["workspace"])
    assert workspace.is_absolute() and workspace not in (primary, linked)
    assert workspace.exists()
    assert _git("rev-parse", "--git-common-dir", cwd=workspace).stdout.strip()
    assert _git("branch", "--show-current", cwd=workspace).stdout.strip() == "ffs/runs/context-tracer"
    assert _git("rev-parse", "HEAD", cwd=workspace).stdout == _git("rev-parse", "HEAD", cwd=primary).stdout
    listed = _git("worktree", "list", "--porcelain", cwd=primary).stdout
    assert f"worktree {workspace}" in listed

    inspected = _cli(state_root, primary, "context", "--run-id", "context-tracer",
                     "--json", env=env)
    assert inspected.returncode == 0, inspected.stderr
    context = _json(inspected)
    for key in ("repository_id", "run_id", "workspace", "evidence_root", "generation"):
        assert context[key] == ready[key]
    assert context["attempt_id"] is None
    assert context["runtime_tuple_hash"] is None


@pytest.mark.parametrize("valid", ["a", "spec-014", "A_b-9", "x" * 64])
def test_validate_run_id_preserves_valid_supported_ids(valid: str) -> None:
    from run_context import validate_run_id

    assert validate_run_id(valid) == valid


@pytest.mark.parametrize("invalid", ["", "-bad", "bad-", "has space", "é", "x" * 65])
def test_validate_run_id_rejects_invalid_bytes_without_truncation(invalid: str) -> None:
    from run_context import InvalidRunId

    with pytest.raises(InvalidRunId):
        from run_context import validate_run_id
        validate_run_id(invalid)


def test_resolve_repository_unifies_linked_checkouts_and_separates_repositories(tmp_path: Path) -> None:
    from run_context import resolve_repository

    primary = _repo(tmp_path, "one")
    linked = tmp_path / "linked"
    _git("worktree", "add", "-q", "-b", "linked", str(linked), cwd=primary)
    other = _repo(tmp_path, "other")
    one = resolve_repository(primary)
    same = resolve_repository(linked)
    distinct = resolve_repository(other)
    assert one.common_dir == same.common_dir
    assert one.filesystem_id == same.filesystem_id
    assert one.common_dir != distinct.common_dir


def test_marker_read_does_not_reopen_a_replaced_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from run_context import _read_marker, resolve_repository

    repo = _repo(tmp_path)
    descriptor = resolve_repository(repo)
    owned = descriptor.common_dir / "ffs"
    owned.mkdir(mode=0o700)
    marker = owned / "repository.json"
    original_id = "4a127162-66c5-4d68-9ea2-a7f8df11280c"
    replacement_id = "d6ee3547-1ec2-4e31-99ed-b4cd45bcb973"
    marker.write_text(
        json.dumps({"schema_version": 1, "repository_id": original_id}),
        encoding="utf-8",
    )
    marker.chmod(0o600)
    original_read_text = Path.read_text

    def replace_before_path_read(path: Path, *args, **kwargs) -> str:
        if path == marker:
            replacement = owned / "replacement.json"
            replacement.write_text(
                json.dumps({"schema_version": 1, "repository_id": replacement_id}),
                encoding="utf-8",
            )
            replacement.chmod(0o600)
            os.replace(replacement, marker)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", replace_before_path_read)
    assert _read_marker(marker) == original_id


def test_resolve_repository_preserves_unrelated_stale_worktree_registration(tmp_path: Path) -> None:
    from run_context import resolve_repository

    primary = _repo(tmp_path)
    stale = tmp_path / "stale-sibling"
    linked = tmp_path / "valid-linked"
    _git("worktree", "add", "-q", "-b", "stale-sibling", str(stale), cwd=primary)
    _git("worktree", "add", "-q", "-b", "valid-linked", str(linked), cwd=primary)
    (linked / "tracked.txt").write_text("valid linked head\n")
    _git("add", "tracked.txt", cwd=linked)
    _git("commit", "-qm", "linked fixture head", cwd=linked)
    preserved = tmp_path / "preserved-fixture-evidence"
    preserved.mkdir()
    stale.rename(preserved / "stale-sibling")

    registry = _git("worktree", "list", "--porcelain", cwd=primary).stdout
    stale_record = _worktree_record(primary, stale)
    assert any(line.startswith("prunable") for line in stale_record)
    administration = _git_admin_snapshot(primary)
    expected_common = (primary / ".git").resolve(strict=True)
    heads = {
        origin: _git("rev-parse", "HEAD", cwd=origin).stdout.strip()
        for origin in (primary, linked)
    }
    assert heads[primary] != heads[linked]

    for origin in (primary, linked):
        descriptor = resolve_repository(origin)
        assert descriptor.checkout == origin.resolve(strict=True)
        assert descriptor.primary_root == primary.resolve(strict=True)
        assert descriptor.common_dir == expected_common
        assert descriptor.head == heads[origin]

    assert _git_admin_snapshot(primary) == administration
    assert _git("worktree", "list", "--porcelain", cwd=primary).stdout == registry
    assert _worktree_record(primary, stale) == stale_record
    assert (preserved / "stale-sibling" / "tracked.txt").read_text() == "base\n"
    assert (primary / "tracked.txt").read_text() == "base\n"
    assert (linked / "tracked.txt").read_text() == "valid linked head\n"
    assert not stale.exists()


def test_context_read_of_nonexistent_store_is_nonmutating(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    state_root = tmp_path / "does-not-exist"
    result = _cli(state_root, repo, "context", "--run-id", "missing", "--json",
                  env=_env(tmp_path))
    assert result.returncode != 0
    refusal = _json(result)
    assert refusal["ok"] is False
    assert isinstance(refusal["recovery_action"], dict)
    assert not state_root.exists()


def test_repository_scope_allows_same_run_id_in_distinct_repositories(tmp_path: Path) -> None:
    first = _repo(tmp_path, "first")
    second = _repo(tmp_path, "second")
    state_root = tmp_path / "authority"
    env = _env(tmp_path)
    one = _start(state_root, first, "shared-id", env=env)
    assert one.returncode == 0, one.stderr
    first_payload = _json(one)
    first_evidence = Path(first_payload["evidence_root"])
    first_sentinel = first_evidence / "first-repository-sentinel"
    first_sentinel.write_text("first repository evidence\n")

    two = _start(state_root, second, "shared-id", env=env)
    assert two.returncode == 0, two.stderr
    second_payload = _json(two)
    second_evidence = Path(second_payload["evidence_root"])
    second_sentinel = second_evidence / "second-repository-sentinel"
    second_sentinel.write_text("second repository evidence\n")

    assert first_payload["repository_id"] != second_payload["repository_id"]
    assert first_payload["workspace"] != second_payload["workspace"]
    assert first_evidence != second_evidence
    assert first_sentinel.read_text() == "first repository evidence\n"
    assert second_sentinel.read_text() == "second repository evidence\n"
    assert not (first_evidence / second_sentinel.name).exists()
    assert not (second_evidence / first_sentinel.name).exists()


def test_upstream_context_fields_are_stable_data_not_paths(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    state_root = tmp_path / "authority"
    env = _env(tmp_path, GSD_PROJECT="project-key", GSD_WORKSTREAM="stream/key",
               GSD_SESSION_KEY="session:key")
    started = _start(state_root, repo, "upstream-context", env=env)
    assert started.returncode == 0, started.stderr
    inspected = _cli(state_root, repo, "context", "--run-id", "upstream-context",
                     "--json", env=env)
    upstream = _json(inspected)["upstream"]
    assert upstream == {"project": "project-key", "workstream": "stream/key",
                        "session_key": "session:key"}


def test_ready_registration_survives_preparer_exit_and_remains_selectable(
    tmp_path: Path,
) -> None:
    primary = _repo(tmp_path)
    state_root = tmp_path / "authority"
    env = _env(tmp_path)
    objective = "durable objective after preparation process exit"
    scope = "phase-05/context"

    first = _start_request(
        state_root,
        primary,
        "durable-first",
        objective=objective,
        scope=scope,
        env=env,
    )
    assert first.returncode == 0, first.stderr
    ready = _json(first)
    assert ready["code"] == "RUN_READY"

    # subprocess.run has reaped the synchronous preparation process. The
    # durable registry, rather than that process lease, must still exclude a
    # second run with the same repository/scope/objective binding.
    second = _start_request(
        state_root,
        primary,
        "durable-second",
        objective=objective,
        scope=scope,
        env=env,
    )
    assert second.returncode == 3, second.stderr
    refusal = _json(second)
    assert refusal["ok"] is False
    assert refusal["code"] == "OBJECTIVE_RESERVED"
    assert refusal["run_id"] == "durable-second"

    inspected = _cli(
        state_root,
        primary,
        "context",
        "--run-id",
        "durable-first",
        "--json",
        env=env,
    )
    assert inspected.returncode == 0, inspected.stderr
    selected = _json(inspected)
    assert selected["run_id"] == ready["run_id"]
    assert selected["repository_id"] == ready["repository_id"]
    assert selected["workspace"] == ready["workspace"]


def test_registered_workspace_cannot_be_repurposed_after_preparer_exit(
    tmp_path: Path,
) -> None:
    from run_state.ownership import (
        ControlStore,
        OwnershipRefused,
        ProcessIdentity,
        StartRequest,
        reserve_resources,
    )

    primary = _repo(tmp_path)
    state_root = tmp_path / "authority"
    env = _env(tmp_path)
    first = _start_request(
        state_root,
        primary,
        "workspace-owner",
        objective="first durable workspace",
        scope="phase-05/context",
        env=env,
    )
    assert first.returncode == 0, first.stderr
    registered = _json(first)
    registered_workspace = Path(registered["workspace"])

    store = ControlStore(state_root / "control.sqlite3")
    with pytest.raises(OwnershipRefused) as rejected:
        reserve_resources(
            store,
            StartRequest(
                "workspace-contender",
                str(registered_workspace),
                "different-objective-digest",
                ProcessIdentity.current(),
                repository_id=registered["repository_id"],
                planning_scope="phase-05/context",
            ),
        )
    assert rejected.value.code == "WORKSPACE_REGISTERED"
    assert registered_workspace.is_dir()
    assert _git("branch", "--show-current", cwd=registered_workspace).stdout.strip() == (
        "ffs/runs/workspace-owner"
    )


@pytest.mark.parametrize("replacement_marker", ["missing", "different"])
def test_repository_replacement_at_same_directory_gets_new_identity(
    tmp_path: Path,
    replacement_marker: str,
) -> None:
    primary = _repo(tmp_path)
    original_inode = (primary.stat().st_dev, primary.stat().st_ino)
    original_state = tmp_path / "original-authority"
    env = _env(tmp_path)
    first = _start(original_state, primary, "original-repository", env=env)
    assert first.returncode == 0, first.stderr
    original_id = _json(first)["repository_id"]

    # Replace only the repository administration data. The checkout directory
    # and its inode remain the same, so path/inode reuse cannot identify it.
    shutil.rmtree(primary / ".git")
    _git("init", "-q", cwd=primary)
    _git("config", "user.email", "replacement@example.test", cwd=primary)
    _git("config", "user.name", "Replacement Fixture", cwd=primary)
    (primary / "tracked.txt").write_text("replacement\n")
    _git("add", "tracked.txt", cwd=primary)
    _git("commit", "-qm", "replacement", cwd=primary)
    assert (primary.stat().st_dev, primary.stat().st_ino) == original_inode

    if replacement_marker == "different":
        foreign = _start(
            tmp_path / "replacement-authority",
            primary,
            "replacement-marker",
            env=env,
        )
        assert foreign.returncode == 0, foreign.stderr
        assert _json(foreign)["repository_id"] != original_id

    observed = _start(
        original_state,
        primary,
        f"observed-{replacement_marker}",
        env=env,
    )
    assert observed.returncode == 0, observed.stderr
    assert _json(observed)["repository_id"] != original_id


def test_new_form_without_state_root_does_not_touch_legacy_database(tmp_path: Path) -> None:
    from run_state.state import RunStore

    primary = _repo(tmp_path)
    legacy_db = tmp_path / "legacy-runs.db"
    legacy = RunStore(legacy_db)
    legacy_run = legacy.create_run(skill="fix", objective="preserve legacy bytes")
    before = (legacy_db.read_bytes(), legacy_db.stat().st_mtime_ns)
    env = _env(tmp_path, RUN_STATE_DB=str(legacy_db))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "run_state.cli",
            "start",
            "--skill",
            "fix",
            "--objective",
            "new form requires fixture authority",
            "--activity",
            "plan",
            "--run-id",
            "missing-state-root",
            "--json",
        ],
        cwd=primary,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2, result.stderr
    refusal = _json(result)
    assert refusal["ok"] is False
    assert refusal["code"] == "STATE_ROOT_REQUIRED"
    assert (legacy_db.read_bytes(), legacy_db.stat().st_mtime_ns) == before
    assert legacy.get_run(legacy_run).objective == "preserve legacy bytes"


def test_workspace_checkout_disables_repository_hooks(tmp_path: Path) -> None:
    primary = _repo(tmp_path)
    canary = tmp_path / "post-checkout-fired"
    hook = primary / ".git" / "hooks" / "post-checkout"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text('#!/bin/sh\n: > "$FFS_TEST_HOOK_CANARY"\n')
    hook.chmod(0o755)
    env = _env(tmp_path, FFS_TEST_HOOK_CANARY=str(canary))

    result = _start(tmp_path / "authority", primary, "hooks-disabled", env=env)
    assert result.returncode == 0, result.stderr
    assert _json(result)["code"] == "RUN_READY"
    assert not canary.exists()


def test_invalid_new_request_does_not_create_git_identity_or_authority(
    tmp_path: Path,
) -> None:
    primary = _repo(tmp_path)
    state_root = tmp_path / "authority"
    before = _git_admin_snapshot(primary)
    result = _start(state_root, primary, "invalid run id", env=_env(tmp_path))

    assert result.returncode == 2
    refusal = _json(result)
    assert refusal["ok"] is False
    assert refusal["code"] == "INVALID_RUN_ID"
    assert _git_admin_snapshot(primary) == before
    assert not state_root.exists()


def test_idempotent_replay_after_workspace_deletion_is_not_stale_ready(
    tmp_path: Path,
) -> None:
    primary = _repo(tmp_path)
    state_root = tmp_path / "authority"
    env = _env(tmp_path)
    request = (
        "start", "--skill", "fix", "--objective", "deleted replay workspace",
        "--activity", "plan", "--run-id", "deleted-replay",
        "--request-key", "deleted-replay-key", "--json",
    )
    started = _cli(state_root, primary, *request, env=env)
    assert started.returncode == 0, started.stderr
    ready = _json(started)
    assert ready["code"] == "RUN_READY"
    workspace = Path(ready["workspace"])
    record = _worktree_record(primary, workspace)
    assert not any(line == "locked" or line.startswith("locked ") for line in record)
    assert _git("status", "--porcelain=v1", cwd=workspace).stdout == ""
    _git("worktree", "remove", str(workspace), cwd=primary)
    assert not workspace.exists()

    replayed = _cli(state_root, primary, *request, env=env)
    assert replayed.returncode == 5
    replay = _json(replayed)
    assert replay["ok"] is False
    assert replay["code"] == "WORKSPACE_MISSING"
    assert replay["ready"] is False
    assert replay["workspace_state"] == "missing"
    assert replay["run_id"] == ready["run_id"]
    assert replay["workspace"] == ready["workspace"]
